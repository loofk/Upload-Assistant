package artifacts

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"strings"
	"testing"
)

func TestLocalStoreWriteOpenAndDeduplicate(t *testing.T) {
	store, err := NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewLocalStore() error = %v", err)
	}
	scope := Scope{JobID: "job", StepID: "step", AttemptID: "attempt"}
	content := "auditable artifact"
	written, err := store.Write(context.Background(), scope, "evidence.txt", strings.NewReader(content))
	if err != nil {
		t.Fatalf("Write() error = %v", err)
	}
	sum := sha256.Sum256([]byte(content))
	if written.SHA256 != hex.EncodeToString(sum[:]) || written.SizeBytes != int64(len(content)) {
		t.Fatalf("artifact evidence = %s/%d", written.SHA256, written.SizeBytes)
	}
	again, err := store.Write(context.Background(), scope, "evidence.txt", strings.NewReader(content))
	if err != nil {
		t.Fatalf("duplicate Write() error = %v", err)
	}
	if again.RelativePath != written.RelativePath {
		t.Fatalf("duplicate path = %q, want %q", again.RelativePath, written.RelativePath)
	}
	file, err := store.Open(written.RelativePath)
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	defer file.Close()
	got, err := io.ReadAll(file)
	if err != nil || string(got) != content {
		t.Fatalf("artifact content/error = %q/%v", got, err)
	}
}

func TestLocalStoreRejectsTraversal(t *testing.T) {
	store, err := NewLocalStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewLocalStore() error = %v", err)
	}
	if _, err := store.Write(context.Background(), Scope{JobID: "../job", StepID: "step", AttemptID: "attempt"}, "file.txt", strings.NewReader("x")); err == nil {
		t.Fatal("Write() traversal error = nil")
	}
	if _, err := store.Open("../../etc/passwd"); err == nil {
		t.Fatal("Open() traversal error = nil")
	}
}
