package mediamanagers

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxResponseBytes = 2 << 20

var ErrValidation = errors.New("media manager request is invalid")

type RuntimeStore interface {
	GetRuntimeMediaManager(context.Context, string) (integrations.RuntimeMediaManager, error)
	RecordMediaManagerHealth(context.Context, string, string, map[string]any, workflow.Actor) error
	AuditMediaManagerAction(context.Context, string, string, map[string]any, workflow.Actor) error
}

type Manager struct {
	store  RuntimeStore
	client *http.Client
}

type ProbeResult struct {
	Name                string `json:"name"`
	Adapter             string `json:"adapter"`
	Status              string `json:"status"`
	Version             string `json:"version,omitempty"`
	AppName             string `json:"app_name,omitempty"`
	InstanceName        string `json:"instance_name,omitempty"`
	ConfigurationSHA256 string `json:"configuration_sha256"`
	ResponseSHA256      string `json:"response_sha256"`
}

type LookupRequest struct {
	TVDBID int64  `json:"tvdb_id,omitempty"`
	TMDBID int64  `json:"tmdb_id,omitempty"`
	Path   string `json:"path,omitempty"`
	Title  string `json:"title,omitempty"`
}

type Metadata struct {
	Title        string   `json:"title,omitempty"`
	Year         int      `json:"year,omitempty"`
	TVDBID       int64    `json:"tvdb_id,omitempty"`
	TMDBID       int64    `json:"tmdb_id,omitempty"`
	TVMazeID     int64    `json:"tvmaze_id,omitempty"`
	IMDbID       string   `json:"imdb_id,omitempty"`
	Genres       []string `json:"genres"`
	ReleaseGroup string   `json:"release_group,omitempty"`
}

type LookupResult struct {
	Name                string   `json:"name"`
	Adapter             string   `json:"adapter"`
	Matched             bool     `json:"matched"`
	Metadata            Metadata `json:"metadata"`
	QuerySHA256         string   `json:"query_sha256"`
	ResponseSHA256      string   `json:"response_sha256"`
	ConfigurationSHA256 string   `json:"configuration_sha256"`
}

func NewManager(store RuntimeStore, client *http.Client) *Manager {
	if client == nil {
		client = &http.Client{}
	}
	clone := *client
	clone.CheckRedirect = func(*http.Request, []*http.Request) error { return errors.New("redirects are disabled") }
	return &Manager{store: store, client: &clone}
}

func (m *Manager) Probe(ctx context.Context, name string, actor workflow.Actor) (ProbeResult, error) {
	runtime, err := m.store.GetRuntimeMediaManager(ctx, name)
	if err != nil {
		return ProbeResult{}, err
	}
	var status struct {
		Version      string `json:"version"`
		AppName      string `json:"appName"`
		InstanceName string `json:"instanceName"`
	}
	body, err := m.getJSON(ctx, runtime, "/api/v3/system/status", nil, &status)
	if err != nil {
		_ = m.store.RecordMediaManagerHealth(ctx, name, "failed", map[string]any{"operation": "probe", "error_code": "request_failed"}, actor)
		return ProbeResult{}, err
	}
	result := ProbeResult{
		Name: runtime.Name, Adapter: runtime.Adapter, Status: "ready", Version: cleanText(status.Version, 80),
		AppName: cleanText(status.AppName, 80), InstanceName: cleanText(status.InstanceName, 120),
		ConfigurationSHA256: runtime.ConfigurationSHA256, ResponseSHA256: sha256Hex(body),
	}
	if result.Version == "" {
		return ProbeResult{}, fmt.Errorf("media manager returned an incomplete system status")
	}
	if err := m.store.RecordMediaManagerHealth(ctx, name, "ready", map[string]any{
		"operation": "probe", "adapter": runtime.Adapter, "version": result.Version,
		"configuration_sha256": result.ConfigurationSHA256, "response_sha256": result.ResponseSHA256,
	}, actor); err != nil {
		return ProbeResult{}, err
	}
	return result, nil
}

