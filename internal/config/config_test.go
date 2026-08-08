package config

import (
	"path/filepath"
	"testing"
)

func TestLoadFromDefaults(t *testing.T) {
	env := map[string]string{"UA_DATABASE_URL": "postgres://ua:secret@db/ua"}
	cfg, err := LoadFrom(func(key string) (string, bool) {
		value, ok := env[key]
		return value, ok
	})
	if err != nil {
		t.Fatalf("LoadFrom() error = %v", err)
	}
	if cfg.ListenAddr != defaultListenAddr {
		t.Fatalf("ListenAddr = %q, want %q", cfg.ListenAddr, defaultListenAddr)
	}
	if cfg.DataDir != defaultDataDir {
		t.Fatalf("DataDir = %q, want %q", cfg.DataDir, defaultDataDir)
	}
}

func TestLoadFromRequiresDatabaseURL(t *testing.T) {
	_, err := LoadFrom(func(string) (string, bool) { return "", false })
	if err == nil {
		t.Fatal("LoadFrom() error = nil, want required database URL error")
	}
}

func TestLoadFromRejectsRelativeDataDir(t *testing.T) {
	env := map[string]string{
		"UA_DATABASE_URL": "postgres://ua:secret@db/ua",
		"UA_DATA_DIR":     "relative/data",
	}
	_, err := LoadFrom(func(key string) (string, bool) {
		value, ok := env[key]
		return value, ok
	})
	if err == nil {
		t.Fatal("LoadFrom() error = nil, want relative data directory error")
	}
}

func TestEnsureDataDirectories(t *testing.T) {
	dir := t.TempDir()
	if err := EnsureDataDirectories(dir); err != nil {
		t.Fatalf("EnsureDataDirectories() error = %v", err)
	}
	for _, name := range []string{"artifacts", "rules", "tmp"} {
		if got := filepath.Join(dir, name); got == "" {
			t.Fatalf("empty path for %s", name)
		}
	}
}
