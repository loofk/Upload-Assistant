package qbittorrent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

const (
	maxResponseBytes      = 4 << 20
	maxFilesResponseBytes = 32 << 20
)

var (
	ErrUnauthorized = errors.New("qBittorrent authentication failed")
	ErrNotFound     = errors.New("qBittorrent torrent not found")
	hashPattern     = regexp.MustCompile(`^[a-fA-F0-9]{40}([a-fA-F0-9]{24})?$`)
)

type Config struct {
	Endpoint    string
	Timeout     time.Duration
	Credentials map[string]string
	HTTPClient  *http.Client
}

type Client struct {
	endpoint      *url.URL
	httpClient    *http.Client
	credentials   map[string]string
	authMu        sync.Mutex
	authenticated bool
}

type ProbeResult struct {
	ApplicationVersion string `json:"application_version"`
	WebAPIVersion      string `json:"webapi_version"`
	Authentication     string `json:"authentication"`
}

type Torrent struct {
	Hash          string  `json:"hash"`
	Name          string  `json:"name"`
	State         string  `json:"state"`
	Progress      float64 `json:"progress"`
	Size          int64   `json:"size"`
	TotalSize     int64   `json:"total_size"`
	Completed     int64   `json:"completed"`
	AmountLeft    int64   `json:"amount_left"`
	Downloaded    int64   `json:"downloaded"`
	Uploaded      int64   `json:"uploaded"`
	DownloadSpeed int64   `json:"dlspeed"`
	UploadSpeed   int64   `json:"upspeed"`
	DownloadLimit int64   `json:"dl_limit"`
	UploadLimit   int64   `json:"up_limit"`
	Ratio         float64 `json:"ratio"`
	SavePath      string  `json:"save_path"`
	ContentPath   string  `json:"content_path"`
	Category      string  `json:"category"`
	Tags          string  `json:"tags"`
	AddedOn       int64   `json:"added_on"`
	CompletionOn  int64   `json:"completion_on"`
	TimeActive    int64   `json:"time_active"`
	SeedingTime   int64   `json:"seeding_time"`
}

type TorrentFile struct {
	Index        int     `json:"index"`
	Name         string  `json:"name"`
	Size         int64   `json:"size"`
	Progress     float64 `json:"progress"`
	Priority     int     `json:"priority"`
	Seed         bool    `json:"is_seed"`
	Availability float64 `json:"availability"`
}

type AddOptions struct {
	SavePath      string
	Category      string
	Tags          []string
	ApplyLabels   *bool
	SkipChecking  bool
	Paused        bool
	UploadLimit   int64
	DownloadLimit int64
}

type AddResult struct {
	Hashes   torrentmeta.InfoHashes `json:"hashes"`
	Observed *Torrent               `json:"observed,omitempty"`
}

func New(config Config) (*Client, error) {
	endpoint, err := url.Parse(strings.TrimRight(strings.TrimSpace(config.Endpoint), "/"))
	if err != nil || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Host == "" {
		return nil, fmt.Errorf("invalid qBittorrent endpoint")
	}
	if endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return nil, fmt.Errorf("qBittorrent endpoint must not contain credentials, query, or fragment")
	}
	if config.Timeout <= 0 {
		config.Timeout = 30 * time.Second
	}
	if config.HTTPClient == nil {
		jar, err := cookiejar.New(nil)
		if err != nil {
			return nil, fmt.Errorf("create qBittorrent cookie jar: %w", err)
		}
		config.HTTPClient = &http.Client{
			Timeout: config.Timeout, Jar: jar,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return errors.New("qBittorrent endpoint redirect is not allowed")
			},
		}
	}
	return &Client{endpoint: endpoint, httpClient: config.HTTPClient, credentials: config.Credentials}, nil
}

func (client *Client) Authenticate(ctx context.Context) error {
	client.authMu.Lock()
	defer client.authMu.Unlock()
	if client.authenticated {
		return nil
	}
	if apiKey := strings.TrimSpace(client.credentials["api_key"]); apiKey != "" {
		client.authenticated = true
		return nil
	}
	username := client.credentials["username"]
	password := client.credentials["password"]
	if username == "" || password == "" {
		return fmt.Errorf("%w: username/password or api_key is required", ErrUnauthorized)
	}
	values := url.Values{"username": {username}, "password": {password}}
	body, err := client.request(ctx, http.MethodPost, "/api/v2/auth/login", strings.NewReader(values.Encode()), "application/x-www-form-urlencoded", false)
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(body)) != "Ok." {
		return ErrUnauthorized
	}
	client.authenticated = true
	return nil
}

