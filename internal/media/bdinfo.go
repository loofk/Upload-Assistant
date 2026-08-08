package media

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

const maxBDInfoOutput = 16 << 20

// BDInfo executes a native BDInfoCLI-compatible binary without a shell. Each
// inspection writes into a private directory so a resumed attempt cannot
// accidentally consume a stale report from an earlier attempt.
type BDInfo struct {
	binary   string
	tempRoot string
	timeout  time.Duration
}

func NewBDInfo(binary, tempRoot string, timeout time.Duration) *BDInfo {
	if strings.TrimSpace(binary) == "" {
		binary = "BDInfo"
	}
	if strings.TrimSpace(tempRoot) == "" {
		tempRoot = os.TempDir()
	}
	if timeout <= 0 {
		timeout = 15 * time.Minute
	}
	return &BDInfo{binary: binary, tempRoot: filepath.Clean(tempRoot), timeout: timeout}
}

func (inspector *BDInfo) Inspect(ctx context.Context, inputPath string) (Inspection, error) {
	root, err := validateBDInfoRoot(inputPath)
	if err != nil {
		return Inspection{}, err
	}
	if !filepath.IsAbs(inspector.tempRoot) {
		return Inspection{}, fmt.Errorf("BDInfo temporary root must be absolute")
	}
	if err := os.MkdirAll(inspector.tempRoot, 0o750); err != nil {
		return Inspection{}, fmt.Errorf("create BDInfo temporary root: %w", err)
	}
	reportDir, err := os.MkdirTemp(inspector.tempRoot, "bdinfo-")
	if err != nil {
		return Inspection{}, fmt.Errorf("create BDInfo report directory: %w", err)
	}
	defer os.RemoveAll(reportDir)
	if err := os.Chmod(reportDir, 0o750); err != nil {
		return Inspection{}, fmt.Errorf("set BDInfo report directory permissions: %w", err)
	}

	runCtx, cancel := context.WithTimeout(ctx, inspector.timeout)
	defer cancel()
	version, err := inspector.version(runCtx)
	if err != nil {
		return Inspection{}, err
	}
	started := time.Now()
	stdout := newBoundedBuffer(maxToolErrorOutput)
	stderr := newBoundedBuffer(maxToolErrorOutput)
	command := exec.CommandContext(runCtx, inspector.binary, "-w", root, reportDir)
	command.Stdout, command.Stderr = stdout, stderr
	if err := command.Run(); err != nil {
		if runCtx.Err() != nil {
			return Inspection{}, fmt.Errorf("BDInfo inspection timed out or was cancelled: %w", runCtx.Err())
		}
		var execError *exec.Error
		if errors.As(err, &execError) {
			return Inspection{}, fmt.Errorf("%w: %s", ErrToolUnavailable, inspector.binary)
		}
		return Inspection{}, fmt.Errorf("BDInfo inspection failed: %s", safeToolMessage(stderr.String()))
	}
	document, err := readSingleBDInfoReport(reportDir)
	if err != nil {
		return Inspection{}, err
	}
	return Inspection{
		Tool: "bdinfo", Version: version, InputPath: root,
		Format: "text", MIMEType: "text/plain; charset=utf-8", Filename: "bdinfo.txt",
		Document: document, DurationMS: time.Since(started).Milliseconds(),
	}, nil
}

func validateBDInfoRoot(inputPath string) (string, error) {
	root := filepath.Clean(strings.TrimSpace(inputPath))
	if !filepath.IsAbs(root) {
		return "", fmt.Errorf("BDInfo input must be an absolute disc root")
	}
	info, err := os.Stat(root)
	if err != nil {
		return "", fmt.Errorf("inspect BDInfo input: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("BDInfo input must be a directory containing BDMV")
	}
	bdmv, err := os.Stat(filepath.Join(root, "BDMV"))
	if err != nil || !bdmv.IsDir() {
		return "", fmt.Errorf("BDInfo input does not contain a BDMV directory")
	}
	return root, nil
}

func (inspector *BDInfo) version(ctx context.Context) (string, error) {
	stdout := newBoundedBuffer(4096)
	stderr := newBoundedBuffer(4096)
	command := exec.CommandContext(ctx, inspector.binary, "-v")
	command.Stdout, command.Stderr = stdout, stderr
	if err := command.Run(); err != nil {
		var execError *exec.Error
		if errors.As(err, &execError) {
			return "", fmt.Errorf("%w: %s", ErrToolUnavailable, inspector.binary)
		}
		return "", fmt.Errorf("read BDInfo version: %s", safeToolMessage(stderr.String()))
	}
	line, _, _ := strings.Cut(strings.TrimSpace(stdout.String()), "\n")
	if line == "" {
		return "", fmt.Errorf("read BDInfo version: empty output")
	}
	if len(line) > 200 {
		line = line[:200]
	}
	return line, nil
}

func readSingleBDInfoReport(directory string) ([]byte, error) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return nil, fmt.Errorf("read BDInfo report directory: %w", err)
	}
	names := make([]string, 0, 1)
	for _, entry := range entries {
		if entry.Type().IsRegular() && strings.EqualFold(filepath.Ext(entry.Name()), ".txt") {
			names = append(names, entry.Name())
		}
	}
	slices.Sort(names)
	if len(names) != 1 {
		return nil, fmt.Errorf("BDInfo produced %d report files; exactly one is required", len(names))
	}
	path := filepath.Join(directory, names[0])
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("BDInfo report is not a regular file")
	}
	if info.Size() <= 0 || info.Size() > maxBDInfoOutput {
		return nil, fmt.Errorf("BDInfo report size is outside the allowed range")
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read BDInfo report: %w", err)
	}
	body = bytes.TrimSpace(body)
	if len(body) == 0 || !utf8.Valid(body) || bytes.IndexByte(body, 0) >= 0 {
		return nil, fmt.Errorf("BDInfo report must be non-empty UTF-8 text without NUL bytes")
	}
	return append([]byte(nil), body...), nil
}
