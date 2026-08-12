package metadataproviders

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const (
	maxResponseBytes    = 4 << 20
	maxDescriptionBytes = 1 << 20
)

var (
	ErrValidation       = errors.New("metadata provider request is invalid")
	errRedirectDisabled = errors.New("metadata provider redirects are disabled")
	imdbPattern         = regexp.MustCompile(`^tt[0-9]{5,12}$`)
	numericPattern      = regexp.MustCompile(`^[0-9]{1,16}$`)
	doubanURL           = regexp.MustCompile(`https?://movie\.douban\.com/subject/([0-9]{1,16})/?`)
)

// ProviderError carries only a stable classification and a bounded message
// that is safe to return to operators. It deliberately never retains the
// request URL because legacy metadata APIs put credentials in query strings.
type ProviderError struct {
	Code    string
	Message string
	cause   error
}

func (err *ProviderError) Error() string { return err.Message }

func (err *ProviderError) Unwrap() error { return err.cause }

func providerError(code, message string, cause error) error {
	return &ProviderError{Code: code, Message: message, cause: cause}
}

func ErrorCode(err error) string {
	var providerErr *ProviderError
	if errors.As(err, &providerErr) {
		return providerErr.Code
	}
	return ""
}

func SafeErrorDetail(err error) string {
	if err == nil {
		return ""
	}
	detail := strings.TrimSpace(err.Error())
	if len(detail) > 512 {
		detail = detail[:512]
	}
	return detail
}

type RuntimeStore interface {
	GetRuntimeMetadataProvider(context.Context, string) (integrations.RuntimeMetadataProvider, error)
	RecordMetadataProviderHealth(context.Context, string, string, map[string]any, workflow.Actor) error
	AuditMetadataProviderAction(context.Context, string, string, map[string]any, workflow.Actor) error
}

type Manager struct {
	store  RuntimeStore
	client *http.Client
}

type Identity struct {
	IMDbID   string `json:"imdb_id,omitempty"`
	TMDbID   string `json:"tmdb_id,omitempty"`
	TMDbType string `json:"tmdb_type,omitempty"`
	DoubanID string `json:"douban_id,omitempty"`
}

type ResolveRequest struct {
	IMDbID   string `json:"imdb_id,omitempty"`
	TMDbID   string `json:"tmdb_id,omitempty"`
	TMDbType string `json:"tmdb_type,omitempty"`
	DoubanID string `json:"douban_id,omitempty"`
}

type CallEvidence struct {
	Sequence       int    `json:"sequence"`
	Purpose        string `json:"purpose"`
	QuerySHA256    string `json:"query_sha256"`
	ResponseSHA256 string `json:"response_sha256"`
	StatusCode     int    `json:"status_code"`
}

type ResolveResult struct {
	Name                string         `json:"name"`
	Adapter             string         `json:"adapter"`
	Matched             bool           `json:"matched"`
	Identity            Identity       `json:"identity"`
	Description         string         `json:"description,omitempty"`
	DescriptionSHA256   string         `json:"description_sha256,omitempty"`
	ConfigurationSHA256 string         `json:"configuration_sha256"`
	QuerySHA256         string         `json:"query_sha256"`
	Calls               []CallEvidence `json:"calls"`
}

// ProbeResult deliberately excludes titles, descriptions and other remote
// content. A probe verifies the configured production contract with a stable,
// public reference and persists the same bounded evidence as a normal lookup.
type ProbeResult struct {
	Name                string         `json:"name"`
	Adapter             string         `json:"adapter"`
	Status              string         `json:"status"`
	Matched             bool           `json:"matched"`
	ConfigurationSHA256 string         `json:"configuration_sha256"`
	QuerySHA256         string         `json:"query_sha256"`
	Calls               []CallEvidence `json:"calls"`
}

func NewManager(store RuntimeStore, client *http.Client) *Manager {
	if client == nil {
		client = &http.Client{}
	}
	clone := *client
	clone.CheckRedirect = func(*http.Request, []*http.Request) error { return errRedirectDisabled }
	return &Manager{store: store, client: &clone}
}

