package torrentmaker

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

const (
	maxTorrentBytes  = 32 << 20
	maxCommandOutput = 64 << 10
)

var sourceTagPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,32}$`)

type Request struct {
	SourceTorrent []byte
	ContentPath   string
	AnnounceURL   string
	SourceTag     string
	TopLevelKeys  []string
}

type Result struct {
	Torrent          []byte `json:"-"`
	Tool             string `json:"tool"`
	Version          string `json:"version"`
	Verification     string `json:"verification"`
	ModifyDurationMS int64  `json:"modify_duration_ms"`
	CheckDurationMS  int64  `json:"check_duration_ms"`
}

type ToolError struct {
	Code      string
	Message   string
	Retryable bool
	Cause     error
}

func (err *ToolError) Error() string {
	if err.Message != "" {
		return err.Message
	}
	return err.Code
}

func (err *ToolError) Unwrap() error { return err.Cause }

func ErrorDetails(err error) (code, message string, retryable bool) {
	var toolError *ToolError
	if errors.As(err, &toolError) {
		return toolError.Code, toolError.Error(), toolError.Retryable
	}
	return "target_torrent_tool_failed", err.Error(), false
}

type commandOutput struct {
	stdout string
	stderr string
}

type commandRunner func(context.Context, string, []string, string) (commandOutput, error)

type Mkbrr struct {
	binary   string
	tempRoot string
	timeout  time.Duration
	run      commandRunner
}

func NewMkbrr(binary, tempRoot string, timeout time.Duration) *Mkbrr {
	if strings.TrimSpace(binary) == "" {
		binary = "mkbrr"
	}
	if timeout <= 0 {
		timeout = 6 * time.Hour
	}
	return &Mkbrr{binary: binary, tempRoot: filepath.Clean(tempRoot), timeout: timeout, run: runCommand}
}

func (maker *Mkbrr) SanitizeAndCheck(ctx context.Context, request Request) (Result, error) {
	if err := maker.validate(request); err != nil {
		return Result{}, &ToolError{Code: "target_torrent_input_invalid", Message: err.Error()}
	}
	runCtx, cancel := context.WithTimeout(ctx, maker.timeout)
	defer cancel()
	versionOutput, err := maker.run(runCtx, maker.binary, []string{"version"}, maker.tempRoot)
	if err != nil {
		return Result{}, maker.commandError(runCtx, "torrent_tool_unavailable", "read mkbrr version", err, versionOutput, true)
	}
	version := firstLine(versionOutput.stdout)
	if version == "" {
		return Result{}, &ToolError{Code: "torrent_tool_version_invalid", Message: "mkbrr returned an empty version"}
	}

	directory, err := os.MkdirTemp(maker.tempRoot, "mkbrr-target-*")
	if err != nil {
		return Result{}, &ToolError{Code: "target_torrent_workspace_failed", Message: "could not create the private mkbrr workspace", Cause: err}
	}
	defer os.RemoveAll(directory)
	if err := os.Chmod(directory, 0o700); err != nil {
		return Result{}, &ToolError{Code: "target_torrent_workspace_failed", Message: "could not protect the mkbrr workspace", Cause: err}
	}
	sourcePath := filepath.Join(directory, "source.torrent")
	if err := os.WriteFile(sourcePath, request.SourceTorrent, 0o600); err != nil {
		return Result{}, &ToolError{Code: "target_torrent_workspace_failed", Message: "could not stage the source torrent for mkbrr", Cause: err}
	}
	targetPath := filepath.Join(directory, "target-candidate.torrent")
	modifyArgs := []string{
		"modify", "--tracker", request.AnnounceURL, "--source", request.SourceTag,
		"--private", "--comment", "", "--no-date", "--no-creator",
		"--skip-prefix", "--quiet", "--output", "target-candidate", sourcePath,
	}
	started := time.Now()
	modifyOutput, err := maker.run(runCtx, maker.binary, modifyArgs, directory)
	modifyDuration := time.Since(started).Milliseconds()
	if err != nil {
		return Result{}, maker.commandError(runCtx, "target_torrent_modify_failed", "mkbrr could not sanitize the source torrent", err, modifyOutput, true)
	}
	torrent, err := os.ReadFile(targetPath)
	if err != nil || len(torrent) == 0 || len(torrent) > maxTorrentBytes {
		return Result{}, &ToolError{Code: "target_torrent_output_invalid", Message: "mkbrr did not create a bounded target torrent artifact", Cause: err}
	}
	torrent, err = torrentmeta.KeepTopLevelFields(torrent, request.TopLevelKeys)
	if err != nil {
		return Result{}, &ToolError{Code: "target_torrent_output_invalid", Message: "mkbrr output could not be minimized to the target top-level profile", Cause: err}
	}
	if err := os.WriteFile(targetPath, torrent, 0o600); err != nil {
		return Result{}, &ToolError{Code: "target_torrent_workspace_failed", Message: "could not stage the minimized target torrent for piece verification", Cause: err}
	}

	started = time.Now()
	checkOutput, err := maker.run(runCtx, maker.binary, []string{"check", targetPath, request.ContentPath, "--quiet"}, directory)
	checkDuration := time.Since(started).Milliseconds()
	if err != nil {
		return Result{}, maker.commandError(runCtx, "target_torrent_content_mismatch", "mkbrr could not verify every target torrent piece against local content", err, checkOutput, false)
	}
	verification := firstLine(checkOutput.stdout)
	if verification == "" {
		verification = "verified"
	}
	return Result{
		Torrent: torrent, Tool: "mkbrr", Version: version, Verification: verification,
		ModifyDurationMS: modifyDuration, CheckDurationMS: checkDuration,
	}, nil
}

func (maker *Mkbrr) validate(request Request) error {
	if len(request.SourceTorrent) == 0 || len(request.SourceTorrent) > maxTorrentBytes {
		return fmt.Errorf("source torrent is empty or exceeds %d bytes", maxTorrentBytes)
	}
	if !filepath.IsAbs(maker.tempRoot) {
		return fmt.Errorf("mkbrr temporary root must be absolute")
	}
	if info, err := os.Stat(maker.tempRoot); err != nil || !info.IsDir() {
		return fmt.Errorf("mkbrr temporary root is unavailable")
	}
	if !filepath.IsAbs(request.ContentPath) || filepath.Clean(request.ContentPath) != request.ContentPath {
		return fmt.Errorf("content path must be a normalized absolute path")
	}
	if info, err := os.Stat(request.ContentPath); err != nil || (!info.Mode().IsRegular() && !info.IsDir()) {
		return fmt.Errorf("content path is not a readable file or directory")
	}
	announce, err := url.Parse(request.AnnounceURL)
	if err != nil || announce.Scheme != "https" || announce.Host == "" || announce.User != nil || announce.RawQuery != "" || announce.Fragment != "" {
		return fmt.Errorf("target announce URL must be HTTPS and contain no credentials, query, or fragment")
	}
	if !sourceTagPattern.MatchString(request.SourceTag) {
		return fmt.Errorf("target source tag is invalid")
	}
	if len(request.TopLevelKeys) == 0 || !containsString(request.TopLevelKeys, "announce") || !containsString(request.TopLevelKeys, "info") {
		return fmt.Errorf("target top-level profile must include announce and info")
	}
	return nil
}

func containsString(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

func (maker *Mkbrr) commandError(ctx context.Context, code, message string, err error, output commandOutput, retryable bool) error {
	if ctx.Err() != nil {
		return &ToolError{Code: "torrent_tool_timeout", Message: "mkbrr timed out or was cancelled", Retryable: true, Cause: ctx.Err()}
	}
	var execError *exec.Error
	if errors.As(err, &execError) {
		return &ToolError{Code: "torrent_tool_unavailable", Message: "the configured mkbrr binary is unavailable", Cause: err}
	}
	detail := safeCommandMessage(output.stderr)
	if detail != "" {
		message += ": " + detail
	}
	return &ToolError{Code: code, Message: message, Retryable: retryable, Cause: err}
}

func runCommand(ctx context.Context, binary string, args []string, directory string) (commandOutput, error) {
	stdout := newLimitedBuffer(maxCommandOutput)
	stderr := newLimitedBuffer(maxCommandOutput)
	command := exec.CommandContext(ctx, binary, args...)
	command.Dir = directory
	command.Stdout = stdout
	command.Stderr = stderr
	err := command.Run()
	if stdout.exceeded || stderr.exceeded {
		return commandOutput{stdout: stdout.String(), stderr: stderr.String()}, fmt.Errorf("mkbrr command output exceeded %d bytes", maxCommandOutput)
	}
	return commandOutput{stdout: stdout.String(), stderr: stderr.String()}, err
}

type limitedBuffer struct {
	buffer   bytes.Buffer
	limit    int
	exceeded bool
}

func newLimitedBuffer(limit int) *limitedBuffer { return &limitedBuffer{limit: limit} }

func (buffer *limitedBuffer) Write(value []byte) (int, error) {
	original := len(value)
	remaining := buffer.limit - buffer.buffer.Len()
	if remaining <= 0 {
		buffer.exceeded = true
		return original, nil
	}
	if len(value) > remaining {
		value = value[:remaining]
		buffer.exceeded = true
	}
	_, _ = buffer.buffer.Write(value)
	return original, nil
}

func (buffer *limitedBuffer) String() string { return buffer.buffer.String() }

func firstLine(value string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(value), "\n")
	line = strings.TrimSpace(line)
	if len(line) > 200 {
		line = line[:200]
	}
	return line
}

func safeCommandMessage(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\n", " "))
	if len(value) > 500 {
		value = value[:500]
	}
	return value
}

var _ io.Writer = (*limitedBuffer)(nil)
