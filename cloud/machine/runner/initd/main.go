// FILE: main.go
// Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
// Created: 2026-09-21
//
// This is the init process (PID 1) for the CodePilot machine runner container.
// It's a Go replacement for what used to be entrypoint.sh — same job, just as
// a compiled binary instead of a bash script: restore config, pull the
// workspace snapshot from B2 if this is a fresh rootfs, start the snapshot
// daemon + agent server + workspace server, scrub secrets out of the
// environment once everyone who needs them has read them, then sit and wait
// for a shutdown signal so we can shut the children down cleanly (and give
// B2 time to finish syncing) before the VM dies.
//
// Copyright (c) 2026 Jahanzeb Ahmed. All rights reserved.
// Licensed under the MIT License — see LICENSE in the repo root.

package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
)

// process handles for everything we spawn. we keep *os.Process (not just a
// bare pid int) because Wait() only behaves correctly on a process we
// actually started ourselves via exec.Cmd in this process — that's what lets
// the Go runtime reap it properly. a raw pid recovered via os.FindProcess
// doesn't give you that guarantee on Linux.
var ( 
	snapshotDaemonProc  *os.Process
	agentServiceProc    *os.Process
	workspaceServerProc *os.Process
)

// signalProcess sends sig to p if p is actually running, and swallows the
// "process already finished" case instead of treating it as fatal — a
// child that exited on its own before we got to shutdown isn't an error,
// it's just done already.
func signalProcess(name string, p *os.Process, sig os.Signal) error {
	if p == nil {
		return nil
	}
	if err := p.Signal(sig); err != nil {
		// ESRCH == no such process, i.e. it already died. don't treat that as
		// a failure, there's nothing left to signal.
		if err == os.ErrProcessDone || err == syscall.ESRCH {
			fmt.Printf("[INFO] %s already exited, nothing to signal\n", name)
			return nil
		}
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

// cleanup blocks until we receive SIGINT/SIGTERM, then shuts every child
// down. order matters here: the snapshot daemon needs to actually finish
// its B2 sync before we let the VM disappear, so we signal it first and
// WAIT for it to exit before touching anything else. agent + workspace
// server get signalled after — we don't need to serialize on those the
// same way, but we still wait on both so main() doesn't return (and the
// process tree doesn't get SIGKILLed by the platform) while they're mid
// shutdown.
//
// unlike the previous version, a failure signalling one child no longer
// aborts the rest — we always attempt all three and report every error we
// hit at the end, otherwise a daemon that already exited would leave the
// agent and workspace server never signalled at all.
func cleanup() error {
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	sig := <-sigChan
	fmt.Printf("[INFO] received signal: %s, shutting down gracefully...\n", sig)

	var errs []error

	if snapshotDaemonProc != nil {
		fmt.Println("[INFO] notifying B2 snapshot daemon to sync...")
		if err := signalProcess("snapshot-daemon", snapshotDaemonProc, sig); err != nil {
			errs = append(errs, err)
		} else if _, err := snapshotDaemonProc.Wait(); err != nil {
			// don't bail here either — we still want to try shutting the
			// other two down even if reaping the daemon itself errored.
			errs = append(errs, fmt.Errorf("waiting for snapshot-daemon: %w", err))
		} else {
			fmt.Println("[INFO] B2 sync complete.")
		}
	}

	if err := signalProcess("agent-service", agentServiceProc, sig); err != nil {
		errs = append(errs, err)
	}
	if err := signalProcess("workspace-server", workspaceServerProc, sig); err != nil {
		errs = append(errs, err)
	}

	// now actually wait for the last two to finish exiting instead of
	// racing main()'s return against them.
	if agentServiceProc != nil {
		if _, err := agentServiceProc.Wait(); err != nil {
			errs = append(errs, fmt.Errorf("waiting for agent-service: %w", err))
		}
	}
	if workspaceServerProc != nil {
		if _, err := workspaceServerProc.Wait(); err != nil {
			errs = append(errs, fmt.Errorf("waiting for workspace-server: %w", err))
		}
	}

	if len(errs) > 0 {
		return fmt.Errorf("cleanup had %d error(s): %v", len(errs), errs)
	}
	return nil
}

// HandleAgentConfig makes sure /opt/codepilot/agent.yaml actually exists and
// isn't empty before anything downstream tries to read it. this only
// matters for local/self-hosted usage where agent.yaml is bind-mounted in —
// on Fly it's always already there from the image build, so this is a
// no-op in prod. same self-heal idea as the old entrypoint.sh had, just
// ported over.
func HandleAgentConfig() {
	const configPath = "/opt/codepilot/agent.yaml"
	const defaultPath = "/opt/codepilot/agent.yaml.default"

	info, err := os.Stat(configPath)
	if err != nil || info.Size() == 0 {
		fmt.Println("[INFO] agent.yaml missing or empty, restoring default configuration...")

		src, err := os.Open(defaultPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[ERROR] failed to open default config: %v\n", err)
			os.Exit(1)
		}
		defer src.Close()

		dst, err := os.Create(configPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "[ERROR] failed to create config: %v\n", err)
			os.Exit(1)
		}
		defer dst.Close()

		// io.Copy streams straight between the two file descriptors instead
		// of reading the whole thing into memory first — agent.yaml is tiny
		// so it wouldn't matter here, but it's the right habit anyway.
		if _, err := io.Copy(dst, src); err != nil {
			fmt.Fprintf(os.Stderr, "[ERROR] failed to copy config: %v\n", err)
			os.Exit(1)
		}
	}
}

// SnapshotFilesystem handles the B2-backed persistent workspace. first boot
// on a fresh rootfs (no /.rootfs_initialized marker) means we pull the
// workspace down from B2 before anything else touches /workspace; every
// boot after that we skip straight to starting the daemon in watch mode so
// it can pick up and sync ongoing changes.
//
// NOTE fixed a real bug here: the old check was
//
//	if err == nil && info.IsNotExist() { ... }
//
// which can't ever be true — if os.Stat succeeds (err == nil) the file
// necessarily exists, so info.IsNotExist() (which also isn't a real method
// on os.FileInfo, that was a compile error too) would never fire. the
// correct check is os.IsNotExist(err), which inspects the *error* Stat
// returned, not the info result. as it was, first-boot restore from B2
// basically never ran.
func SnapshotFilesystem() {
	_, err := os.Stat("/.rootfs_initialized")
	if os.IsNotExist(err) {
		fmt.Println("[INFO] new rootfs detected. downloading workspace from B2...")
		cmd := exec.Command("snapshot-daemon", "--restore") // pointer to exec.Cmd
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		// run synchronously (Run, not Start) — we need the restore to
		// actually finish before /workspace is usable by anything else.
		if err := cmd.Run(); err != nil {
			fmt.Fprintf(os.Stderr, "[ERROR] failed to restore snapshot: %v\n", err)
		}
		file, err := os.Create("/.rootfs_initialized")
		if err != nil {
			fmt.Fprintf(os.Stderr, "[ERROR] failed to create rootfs initialized marker: %v\n", err)
			os.Exit(1)
		}
		file.Close()
	} else {
		fmt.Println("[INFO] rootfs already initialized, skipping snapshot restore.")
	}

	fmt.Println("[INFO] starting B2 snapshot daemon in watch mode...")
	cmd := exec.Command("snapshot-daemon")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "[ERROR] failed to start snapshot daemon: %v\n", err)
		return
	}
	snapshotDaemonProc = cmd.Process
}

