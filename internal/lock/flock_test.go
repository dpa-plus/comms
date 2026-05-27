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
