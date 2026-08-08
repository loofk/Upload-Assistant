package migrations

import (
	"io/fs"
	"sort"
	"strconv"
	"strings"
	"testing"
)

func TestMigrationFilesAreOrderedAndNamed(t *testing.T) {
	entries, err := fs.Glob(Files, "*.sql")
	if err != nil {
		t.Fatalf("Glob() error = %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("no embedded migrations")
	}
	sorted := append([]string(nil), entries...)
	sort.Strings(sorted)
	for i, name := range sorted {
		prefix := strings.Repeat("0", 4-len(strconv.Itoa(i+1))) + strconv.Itoa(i+1) + "_"
		if !strings.HasPrefix(name, prefix) {
			t.Fatalf("migration %d has unexpected name %q", i+1, name)
		}
		body, readErr := Files.ReadFile(name)
		if readErr != nil {
			t.Fatalf("ReadFile(%q) error = %v", name, readErr)
		}
		if len(strings.TrimSpace(string(body))) == 0 {
			t.Fatalf("migration %q is empty", name)
		}
	}
}