func (m *Manager) Probe(ctx context.Context, name string, actor workflow.Actor) (ProbeResult, error) {
	runtime, err := m.store.GetRuntimeMetadataProvider(ctx, name)
	if err != nil {
		return ProbeResult{}, err
	}
	var request ResolveRequest
	switch runtime.Adapter {
	case "tmdb":
		request = ResolveRequest{TMDbID: "550", TMDbType: "movie"}
	case "ptgen":
		request = ResolveRequest{IMDbID: "tt0111161"}
	default:
		return ProbeResult{}, fmt.Errorf("%w: unsupported adapter", ErrValidation)
	}
	result, err := m.Resolve(ctx, name, request, actor)
	if err != nil {
		return ProbeResult{}, err
	}
	if !result.Matched {
		_ = m.store.RecordMetadataProviderHealth(ctx, runtime.Name, "failed", map[string]any{
			"operation": "probe", "adapter": runtime.Adapter, "error_code": "probe_reference_not_found",
			"configuration_sha256": runtime.ConfigurationSHA256, "query_sha256": result.QuerySHA256,
		}, actor)
		return ProbeResult{}, fmt.Errorf("metadata provider did not resolve the stable probe reference")
	}
	return ProbeResult{
		Name: result.Name, Adapter: result.Adapter, Status: "ready", Matched: result.Matched,
		ConfigurationSHA256: result.ConfigurationSHA256, QuerySHA256: result.QuerySHA256, Calls: result.Calls,
	}, nil
}

func (m *Manager) Resolve(ctx context.Context, name string, input ResolveRequest, actor workflow.Actor) (ResolveResult, error) {
	input = normalizeRequest(input)
	if err := validateRequest(input); err != nil {
		return ResolveResult{}, err
	}
	runtime, err := m.store.GetRuntimeMetadataProvider(ctx, name)
	if err != nil {
		return ResolveResult{}, err
	}
	queryBody, _ := json.Marshal(input)
	result := ResolveResult{
		Name: runtime.Name, Adapter: runtime.Adapter, ConfigurationSHA256: runtime.ConfigurationSHA256,
		QuerySHA256: sha256Hex(queryBody), Calls: []CallEvidence{},
	}
	switch runtime.Adapter {
	case "tmdb":
		result.Identity, result.Calls, err = m.resolveTMDb(ctx, runtime, input)
	case "ptgen":
		result.Identity, result.Description, result.Calls, err = m.resolvePTGen(ctx, runtime, input)
	default:
		err = fmt.Errorf("%w: unsupported adapter", ErrValidation)
	}
	if err != nil {
		errorCode := ErrorCode(err)
		if errorCode == "" {
			errorCode = "request_failed"
		}
		_ = m.store.RecordMetadataProviderHealth(ctx, runtime.Name, "failed", map[string]any{
			"operation": "resolve", "adapter": runtime.Adapter, "error_code": errorCode,
			"error_detail":         SafeErrorDetail(err),
			"configuration_sha256": runtime.ConfigurationSHA256, "query_sha256": result.QuerySHA256,
		}, actor)
		return ResolveResult{}, err
	}
	for index := range result.Calls {
		result.Calls[index].Sequence = index + 1
	}
	result.Matched = result.Identity.IMDbID != "" || result.Identity.TMDbID != "" || result.Identity.DoubanID != "" || result.Description != ""
	if result.Description != "" {
		result.DescriptionSHA256 = sha256Hex([]byte(result.Description))
	}
	details := map[string]any{
		"adapter": runtime.Adapter, "matched": result.Matched, "query_sha256": result.QuerySHA256,
		"configuration_sha256": result.ConfigurationSHA256, "calls": result.Calls,
		"metadata_ids": result.Identity, "description_sha256": result.DescriptionSHA256,
		"description_size_bytes": len([]byte(result.Description)),
	}
	if err := m.store.AuditMetadataProviderAction(ctx, runtime.Name, "resolve", details, actor); err != nil {
		return ResolveResult{}, err
	}
	if err := m.store.RecordMetadataProviderHealth(ctx, runtime.Name, "ready", map[string]any{
		"operation": "resolve", "adapter": runtime.Adapter, "matched": result.Matched,
		"configuration_sha256": result.ConfigurationSHA256, "response_count": len(result.Calls),
	}, actor); err != nil {
		return ResolveResult{}, err
	}
	return result, nil
}

