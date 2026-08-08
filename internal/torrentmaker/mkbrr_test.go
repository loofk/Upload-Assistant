package torrentmaker

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestMkbrrSanitizesThenChecksInPrivateWorkspace(t *testing.T) {
	tempRoot := t.TempDir()
	content := filepath.Join(t.TempDir(), "video.mkv")
	if err := os.WriteFile(content, []byte("content"), 0o600); err != nil {
		t.Fatal(err)
	}
	maker := NewMkbrr("mkbrr-fixture", tempRoot, time.Minute)
	calls := make([][]string, 0)
	maker.run = func(_ context.Context, binary string, args []string, directory string) (commandOutput, error) {
		if binary != "mkbrr-fixture" {
			t.Fatalf("binary = %q", binary)
		}
		calls = append(calls, append([]string(nil), args...))
		switch args[0] {
		case "version":
			return commandOutput{stdout: "mkbrr version: v1.23.0\n"}, nil
		case "modify":
			if !slices.Contains(args, "https://fake.tracker") || !slices.Contains(args, "MTEAM") ||
				!slices.Contains(args, "--no-date") || !slices.Contains(args, "--no-creator") {
				t.Fatalf("modify args = %#v", args)
			}
			if err := os.WriteFile(filepath.Join(directory, "target-candidate.torrent"), realContractTargetTorrent([]byte("content")), 0o600); err != nil {
				t.Fatal(err)
			}
			return commandOutput{stdout: filepath.Join(directory, "target-candidate.torrent")}, nil
		case "check":
			if args[2] != content || args[3] != "--quiet" {
				t.Fatalf("check args = %#v", args)
			}
			return commandOutput{stdout: "100.00%\n"}, nil
		default:
			t.Fatalf("unexpected args = %#v", args)
			return commandOutput{}, nil
		}
	}
	result, err := maker.SanitizeAndCheck(context.Background(), Request{
		SourceTorrent: []byte("source"), ContentPath: content,
		AnnounceURL: "https://fake.tracker", SourceTag: "MTEAM",
		TopLevelKeys: []string{"announce", "info"},
	})
	if err != nil || len(result.Torrent) == 0 || result.Version != "mkbrr version: v1.23.0" || result.Verification != "100.00%" || len(calls) != 3 {
		t.Fatalf("SanitizeAndCheck() result/error/calls = %#v/%v/%#v", result, err, calls)
	}
	entries, err := os.ReadDir(tempRoot)
	if err != nil || len(entries) != 0 {
		t.Fatalf("private workspace cleanup entries/error = %#v/%v", entries, err)
	}
}

func TestMkbrrFailsClosedOnInvalidInputsAndCheckFailure(t *testing.T) {
	tempRoot := t.TempDir()
	content := filepath.Join(t.TempDir(), "video.mkv")
	if err := os.WriteFile(content, []byte("content"), 0o600); err != nil {
		t.Fatal(err)
	}
	maker := NewMkbrr("mkbrr", tempRoot, time.Minute)
	_, err := maker.SanitizeAndCheck(context.Background(), Request{
		SourceTorrent: []byte("source"), ContentPath: content,
		AnnounceURL: "http://tracker.invalid/passkey", SourceTag: "MTEAM",
		TopLevelKeys: []string{"announce", "info"},
	})
	code, _, _ := ErrorDetails(err)
	if code != "target_torrent_input_invalid" {
		t.Fatalf("invalid input code/error = %q/%v", code, err)
	}

	maker.run = func(_ context.Context, _ string, args []string, directory string) (commandOutput, error) {
		switch args[0] {
		case "version":
			return commandOutput{stdout: "v1.23.0"}, nil
		case "modify":
			return commandOutput{}, os.WriteFile(filepath.Join(directory, "target-candidate.torrent"), realContractTargetTorrent([]byte("content")), 0o600)
		case "check":
			return commandOutput{stderr: "piece 2 mismatch"}, errors.New("exit 1")
		default:
			return commandOutput{}, nil
		}
	}
	_, err = maker.SanitizeAndCheck(context.Background(), Request{
		SourceTorrent: []byte("source"), ContentPath: content,
		AnnounceURL: "https://fake.tracker", SourceTag: "MTEAM",
		TopLevelKeys: []string{"announce", "info"},
	})
	code, message, retryable := ErrorDetails(err)
	if code != "target_torrent_content_mismatch" || retryable || !strings.Contains(message, "piece 2 mismatch") {
		t.Fatalf("check failure details = %q/%q/%v", code, message, retryable)
	}
}