// RunAgentService kicks off agent_server.py in the background. it reads
// its secrets straight out of this process's environment before we scrub
// them below, then scrubs its own copy internally — see the env-var loop
// in main() for the parent-side half of that.
func RunAgentService() {
	fmt.Println("[INFO] starting CodePilot agent service...")
	cmd := exec.Command("python3", "/opt/codepilot/agent_server.py")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "[ERROR] failed to start agent service: %v\n", err)
		return
	}
	agentServiceProc = cmd.Process
}

// StartWorkspaceServer launches the Rust workspace-bridge binary that
// actually serves the IDE on :8080.
func StartWorkspaceServer() {
	fmt.Println("[INFO] starting CodePilot workspace server...")
	cmd := exec.Command("codepilot-server")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "[ERROR] failed to start workspace server: %v\n", err)
		return
	}
	workspaceServerProc = cmd.Process
}

func main() {
	HandleAgentConfig()  // make sure agent.yaml exists before anyone reads it
	SnapshotFilesystem() // restore from B2 on first boot + start the watch daemon
	RunAgentService()    // agent_server.py reads its secrets from env, then we scrub below

	// scrub secrets from THIS process's environment once the agent service
	// has already forked off with its own copy. anything forked after this
	// point (workspace server included) won't inherit them.
	envVars := []string{"MACHINE_SECRET", "CONTROL_PLANE_URL", "DASHSCOPE_API_KEY", "ALIBABA_API_KEY", "VOYAGE_API_KEY", "TAVILY_API_KEY"}
	for _, envVar := range envVars {
		os.Unsetenv(envVar)
	}

	// belt and suspenders — Fly sometimes writes secrets out to these files,
	// so nuke them too.
	if err := os.Remove("/.env"); err != nil && !os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "[ERROR] failed to delete /.env: %v\n", err)
	}
	if err := os.Remove("/etc/environment"); err != nil && !os.IsNotExist(err) {
		fmt.Fprintf(os.Stderr, "[ERROR] failed to delete /etc/environment: %v\n", err)
	}

	StartWorkspaceServer()

	// block here until SIGINT/SIGTERM, then shut everything down in order
	if err := cleanup(); err != nil {
		fmt.Fprintf(os.Stderr, "[ERROR] cleanup finished with errors: %v\n", err)
		os.Exit(1)
	}
}