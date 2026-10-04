// FILE: main.go
// Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
// Created: 2026-10-02
//
// Description: Unified codepilot-workspace binary. No Docker required.
//   - First-run: pulls OCI image from GHCR, extracts into /var/lib/codepilot/rootfs/
//   - Seeds ~/.codepilot/agent.yaml and sessions/ on the host (persists across restarts)
//   - Launches an isolated Linux container (PID/UTS/Mount namespaces + cgroups v2)
//   - Waits for the HTTP server, then opens a chromeless browser window
//
// Linux-only by design — namespaces and /proc are Linux kernel primitives.
//
// Copyright (c) 2026 Jahanzeb Ahmed. All rights reserved.
// Licensed under the MIT License — see LICENSE in the repo root.

package main

import (
	"archive/tar"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/google/go-containerregistry/pkg/crane"
)

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const (
	imageRef   = "ghcr.io/jahanzeb-git/codepilot-workspace:latest"
	rootfsPath = "/var/lib/codepilot/rootfs"
	pidFile    = "/tmp/codepilot.pid"
	selfBinary = "/usr/local/bin/codepilot-workspace"
	hostPort   = 8080
)

// agentYAML is written to ~/.codepilot/agent.yaml on the very first run.
// The user can edit this file freely — we never overwrite it after creation.
const agentYAML = `# agent.yaml — CodePilot configuration
# Edit this file to customise the agent. Changes persist across restarts.
agent:
  name: "Codepilot"
  role: "LEVEL: Expert Full Stack Software Engineer"
  system_prompt: "You're Codepilot an agentic software assistance interfaced through IDE on user browser. Your agentic runtime is based on codepilot-ai library. You must smartly work and wise about token usage but solve TASK(s) at your own pace."

  model:
    provider: "experientiallabs"
    name: "qwen3.8-27b"
    api_key_env: "EXPERIENTIAL_API_KEY"
    temperature: 1.0
    max_tokens: 65536
    thinking:
      enabled: false

  runtime:
    work_dir: "/workspace"
    max_steps: 35
    unsafe_mode: true

  sub_agents:
    enabled: false
    max_steps: 20

  memory:
    max_context_tokens: 100000
    context_stress_multiplier: 1.0
    context_stress_trigger: 0.78

  tools:
    - name: "write_file"
      enabled: true
      config:
        require_permission: true
    - name: "edit_file"
      enabled: true
      config:
        require_permission: true
    - name: "view_file"
      enabled: true
    - name: "execute"
      enabled: true
      config:
        require_permission: true
        max_output_chars: 10000
    - name: "read_output"
      enabled: true
    - name: "send_input"
      enabled: true
    - name: "terminate_terminal"
      enabled: true
    - name: "ask_user"
      enabled: true
    - name: "find"
      enabled: true
    - name: "semantic_search"
      enabled: true
      config:
        api_key_env: "VOYAGE_API_KEY"
        model: "voyage-code-3"
        base_url: "https://api.voyageai.com/v1"
        provider: "openai"
        max_results: 5
        timeout: 60
        max_output_chars: 8000
    - name: "mcp"
      enabled: true
      config:
        embedding_model: "voyage-code-3"
        embedding_api_key_env: "VOYAGE_API_KEY"
        embedding_base_url: "https://api.voyageai.com/v1"
        top_k: 3
        servers: []
    - name: "search_web"
      enabled: true
      config:
        require_permission: false
`

// ---------------------------------------------------------------------------
// main — dispatch on os.Args
// ---------------------------------------------------------------------------

func main() {
	// No args → user typed `codepilot-workspace` in their terminal.
	// This is the user-facing SETUP stage.
	if len(os.Args) < 2 {
		setup()
		return
	}

	switch os.Args[1] {
	case "run":
		// Internal re-exec: called by setup() to attach Cloneflags during fork.
		run()
	case "child":
		// Internal re-exec: we are now inside the new namespaces. chroot + exec initd.
		child()
	case "stop":
		// User-facing: gracefully stop a running container.
		stop()
	default:
		fmt.Fprintf(os.Stderr, "  unknown command %q\n", os.Args[1])
		os.Exit(1)
	}
}

// ---------------------------------------------------------------------------
// setup — orchestrates the full launch sequence
// ---------------------------------------------------------------------------

