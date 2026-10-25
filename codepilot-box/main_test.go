package main

import (
	"net"
	"os"
	"testing"
)

func TestPidAlive(t *testing.T) {
	if !pidAlive(os.Getpid()) {
		t.Fatalf("current process should be alive")
	}
	if pidAlive(1 << 30) {
		t.Fatalf("nonexistent pid reported alive")
	}
}

func TestSameExecutable(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}

	if !sameExecutable(exe, exe) {
		t.Fatalf("the same path should match itself")
	}
	if sameExecutable(exe, "/bin/sh") {
		t.Fatalf("different binaries must not match")
	}
}

func TestPortInUse(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port

	if !portInUse(port) {
		_ = listener.Close()
		t.Fatalf("listening port %d was reported free", port)
	}

	if err := listener.Close(); err != nil {
		t.Fatal(err)
	}
	if portInUse(port) {
		t.Fatalf("closed port %d was reported in use", port)
	}
}