func (m *Manager) Lookup(ctx context.Context, name string, input LookupRequest, actor workflow.Actor) (LookupResult, error) {
	runtime, err := m.store.GetRuntimeMediaManager(ctx, name)
	if err != nil {
		return LookupResult{}, err
	}
	input.Path = strings.TrimSpace(input.Path)
	input.Title = strings.TrimSpace(input.Title)
	queryBody, _ := json.Marshal(input)
	result := LookupResult{Name: runtime.Name, Adapter: runtime.Adapter, QuerySHA256: sha256Hex(queryBody), ConfigurationSHA256: runtime.ConfigurationSHA256}
	var responseBody []byte
	switch runtime.Adapter {
	case "sonarr":
		result.Metadata, responseBody, err = m.lookupSonarr(ctx, runtime, input)
	case "radarr":
		result.Metadata, responseBody, err = m.lookupRadarr(ctx, runtime, input)
	default:
		err = fmt.Errorf("%w: unsupported adapter", ErrValidation)
	}
	if err != nil {
		return LookupResult{}, err
	}
	result.Matched = result.Metadata.TVDBID > 0 || result.Metadata.TMDBID > 0 || result.Metadata.IMDbID != ""
	result.ResponseSHA256 = sha256Hex(responseBody)
	if err := m.store.AuditMediaManagerAction(ctx, runtime.Name, "lookup", map[string]any{
		"adapter": runtime.Adapter, "matched": result.Matched, "query_sha256": result.QuerySHA256,
		"response_sha256": result.ResponseSHA256, "configuration_sha256": result.ConfigurationSHA256,
		"metadata_ids": map[string]any{"tvdb_id": result.Metadata.TVDBID, "tmdb_id": result.Metadata.TMDBID, "imdb_id": result.Metadata.IMDbID},
	}, actor); err != nil {
		return LookupResult{}, err
	}
	return result, nil
}

func (m *Manager) lookupSonarr(ctx context.Context, runtime integrations.RuntimeMediaManager, input LookupRequest) (Metadata, []byte, error) {
	query := url.Values{"includeSeasonImages": []string{"false"}}
	endpoint := "/api/v3/series"
	if input.TVDBID > 0 {
		query.Set("tvdbId", strconv.FormatInt(input.TVDBID, 10))
	} else if input.Path != "" && input.Title != "" {
		endpoint = "/api/v3/parse"
		query = url.Values{"path": []string{input.Path}, "title": []string{input.Title}}
	} else {
		return Metadata{}, nil, fmt.Errorf("%w: Sonarr lookup requires tvdb_id or both path and title", ErrValidation)
	}
	var raw json.RawMessage
	body, err := m.getJSON(ctx, runtime, endpoint, query, &raw)
	if err != nil {
		return Metadata{}, nil, err
	}
	var item sonarrSeries
	if endpoint == "/api/v3/parse" {
		var parsed struct {
			Series            sonarrSeries `json:"series"`
			ParsedEpisodeInfo struct {
				ReleaseGroup string `json:"releaseGroup"`
			} `json:"parsedEpisodeInfo"`
		}
		if err := json.Unmarshal(raw, &parsed); err != nil {
			return Metadata{}, nil, fmt.Errorf("decode Sonarr parse response: %w", err)
		}
		item = parsed.Series
		item.ReleaseGroup = parsed.ParsedEpisodeInfo.ReleaseGroup
	} else {
		var items []sonarrSeries
		if err := json.Unmarshal(raw, &items); err != nil {
			return Metadata{}, nil, fmt.Errorf("decode Sonarr series response: %w", err)
		}
		if len(items) == 0 {
			return Metadata{Genres: []string{}}, body, nil
		}
		item = items[0]
	}
	return Metadata{Title: cleanText(item.Title, 300), Year: item.Year, TVDBID: item.TVDBID, TMDBID: item.TMDBID,
		TVMazeID: item.TVMazeID, IMDbID: normalizeIMDb(item.IMDbID), Genres: cleanStrings(item.Genres, 40), ReleaseGroup: cleanText(item.ReleaseGroup, 120)}, body, nil
}

