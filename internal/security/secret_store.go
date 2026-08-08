package security

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type SecretStore struct {
	pool    *pgxpool.Pool
	keyring *Keyring
}

type StoredSecret struct {
	ID                  string
	CiphertextSHA256    string
	CiphertextSizeBytes int64
}

func NewSecretStore(pool *pgxpool.Pool, keyring *Keyring) *SecretStore {
	return &SecretStore{pool: pool, keyring: keyring}
}

func (s *SecretStore) Put(ctx context.Context, purpose string, plaintext []byte, createdBy string) (string, error) {
	stored, err := s.PutDetailed(ctx, purpose, plaintext, createdBy)
	return stored.ID, err
}

func (s *SecretStore) PutDetailed(ctx context.Context, purpose string, plaintext []byte, createdBy string) (StoredSecret, error) {
	if purpose == "" || len(plaintext) == 0 {
		return StoredSecret{}, errors.New("secret purpose and plaintext are required")
	}
	encrypted, err := s.keyring.Encrypt(purpose, plaintext)
	if err != nil {
		return StoredSecret{}, err
	}
	var creator any
	if parsed, err := uuid.Parse(createdBy); err == nil {
		creator = parsed
	}
	var id string
	err = s.pool.QueryRow(ctx, `
		INSERT INTO secrets(purpose, ciphertext, nonce, key_version, created_by)
		VALUES ($1, $2, $3, $4, $5) RETURNING id::text`,
		purpose, encrypted.Ciphertext, encrypted.Nonce, encrypted.KeyVersion, creator,
	).Scan(&id)
	if err != nil {
		return StoredSecret{}, fmt.Errorf("store encrypted secret: %w", err)
	}
	digest := sha256.New()
	var version [8]byte
	binary.BigEndian.PutUint64(version[:], uint64(encrypted.KeyVersion))
	_, _ = digest.Write(version[:])
	_, _ = digest.Write(encrypted.Nonce)
	_, _ = digest.Write(encrypted.Ciphertext)
	return StoredSecret{
		ID: id, CiphertextSHA256: hex.EncodeToString(digest.Sum(nil)),
		CiphertextSizeBytes: int64(len(encrypted.Nonce) + len(encrypted.Ciphertext)),
	}, nil
}

// Fingerprint returns a domain-separated, master-keyed HMAC. It is suitable
// for exact review gates over secret-bearing material because it does not
// expose a plain content hash that could be used as an offline guessing oracle.
func (s *SecretStore) Fingerprint(purpose string, plaintext []byte) (string, error) {
	if strings.TrimSpace(purpose) == "" || len(plaintext) == 0 {
		return "", errors.New("fingerprint purpose and plaintext are required")
	}
	key, ok := s.keyring.keys[s.keyring.active]
	if !ok || len(key) != 32 {
		return "", errors.New("active master key is unavailable")
	}
	digest := hmac.New(sha256.New, key)
	_, _ = digest.Write([]byte("upload-assistant:v2:fingerprint:" + purpose + "\x00"))
	_, _ = digest.Write(plaintext)
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func (s *SecretStore) Get(ctx context.Context, id, expectedPurpose string) ([]byte, error) {
	var purpose string
	var encrypted EncryptedValue
	err := s.pool.QueryRow(ctx, `
		SELECT purpose, ciphertext, nonce, key_version FROM secrets WHERE id = $1`, id).Scan(
		&purpose, &encrypted.Ciphertext, &encrypted.Nonce, &encrypted.KeyVersion,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, errors.New("secret not found")
	}
	if err != nil {
		return nil, fmt.Errorf("load encrypted secret: %w", err)
	}
	if purpose != expectedPurpose {
		return nil, errors.New("secret purpose mismatch")
	}
	return s.keyring.Decrypt(purpose, encrypted)
}

// Delete removes one encrypted value only when its purpose exactly matches.
// Callers use this for compensating failed configuration writes and retention
// cleanup; plaintext is never loaded as part of deletion.
func (s *SecretStore) Delete(ctx context.Context, id, expectedPurpose string) error {
	command, err := s.pool.Exec(ctx, "DELETE FROM secrets WHERE id = $1 AND purpose = $2", id, expectedPurpose)
	if err != nil {
		return fmt.Errorf("delete encrypted secret: %w", err)
	}
	if command.RowsAffected() == 0 {
		return errors.New("secret not found or purpose mismatch")
	}
	return nil
}
