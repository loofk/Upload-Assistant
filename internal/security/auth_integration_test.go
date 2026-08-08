package security

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/database"
)

func TestAuthBootstrapTokenAndEncryptedSecret(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatalf("database.Open() error = %v", err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatalf("database.Migrate() error = %v", err)
	}
	auth := NewAuthStore(pool)
	result, err := auth.BootstrapAdmin(ctx, "integration-admin", "correct horse battery staple")
	if err != nil {
		t.Fatalf("BootstrapAdmin() error = %v", err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM users WHERE id = $1", result.UserID) })
	if result.Token == "" || result.Role != "admin" {
		t.Fatalf("bootstrap result token/role = %t/%s", result.Token != "", result.Role)
	}
	principal, err := auth.AuthenticateToken(ctx, result.Token)
	if err != nil {
		t.Fatalf("AuthenticateToken() error = %v", err)
	}
	if principal.UserID != result.UserID || !principal.HasScope("jobs:write") || !principal.HasScope("downloader:destructive") {
		t.Fatalf("unexpected principal: %#v", principal)
	}
	if _, err := auth.AuthenticateToken(ctx, "ua_invalid-token-value-that-is-long-enough"); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("AuthenticateToken() invalid error = %v", err)
	}
	if _, err := auth.BootstrapAdmin(ctx, "second-admin", "correct horse battery staple"); !errors.Is(err, ErrBootstrap) {
		t.Fatalf("second BootstrapAdmin() error = %v, want ErrBootstrap", err)
	}

	masterKey := make([]byte, 32)
	if _, err := rand.Read(masterKey); err != nil {
		t.Fatalf("generate test master key: %v", err)
	}
	keyring, err := ParseKeyring(bytes.NewBufferString("1:" + base64.RawStdEncoding.EncodeToString(masterKey)))
	if err != nil {
		t.Fatalf("ParseKeyring() error = %v", err)
	}
	secrets := NewSecretStore(pool, keyring)
	secretID, err := secrets.Put(ctx, "TRACKERS.U2.cookie", []byte("uid=secret"), result.UserID)
	if err != nil {
		t.Fatalf("SecretStore.Put() error = %v", err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM secrets WHERE id = $1", secretID) })
	plaintext, err := secrets.Get(ctx, secretID, "TRACKERS.U2.cookie")
	if err != nil || string(plaintext) != "uid=secret" {
		t.Fatalf("SecretStore.Get() plaintext/error = %q/%v", plaintext, err)
	}
	if _, err := secrets.Get(ctx, secretID, "TRACKERS.CHD.cookie"); err == nil {
		t.Fatal("SecretStore.Get() accepted wrong purpose")
	}
}