func (client *Client) Probe(ctx context.Context) (ProbeResult, error) {
	if err := client.Authenticate(ctx); err != nil {
		return ProbeResult{}, err
	}
	version, err := client.request(ctx, http.MethodGet, "/api/v2/app/version", nil, "", true)
	if err != nil {
		return ProbeResult{}, err
	}
	apiVersion, err := client.request(ctx, http.MethodGet, "/api/v2/app/webapiVersion", nil, "", true)
	if err != nil {
		return ProbeResult{}, err
	}
	authentication := "cookie"
	if client.credentials["api_key"] != "" {
		authentication = "api_key"
	}
	return ProbeResult{
		ApplicationVersion: strings.TrimSpace(string(version)),
		WebAPIVersion:      strings.TrimSpace(string(apiVersion)), Authentication: authentication,
	}, nil
}

func (client *Client) Get(ctx context.Context, hash string) (Torrent, error) {
	if err := validateHash(hash); err != nil {
		return Torrent{}, err
	}
	if err := client.Authenticate(ctx); err != nil {
		return Torrent{}, err
	}
	query := url.Values{"hashes": {strings.ToLower(hash)}}
	body, err := client.request(ctx, http.MethodGet, "/api/v2/torrents/info?"+query.Encode(), nil, "", true)
	if err != nil {
		return Torrent{}, err
	}
	var torrents []Torrent
	if err := json.Unmarshal(body, &torrents); err != nil {
		return Torrent{}, fmt.Errorf("decode qBittorrent torrent info: %w", err)
	}
	if len(torrents) == 0 {
		return Torrent{}, ErrNotFound
	}
	for _, torrent := range torrents {
		if strings.EqualFold(torrent.Hash, hash) {
			return torrent, nil
		}
	}
	return Torrent{}, ErrNotFound
}

func (client *Client) Files(ctx context.Context, hash string) ([]TorrentFile, error) {
	if err := validateHash(hash); err != nil {
		return nil, err
	}
	if err := client.Authenticate(ctx); err != nil {
		return nil, err
	}
	query := url.Values{"hash": {strings.ToLower(hash)}}
	body, err := client.requestLimit(
		ctx, http.MethodGet, "/api/v2/torrents/files?"+query.Encode(), nil, "", true, maxFilesResponseBytes,
	)
	if err != nil {
		return nil, err
	}
	var files []TorrentFile
	if err := json.Unmarshal(body, &files); err != nil {
		return nil, fmt.Errorf("decode qBittorrent torrent files: %w", err)
	}
	if len(files) == 0 {
		return nil, ErrNotFound
	}
	if len(files) > 200_000 {
		return nil, fmt.Errorf("qBittorrent torrent file count exceeds 200000")
	}
	return files, nil
}

