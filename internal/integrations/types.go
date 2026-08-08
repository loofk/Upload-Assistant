package integrations

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strings"
	"time"
)

var (
	ErrNotFound   = errors.New("integration resource not found")
	ErrValidation = errors.New("integration configuration is invalid")
)

var safeNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

type EndpointConfig struct {
	Endpoint       string         `json:"endpoint"`
	TimeoutSeconds int            `json:"timeout_seconds,omitempty"`
	Options        map[string]any `json:"options,omitempty"`
}

type PathMapping struct {
	RemotePath string `json:"remote_path"`
	LocalPath  string `json:"local_path"`
	Priority   int    `json:"priority,omitempty"`
}

type DownloaderInput struct {
	Adapter      string            `json:"adapter"`
	Enabled      *bool             `json:"enabled,omitempty"`
	Config       EndpointConfig    `json:"config"`
	Credentials  map[string]string `json:"credentials,omitempty"`
	PathMappings []PathMapping     `json:"path_mappings,omitempty"`
}

type Downloader struct {
	ID               string          `json:"id"`
	Name             string          `json:"name"`
	Adapter          string          `json:"adapter"`
	Enabled          bool            `json:"enabled"`
	Config           json.RawMessage `json:"config"`
	CredentialFields []string        `json:"credential_fields"`
	PathMappings     []PathMapping   `json:"path_mappings"`
	HealthStatus     string          `json:"health_status"`
	LastHealthCheck  *time.Time      `json:"last_health_check_at,omitempty"`
	CreatedAt        time.Time       `json:"created_at"`
	UpdatedAt        time.Time       `json:"updated_at"`
}

type RuntimeDownloader struct {
	Downloader
	EndpointConfig EndpointConfig    `json:"-"`
	Credentials    map[string]string `json:"-"`
}

type ImageHostInput struct {
	Adapter     string            `json:"adapter"`
	Enabled     *bool             `json:"enabled,omitempty"`
	Priority    int               `json:"priority,omitempty"`
	Config      EndpointConfig    `json:"config"`
	Credentials map[string]string `json:"credentials,omitempty"`
}

type ImageHost struct {
	ID               string          `json:"id"`
	Name             string          `json:"name"`
	Adapter          string          `json:"adapter"`
	Enabled          bool            `json:"enabled"`
	Priority         int             `json:"priority"`
	Config           json.RawMessage `json:"config"`
	CredentialFields []string        `json:"credential_fields"`
	HealthStatus     string          `json:"health_status"`
	LastHealthCheck  *time.Time      `json:"last_health_check_at,omitempty"`
	CreatedAt        time.Time       `json:"created_at"`
	UpdatedAt        time.Time       `json:"updated_at"`
}

type ScreenshotProfileInput struct {
	Name    string         `json:"name"`
	Enabled *bool          `json:"enabled,omitempty"`
	Config  map[string]any `json:"config"`
}

type ScreenshotProfile struct {
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Revision  int             `json:"revision"`
	Enabled   bool            `json:"enabled"`
	Config    json.RawMessage `json:"config"`
	CreatedAt time.Time       `json:"created_at"`
}

type SiteCredential struct {
	ID        string    `json:"id"`
	SiteCode  string    `json:"site_code"`
	Name      string    `json:"name"`
	Enabled   bool      `json:"enabled"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// RuntimeSite is intentionally only exposed to in-process adapters. Credentials
// are decrypted at the last possible moment and must never be serialized into an
// API response, log record, workflow snapshot, or audit payload.
type RuntimeSite struct {
	Code        string            `json:"-"`
	Name        string            `json:"-"`
	Adapter     string            `json:"-"`
	Config      json.RawMessage   `json:"-"`
	Credentials map[string]string `json:"-"`
}

func validateResourceName(kind, name string) error {
	if !safeNamePattern.MatchString(name) {
		return fmt.Errorf("%w: %s name must match %s", ErrValidation, kind, safeNamePattern.String())
	}
	return nil
}

func validateEndpointConfig(config EndpointConfig) ([]byte, error) {
	parsed, err := url.Parse(strings.TrimSpace(config.Endpoint))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return nil, fmt.Errorf("%w: endpoint must be an absolute HTTP(S) URL", ErrValidation)
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("%w: endpoint must not contain credentials, query parameters, or a fragment", ErrValidation)
	}
	config.Endpoint = strings.TrimRight(parsed.String(), "/")
	if config.TimeoutSeconds == 0 {
		config.TimeoutSeconds = 30
	}
	if config.TimeoutSeconds < 1 || config.TimeoutSeconds > 300 {
		return nil, fmt.Errorf("%w: timeout_seconds must be between 1 and 300", ErrValidation)
	}
	if containsSecretLikeKey(config.Options) {
		return nil, fmt.Errorf("%w: secret-like options must be supplied through credentials", ErrValidation)
	}
	body, err := json.Marshal(config)
	if err != nil {
		return nil, fmt.Errorf("%w: serialize endpoint config: %v", ErrValidation, err)
	}
	return body, nil
}

func validateCredentials(credentials map[string]string) ([]string, []byte, error) {
	if len(credentials) == 0 {
		return []string{}, nil, nil
	}
	fields := make([]string, 0, len(credentials))
	for name, value := range credentials {
		if err := validateResourceName("credential field", name); err != nil {
			return nil, nil, err
		}
		if strings.TrimSpace(value) == "" {
			return nil, nil, fmt.Errorf("%w: credential %s must not be empty", ErrValidation, name)
		}
		fields = append(fields, name)
	}
	slices.Sort(fields)
	body, err := json.Marshal(credentials)
	if err != nil {
		return nil, nil, fmt.Errorf("%w: serialize credentials: %v", ErrValidation, err)
	}
	return fields, body, nil
}

func validateMappings(mappings []PathMapping) error {
	seen := make(map[string]struct{}, len(mappings))
	for _, mapping := range mappings {
		remote := strings.TrimSpace(mapping.RemotePath)
		local := strings.TrimSpace(mapping.LocalPath)
		if !strings.HasPrefix(remote, "/") || !strings.HasPrefix(local, "/") {
			return fmt.Errorf("%w: path mappings must use absolute Linux paths", ErrValidation)
		}
		if path.Clean(remote) != remote || path.Clean(local) != local {
			return fmt.Errorf("%w: path mappings must be normalized", ErrValidation)
		}
		if _, exists := seen[remote]; exists {
			return fmt.Errorf("%w: duplicate remote path %s", ErrValidation, remote)
		}
		seen[remote] = struct{}{}
	}
	return nil
}

func containsSecretLikeKey(value any) bool {
	switch typed := value.(type) {
	case map[string]any:
		for key, nested := range typed {
			lower := strings.ToLower(key)
			for _, forbidden := range []string{"password", "passwd", "secret", "token", "cookie", "api_key", "apikey", "passkey"} {
				if strings.Contains(lower, forbidden) {
					return true
				}
			}
			if containsSecretLikeKey(nested) {
				return true
			}
		}
	case []any:
		for _, nested := range typed {
			if containsSecretLikeKey(nested) {
				return true
			}
		}
	}
	return false
}