func setup() {
	if os.Geteuid() != 0 {
		fmt.Fprintf(os.Stderr, "  fatal: CodePilot Workspace requires root privileges to create Linux namespaces and cgroups.\n")
		fmt.Fprintf(os.Stderr, "  Please run: sudo codepilot-workspace\n")
		os.Exit(1)
	}

	printBanner()

	if isRunning() {
		fmt.Println("  Workspace is already running.")
		fmt.Printf("  url:  http://localhost:%d\n", hostPort)
		fmt.Println("  stop: sudo codepilot-workspace stop")
		return
	}

	must(pullSetup())
	must(seedDefaults())

	cmd := exec.Command(selfBinary, "run")
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	must(cmd.Start())

	must(waitForServer())

	fmt.Println("\n  Workspace is ready.")
	fmt.Printf("  url:  http://localhost:%d\n", hostPort)
	fmt.Println("  stop: sudo codepilot-workspace stop\n")
	// setup() exits here. The container process keeps running independently.
}

// ---------------------------------------------------------------------------
// isRunning — safely checks whether the container is already up
// ---------------------------------------------------------------------------

func isRunning() bool {
	data, err := os.ReadFile(pidFile)
	if err != nil {
		return false // PID file missing → not running
	}

	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		return false
	}

	// /proc/<pid>/exe is a symlink that only exists while the process is alive.
	// If the PID was recycled by another process, exe will point to a different binary.
	exe, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", pid))
	if err != nil {
		return false // process dead or PID doesn't exist
	}

	return exe == selfBinary // confirm it's OUR process, not a recycled PID
}

// ---------------------------------------------------------------------------
// run — sets up Linux namespaces + cgroups, then re-execs as child
// ---------------------------------------------------------------------------

func run() {

	// Re-exec ourselves with "child". The Cloneflags are passed to the kernel's
	// clone() syscall during this fork — that is the only moment Linux accepts them.
	cmd := exec.Command(selfBinary, "child")
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Cloneflags:   syscall.CLONE_NEWUTS | syscall.CLONE_NEWPID | syscall.CLONE_NEWNS,
		Unshareflags: syscall.CLONE_NEWNS,
	}

	// Apply cgroup resource limits before launching the child.
	cg()

	// Start the child process (non-blocking).
	must(cmd.Start())

	// Write the HOST-VISIBLE PID now that the kernel has assigned it.
	// cmd.Process.Pid is what the host sees — inside the PID namespace,
	// the child sees itself as PID 1. We need the host PID for /proc/<pid>/exe.
	_ = os.WriteFile(pidFile, []byte(strconv.Itoa(cmd.Process.Pid)), 0644)

	// Wait blocks until the container exits — this IS the container's lifetime.
	must(cmd.Wait())

	// Container exited cleanly — remove the stale PID file.
	_ = os.Remove(pidFile)
}

// ---------------------------------------------------------------------------
// child — runs inside the new namespaces; chroot + exec initd
// ---------------------------------------------------------------------------

