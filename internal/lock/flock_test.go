package lock

import (
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestAcquireRelease(t *testing.T) {
	path := filepath.Join(t.TempDir(), "test.lock")
	h, err := Acquire(path)
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	if err := h.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	// Re-acquire after release should succeed.
	h2, err := Acquire(path)
	if err != nil {
		t.Fatalf("re-Acquire: %v", err)
	}
	_ = h2.Close()
}

func TestTryAcquireBlocked(t *testing.T) {
	path := filepath.Join(t.TempDir(), "test.lock")
	h, err := Acquire(path)
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	defer h.Close()

	_, err = TryAcquire(path)
	if !errors.Is(err, ErrLocked) {
		t.Fatalf("expected ErrLocked, got %v", err)
	}
}

func TestTryAcquireOK(t *testing.T) {
	path := filepath.Join(t.TempDir(), "test.lock")

	// First acquire succeeds.
	h, ok, err := TryAcquireOK(path)
	if err != nil || !ok || h == nil {
		t.Fatalf("first TryAcquireOK: handle=%v ok=%v err=%v", h, ok, err)
	}

	// While held, repeated attempts report ok=false with no error, and must
	// NOT leak file descriptors: this loop opens+closes an FD per call, so a
	// leak would exhaust the per-process FD limit and surface as an open error.
	for i := 0; i < 4096; i++ {
		h2, ok2, err2 := TryAcquireOK(path)
		if err2 != nil {
			t.Fatalf("TryAcquireOK while held (iter %d): unexpected error %v (FD leak?)", i, err2)
		}
		if ok2 || h2 != nil {
			t.Fatalf("TryAcquireOK while held (iter %d): got ok=%v handle=%v, want ok=false handle=nil", i, ok2, h2)
		}
	}

	// After release, the lock is obtainable again.
	if err := h.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	h3, ok3, err3 := TryAcquireOK(path)
	if err3 != nil || !ok3 || h3 == nil {
		t.Fatalf("TryAcquireOK after release: handle=%v ok=%v err=%v", h3, ok3, err3)
	}
	_ = h3.Close()
}

func TestSerializeTwoGoroutines(t *testing.T) {
	path := filepath.Join(t.TempDir(), "test.lock")

	// Goroutine A holds the lock for 50ms.
	done := make(chan struct{})
	go func() {
		h, err := Acquire(path)
		if err != nil {
			t.Errorf("A Acquire: %v", err)
			close(done)
			return
		}
		time.Sleep(50 * time.Millisecond)
		_ = h.Close()
		close(done)
	}()

	// Give A a moment to acquire.
	time.Sleep(5 * time.Millisecond)

	// B should block until A releases.
	start := time.Now()
	h, err := Acquire(path)
	if err != nil {
		t.Fatalf("B Acquire: %v", err)
	}
	elapsed := time.Since(start)
	_ = h.Close()
	<-done

	if elapsed < 30*time.Millisecond {
		t.Fatalf("B acquired too quickly: %v (expected to block on A)", elapsed)
	}
}
