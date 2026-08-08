package mteam

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var imdbPattern = regexp.MustCompile(`^tt[0-9]{5,12}$`)

var _ sites.TargetDuplicateAdapter = (*Client)(nil)

const (
	defaultAPIEndpoint = "https://api.m-team.cc"
	maxAPIResponse     = 8 << 20
	maxDupeCandidates  = 100
)

type RuntimeSiteStore interface {
	GetRuntimeSite(context.Context, string) (integrations.RuntimeSite, error)
	AuditSiteAction(context.Context, string, string, map[string]any, workflow.Actor) error
}

type DuplicateQuery = sites.TargetDuplicateQuery
type DuplicateCandidate = sites.TargetDuplicateCandidate
type DuplicateEvidence = sites.TargetDuplicateEvidence

type Client struct {
	store      RuntimeSiteStore
	httpClient *http.Client
}

func NewClient(store RuntimeSiteStore, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 60 * time.Second}
	} else {
		clone := *httpClient
		httpClient = &clone
		if httpClient.Timeout <= 0 {
			httpClient.Timeout = 60 * time.Second
		}
	}
	httpClient.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return errors.New("MTEAM API redirect is not allowed")
	}
	return &Client{store: store, httpClient: httpClient}
}

func (*Client) SiteCode() string { return "MTEAM" }

func (client *Client) DuplicateCheck(ctx context.Context, query DuplicateQuery, actor workflow.Actor) (DuplicateEvidence, error) {
	query.IMDbID = strings.ToLower(strings.TrimSpace(query.IMDbID))
	if !imdbPattern.MatchString(query.IMDbID) {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_identity_required", "MTEAM duplicate check requires an IMDb id in tt1234567 form", false, nil)
	}
	runtime, err := client.store.GetRuntimeSite(ctx, "MTEAM")
	if err != nil {
		return DuplicateEvidence{}, sites.NewAdapterError("site_configuration_unavailable", "MTEAM site configuration is unavailable or disabled", false, err)
	}
	if runtime.Adapter != "mteam_api" {
		return DuplicateEvidence{}, sites.NewAdapterError("site_adapter_mismatch", "configured MTEAM site adapter is not mteam_api", false, nil)
	}
	apiKey := strings.TrimSpace(runtime.Credentials["api_key"])
	if apiKey == "" {
		return DuplicateEvidence{}, sites.NewAdapterError("site_api_key_required", "an enabled MTEAM api_key credential is required", false, nil)
	}
	config, endpoint, err := parseAPIConfig(runtime.Config)
	if err != nil {
		return DuplicateEvidence{}, sites.NewAdapterError("site_configuration_invalid", err.Error(), false, err)
	}
	payload := map[string]any{
		"mode": "normal", "visible": 1, "categories": []any{},
		"pageNumber": 1, "pageSize": maxDupeCandidates, "imdb": query.IMDbID,
	}
	body, _ := json.Marshal(payload)
	requestURL := resolveAPI(endpoint, "/api/torrent/search")
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL.String(), bytes.NewReader(body))
	if err != nil {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_request_failed", "could not build MTEAM duplicate-check request", false, err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", "Upload-Assistant-Go/2")
	request.Header.Set("x-api-key", apiKey)
	httpClient := *client.httpClient
	if config.TimeoutSeconds > 0 {
		httpClient.Timeout = time.Duration(config.TimeoutSeconds) * time.Second
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_request_failed", "MTEAM duplicate-check request failed", true, nil)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxAPIResponse+1))
	if err != nil || len(responseBody) > maxAPIResponse {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_response_invalid", "MTEAM duplicate-check response is unreadable or too large", false, err)
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return DuplicateEvidence{}, sites.NewAdapterError("site_authentication_failed", "MTEAM rejected the API key", false, nil)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_request_failed", fmt.Sprintf("MTEAM duplicate-check returned HTTP %d", response.StatusCode), response.StatusCode >= 500, nil)
	}
	items, err := parseSearchResponse(responseBody)
	if err != nil {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_response_invalid", "MTEAM duplicate-check returned an invalid response", false, err)
	}
	candidates, err := normalizeCandidates(items)
	if err != nil {
		return DuplicateEvidence{}, sites.NewAdapterError("target_duplicate_response_invalid", "MTEAM duplicate-check results are invalid", false, err)
	}
	configurationSHA := runtime.ConfigurationSHA256
	if configurationSHA == "" {
		digest := sha256.Sum256(runtime.Config)
		configurationSHA = hex.EncodeToString(digest[:])
	}
	evidence := DuplicateEvidence{
		SiteCode: "MTEAM", Adapter: runtime.Adapter, ConfigurationSHA256: configurationSHA,
		Query: query, Duplicate: len(items) > 0, ResultCount: len(items),
		Candidates: candidates, CandidatesTruncated: len(items) > len(candidates), CheckedAt: time.Now().UTC(),
	}
	ids := make([]string, 0, len(candidates))
	for _, candidate := range candidates {
		if candidate.ID != "" {
			ids = append(ids, candidate.ID)
		}
	}
	if err := client.store.AuditSiteAction(ctx, "MTEAM", "target.duplicate_check", map[string]any{
		"imdb_id": query.IMDbID, "duplicate": evidence.Duplicate,
		"result_count": evidence.ResultCount, "candidate_ids": ids,
		"configuration_sha256": configurationSHA,
	}, actor); err != nil {
		return DuplicateEvidence{}, err
	}
	return evidence, nil
}

