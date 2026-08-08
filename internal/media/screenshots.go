package media

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

const maxScreenshotBytes = 25 << 20

type Screenshot struct {
	Index     int     `json:"index"`
	Timestamp float64 `json:"timestamp_seconds"`
	Format    string  `json:"format"`
	Filename  string  `json:"filename"`
	MIMEType  string  `json:"mime_type"`
	Bytes     []byte  `json:"-"`
	SizeBytes int64   `json:"size_bytes"`
}

type ScreenshotBatch struct {
	Tool            string       `json:"tool"`
	Version         string       `json:"version"`
	InputPath       string       `json:"input_path"`
	DurationSeconds float64      `json:"duration_seconds"`
	Screenshots     []Screenshot `json:"screenshots"`
	DurationMS      int64        `json:"duration_ms"`
}

type FFmpegScreenshots struct {
	ffmpeg  string
	ffprobe string
	timeout time.Duration
}

func NewFFmpegScreenshots(ffmpeg, ffprobe string, timeout time.Duration) *FFmpegScreenshots {
	if strings.TrimSpace(ffmpeg) == "" {
		ffmpeg = "ffmpeg"
	}
	if strings.TrimSpace(ffprobe) == "" {
		ffprobe = "ffprobe"
	}
	if timeout <= 0 {
		timeout = 5 * time.Minute
	}
	return &FFmpegScreenshots{ffmpeg: ffmpeg, ffprobe: ffprobe, timeout: timeout}
}

func (generator *FFmpegScreenshots) Generate(ctx context.Context, inputPath string, config integrations.ScreenshotConfig) (ScreenshotBatch, error) {
	info, err := os.Stat(inputPath)
	if err != nil || !info.Mode().IsRegular() {
		return ScreenshotBatch{}, fmt.Errorf("screenshot input is not a readable regular file")
	}
	runCtx, cancel := context.WithTimeout(ctx, generator.timeout)
	defer cancel()
	started := time.Now()
	version, err := generator.ffmpegVersion(runCtx)
	if err != nil {
		return ScreenshotBatch{}, err
	}
	duration, err := generator.duration(runCtx, inputPath)
	if err != nil {
		return ScreenshotBatch{}, err
	}
	if config.Count < 1 || config.Count > 20 {
		return ScreenshotBatch{}, fmt.Errorf("screenshot count must be between 1 and 20")
	}
	timestamps := screenshotTimestamps(duration, config)
	result := ScreenshotBatch{
		Tool: "ffmpeg", Version: version, InputPath: inputPath,
		DurationSeconds: duration, Screenshots: make([]Screenshot, 0, len(timestamps)),
	}
	for index, timestamp := range timestamps {
		screenshot, err := generator.capture(runCtx, inputPath, index+1, timestamp, config)
		if err != nil {
			return ScreenshotBatch{}, err
		}
		result.Screenshots = append(result.Screenshots, screenshot)
	}
	result.DurationMS = time.Since(started).Milliseconds()
	return result, nil
}

func (generator *FFmpegScreenshots) duration(ctx context.Context, inputPath string) (float64, error) {
	stdout := newBoundedBuffer(4096)
	stderr := newBoundedBuffer(maxToolErrorOutput)
	command := exec.CommandContext(ctx, generator.ffprobe,
		"-v", "error", "-show_entries", "format=duration",
		"-of", "default=noprint_wrappers=1:nokey=1", inputPath,
	)
	command.Stdout, command.Stderr = stdout, stderr
	if err := command.Run(); err != nil {
		var execError *exec.Error
		if errors.As(err, &execError) {
			return 0, fmt.Errorf("%w: %s", ErrToolUnavailable, generator.ffprobe)
		}
		return 0, fmt.Errorf("ffprobe duration failed: %s", safeToolMessage(stderr.String()))
	}
	duration, err := strconv.ParseFloat(strings.TrimSpace(stdout.String()), 64)
	if err != nil || math.IsNaN(duration) || math.IsInf(duration, 0) || duration <= 0 {
		return 0, fmt.Errorf("ffprobe returned an invalid duration")
	}
	return duration, nil
}