func (m *Manager) resolveTMDb(ctx context.Context, runtime integrations.RuntimeMetadataProvider, input ResolveRequest) (Identity, []CallEvidence, error) {
	if input.TMDbID != "" {
		var response struct {
			IMDbID string `json:"imdb_id"`
		}
		call, err := m.doJSON(ctx, runtime, http.MethodGet, "/3/"+input.TMDbType+"/"+input.TMDbID+"/external_ids", url.Values{}, &response, "external_ids")
		if err != nil {
			return Identity{}, nil, err
		}
		identity := Identity{TMDbID: input.TMDbID, TMDbType: input.TMDbType, IMDbID: normalizeIMDb(response.IMDbID)}
		if input.IMDbID != "" && identity.IMDbID != "" && input.IMDbID != identity.IMDbID {
			return Identity{}, nil, fmt.Errorf("%w: TMDb external IDs conflict with the supplied IMDb id", ErrValidation)
		}
		if identity.IMDbID == "" {
			identity.IMDbID = input.IMDbID
		}
		return identity, []CallEvidence{call}, nil
	}
	var response struct {
		MovieResults []struct {
			ID int64 `json:"id"`
		} `json:"movie_results"`
		TVResults []struct {
			ID int64 `json:"id"`
		} `json:"tv_results"`
	}
	query := url.Values{"external_source": []string{"imdb_id"}}
	if language := stringOption(runtime.EndpointConfig.Options, "language"); language != "" {
		query.Set("language", language)
	}
	call, err := m.doJSON(ctx, runtime, http.MethodGet, "/3/find/"+input.IMDbID, query, &response, "find_by_imdb")
	if err != nil {
		return Identity{}, nil, err
	}
	total := len(response.MovieResults) + len(response.TVResults)
	if total == 0 {
		return Identity{IMDbID: input.IMDbID}, []CallEvidence{call}, nil
	}
	if total != 1 {
		return Identity{}, nil, fmt.Errorf("%w: TMDb returned ambiguous movie/TV matches for the IMDb id", ErrValidation)
	}
	identity := Identity{IMDbID: input.IMDbID}
	if len(response.MovieResults) == 1 {
		identity.TMDbID, identity.TMDbType = strconv.FormatInt(response.MovieResults[0].ID, 10), "movie"
	} else {
		identity.TMDbID, identity.TMDbType = strconv.FormatInt(response.TVResults[0].ID, 10), "tv"
	}
	return identity, []CallEvidence{call}, nil
}

func (m *Manager) resolvePTGen(ctx context.Context, runtime integrations.RuntimeMetadataProvider, input ResolveRequest) (Identity, string, []CallEvidence, error) {
	identity := Identity{IMDbID: input.IMDbID, TMDbID: input.TMDbID, TMDbType: input.TMDbType, DoubanID: input.DoubanID}
	query := url.Values{}
	purpose := "douban_description"
	if input.DoubanID != "" {
		query.Set("url", "https://movie.douban.com/subject/"+input.DoubanID+"/")
	} else {
		purpose = "imdb_discovery"
		query.Set("source", "imdb")
		query.Set("sid", input.IMDbID)
	}
	first, call, raw, err := m.requestPTGen(ctx, runtime, query, purpose)
	if err != nil {
		return Identity{}, "", nil, err
	}
	calls := []CallEvidence{call}
	if input.DoubanID == "" {
		if match := doubanURL.FindSubmatch(raw); len(match) == 2 {
			identity.DoubanID = string(match[1])
			secondQuery := url.Values{"url": []string{"https://movie.douban.com/subject/" + identity.DoubanID + "/"}}
			second, secondCall, _, secondErr := m.requestPTGen(ctx, runtime, secondQuery, "douban_description")
			if secondErr != nil {
				return Identity{}, "", nil, secondErr
			}
			first = second
			calls = append(calls, secondCall)
		}
	}
	description := strings.TrimSpace(first.Format)
	if description == "" {
		return Identity{}, "", nil, providerError("provider_output_missing", "metadata provider returned no PTGen description", nil)
	}
	if len([]byte(description)) > maxDescriptionBytes || !utf8.ValidString(description) || strings.ContainsRune(description, '\x00') {
		return Identity{}, "", nil, providerError("provider_output_invalid", "metadata provider returned an invalid or oversized PTGen description", nil)
	}
	return identity, description, calls, nil
}