type apiConfig struct {
	Endpoint       string         `json:"endpoint,omitempty"`
	TimeoutSeconds int            `json:"timeout_seconds,omitempty"`
	Options        map[string]any `json:"options,omitempty"`
}

func parseAPIConfig(body json.RawMessage) (apiConfig, *url.URL, error) {
	config := apiConfig{Endpoint: defaultAPIEndpoint, TimeoutSeconds: 60}
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) > 0 && !bytes.Equal(trimmed, []byte("null")) && !bytes.Equal(trimmed, []byte("{}")) {
		decoder := json.NewDecoder(bytes.NewReader(trimmed))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&config); err != nil {
			return apiConfig{}, nil, fmt.Errorf("decode MTEAM API config: %w", err)
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			return apiConfig{}, nil, fmt.Errorf("decode MTEAM API config: trailing JSON value")
		}
	}
	if config.Endpoint == "" {
		config.Endpoint = defaultAPIEndpoint
	}
	if config.TimeoutSeconds == 0 {
		config.TimeoutSeconds = 60
	}
	if config.TimeoutSeconds < 1 || config.TimeoutSeconds > 300 {
		return apiConfig{}, nil, fmt.Errorf("MTEAM API timeout must be between 1 and 300 seconds")
	}
	parsed, err := url.Parse(strings.TrimRight(config.Endpoint, "/"))
	if err != nil || parsed.Host == "" || (parsed.Scheme != "https" && parsed.Scheme != "http") {
		return apiConfig{}, nil, fmt.Errorf("MTEAM API endpoint is invalid")
	}
	loopback := parsed.Hostname() == "localhost" || parsed.Hostname() == "127.0.0.1" || parsed.Hostname() == "::1"
	if parsed.Scheme != "https" && !loopback {
		return apiConfig{}, nil, fmt.Errorf("MTEAM API endpoint must use HTTPS")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return apiConfig{}, nil, fmt.Errorf("MTEAM API endpoint must not contain credentials, query, or fragment")
	}
	return config, parsed, nil
}

func resolveAPI(base *url.URL, path string) *url.URL {
	result := *base
	result.Path = strings.TrimRight(base.Path, "/") + path
	return &result
}

func parseSearchResponse(body []byte) ([]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var response struct {
		Code    any             `json:"code"`
		Message string          `json:"message"`
		Data    json.RawMessage `json:"data"`
	}
	if err := decoder.Decode(&response); err != nil || !successCode(response.Code) || len(response.Data) == 0 {
		return nil, fmt.Errorf("unsuccessful API envelope")
	}
	return extractItemList(response.Data)
}

func extractItemList(body json.RawMessage) ([]json.RawMessage, error) {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) == 0 {
		return nil, fmt.Errorf("missing search data")
	}
	if trimmed[0] == '[' {
		var items []json.RawMessage
		return items, json.Unmarshal(trimmed, &items)
	}
	if trimmed[0] != '{' {
		return nil, fmt.Errorf("search data is not a list or object")
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &object); err != nil {
		return nil, err
	}
	for _, key := range []string{"data", "torrents", "list", "items"} {
		if nested, exists := object[key]; exists {
			return extractItemList(nested)
		}
	}
	return nil, fmt.Errorf("search response does not contain a result list")
}

func successCode(value any) bool {
	switch typed := value.(type) {
	case json.Number:
		return typed.String() == "0"
	case float64:
		return typed == 0
	case string:
		return strings.TrimSpace(typed) == "0"
	default:
		return false
	}
}

func normalizeCandidates(items []json.RawMessage) ([]DuplicateCandidate, error) {
	limit := len(items)
	if limit > maxDupeCandidates {
		limit = maxDupeCandidates
	}
	result := make([]DuplicateCandidate, 0, limit)
	for index, body := range items[:limit] {
		var raw map[string]any
		decoder := json.NewDecoder(bytes.NewReader(body))
		decoder.UseNumber()
		if err := decoder.Decode(&raw); err != nil || raw == nil {
			return nil, fmt.Errorf("candidate %d is not an object", index)
		}
		candidate := DuplicateCandidate{
			ID:        firstString(raw, "id", "torrentId", "torrent_id"),
			Name:      firstString(raw, "name", "title"),
			SizeBytes: firstInt64(raw, "size", "sizeBytes", "size_bytes"),
			Category:  firstString(raw, "category", "categoryId", "category_id"),
			Standard:  firstString(raw, "standard", "standardId", "standard_id"),
			IMDbID:    firstString(raw, "imdb", "imdbId", "imdb_id"),
			CreatedAt: firstString(raw, "createdDate", "createdAt", "created_at", "uploadDate"),
		}
		if candidate.Name == "" {
			candidate.Name = "(unnamed MTEAM search result)"
		}
		result = append(result, candidate)
	}
	return result, nil
}

func firstString(values map[string]any, keys ...string) string {
	for _, key := range keys {
		switch value := values[key].(type) {
		case string:
			if strings.TrimSpace(value) != "" {
				return strings.TrimSpace(value)
			}
		case json.Number:
			return value.String()
		case float64:
			return strconv.FormatFloat(value, 'f', -1, 64)
		}
	}
	return ""
}

func firstInt64(values map[string]any, keys ...string) int64 {
	for _, key := range keys {
		var result int64
		switch value := values[key].(type) {
		case json.Number:
			result, _ = value.Int64()
		case float64:
			result = int64(value)
		case string:
			result, _ = strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		}
		if result > 0 {
			return result
		}
	}
	return 0
}
