package media

import (
	"context"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

func TestFFmpegScreenshotsUsesProfileAndStableTimestamps(t *testing.T) {
	directory := t.TempDir()
	ffprobe := filepath.Join(directory, "ffprobe")
	if err := os.WriteFile(ffprobe, []byte("#!/bin/sh\necho 100\n"), 0o750); err != nil {
		t.Fatal(err)
	}
	ffmpeg := filepath.Join(directory, "ffmpeg")
	script := `#!/bin/sh
if [ "$1" = "-version" ]; then
  echo "ffmpeg fixture 1.0"
  exit 0
fi
printf '\211PNG\r\n\032\nfixture'
`
	if err := os.WriteFile(ffmpeg, []byte(script), 0o750); err != nil {
		t.Fatal(err)
	}
	input := filepath.Join(directory, "video.mkv")
	if err := os.WriteFile(input, []byte("fixture"), 0o640); err != nil {
		t.Fatal(err)
	}
	config := integrations.ScreenshotConfig{Count: 2, Format: "png", Quality: 90, StartPercent: 0.1, EndPercent: 0.9}
	result, err := NewFFmpegScreenshots(ffmpeg, ffprobe, 5*time.Second).Generate(context.Background(), input, config)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Screenshots) != 2 || result.Version != "ffmpeg fixture 1.0" ||
		math.Abs(result.Screenshots[0].Timestamp-36.666666) > 0.001 || math.Abs(result.Screenshots[1].Timestamp-63.333333) > 0.001 {
		t.Fatalf("screenshot batch = %#v", result)
	}
}