func child() {

	// NOTE: PID file is written by run() using cmd.Process.Pid (host-visible PID).
	// We do NOT write it here because os.Getpid() inside a new PID namespace
	// returns 1 — useless for /proc/<pid>/exe lookups on the host.

	// Determine paths that need to be bind-mounted.
	workspaceDir, err := os.Getwd()
	must(err)

	home, err := os.UserHomeDir()
	must(err)
	agentYAMLPath := filepath.Join(home, ".codepilot", "agent.yaml")
	sessionsPath := filepath.Join(home, ".codepilot", "sessions")

	// Create mount-point stubs inside rootfs. They must exist before bind-mount.
	must(os.MkdirAll(filepath.Join(rootfsPath, "workspace"), 0755))
	must(os.MkdirAll(filepath.Join(rootfsPath, "opt/codepilot"), 0755))
	must(os.MkdirAll(filepath.Join(rootfsPath, "root/.codepilot/sessions"), 0755))
	must(os.MkdirAll(filepath.Join(rootfsPath, "dev"), 0755))
	must(os.MkdirAll(filepath.Join(rootfsPath, "sys"), 0755))

	// ---- Bind-mounts — MUST happen BEFORE chroot ----
	//
	// Make our new mount namespace strictly private so our mounts don't
	// leak back out to the host OS.
	must(syscall.Mount("", "/", "", syscall.MS_PRIVATE|syscall.MS_REC, ""))

	// 1. User's current project directory → /workspace inside container
	must(syscall.Mount(workspaceDir, filepath.Join(rootfsPath, "workspace"), "", syscall.MS_BIND, ""))

	// 2. Persistent agent config (survives container upgrades)
	must(syscall.Mount(agentYAMLPath, filepath.Join(rootfsPath, "opt/codepilot/agent.yaml"), "", syscall.MS_BIND, ""))

	// 3. Persistent sessions directory
	must(syscall.Mount(sessionsPath, filepath.Join(rootfsPath, "root/.codepilot/sessions"), "", syscall.MS_BIND, ""))

	// 4. Critical pseudo-filesystems (fixes "open /dev/null: no such file or directory")
	must(syscall.Mount("/dev", filepath.Join(rootfsPath, "dev"), "", syscall.MS_BIND|syscall.MS_REC, ""))
	must(syscall.Mount("/sys", filepath.Join(rootfsPath, "sys"), "", syscall.MS_BIND|syscall.MS_REC, ""))

	// 5. Fresh devpts for the new PID namespace!
	must(os.MkdirAll(filepath.Join(rootfsPath, "dev", "pts"), 0755))
	must(syscall.Mount("devpts", filepath.Join(rootfsPath, "dev", "pts"), "devpts", 0, "newinstance,ptmxmode=0666,mode=0620,gid=5"))

	// Bind mount the container's ptmx node over the host's /dev/ptmx node.
	// This guarantees that any process opening /dev/ptmx gets a PTY in the container's devpts!
	must(syscall.Mount(filepath.Join(rootfsPath, "dev", "pts", "ptmx"), filepath.Join(rootfsPath, "dev", "ptmx"), "", syscall.MS_BIND, ""))

	// Set container hostname (visible to processes inside the UTS namespace).
	_ = syscall.Sethostname([]byte("codepilot-workspace"))

	// Signal initd that this is a local daemonless run, so it skips B2 logic.
	_ = os.Setenv("CODEPILOT_LOCAL", "true")
	_ = os.Setenv("STATIC_PATH", "/opt/codepilot/frontend/dist")

	// Placeholder env vars. User configures real keys via the Settings UI.
	_ = os.Setenv("EXPERIENTIAL_API_KEY", "not-configured")
	_ = os.Setenv("DEEPSEEK_API_KEY", "not-configured")
	_ = os.Setenv("VOYAGE_API_KEY", "not-configured")
	_ = os.Setenv("TAVILY_API_KEY", "not-configured")
	_ = os.Setenv("TERM", "xterm-256color")

	// chroot — update this process's filesystem root pointer to rootfsPath.
	// From here on, "/" means rootfsPath on the host.
	must(syscall.Chroot(rootfsPath))
	must(syscall.Chdir("/workspace"))

	// Mount a fresh /proc. Because we have a new PID namespace, this /proc
	// only exposes container PIDs — the host is invisible.
	must(syscall.Mount("proc", "/proc", "proc", 0, ""))

	// The namespace child inherited setup()'s terminal file descriptors.  Those
	// descriptors belong to the host's devpts instance; after chrooting and
	// mounting our own devpts, writes to them can fail with EIO.  A Rust tracing
	// worker treats that write failure as fatal, which used to tear down a live
	// terminal WebSocket and SIGHUP its bash.  Docker/Fly provide logging pipes
	// for PID 1; the standalone runtime must provide stable stdio itself.
	redirectContainerStdio()

	// syscall.Exec replaces the current process image with initd.
	// initd is the CMD from the Dockerfile — it starts the agent server,
	// Rust workspace server, and manages their lifecycle (SIGTERM forwarding etc.).
	// This call does NOT return.
	must(syscall.Exec("/opt/codepilot/initd", []string{"/opt/codepilot/initd"}, os.Environ()))
}