type ptgenResponse struct {
	Success bool            `json:"success"`
	Error   json.RawMessage `json:"error"`
	Format  string          `json:"format"`
}

func (m *Manager) requestPTGen(ctx context.Context, runtime integrations.RuntimeMetadataProvider, query url.Values, purpose string) (ptgenResponse, CallEvidence, []byte, error) {
	endpoint := runtime.EndpointConfig.Endpoint
	if !strings.HasSuffix(strings.TrimRight(endpoint, "/"), "/api") && !strings.Contains(endpoint, "/api/") {
		endpoint = strings.TrimRight(endpoint, "/") + "/api"
	}
	var response ptgenResponse
	call, raw, err := m.doJSONEndpoint(ctx, runtime, http.MethodPost, endpoint, query, &response, purpose)
	if err != nil {
		return ptgenResponse{}, CallEvidence{}, nil, err
	}
	if !response.Success || hasJSONError(response.Error) {
		return ptgenResponse{}, CallEvidence{}, nil, providerError("provider_rejected", "metadata provider returned an unsuccessful PTGen response", nil)
	}
	return response, call, raw, nil
}

func (m *Manager) doJSON(ctx context.Context, runtime integrations.RuntimeMetadataProvider, method, suffix string, query url.Values, target any, purpose string) (CallEvidence, error) {
	call, _, err := m.doJSONEndpoint(ctx, runtime, method, strings.TrimRight(runtime.EndpointConfig.Endpoint, "/")+suffix, query, target, purpose)
	return call, err
}

func (m *Manager) doJSONEndpoint(ctx context.Context, runtime integrations.RuntimeMetadataProvider, method, endpoint string, query url.Values, target any, purpose string) (CallEvidence, []byte, error) {
	publicQuery := cloneValues(query)
	requestQuery := cloneValues(query)
	if key := strings.TrimSpace(runtime.Credentials["api_key"]); key != "" {
		if runtime.Adapter == "ptgen" {
			requestQuery.Set("key", key)
		} else {
			requestQuery.Set("api_key", key)
		}
	}
	parsed, err := url.Parse(endpoint)
	if err != nil {
		return CallEvidence{}, nil, fmt.Errorf("build metadata provider request: %w", err)
	}
	parsed.RawQuery = requestQuery.Encode()
	timeout := time.Duration(runtime.EndpointConfig.TimeoutSeconds) * time.Second
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, method, parsed.String(), bytes.NewReader(nil))
	if err != nil {
		return CallEvidence{}, nil, fmt.Errorf("build metadata provider request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", "Upload-Assistant/v2")
	response, err := m.client.Do(request)
	if err != nil {
		// net/http errors can include the complete request URL. That URL contains
		// query credentials for these legacy APIs, so never propagate it.
		return CallEvidence{}, nil, classifyTransportError(err, parsed.Hostname(), requestCtx.Err())
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return CallEvidence{}, nil, providerError("provider_response_unreadable", "metadata provider response could not be read", err)
	}
	if len(body) > maxResponseBytes {
		return CallEvidence{}, nil, providerError("provider_response_too_large", fmt.Sprintf("metadata provider response exceeds %d bytes", maxResponseBytes), nil)
	}
	queryEvidence, _ := json.Marshal(map[string]any{"method": method, "endpoint_path": parsed.Path, "query": publicQuery})
	call := CallEvidence{Purpose: purpose, QuerySHA256: sha256Hex(queryEvidence), ResponseSHA256: sha256Hex(body), StatusCode: response.StatusCode}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return CallEvidence{}, nil, classifyHTTPStatus(response.StatusCode)
	}
	if err := json.Unmarshal(body, target); err != nil {
		return CallEvidence{}, nil, providerError("provider_response_invalid", "metadata provider returned invalid JSON", err)
	}
	return call, body, nil
}