func (client *Client) Add(ctx context.Context, metainfo []byte, options AddOptions) (AddResult, error) {
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		return AddResult{}, err
	}
	if err := client.Authenticate(ctx); err != nil {
		return AddResult{}, err
	}
	if options.UploadLimit < 0 || options.DownloadLimit < 0 {
		return AddResult{}, fmt.Errorf("qBittorrent limits must not be negative")
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	part, err := writer.CreateFormFile("torrents", "source.torrent")
	if err != nil {
		return AddResult{}, fmt.Errorf("create qBittorrent torrent form: %w", err)
	}
	if _, err := part.Write(metainfo); err != nil {
		return AddResult{}, fmt.Errorf("write qBittorrent torrent form: %w", err)
	}
	fields := map[string]string{
		"savepath": options.SavePath, "category": options.Category,
		"tags": strings.Join(options.Tags, ","), "skip_checking": strconv.FormatBool(options.SkipChecking),
		"paused": strconv.FormatBool(options.Paused), "upLimit": strconv.FormatInt(options.UploadLimit, 10),
		"dlLimit": strconv.FormatInt(options.DownloadLimit, 10),
	}
	for name, value := range fields {
		if value == "" {
			continue
		}
		if err := writer.WriteField(name, value); err != nil {
			return AddResult{}, fmt.Errorf("write qBittorrent field %s: %w", name, err)
		}
	}
	if err := writer.Close(); err != nil {
		return AddResult{}, fmt.Errorf("close qBittorrent torrent form: %w", err)
	}
	response, err := client.request(ctx, http.MethodPost, "/api/v2/torrents/add", &body, writer.FormDataContentType(), true)
	if err != nil {
		return AddResult{}, err
	}
	if message := strings.TrimSpace(string(response)); message != "" && message != "Ok." {
		return AddResult{}, fmt.Errorf("qBittorrent rejected torrent: %s", safeMessage(message))
	}
	result := AddResult{Hashes: hashes}
	for attempt := 0; attempt < 10; attempt++ {
		for _, hash := range []string{hashes.V1SHA1, hashes.V2SHA256} {
			torrent, getErr := client.Get(ctx, hash)
			if getErr == nil {
				result.Observed = &torrent
				return result, nil
			}
			if !errors.Is(getErr, ErrNotFound) {
				return AddResult{}, getErr
			}
		}
		select {
		case <-ctx.Done():
			return AddResult{}, ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
	return result, nil
}

func (client *Client) SetLimits(ctx context.Context, hash string, downloadBytesPerSecond, uploadBytesPerSecond int64) error {
	if err := validateHash(hash); err != nil {
		return err
	}
	if downloadBytesPerSecond < 0 || uploadBytesPerSecond < 0 {
		return fmt.Errorf("qBittorrent limits must not be negative")
	}
	if err := client.Authenticate(ctx); err != nil {
		return err
	}
	operations := []struct {
		endpoint string
		limit    int64
	}{
		{endpoint: "/api/v2/torrents/setDownloadLimit", limit: downloadBytesPerSecond},
		{endpoint: "/api/v2/torrents/setUploadLimit", limit: uploadBytesPerSecond},
	}
	for _, operation := range operations {
		endpoint, limit := operation.endpoint, operation.limit
		values := url.Values{"hashes": {strings.ToLower(hash)}, "limit": {strconv.FormatInt(limit, 10)}}
		if _, err := client.request(ctx, http.MethodPost, endpoint, strings.NewReader(values.Encode()), "application/x-www-form-urlencoded", true); err != nil {
			return err
		}
	}
	return nil
}

func (client *Client) WaitComplete(ctx context.Context, hash string, interval time.Duration) (Torrent, error) {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	for {
		torrent, err := client.Get(ctx, hash)
		if err != nil {
			return Torrent{}, err
		}
		if torrent.Progress >= 1 || (torrent.TotalSize > 0 && torrent.AmountLeft == 0) {
			return torrent, nil
		}
		lowerState := strings.ToLower(torrent.State)
		if strings.Contains(lowerState, "error") || strings.Contains(lowerState, "missing") {
			return Torrent{}, fmt.Errorf("qBittorrent torrent entered state %s", torrent.State)
		}
		select {
		case <-ctx.Done():
			return Torrent{}, ctx.Err()
		case <-time.After(interval):
		}
	}
}

func (client *Client) request(ctx context.Context, method, apiPath string, body io.Reader, contentType string, authenticated bool) ([]byte, error) {
	return client.requestLimit(ctx, method, apiPath, body, contentType, authenticated, maxResponseBytes)
}

func (client *Client) requestLimit(ctx context.Context, method, apiPath string, body io.Reader, contentType string, authenticated bool, responseLimit int64) ([]byte, error) {
	requestURL := client.endpoint.String() + apiPath
	request, err := http.NewRequestWithContext(ctx, method, requestURL, body)
	if err != nil {
		return nil, fmt.Errorf("create qBittorrent request: %w", err)
	}
	request.Header.Set("Accept", "application/json, text/plain")
	request.Header.Set("Origin", client.endpoint.Scheme+"://"+client.endpoint.Host)
	request.Header.Set("Referer", client.endpoint.String()+"/")
	request.Header.Set("User-Agent", "Upload-Assistant/2")
	if contentType != "" {
		request.Header.Set("Content-Type", contentType)
	}
	if authenticated {
		if apiKey := strings.TrimSpace(client.credentials["api_key"]); apiKey != "" {
			request.Header.Set("Authorization", "Bearer "+apiKey)
		}
	}
	response, err := client.httpClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("qBittorrent request failed: %w", err)
	}
	defer response.Body.Close()
	limited := io.LimitReader(response.Body, responseLimit+1)
	responseBody, err := io.ReadAll(limited)
	if err != nil {
		return nil, fmt.Errorf("read qBittorrent response: %w", err)
	}
	if int64(len(responseBody)) > responseLimit {
		return nil, fmt.Errorf("qBittorrent response exceeds %d bytes", responseLimit)
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return nil, ErrUnauthorized
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("qBittorrent API returned HTTP %d: %s", response.StatusCode, safeMessage(string(responseBody)))
	}
	return responseBody, nil
}

func validateHash(hash string) error {
	if !hashPattern.MatchString(strings.TrimSpace(hash)) {
		return fmt.Errorf("invalid qBittorrent torrent hash")
	}
	return nil
}

func safeMessage(message string) string {
	message = strings.TrimSpace(strings.ReplaceAll(message, "\n", " "))
	if len(message) > 300 {
		message = message[:300]
	}
	return message
}
