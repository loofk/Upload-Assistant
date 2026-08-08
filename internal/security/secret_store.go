package security

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type SecretStore struct {
	pool    *pgxpool.Pool
	keyring *Keyring
}

func NewSecretStore(pool *pgxpool.Pool, keyring *Keyring) *SecretStore {
	return &SecretStore{pool: pool, keyring: keyring}
}

func (s *SecretStore) Put(ctx context.Context, purpose string, plaintext []byte, createdBy string) (string, error) {
	if purpose == "" || len(plaintext) == 0 {
		return "", errors.New("secret purpose and plaintext are required")
	}
	encrypted, err := s.keyring.Encrypt(purpose, plaintext)
	if err != nil {
		return "", err
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
		return "", fmt.Errorf("store encrypted secret: %w", err)
	}
	return id, nil
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