func classifyTransportError(err error, hostname string, requestContextErr error) error {
	if errors.Is(requestContextErr, context.Canceled) || errors.Is(err, context.Canceled) {
		return providerError("provider_request_cancelled", "metadata provider request was cancelled", err)
	}
	if errors.Is(requestContextErr, context.DeadlineExceeded) || errors.Is(err, context.DeadlineExceeded) {
		return providerError("provider_timeout", "metadata provider request timed out", err)
	}
	if strings.HasSuffix(strings.ToLower(strings.TrimSpace(hostname)), ".workers.dev") {
		return providerError(
			"provider_workers_dev_unreachable",
			"the configured workers.dev endpoint is unreachable from this runtime; bind the Worker to a reachable custom domain and save its /api URL",
			err,
		)
	}
	if errors.Is(err, errRedirectDisabled) {
		return providerError("provider_redirect_rejected", "metadata provider returned a redirect; save the final HTTPS API endpoint", err)
	}
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return providerError("provider_dns_failed", "metadata provider hostname could not be resolved", err)
	}
	var hostnameErr x509.HostnameError
	var authorityErr x509.UnknownAuthorityError
	var certificateErr x509.CertificateInvalidError
	if errors.As(err, &hostnameErr) || errors.As(err, &authorityErr) || errors.As(err, &certificateErr) {
		return providerError("provider_tls_failed", "metadata provider TLS certificate validation failed", err)
	}
	var networkErr net.Error
	if errors.As(err, &networkErr) && networkErr.Timeout() {
		return providerError("provider_timeout", "metadata provider request timed out", err)
	}
	return providerError("provider_connection_failed", "metadata provider connection failed", err)
}

func classifyHTTPStatus(statusCode int) error {
	switch {
	case statusCode == http.StatusUnauthorized || statusCode == http.StatusForbidden:
		return providerError("provider_authentication_failed", fmt.Sprintf("metadata provider rejected authentication with HTTP %d", statusCode), nil)
	case statusCode == http.StatusTooManyRequests:
		return providerError("provider_rate_limited", "metadata provider rate limit was exceeded (HTTP 429)", nil)
	case statusCode >= 500:
		return providerError("provider_upstream_failed", fmt.Sprintf("metadata provider returned HTTP %d", statusCode), nil)
	default:
		return providerError("provider_http_error", fmt.Sprintf("metadata provider returned HTTP %d", statusCode), nil)
	}
}

func normalizeRequest(input ResolveRequest) ResolveRequest {
	input.IMDbID = normalizeIMDb(input.IMDbID)
	input.TMDbID = strings.TrimSpace(input.TMDbID)
	input.TMDbType = strings.ToLower(strings.TrimSpace(input.TMDbType))
	input.DoubanID = strings.TrimSpace(input.DoubanID)
	return input
}

func validateRequest(input ResolveRequest) error {
	if input.IMDbID == "" && input.TMDbID == "" && input.DoubanID == "" {
		return fmt.Errorf("%w: imdb_id, tmdb_id, or douban_id is required", ErrValidation)
	}
	if input.IMDbID != "" && !imdbPattern.MatchString(input.IMDbID) {
		return fmt.Errorf("%w: IMDb id must use tt followed by digits", ErrValidation)
	}
	if input.TMDbID != "" && !numericPattern.MatchString(input.TMDbID) {
		return fmt.Errorf("%w: TMDb id must contain only digits", ErrValidation)
	}
	if input.DoubanID != "" && !numericPattern.MatchString(input.DoubanID) {
		return fmt.Errorf("%w: Douban id must contain only digits", ErrValidation)
	}
	if input.TMDbID != "" && input.TMDbType != "movie" && input.TMDbType != "tv" {
		return fmt.Errorf("%w: tmdb_type must be movie or tv with tmdb_id", ErrValidation)
	}
	return nil
}

func normalizeIMDb(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if value != "" && !strings.HasPrefix(value, "tt") {
		value = "tt" + value
	}
	return value
}

func hasJSONError(value json.RawMessage) bool {
	trimmed := strings.TrimSpace(string(value))
	return trimmed != "" && trimmed != "null" && trimmed != "false" && trimmed != `""` && trimmed != "{}" && trimmed != "[]"
}

func stringOption(options map[string]any, name string) string {
	value, _ := options[name].(string)
	return strings.TrimSpace(value)
}

func cloneValues(input url.Values) url.Values {
	result := make(url.Values, len(input))
	for key, values := range input {
		result[key] = append([]string(nil), values...)
	}
	return result
}

func sha256Hex(value []byte) string {
	digest := sha256.Sum256(value)
	return hex.EncodeToString(digest[:])
}
