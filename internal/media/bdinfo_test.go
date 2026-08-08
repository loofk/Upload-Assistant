package media

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestBDInfoRunsNonInteractiveIsolatedInspection(t *testing.T) {
	directory := t.TempDir()
	binary := filepath.Join(directory, "fixture-bdinfo")
	script := `#!/bin/sh
if [ "$1" = "-v" ]; then
  echo "BDInfo 1.0.5"
  exit 0
fi
if [ "$1" != "-w" ]; then exit 9; fi
printf 'DISC INFO:\nDisc Title: Fixture\nPLAYLIST REPORT:\nName: 00001.MPLS\nVideo: 1080p' > "$3/BDINFO.txt"
`
	if err := os.WriteFile(binary, []byte(script), 0o750); err != nil {
		t.Fatal(err)
	}
	discRoot := filepath.Join(directory, "disc")
	if err := os.MkdirAll(filepath.Join(discRoot, "BDMV", "STREAM"), 0o750); err != nil {
		t.Fatal(err)
	}
	result, err := NewBDInfo(binary, filepath.Join(directory, "tmp"), 5*time.Second).Inspect(context.Background(), discRoot)
	if err != nil {
		t.Fatal(err)
	}
	if result.Tool != "bdinfo" || result.Format != "text" || result.Filename != "bdinfo.txt" || !strings.Contains(string(result.Document), "00001.MPLS") {
		t.Fatalf("inspection = %#v", result)
	}
	entries, err := os.ReadDir(filepath.Join(directory, "tmp"))
	if err != nil || len(entries) != 0 {
		t.Fatalf("temporary reports were not cleaned: %#v/%v", entries, err)
	}
}

func TestBDInfoRejectsAmbiguousAndNonDiscReports(t *testing.T) {
	directory := t.TempDir()
	nonDisc := filepath.Join(directory, "not-disc")
	if err := os.Mkdir(nonDisc, 0o750); err != nil {
		t.Fatal(err)
	}
	if _, err := NewBDInfo("missing", filepath.Join(directory, "tmp"), time.Second).Inspect(context.Background(), nonDisc); err == nil {
		t.Fatal("Inspect() non-disc error = nil")
	}

	reports := filepath.Join(directory, "reports")
	if err := os.Mkdir(reports, 0o750); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"one.txt", "two.txt"} {
		if err := os.WriteFile(filepath.Join(reports, name), []byte(name), 0o640); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := readSingleBDInfoReport(reports); err == nil {
		t.Fatal("readSingleBDInfoReport() ambiguous error = nil")
	}
}