// redirectContainerStdio gives initd and every child it starts stable standard
// descriptors inside the chroot. Runtime logs stay available on the host at
// /var/lib/codepilot/rootfs/tmp/codepilot-runtime.log.
func redirectContainerStdio() {
	stdin, err := os.OpenFile("/dev/null", os.O_RDONLY, 0)
	must(err)
	logFile, err := os.OpenFile("/tmp/codepilot-runtime.log", os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	must(err)

	must(syscall.Dup2(int(stdin.Fd()), 0))
	must(syscall.Dup2(int(logFile.Fd()), 1))
	must(syscall.Dup2(int(logFile.Fd()), 2))
}

// ---------------------------------------------------------------------------
// pullSetup — pull OCI image from GHCR and extract into rootfsPath
// ---------------------------------------------------------------------------

func pullSetup() error {
	if _, err := os.Stat(rootfsPath); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("checking rootfs: %w", err)
	}

	if err := os.MkdirAll(rootfsPath, 0755); err != nil {
		return fmt.Errorf("creating rootfs dir: %w", err)
	}

	fmt.Printf("  Pulling base image %s...\n", imageRef)

	img, err := crane.Pull(imageRef)
	if err != nil {
		return fmt.Errorf("pulling image: %w", err)
	}

	reader, writer := io.Pipe()
	go func() {
		writer.CloseWithError(crane.Export(img, writer))
	}()

	fmt.Print("  Extracting rootfs...")
	if err := extractTar(reader, rootfsPath); err != nil {
		return fmt.Errorf("extracting rootfs: %w", err)
	}
	fmt.Print("\r\033[K  Extracting rootfs... done.\n")
	return nil
}

// ---------------------------------------------------------------------------
// extractTar — unpack a tar stream into destDir safely
// ---------------------------------------------------------------------------

func extractTar(r io.Reader, destDir string) error {
	tr := tar.NewReader(r)
	count := 0

	for {
		header, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("reading tar: %w", err)
		}

		count++
		if count%100 == 0 {
			fmt.Printf("\r\033[K  Extracting rootfs... %d files", count)
		}

		// Sanitise entry name — block absolute paths and path traversal attacks.
		name := filepath.Clean(header.Name)
		if filepath.IsAbs(name) {
			return fmt.Errorf("unsafe absolute path in archive: %q", header.Name)
		}
		if name == ".." || strings.HasPrefix(name, ".."+string(os.PathSeparator)) {
			return fmt.Errorf("path traversal in archive: %q", header.Name)
		}

		target := filepath.Join(destDir, name)

		// Belt-and-suspenders: verify the resolved path stays inside destDir.
		rel, err := filepath.Rel(destDir, target)
		if err != nil {
			return fmt.Errorf("computing relative path: %w", err)
		}
		if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
			return fmt.Errorf("archive path escapes rootfs: %q", header.Name)
		}

		switch header.Typeflag {

		case tar.TypeDir:
			if err := os.MkdirAll(target, os.FileMode(header.Mode)); err != nil {
				return fmt.Errorf("creating dir %q: %w", target, err)
			}

		case tar.TypeReg, tar.TypeRegA:
			if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return fmt.Errorf("creating parent dir: %w", err)
			}
			f, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(header.Mode))
			if err != nil {
				return fmt.Errorf("creating file %q: %w", target, err)
			}
			_, copyErr := io.Copy(f, tr)
			closeErr := f.Close()
			if copyErr != nil {
				return fmt.Errorf("writing file %q: %w", target, copyErr)
			}
			if closeErr != nil {
				return fmt.Errorf("closing file %q: %w", target, closeErr)
			}

		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return fmt.Errorf("creating symlink parent: %w", err)
			}
			_ = os.Remove(target)
			if err := os.Symlink(header.Linkname, target); err != nil {
				return fmt.Errorf("creating symlink %q → %q: %w", target, header.Linkname, err)
			}

		case tar.TypeLink:
			linkTarget := filepath.Join(destDir, filepath.Clean(header.Linkname))
			if filepath.IsAbs(header.Linkname) {
				return fmt.Errorf("unsafe absolute hard-link target: %q", header.Linkname)
			}
			rel, err := filepath.Rel(destDir, linkTarget)
			if err != nil {
				return fmt.Errorf("checking hard-link target: %w", err)
			}
			if rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
				return fmt.Errorf("hard-link escapes rootfs: %q", header.Linkname)
			}
			if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return fmt.Errorf("creating hard-link parent: %w", err)
			}
			_ = os.Remove(target)
			if err := os.Link(linkTarget, target); err != nil {
				return fmt.Errorf("creating hard link %q → %q: %w", target, linkTarget, err)
			}

		default:
			// Skip device nodes, FIFOs, etc. — unsafe to create on the host.

		}

		// Best-effort: restore ownership (requires root/CAP_CHOWN) and timestamps.
		_ = os.Lchown(target, header.Uid, header.Gid)
		_ = os.Chtimes(target, header.AccessTime, header.ModTime)
	}

	return nil
}

// ---------------------------------------------------------------------------
// seedDefaults — write ~/.codepilot/agent.yaml and sessions/ on first run
// ---------------------------------------------------------------------------

