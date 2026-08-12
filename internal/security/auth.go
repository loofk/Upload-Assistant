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

type IssuedToken struct {
	UserID   string   `json:"user_id"`
	Username string   `json:"username"`
	Name     string   `json:"name"`
	Token    string   `json:"token"`
	Scopes   []string `json:"scopes"`
}

type TokenRecord struct {
	ID         string     `json:"id"`
	Prefix     string     `json:"prefix"`
	Name       string     `json:"name"`
	Scopes     []string   `json:"scopes"`
	CreatedAt  time.Time  `json:"created_at"`
	ExpiresAt  *time.Time `json:"expires_at,omitempty"`
	LastUsedAt *time.Time `json:"last_used_at,omitempty"`
	RevokedAt  *time.Time `json:"revoked_at,omitempty"`
}

type CreateTokenInput struct {
	Name          string
	Scopes        []string
	ExpiresInDays int
}

type CreatedToken struct {
	TokenRecord
	Token string `json:"token"`
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

// IssueAdminToken is a local recovery path for an operator who still controls
// the service host but no longer has the one-time bootstrap token. The token is
// generated by the service, shown once by the CLI, and the action is audited.
func (s *AuthStore) IssueAdminToken(ctx context.Context, username, name string) (IssuedToken, error) {
	username = strings.TrimSpace(username)
	name = strings.TrimSpace(name)
	if len(username) < 3 || len(username) > 64 {
		return IssuedToken{}, errors.New("username must contain 3 to 64 characters")
	}
	if len(name) < 1 || len(name) > 64 {
		return IssuedToken{}, errors.New("token name must contain 1 to 64 characters")
	}

	token, tokenPrefix, tokenHash, err := generateToken()
	if err != nil {
		return IssuedToken{}, err
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return IssuedToken{}, fmt.Errorf("begin API token issue: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var userID, role string
	if err := tx.QueryRow(ctx, `
		SELECT id::text, role
		FROM users
		WHERE username = $1 AND disabled_at IS NULL
		FOR UPDATE`, username).Scan(&userID, &role); errors.Is(err, pgx.ErrNoRows) {
		return IssuedToken{}, ErrUnauthorized
	} else if err != nil {
		return IssuedToken{}, fmt.Errorf("load administrator for API token issue: %w", err)
	}
	if role != "admin" {
		return IssuedToken{}, ErrForbidden
	}

	scopes := []string{"*"}
	if _, err := tx.Exec(ctx, `
		INSERT INTO api_tokens(user_id, name, token_prefix, token_hash, scopes)
		VALUES ($1, $2, $3, $4, $5)`, userID, name, tokenPrefix, tokenHash, scopes); err != nil {
		return IssuedToken{}, fmt.Errorf("insert recovery API token: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ('system', 'local-admin', 'user.api_token_issued', 'user', $1,
		        jsonb_build_object('token_name', $2::text, 'scopes', $3::text[]))`, userID, name, scopes); err != nil {
		return IssuedToken{}, fmt.Errorf("audit recovery API token issue: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return IssuedToken{}, fmt.Errorf("commit API token issue: %w", err)
	}
	return IssuedToken{UserID: userID, Username: username, Name: name, Token: token, Scopes: scopes}, nil
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

func (s *AuthStore) ListTokens(ctx context.Context, principal Principal) ([]TokenRecord, error) {
	rows, err := s.pool.Query(ctx, `SELECT id::text,token_prefix,name,scopes,created_at,expires_at,last_used_at,revoked_at
		FROM api_tokens WHERE user_id=$1 ORDER BY created_at DESC`, principal.UserID)
	if err != nil {
		return nil, fmt.Errorf("list API tokens: %w", err)
	}
	defer rows.Close()
	result := []TokenRecord{}
	for rows.Next() {
		var item TokenRecord
		if err := rows.Scan(&item.ID, &item.Prefix, &item.Name, &item.Scopes, &item.CreatedAt, &item.ExpiresAt, &item.LastUsedAt, &item.RevokedAt); err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

func (s *AuthStore) CreateToken(ctx context.Context, principal Principal, input CreateTokenInput, traceID string) (CreatedToken, error) {
	input.Name = strings.TrimSpace(input.Name)
	if input.Name == "" || len(input.Name) > 64 || len(input.Scopes) == 0 {
		return CreatedToken{}, errors.New("token name and scopes are required")
	}
	if input.ExpiresInDays == 0 {
		input.ExpiresInDays = 30
	}
	if input.ExpiresInDays < 1 || input.ExpiresInDays > 365 {
		return CreatedToken{}, errors.New("token expiry must be between 1 and 365 days")
	}
	seen := map[string]bool{}
	for _, scope := range input.Scopes {
		if scope == "" || !isKnownScope(scope) || seen[scope] || scope == "*" && !containsScope(principal.TokenScopes, "*") || scope != "*" && !principal.HasScope(scope) {
			return CreatedToken{}, ErrForbidden
		}
		seen[scope] = true
	}
	token, prefix, hash, err := generateToken()
	if err != nil {
		return CreatedToken{}, err
	}
	expires := time.Now().UTC().Add(time.Duration(input.ExpiresInDays) * 24 * time.Hour)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return CreatedToken{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var record TokenRecord
	err = tx.QueryRow(ctx, `INSERT INTO api_tokens(user_id,name,token_prefix,token_hash,scopes,expires_at)
		VALUES($1,$2,$3,$4,$5,$6) RETURNING id::text,token_prefix,name,scopes,created_at,expires_at,last_used_at,revoked_at`, principal.UserID, input.Name, prefix, hash, input.Scopes, expires).Scan(&record.ID, &record.Prefix, &record.Name, &record.Scopes, &record.CreatedAt, &record.ExpiresAt, &record.LastUsedAt, &record.RevokedAt)
	if err != nil {
		return CreatedToken{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
		VALUES('user',$1,'api_token.create','api_token',$2,NULLIF($3,'')::uuid,jsonb_build_object('name',$4::text,'scopes',$5::text[],'expires_at',$6::timestamptz))`, principal.UserID, record.ID, traceID, input.Name, input.Scopes, expires); err != nil {
		return CreatedToken{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return CreatedToken{}, err
	}
	return CreatedToken{TokenRecord: record, Token: token}, nil
}

func isKnownScope(scope string) bool {
	if scope == "*" {
		return true
	}
	switch scope {
	case "jobs:read", "jobs:write", "upload:confirm", "config:read", "config:manage",
		"downloader:manage", "downloader:destructive", "audit:read", "logs:read", "logs:export",
		"diagnostics:read", "diagnostics:run", "operations:read", "operations:manage", "llm:manage",
		"backups:read", "backups:manage", "tokens:manage":
		return true
	default:
		return false
	}
}

func (s *AuthStore) RevokeToken(ctx context.Context, principal Principal, tokenID, traceID string) (TokenRecord, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return TokenRecord{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var record TokenRecord
	err = tx.QueryRow(ctx, `UPDATE api_tokens SET revoked_at=COALESCE(revoked_at,now()) WHERE id=$1 AND user_id=$2 RETURNING id::text,token_prefix,name,scopes,created_at,expires_at,last_used_at,revoked_at`, tokenID, principal.UserID).Scan(&record.ID, &record.Prefix, &record.Name, &record.Scopes, &record.CreatedAt, &record.ExpiresAt, &record.LastUsedAt, &record.RevokedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return TokenRecord{}, ErrUnauthorized
	}
	if err != nil {
		return TokenRecord{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload) VALUES('user',$1,'api_token.revoke','api_token',$2,NULLIF($3,'')::uuid,jsonb_build_object('prefix',$4::text))`, principal.UserID, record.ID, traceID, record.Prefix); err != nil {
		return TokenRecord{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return TokenRecord{}, err
	}
	return record, nil
}

func containsScope(scopes []string, expected string) bool {
	for _, scope := range scopes {
		if scope == expected {
			return true
		}
	}
	return false
}

func roleAllows(role, scope string) bool {
	switch role {
	case "admin":
		return true
	case "operator":
		return scope == "jobs:read" || scope == "jobs:write" || scope == "upload:confirm" || scope == "config:read" || scope == "downloader:manage" || scope == "audit:read" || scope == "logs:read" || scope == "logs:export" || scope == "diagnostics:read" || scope == "diagnostics:run" || scope == "operations:read" || scope == "backups:read"
	case "auditor":
		return scope == "jobs:read" || scope == "config:read" || scope == "audit:read" || scope == "logs:read" || scope == "diagnostics:read" || scope == "operations:read" || scope == "backups:read"
	default:
		return false
	}
}
