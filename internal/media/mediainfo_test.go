package media

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestMediaInfoRunsBoundedJSONInspection(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "fixture-mediainfo")
	script := `#!/bin/sh
if [ "$1" = "--Version" ]; then
  echo "MediaInfoLib - v24.01"
  exit 0
fi
echo '{"media":{"track":[{"@type":"General","FileSize":"13"}]}}'
`
	if err := os.WriteFile(binary, []byte(script), 0o750); err != nil {
		t.Fatal(err)
	}
	input := filepath.Join(directory, "video.mkv")
	if err := os.WriteFile(input, []byte("fixture-video"), 0o640); err != nil {
		t.Fatal(err)
	}
	result, err := NewMediaInfo(binary, 5*time.Second).Inspect(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	if result.Tool != "mediainfo" || result.Version != "MediaInfoLib - v24.01" || !json.Valid(result.Document) {
		t.Fatalf("inspection = %#v", result)
	}
}

func TestMediaInfoRejectsInvalidToolOutput(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "fixture-mediainfo")
	script := "#!/bin/sh\nif [ \"$1\" = \"--Version\" ]; then echo version; else echo invalid; fi\n"
	if err := os.WriteFile(binary, []byte(script), 0o750); err != nil {
		t.Fatal(err)
	}
	input := filepath.Join(directory, "video.mkv")
	if err := os.WriteFile(input, []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	if _, err := NewMediaInfo(binary, time.Second).Inspect(context.Background(), input); err == nil {
		t.Fatal("Inspect() invalid output error = nil")
	}
}
