package event

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"unicode/utf8"
)

// maxLineBytes caps how long a single JSONL line is allowed to be. We never
// expect a real event to exceed a few KB, but enforce a hard ceiling so a
// runaway writer can't blow out memory during replay.
const maxLineBytes = 1 << 20 // 1 MiB

// Append writes one event as a JSONL line to the log at path.
//
// The caller MUST hold the per-repo flock before invoking this. Append opens
// the file with O_APPEND, so writes always land at the current end of file
// regardless of seek position.
func Append(path string, e Event) error {
	line, err := e.Encode()
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("event log: open: %w", err)
	}
	defer func() { _ = f.Close() }()
	for len(line) > 0 {
		n, err := f.Write(line)
		if err != nil {
			return fmt.Errorf("event log: write: %w", err)
		}
		if n == 0 {
			return fmt.Errorf("event log: write: %w", io.ErrShortWrite)
		}
		line = line[n:]
	}
	return nil
}

// AppendBatch writes several events as consecutive JSONL lines in one open +
// one write. The caller MUST hold the per-repo flock.
//
// It keeps a multi-event command (batch claim, release --all-mine) all-or-
// nothing on the WRITE side too: every event is encoded up front, so a bad
// encode writes nothing, and on a partial write fault (e.g. ENOSPC mid-buffer)
// the file is truncated back to its pre-write size. A single Write also means
// no other holder of the log can interleave between the batch's lines.
func AppendBatch(path string, evs []Event) error {
	if len(evs) == 0 {
		return nil
	}
	var buf []byte
	for _, e := range evs {
		line, err := e.Encode()
		if err != nil {
			return err
		}
		buf = append(buf, line...)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("event log: open: %w", err)
	}
	defer func() { _ = f.Close() }()
	start, err := f.Seek(0, io.SeekEnd)
	if err != nil {
		return fmt.Errorf("event log: seek: %w", err)
	}
	if _, err := f.Write(buf); err != nil {
		// Roll back any partially written bytes so the batch is all-or-nothing.
		_ = f.Truncate(start)
		return fmt.Errorf("event log: write: %w", err)
	}
	return nil
}

// Read parses the entire JSONL log at path.
//
// Recovery policy (matches the plan's "JSONL parse rules" table):
//   - Missing file → empty slice, no error.
//   - Blank lines → silently skipped.
//   - Trailing unterminated final line → logged to stderr, ignored.
//   - Malformed JSON before EOF → returns ErrCorrupt with line number.
//   - Invalid UTF-8 → returns ErrCorrupt.
//   - Lines longer than maxLineBytes → returns ErrCorrupt.
//   - Duplicate event IDs → first wins, duplicates dropped silently.
func Read(path string) ([]Event, error) {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("event log: open: %w", err)
	}
	defer func() { _ = f.Close() }()

	br := bufio.NewReaderSize(f, 64*1024)
	var (
		events  []Event
		seen    = make(map[string]struct{})
		lineNum int
	)
	for {
		lineNum++
		line, hasNewline, err := readLine(br)
		switch {
		case len(line) == 0 && errors.Is(err, io.EOF):
			return events, nil
		case errors.Is(err, errLineTooLong):
			return nil, &ErrCorrupt{Path: path, Line: lineNum, Cause: fmt.Errorf("line exceeds %d bytes", maxLineBytes)}
		case err != nil && !errors.Is(err, io.EOF):
			return nil, fmt.Errorf("event log: read line %d: %w", lineNum, err)
		}
		// If we got data but no newline and we're at EOF, that's a torn final line.
		if !hasNewline {
			// Trailing whitespace-only data is just an unterminated empty line; ignore.
			if onlyWhitespace(line) {
				return events, nil
			}
			fmt.Fprintf(os.Stderr, "comms: warning: log %s ends with unterminated line %d, ignored\n", path, lineNum)
			return events, nil
		}
		// Strip the trailing newline for parsing.
		trimmed := line[:len(line)-1]
		if onlyWhitespace(trimmed) {
			continue
		}
		if !utf8.Valid(trimmed) {
			return nil, &ErrCorrupt{Path: path, Line: lineNum, Cause: fmt.Errorf("invalid UTF-8")}
		}
		ev, derr := Decode(trimmed)
		if derr != nil {
			return nil, &ErrCorrupt{Path: path, Line: lineNum, Cause: derr}
		}
		if _, dup := seen[ev.ID]; dup {
			continue
		}
		seen[ev.ID] = struct{}{}
		events = append(events, ev)

		if errors.Is(err, io.EOF) {
			return events, nil
		}
	}
}

var errLineTooLong = errors.New("line too long")

func readLine(br *bufio.Reader) ([]byte, bool, error) {
	var out []byte
	for {
		frag, err := br.ReadSlice('\n')
		out = append(out, frag...)
		if len(out) > maxLineBytes {
			return nil, false, errLineTooLong
		}
		switch {
		case err == nil:
			return out, true, nil
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		case errors.Is(err, io.EOF):
			return out, false, io.EOF
		default:
			return out, false, err
		}
	}
}

// ErrCorrupt signals an unrecoverable parse error mid-log. The CLI maps this
// to exit code 2.
type ErrCorrupt struct {
	Path  string
	Line  int
	Cause error
}

func (e *ErrCorrupt) Error() string {
	return fmt.Sprintf("event log %s: corrupt at line %d: %v", e.Path, e.Line, e.Cause)
}

func (e *ErrCorrupt) Unwrap() error { return e.Cause }

func onlyWhitespace(b []byte) bool {
	for _, c := range b {
		switch c {
		case ' ', '\t', '\r', '\n':
			continue
		default:
			return false
		}
	}
	return true
}
