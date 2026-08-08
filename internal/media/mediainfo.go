package media

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

var ErrToolUnavailable = errors.New("media inspection tool is unavailable")

const (
	maxMediaInfoOutput = 16 << 20
	maxToolErrorOutput = 64 << 10
)

type Inspection struct {
	Tool       string          `json:"tool"`
	Version    string          `json:"version"`
	InputPath  string          `json:"input_path"`
	Document   json.RawMessage `json:"document"`
	DurationMS int64           `json:"duration_ms"`
}

type MediaInfo struct {
	binary  string
	timeout time.Duration
}

func NewMediaInfo(binary string, timeout time.Duration) *MediaInfo {
	if strings.TrimSpace(binary) == "" {
		binary = "mediainfo"
	}
	if timeout <= 0 {
		timeout = 2 * time.Minute
	}
	return &MediaInfo{binary: binary, timeout: timeout}
}

func (inspector *MediaInfo) Inspect(ctx context.Context, inputPath string) (Inspection, error) {
	info, err := os.Stat(inputPath)
	if err != nil {
		return Inspection{}, fmt.Errorf("inspect media input: %w", err)
	}
	if !info.Mode().IsRegular() {
		return Inspection{}, fmt.Errorf("media input is not a regular file")
	}
	runCtx, cancel := context.WithTimeout(ctx, inspector.timeout)
	defer cancel()
	version, err := inspector.version(runCtx)
	if err != nil {
		return Inspection{}, err
	}
	started := time.Now()
	stdout := newBoundedBuffer(maxMediaInfoOutput)
	stderr := newBoundedBuffer(maxToolErrorOutput)
	command := exec.CommandContext(runCtx, inspector.binary, "--Output=JSON", inputPath)
	command.Stdout = stdout
	command.Stderr = stderr
	if err := command.Run(); err != nil {
		if runCtx.Err() != nil {
			return Inspection{}, fmt.Errorf("MediaInfo inspection timed out or was cancelled: %w", runCtx.Err())
		}
		var execError *exec.Error
		if errors.As(err, &execError) {
			return Inspection{}, fmt.Errorf("%w: %s", ErrToolUnavailable, inspector.binary)
		}
		return Inspection{}, fmt.Errorf("MediaInfo inspection failed: %s", safeToolMessage(stderr.String()))
	}
	if stdout.Exceeded() {
		return Inspection{}, fmt.Errorf("MediaInfo output exceeds %d bytes", maxMediaInfoOutput)
	}
	body := bytes.TrimSpace(stdout.Bytes())
	if !json.Valid(body) {
		return Inspection{}, fmt.Errorf("MediaInfo returned invalid JSON")
	}
	var envelope struct {
		Media json.RawMessage `json:"media"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil || len(envelope.Media) == 0 || string(envelope.Media) == "null" {
		return Inspection{}, fmt.Errorf("MediaInfo JSON does not contain media data")
	}
	compact := make([]byte, 0, len(body))
	buffer := bytes.NewBuffer(compact)
	if err := json.Compact(buffer, body); err != nil {
		return Inspection{}, fmt.Errorf("compact MediaInfo JSON: %w", err)
	}
	return Inspection{
		Tool: "mediainfo", Version: version, InputPath: inputPath,
		Document:   append(json.RawMessage(nil), buffer.Bytes()...),
		DurationMS: time.Since(started).Milliseconds(),
	}, nil
}

func (inspector *MediaInfo) version(ctx context.Context) (string, error) {
	stdout := newBoundedBuffer(4096)
	stderr := newBoundedBuffer(4096)
	command := exec.CommandContext(ctx, inspector.binary, "--Version")
	command.Stdout, command.Stderr = stdout, stderr
	if err := command.Run(); err != nil {
		var execError *exec.Error
		if errors.As(err, &execError) {
			return "", fmt.Errorf("%w: %s", ErrToolUnavailable, inspector.binary)
		}
		return "", fmt.Errorf("read MediaInfo version: %s", safeToolMessage(stderr.String()))
	}
	line, _, _ := strings.Cut(strings.TrimSpace(stdout.String()), "\n")
	if len(line) > 200 {
		line = line[:200]
	}
	return line, nil
}

type boundedBuffer struct {
	buffer   bytes.Buffer
	limit    int
	exceeded bool
}

func newBoundedBuffer(limit int) *boundedBuffer { return &boundedBuffer{limit: limit} }

func (buffer *boundedBuffer) Write(value []byte) (int, error) {
	originalLength := len(value)
	remaining := buffer.limit - buffer.buffer.Len()
	if remaining <= 0 {
		buffer.exceeded = true
		return originalLength, nil
	}
	if len(value) > remaining {
		value = value[:remaining]
		buffer.exceeded = true
	}
	_, _ = buffer.buffer.Write(value)
	return originalLength, nil
}

func (buffer *boundedBuffer) Bytes() []byte  { return buffer.buffer.Bytes() }
func (buffer *boundedBuffer) String() string { return buffer.buffer.String() }
func (buffer *boundedBuffer) Exceeded() bool { return buffer.exceeded }

func safeToolMessage(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\n", " "))
	if value == "" {
		return "tool exited unsuccessfully"
	}
	if len(value) > 500 {
		value = value[:500]
	}
	return value
}