func (generator *FFmpegScreenshots) ffmpegVersion(ctx context.Context) (string, error) {
	stdout := newBoundedBuffer(4096)
	stderr := newBoundedBuffer(4096)
	command := exec.CommandContext(ctx, generator.ffmpeg, "-version")
	command.Stdout, command.Stderr = stdout, stderr
	if err := command.Run(); err != nil {
		var execError *exec.Error
		if errors.As(err, &execError) {
			return "", fmt.Errorf("%w: %s", ErrToolUnavailable, generator.ffmpeg)
		}
		return "", fmt.Errorf("read ffmpeg version: %s", safeToolMessage(stderr.String()))
	}
	line, _, _ := strings.Cut(strings.TrimSpace(stdout.String()), "\n")
	if len(line) > 200 {
		line = line[:200]
	}
	return line, nil
}

func (generator *FFmpegScreenshots) capture(ctx context.Context, inputPath string, index int, timestamp float64, config integrations.ScreenshotConfig) (Screenshot, error) {
	format, extension, mimeType, codec := screenshotFormat(config.Format)
	args := []string{
		"-hide_banner", "-loglevel", "error", "-ss", strconv.FormatFloat(timestamp, 'f', 3, 64),
		"-i", inputPath, "-frames:v", "1",
	}
	if config.Width > 0 {
		args = append(args, "-vf", fmt.Sprintf("scale=%d:-2", config.Width))
	}
	switch format {
	case "jpg":
		quality := 31 - int(math.Round(float64(config.Quality-1)*29/99))
		args = append(args, "-q:v", strconv.Itoa(quality))
	case "webp":
		args = append(args, "-quality", strconv.Itoa(config.Quality))
	}
	args = append(args, "-f", "image2pipe", "-vcodec", codec, "pipe:1")
	stdout := newBoundedBuffer(maxScreenshotBytes)
	stderr := newBoundedBuffer(maxToolErrorOutput)
	command := exec.CommandContext(ctx, generator.ffmpeg, args...)
	command.Stdout, command.Stderr = stdout, stderr
	if err := command.Run(); err != nil {
		return Screenshot{}, fmt.Errorf("ffmpeg screenshot %d failed: %s", index, safeToolMessage(stderr.String()))
	}
	if stdout.Exceeded() || len(stdout.Bytes()) == 0 {
		return Screenshot{}, fmt.Errorf("ffmpeg screenshot %d is empty or exceeds %d bytes", index, maxScreenshotBytes)
	}
	body := append([]byte(nil), stdout.Bytes()...)
	if !validImageSignature(format, body) {
		return Screenshot{}, fmt.Errorf("ffmpeg screenshot %d has an invalid %s signature", index, format)
	}
	return Screenshot{
		Index: index, Timestamp: timestamp, Format: format,
		Filename: fmt.Sprintf("screenshot-%02d.%s", index, extension), MIMEType: mimeType,
		Bytes: body, SizeBytes: int64(len(body)),
	}, nil
}

func screenshotTimestamps(duration float64, config integrations.ScreenshotConfig) []float64 {
	start := duration * config.StartPercent
	end := duration * config.EndPercent
	result := make([]float64, config.Count)
	for index := range result {
		fraction := float64(index+1) / float64(config.Count+1)
		result[index] = start + (end-start)*fraction
	}
	return result
}

func screenshotFormat(value string) (format, extension, mimeType, codec string) {
	switch strings.ToLower(value) {
	case "jpg":
		return "jpg", "jpg", "image/jpeg", "mjpeg"
	case "webp":
		return "webp", "webp", "image/webp", "libwebp"
	default:
		return "png", "png", "image/png", "png"
	}
}

func validImageSignature(format string, body []byte) bool {
	switch format {
	case "png":
		return len(body) >= 8 && string(body[:8]) == "\x89PNG\r\n\x1a\n"
	case "jpg":
		return len(body) >= 3 && body[0] == 0xff && body[1] == 0xd8 && body[2] == 0xff
	case "webp":
		return len(body) >= 12 && string(body[:4]) == "RIFF" && string(body[8:12]) == "WEBP"
	default:
		return false
	}
}
