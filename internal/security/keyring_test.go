package security

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
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