func (m *Manager) lookupRadarr(ctx context.Context, runtime integrations.RuntimeMediaManager, input LookupRequest) (Metadata, []byte, error) {
	endpoint := "/api/v3/movie"
	query := url.Values{"excludeLocalCovers": []string{"true"}}
	if input.TMDBID > 0 {
		query.Set("tmdbId", strconv.FormatInt(input.TMDBID, 10))
	} else if input.Path != "" {
		endpoint = "/api/v3/movie/lookup"
		query = url.Values{"term": []string{input.Path}}
	} else {
		return Metadata{}, nil, fmt.Errorf("%w: Radarr lookup requires tmdb_id or path", ErrValidation)
	}
	var items []radarrMovie
	body, err := m.getJSON(ctx, runtime, endpoint, query, &items)
	if err != nil {
		return Metadata{}, nil, err
	}
	if len(items) == 0 {
		return Metadata{Genres: []string{}}, body, nil
	}
	item := items[0]
	if input.Path != "" && input.TMDBID == 0 {
		found := false
		for _, candidate := range items {
			if strings.TrimSpace(candidate.MovieFile.OriginalFilePath) == input.Path || strings.TrimSpace(candidate.Path) == input.Path {
				item, found = candidate, true
				break
			}
		}
		if !found {
			return Metadata{Genres: []string{}}, body, nil
		}
	}
	return Metadata{Title: cleanText(item.Title, 300), Year: item.Year, TMDBID: item.TMDBID,
		IMDbID: normalizeIMDb(item.IMDbID), Genres: cleanStrings(item.Genres, 40), ReleaseGroup: cleanText(item.MovieFile.ReleaseGroup, 120)}, body, nil
}

type sonarrSeries struct {
	Title        string   `json:"title"`
	Year         int      `json:"year"`
	TVDBID       int64    `json:"tvdbId"`
	TMDBID       int64    `json:"tmdbId"`
	TVMazeID     int64    `json:"tvMazeId"`
	IMDbID       string   `json:"imdbId"`
	Genres       []string `json:"genres"`
	ReleaseGroup string   `json:"releaseGroup"`
}

type radarrMovie struct {
	Title     string   `json:"title"`
	Path      string   `json:"path"`
	Year      int      `json:"year"`
	TMDBID    int64    `json:"tmdbId"`
	IMDbID    string   `json:"imdbId"`
	Genres    []string `json:"genres"`
	MovieFile struct {
		OriginalFilePath string `json:"originalFilePath"`
		ReleaseGroup     string `json:"releaseGroup"`
	} `json:"movieFile"`
}

func (m *Manager) getJSON(ctx context.Context, runtime integrations.RuntimeMediaManager, endpoint string, query url.Values, target any) ([]byte, error) {
	base, err := url.Parse(runtime.EndpointConfig.Endpoint)
	if err != nil {
		return nil, fmt.Errorf("invalid configured media manager endpoint")
	}
	base.Path = path.Join(base.Path, endpoint)
	base.RawQuery = query.Encode()
	requestCtx, cancel := context.WithTimeout(ctx, time.Duration(runtime.EndpointConfig.TimeoutSeconds)*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodGet, base.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("build media manager request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("X-Api-Key", runtime.Credentials["api_key"])
	response, err := m.client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("media manager request failed")
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read media manager response: %w", err)
	}
	if len(body) > maxResponseBytes {
		return nil, fmt.Errorf("media manager response exceeds %d bytes", maxResponseBytes)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("media manager returned HTTP %d", response.StatusCode)
	}
	if err := json.Unmarshal(body, target); err != nil {
		return nil, fmt.Errorf("decode media manager response: %w", err)
	}
	return body, nil
}

func normalizeIMDb(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if !strings.HasPrefix(value, "tt") {
		value = "tt" + value
	}
	for _, character := range strings.TrimPrefix(value, "tt") {
		if character < '0' || character > '9' {
			return ""
		}
	}
	return value
}

func cleanText(value string, limit int) string {
	value = strings.Map(func(r rune) rune {
		if r == '\r' || r == '\n' || r == '\x00' {
			return ' '
		}
		return r
	}, strings.TrimSpace(value))
	if len(value) > limit {
		value = value[:limit]
	}
	return value
}

func cleanStrings(values []string, limit int) []string {
	if len(values) > limit {
		values = values[:limit]
	}
	result := make([]string, 0, len(values))
	for _, value := range values {
		if cleaned := cleanText(value, 80); cleaned != "" {
			result = append(result, cleaned)
		}
	}
	return result
}

func sha256Hex(body []byte) string {
	hash := sha256.Sum256(body)
	return hex.EncodeToString(hash[:])
}
