package security

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	ErrUnauthorized = errors.New("authentication failed")
	ErrForbidden    = errors.New("permission denied")
	ErrBootstrap    = errors.New("administrator bootstrap is unavailable")
)

type Principal struct {
	UserID      string
	Username    string
	Role        string
	TokenID     string
	TokenScopes []string
}

func (p Principal) HasScope(scope string) bool {
	if !roleAllows(p.Role, scope) {
		return false
	}
	for _, tokenScope := range p.TokenScopes {
		if tokenScope == "*" || tokenScope == scope {
			return true
		}
	}
	return false
}

type AuthStore struct {
	pool *pgxpool.Pool
}

type BootstrapResult struct {
	UserID   string   `json:"user_id"`
	Username string   `json:"username"`
	Role     string   `json:"role"`
	Token    string   `json:"token"`
	Scopes   []string `json:"scopes"`
}

func NewAuthStore(pool *pgxpool.Pool) *AuthStore { return &AuthStore{pool: pool} }

func (s *AuthStore) BootstrapAdmin(ctx context.Context, username, password string) (BootstrapResult, error) {
	username = strings.TrimSpace(username)
	if len(username) < 3 || len(username) > 64 {
		return BootstrapResult{}, errors.New("username must contain 3 to 64 characters")
	}
	passwordHash, err := HashPassword(password)
	if err != nil {
		return BootstrapResult{}, err
	}
	token, tokenPrefix, tokenHash, err := generateToken()
	if err != nil {
		return BootstrapResult{}, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return BootstrapResult{}, fmt.Errorf("begin admin bootstrap: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, "LOCK TABLE users IN EXCLUSIVE MODE"); err != nil {
		return BootstrapResult{}, fmt.Errorf("lock users for bootstrap: %w", err)
	}
	var count int
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM users").Scan(&count); err != nil {
		return BootstrapResult{}, fmt.Errorf("count users: %w", err)
	}
	if count != 0 {
		return BootstrapResult{}, ErrBootstrap
	}
	var userID string
	if err := tx.QueryRow(ctx, `
		INSERT INTO users(username, password_hash, role)
		VALUES ($1, $2, 'admin') RETURNING id::text`, username, passwordHash).Scan(&userID); err != nil {
		return BootstrapResult{}, fmt.Errorf("insert bootstrap administrator: %w", err)
	}
	scopes := []string{"*"}
	if _, err := tx.Exec(ctx, `
		INSERT INTO api_tokens(user_id, name, token_prefix, token_hash, scopes)
		VALUES ($1, 'bootstrap', $2, $3, $4)`, userID, tokenPrefix, tokenHash, scopes); err != nil {
		return BootstrapResult{}, fmt.Errorf("insert bootstrap API token: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ('system', 'bootstrap', 'user.bootstrap_admin', 'user', $1, $2)`,
		userID, []byte(`{"role":"admin"}`)); err != nil {
		return BootstrapResult{}, fmt.Errorf("audit administrator bootstrap: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return BootstrapResult{}, fmt.Errorf("commit administrator bootstrap: %w", err)
	}
	return BootstrapResult{UserID: userID, Username: username, Role: "admin", Token: token, Scopes: scopes}, nil
}

func (s *AuthStore) AuthenticateToken(ctx context.Context, token string) (Principal, error) {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, "ua_") || len(token) < 32 {
		return Principal{}, ErrUnauthorized
	}
	hash := sha256.Sum256([]byte(token))
	var principal Principal
	err := s.pool.QueryRow(ctx, `
		SELECT u.id::text, u.username, u.role, t.id::text, t.scopes
		FROM api_tokens t
		JOIN users u ON u.id = t.user_id
		WHERE t.token_hash = $1 AND t.revoked_at IS NULL
		  AND (t.expires_at IS NULL OR t.expires_at > now())
		  AND u.disabled_at IS NULL`, hash[:]).Scan(
		&principal.UserID, &principal.Username, &principal.Role, &principal.TokenID, &principal.TokenScopes,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return Principal{}, ErrUnauthorized
	}
	if err != nil {
		return Principal{}, fmt.Errorf("authenticate API token: %w", err)
	}
	_, _ = s.pool.Exec(ctx, "UPDATE api_tokens SET last_used_at = $2 WHERE id = $1", principal.TokenID, time.Now().UTC())
	return principal, nil
}

func generateToken() (token, prefix string, hash []byte, err error) {
	random := make([]byte, 32)
	if _, err := rand.Read(random); err != nil {
		return "", "", nil, fmt.Errorf("generate API token: %w", err)
	}
	token = "ua_" + base64.RawURLEncoding.EncodeToString(random)
	prefix = token[:11]
	sum := sha256.Sum256([]byte(token))
	return token, prefix, sum[:], nil
}

func roleAllows(role, scope string) bool {
	switch role {
	case "admin":
		return true
	case "operator":
		return scope == "jobs:read" || scope == "jobs:write" || scope == "upload:confirm" || scope == "config:read" || scope == "downloader:manage" || scope == "audit:read"
	case "auditor":
		return scope == "jobs:read" || scope == "config:read" || scope == "audit:read"
	default:
		return false
	}
}
