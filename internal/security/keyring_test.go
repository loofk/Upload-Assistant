package security

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func TestKeyringEncryptDecryptAndRotation(t *testing.T) {
	first := make([]byte, 32)
	second := make([]byte, 32)
	if _, err := rand.Read(first); err != nil {
		t.Fatal(err)
	}
	if _, err := rand.Read(second); err != nil {
		t.Fatal(err)
	}
	content := "1:" + base64.RawStdEncoding.EncodeToString(first) + "\n2:" + base64.RawStdEncoding.EncodeToString(second) + "\n"
	keyring, err := ParseKeyring(bytes.NewBufferString(content))
	if err != nil {
		t.Fatalf("ParseKeyring() error = %v", err)
	}
	if keyring.ActiveVersion() != 2 {
		t.Fatalf("ActiveVersion() = %d, want 2", keyring.ActiveVersion())
	}
	encrypted, err := keyring.Encrypt("TRACKERS.U2.cookie", []byte("secret-cookie"))
	if err != nil {
		t.Fatalf("Encrypt() error = %v", err)
	}
	plaintext, err := keyring.Decrypt("TRACKERS.U2.cookie", encrypted)
	if err != nil || string(plaintext) != "secret-cookie" {
		t.Fatalf("Decrypt() plaintext/error = %q/%v", plaintext, err)
	}
	if _, err := keyring.Decrypt("TRACKERS.CHD.cookie", encrypted); err == nil {
		t.Fatal("Decrypt() accepted different purpose")
	}
}

func TestLoadOrCreateKeyringPersistsSecureFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "keys", "master-keys")
	first, created, err := LoadOrCreateKeyring(path)
	if err != nil || !created || first.ActiveVersion() != 1 {
		t.Fatalf("first LoadOrCreateKeyring() created/version/error = %t/%d/%v", created, first.ActiveVersion(), err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("master key mode = %o", info.Mode().Perm())
	}
	second, created, err := LoadOrCreateKeyring(path)
	if err != nil || created || second.ActiveVersion() != first.ActiveVersion() {
		t.Fatalf("second LoadOrCreateKeyring() created/version/error = %t/%d/%v", created, second.ActiveVersion(), err)
	}
}

func TestLoadKeyringRejectsSymlink(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "target")
	if err := os.WriteFile(target, []byte("1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(directory, "master-keys")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadKeyring(link); err == nil {
		t.Fatal("LoadKeyring() accepted a symlink")
	}
}