func seedDefaults() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("finding home dir: %w", err)
	}

	codepilotHome := filepath.Join(home, ".codepilot")
	sessionsDir := filepath.Join(codepilotHome, "sessions")
	agentYAMLFile := filepath.Join(codepilotHome, "agent.yaml")

	if err := os.MkdirAll(sessionsDir, 0755); err != nil {
		return fmt.Errorf("creating sessions dir: %w", err)
	}

	// Only write if agent.yaml does not already exist — preserve user edits.
	if _, err := os.Stat(agentYAMLFile); errors.Is(err, os.ErrNotExist) {

		if err := os.WriteFile(agentYAMLFile, []byte(agentYAML), 0644); err != nil {
			return fmt.Errorf("writing agent.yaml: %w", err)
		}
	}

	return nil
}

// ---------------------------------------------------------------------------
// waitForServer — polls localhost:8080 until the server responds (60s max)
// ---------------------------------------------------------------------------

func waitForServer() error {
	url := fmt.Sprintf("http://localhost:%d", hostPort)
	fmt.Print("  Waiting for server...")

	for range 60 {
		if exec.Command("curl", "-fsS", url).Run() == nil {
			fmt.Println(" \033[38;5;45m✓\033[0m")
			return nil
		}
		fmt.Print(".")
		time.Sleep(time.Second)
	}

	fmt.Println()
	return fmt.Errorf("server did not respond after 60 s at %s", url)
}

// ---------------------------------------------------------------------------
// printBanner — premium ANSI terminal banner shown on every launch
// ---------------------------------------------------------------------------

func printBanner() {
	const (
		cyan  = "\033[38;5;45m"
		bold  = "\033[1m"
		dim   = "\033[38;5;245m"
		reset = "\033[0m"
	)

	fmt.Println()
	fmt.Printf("  %s█%s %sC O D E P I L O T   W O R K S P A C E%s\n", cyan, reset, bold, reset)
	fmt.Printf("  %s█%s %sAI Development Environment · v1.0%s\n", cyan, reset, dim, reset)
	fmt.Printf("  %s█%s %sLinux · No Docker · No Daemon%s\n", cyan, reset, dim, reset)
	fmt.Println()
}

// ---------------------------------------------------------------------------
// stop — gracefully shut down a running container
// ---------------------------------------------------------------------------

func stop() {
	stoppedViaPID := false

	data, err := os.ReadFile(pidFile)
	if err == nil {
		pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
		if err == nil {
			exe, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", pid))
			if err == nil && exe == selfBinary {
				process, err := os.FindProcess(pid)
				if err == nil {
					if err := process.Signal(syscall.SIGTERM); err == nil {
						fmt.Println("  Workspace stopped gracefully.")
						stoppedViaPID = true
					}
				}
			}
		}
	}

	_ = os.Remove(pidFile)

	if !stoppedViaPID {
		fmt.Println("  No running workspace found via PID file.")
	}

	// Fallback: kill any process on hostPort (8080)
	fmt.Println("  Checking for orphaned processes on port 8080...")
	out, _ := exec.Command("sh", "-c", fmt.Sprintf("lsof -t -i :%d", hostPort)).Output()
	pids := strings.TrimSpace(string(out))
	if pids != "" {
		_ = exec.Command("sh", "-c", fmt.Sprintf("kill -9 %s", strings.ReplaceAll(pids, "\n", " "))).Run()
		fmt.Println("  Found and forcefully terminated an orphaned workspace process.")
	} else if !stoppedViaPID {
		fmt.Println("  Port 8080 is clear. Nothing to stop.")
	}
}

// ---------------------------------------------------------------------------
// cg — apply cgroup v2 resource limits to this process tree
// ---------------------------------------------------------------------------

func cg() {
	cgroupDir := "/sys/fs/cgroup/codepilot-workspace"
	must(os.MkdirAll(cgroupDir, 0755))
	must(os.WriteFile(filepath.Join(cgroupDir, "pids.max"), []byte("200"), 0700))
	must(os.WriteFile(filepath.Join(cgroupDir, "memory.max"), []byte("8589934592"), 0700)) // 8 GiB
	must(os.WriteFile(filepath.Join(cgroupDir, "cgroup.procs"), []byte(strconv.Itoa(os.Getpid())), 0700))
}

// ---------------------------------------------------------------------------
// must — exit with a clear message on any fatal error
// ---------------------------------------------------------------------------

func must(err error) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "  fatal: %v\n", err)
		os.Exit(1)
	}
}
