package rulecollector

import (
	"errors"
	"time"
)

var (
	ErrNotFound   = errors.New("rule collection resource not found")
	ErrInvalid    = errors.New("rule collection input is invalid")
	ErrConflict   = errors.New("rule collection state conflicts with the request")
	ErrCredential = errors.New("rule collection cookie is unavailable")
)

const (
	MaxSources             = 20
	MaxRawResponseBytes    = 4 << 20
	MaxNormalizedTextBytes = 1 << 20
	MaxAggregateTextBytes  = 8 << 20
	SourceAuthNone         = "none"
	SourceAuthSiteCookie   = "site_cookie"
)

type SourceInput struct {
	ID       string `json:"id"`
	URL      string `json:"url"`
	Scope    string `json:"scope"`
	AuthMode string `json:"auth_mode"`
}

type SourceSetInput struct {
	Sources              []SourceInput `json:"sources"`
	ScopeConfirmed       bool          `json:"scope_confirmed"`
	CookieHostsConfirmed bool          `json:"cookie_hosts_confirmed"`
}

type SourceSet struct {
	SiteCode             string        `json:"site_code"`
	Sources              []SourceInput `json:"sources"`
	Fingerprint          string        `json:"fingerprint"`
	ScopeConfirmed       bool          `json:"scope_confirmed"`
	CookieHostsConfirmed bool          `json:"cookie_hosts_confirmed"`
	CookieConfigured     bool          `json:"cookie_configured"`
	CookieRequired       bool          `json:"cookie_required"`
	UpdatedAt            time.Time     `json:"updated_at"`
}

type CreateRunInput struct {
	SourceSetFingerprint string `json:"source_set_fingerprint"`
	ProviderID           string `json:"provider_id"`
	Confirm              bool   `json:"confirm"`
	IdempotencyKey       string `json:"-"`
	TraceID              string `json:"-"`
}

type CollectionDocument struct {
	ID          string     `json:"id"`
	SourceID    string     `json:"source_id"`
	URL         string     `json:"url"`
	Scope       string     `json:"scope"`
	AuthMode    string     `json:"auth_mode"`
	Status      string     `json:"status"`
	HTTPStatus  int        `json:"http_status,omitempty"`
	ContentType string     `json:"content_type,omitempty"`
	SizeBytes   int64      `json:"size_bytes,omitempty"`
	TextSHA256  string     `json:"text_sha256,omitempty"`
	ErrorCode   string     `json:"error_code,omitempty"`
	ErrorDetail string     `json:"error_detail,omitempty"`
	CapturedAt  *time.Time `json:"captured_at,omitempty"`
}

type CollectionRun struct {
	ID                   string               `json:"id"`
	SiteCode             string               `json:"site_code"`
	SourceSetFingerprint string               `json:"source_set_fingerprint"`
	ProviderID           string               `json:"provider_id"`
	ProviderConfigSHA256 string               `json:"provider_config_sha256,omitempty"`
	Status               string               `json:"status"`
	NotBefore            time.Time            `json:"not_before"`
	RuleRevisionID       string               `json:"rule_revision_id,omitempty"`
	ErrorCode            string               `json:"error_code,omitempty"`
	ErrorDetail          string               `json:"error_detail,omitempty"`
	Documents            []CollectionDocument `json:"documents"`
	CreatedAt            time.Time            `json:"created_at"`
	StartedAt            *time.Time           `json:"started_at,omitempty"`
	CompletedAt          *time.Time           `json:"completed_at,omitempty"`
	UpdatedAt            time.Time            `json:"updated_at"`
}

func (run CollectionRun) Terminal() bool { return run.Status == "ready" || run.Status == "failed" }
