package transmission

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"strings"
	"sync/atomic"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

const maxResponseBytes = 32 << 20

var hashPattern = regexp.MustCompile(`^[a-fA-F0-9]{40}([a-fA-F0-9]{24})?$`)

// PartialAddError means Transmission accepted the torrent but a mandatory
// follow-up mutation (currently per-torrent limits) failed. Callers must audit
// the hash and reconcile or retry; torrent-add is idempotent for duplicates.
type PartialAddError struct {
	Hash string
	Err  error
}

func (err *PartialAddError) Error() string {
	return fmt.Sprintf("Transmission added torrent %s but post-add configuration failed: %v", err.Hash, err.Err)
}

func (err *PartialAddError) Unwrap() error { return err.Err }

func (err *PartialAddError) PartialHash() string { return err.Hash }

type Config struct {
	Endpoint    string
	Timeout     time.Duration
	Credentials map[string]string
	HTTPClient  *http.Client
}

type Client struct {
	endpoint    *url.URL
	httpClient  *http.Client
	credentials map[string]string
	sessionID   atomic.Value
	tag         atomic.Int64
	protocol    atomic.Int32
}

type rpcRequest struct {
	Method    string `json:"method"`
	Arguments any    `json:"arguments"`
	Tag       int64  `json:"tag"`
}

type rpcResponse struct {
	Result    string          `json:"result"`
	Arguments json.RawMessage `json:"arguments"`
	Tag       int64           `json:"tag"`
}

type jsonRPCRequest struct {
	JSONRPC string `json:"jsonrpc"`
	Method  string `json:"method"`
	Params  any    `json:"params"`
	ID      int64  `json:"id"`
}

type jsonRPCResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	Result  json.RawMessage `json:"result"`
	Error   *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error,omitempty"`
	ID int64 `json:"id"`
}

type torrent struct {
	HashString         string      `json:"hashString"`
	Name               string      `json:"name"`
	Status             int         `json:"status"`
	ErrorString        string      `json:"errorString"`
	PercentDone        float64     `json:"percentDone"`
	TotalSize          int64       `json:"totalSize"`
	LeftUntilDone      int64       `json:"leftUntilDone"`
	DownloadedEver     int64       `json:"downloadedEver"`
	UploadedEver       int64       `json:"uploadedEver"`
	RateDownload       int64       `json:"rateDownload"`
	RateUpload         int64       `json:"rateUpload"`
	DownloadLimit      int64       `json:"downloadLimit"`
	DownloadLimited    bool        `json:"downloadLimited"`
	UploadLimit        int64       `json:"uploadLimit"`
	UploadLimited      bool        `json:"uploadLimited"`
	UploadRatio        float64     `json:"uploadRatio"`
	DownloadDir        string      `json:"downloadDir"`
	Labels             []string    `json:"labels"`
	AddedDate          int64       `json:"addedDate"`
	DoneDate           int64       `json:"doneDate"`
	SecondsDownloading int64       `json:"secondsDownloading"`
	SecondsSeeding     int64       `json:"secondsSeeding"`
	IsFinished         bool        `json:"isFinished"`
	Files              []file      `json:"files"`
	FileStats          []fileStats `json:"fileStats"`
}

type file struct {
	Name           string `json:"name"`
	Length         int64  `json:"length"`
	BytesCompleted int64  `json:"bytesCompleted"`
}

type fileStats struct {
	Wanted   wantedValue `json:"wanted"`
	Priority int         `json:"priority"`
}

type wantedValue bool

func (value *wantedValue) UnmarshalJSON(body []byte) error {
	var boolean bool
	if err := json.Unmarshal(body, &boolean); err == nil {
		*value = wantedValue(boolean)
		return nil
	}
	var number int
	if err := json.Unmarshal(body, &number); err != nil || (number != 0 && number != 1) {
		return fmt.Errorf("invalid Transmission wanted value")
	}
	*value = wantedValue(number == 1)
	return nil
}

var torrentFields = []string{
	"hashString", "name", "status", "errorString", "percentDone", "totalSize", "leftUntilDone",
	"downloadedEver", "uploadedEver", "rateDownload", "rateUpload", "downloadLimit", "downloadLimited",
	"uploadLimit", "uploadLimited", "uploadRatio", "downloadDir", "labels", "addedDate", "doneDate",
	"secondsDownloading", "secondsSeeding", "isFinished",
}

func New(config Config) (*Client, error) {
	endpoint, err := url.Parse(strings.TrimSpace(config.Endpoint))
	if err != nil || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Host == "" {
		return nil, fmt.Errorf("invalid Transmission endpoint")
	}
	if endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return nil, fmt.Errorf("Transmission endpoint must not contain credentials, query, or fragment")
	}
	if endpoint.Path == "" || endpoint.Path == "/" {
		endpoint.Path = "/transmission/rpc"
	}
	if config.Timeout <= 0 {
		config.Timeout = 30 * time.Second
	}
	username := strings.TrimSpace(config.Credentials["username"])
	password := config.Credentials["password"]
	if (username == "") != (password == "") {
		return nil, fmt.Errorf("%w: Transmission username and password must be supplied together", qbittorrent.ErrUnauthorized)
	}
	if config.HTTPClient == nil {
		config.HTTPClient = &http.Client{
			Timeout: config.Timeout,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return errors.New("Transmission endpoint redirect is not allowed")
			},
		}
	}
	return &Client{endpoint: endpoint, httpClient: config.HTTPClient, credentials: config.Credentials}, nil
}

func (client *Client) Probe(ctx context.Context) (qbittorrent.ProbeResult, error) {
	var arguments struct {
		Version           string `json:"version"`
		RPCVersion        int    `json:"rpcVersion"`
		RPCVersionMinimum int    `json:"rpcVersionMinimum"`
	}
	if err := client.call(ctx, "session-get", map[string]any{
		"fields": []string{"version", "rpc-version", "rpc-version-minimum"},
	}, &arguments); err != nil {
		return qbittorrent.ProbeResult{}, err
	}
	authentication := "none"
	if client.credentials["username"] != "" {
		authentication = "basic"
	}
	return qbittorrent.ProbeResult{
		ApplicationVersion: arguments.Version,
		WebAPIVersion:      fmt.Sprintf("rpc-%d (min %d)", arguments.RPCVersion, arguments.RPCVersionMinimum),
		Authentication:     authentication,
	}, nil
}

func (client *Client) Get(ctx context.Context, hash string) (qbittorrent.Torrent, error) {
	if err := validateHash(hash); err != nil {
		return qbittorrent.Torrent{}, err
	}
	items, err := client.getTorrents(ctx, []string{strings.ToLower(hash)}, torrentFields)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	for _, item := range items {
		if strings.EqualFold(item.HashString, hash) {
			return normalizeTorrent(item), nil
		}
	}
	return qbittorrent.Torrent{}, qbittorrent.ErrNotFound
}

func (client *Client) Files(ctx context.Context, hash string) ([]qbittorrent.TorrentFile, error) {
	if err := validateHash(hash); err != nil {
		return nil, err
	}
	items, err := client.getTorrents(ctx, []string{strings.ToLower(hash)}, []string{"hashString", "files", "fileStats"})
	if err != nil {
		return nil, err
	}
	if len(items) == 0 {
		return nil, qbittorrent.ErrNotFound
	}
	if len(items[0].Files) > 200_000 {
		return nil, fmt.Errorf("Transmission torrent file count exceeds 200000")
	}
	result := make([]qbittorrent.TorrentFile, 0, len(items[0].Files))
	for index, item := range items[0].Files {
		progress := float64(0)
		if item.Length == 0 || item.BytesCompleted >= item.Length {
			progress = 1
		} else if item.BytesCompleted > 0 {
			progress = float64(item.BytesCompleted) / float64(item.Length)
		}
		priority, wanted := 0, true
		if index < len(items[0].FileStats) {
			priority, wanted = items[0].FileStats[index].Priority, bool(items[0].FileStats[index].Wanted)
		}
		if !wanted {
			priority = 0
		}
		result = append(result, qbittorrent.TorrentFile{
			Index: index, Name: item.Name, Size: item.Length, Progress: progress,
			Priority: priority, Seed: progress >= 0.999999,
		})
	}
	return result, nil
}

func (client *Client) Add(ctx context.Context, metainfo []byte, options qbittorrent.AddOptions) (qbittorrent.AddResult, error) {
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		return qbittorrent.AddResult{}, err
	}
	if options.UploadLimit < 0 || options.DownloadLimit < 0 {
		return qbittorrent.AddResult{}, fmt.Errorf("Transmission limits must not be negative")
	}
	if options.SkipChecking {
		return qbittorrent.AddResult{}, fmt.Errorf("Transmission does not support skip_checking; verification cannot be bypassed")
	}
	labels := append([]string(nil), options.Tags...)
	if strings.TrimSpace(options.Category) != "" {
		labels = append([]string{strings.TrimSpace(options.Category)}, labels...)
	}
	arguments := map[string]any{
		"metainfo": base64.StdEncoding.EncodeToString(metainfo),
		"paused":   options.Paused,
	}
	if options.SavePath != "" {
		arguments["download-dir"] = options.SavePath
	}
	if len(labels) > 0 {
		arguments["labels"] = uniqueStrings(labels)
	}
	var response struct {
		Added     *addedTorrent `json:"torrentAdded"`
		Duplicate *addedTorrent `json:"torrentDuplicate"`
	}
	if err := client.call(ctx, "torrent-add", arguments, &response); err != nil {
		return qbittorrent.AddResult{}, err
	}
	added := response.Added
	if added == nil {
		added = response.Duplicate
	}
	result := qbittorrent.AddResult{Hashes: hashes}
	addedHash := hashes.V1SHA1
	if added != nil && added.HashString != "" {
		addedHash = added.HashString
	}
	if options.DownloadLimit > 0 || options.UploadLimit > 0 {
		if addedHash == "" {
			return result, &PartialAddError{Hash: "unavailable", Err: errors.New("Transmission did not return a torrent hash for rate-limit application")}
		}
		if err := client.SetLimits(ctx, addedHash, options.DownloadLimit, options.UploadLimit); err != nil {
			return result, &PartialAddError{Hash: strings.ToLower(addedHash), Err: err}
		}
	}
	if addedHash != "" {
		observed, getErr := client.Get(ctx, addedHash)
		if getErr == nil {
			result.Observed = &observed
		} else if !errors.Is(getErr, qbittorrent.ErrNotFound) {
			return qbittorrent.AddResult{}, getErr
		}
	}
	return result, nil
}

type addedTorrent struct {
	HashString string `json:"hashString"`
	ID         int64  `json:"id"`
	Name       string `json:"name"`
}

func (client *Client) SetLimits(ctx context.Context, hash string, downloadBytesPerSecond, uploadBytesPerSecond int64) error {
	if err := validateHash(hash); err != nil {
		return err
	}
	if downloadBytesPerSecond < 0 || uploadBytesPerSecond < 0 {
		return fmt.Errorf("Transmission limits must not be negative")
	}
	arguments := map[string]any{"ids": []string{strings.ToLower(hash)}}
	applyLimitArguments(arguments, "download", downloadBytesPerSecond)
	applyLimitArguments(arguments, "upload", uploadBytesPerSecond)
	return client.call(ctx, "torrent-set", arguments, nil)
}

func (client *Client) WaitComplete(ctx context.Context, hash string, interval time.Duration) (qbittorrent.Torrent, error) {
	if interval <= 0 {
		interval = 5 * time.Second
	}
	for {
		item, err := client.Get(ctx, hash)
		if err != nil {
			return qbittorrent.Torrent{}, err
		}
		if item.Progress >= 1 || (item.TotalSize > 0 && item.AmountLeft == 0) {
			return item, nil
		}
		if strings.HasPrefix(item.State, "error:") {
			return qbittorrent.Torrent{}, fmt.Errorf("Transmission torrent entered state %s", item.State)
		}
		select {
		case <-ctx.Done():
			return qbittorrent.Torrent{}, ctx.Err()
		case <-time.After(interval):
		}
	}
}

func (client *Client) getTorrents(ctx context.Context, ids []string, fields []string) ([]torrent, error) {
	var response struct {
		Torrents []torrent `json:"torrents"`
	}
	if err := client.call(ctx, "torrent-get", map[string]any{"ids": ids, "fields": fields}, &response); err != nil {
		return nil, err
	}
	return response.Torrents, nil
}

func (client *Client) call(ctx context.Context, method string, arguments any, target any) error {
	tag := client.tag.Add(1)
	for attempt := 0; attempt < 2; attempt++ {
		protocol := client.protocol.Load()
		var requestPayload any = rpcRequest{Method: method, Arguments: arguments, Tag: tag}
		if protocol == 1 {
			requestPayload = jsonRPCRequest{
				JSONRPC: "2.0", Method: strings.ReplaceAll(method, "-", "_"),
				Params: jsonRPCArguments(arguments), ID: tag,
			}
		}
		payload, err := json.Marshal(requestPayload)
		if err != nil {
			return fmt.Errorf("encode Transmission %s request: %w", method, err)
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, client.endpoint.String(), bytes.NewReader(payload))
		if err != nil {
			return fmt.Errorf("create Transmission request: %w", err)
		}
		request.Header.Set("Accept", "application/json")
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("User-Agent", "Upload-Assistant/2")
		if value, ok := client.sessionID.Load().(string); ok && value != "" {
			request.Header.Set("X-Transmission-Session-Id", value)
		}
		if client.credentials["username"] != "" {
			request.SetBasicAuth(client.credentials["username"], client.credentials["password"])
		}
		response, err := client.httpClient.Do(request)
		if err != nil {
			return fmt.Errorf("Transmission request failed: %w", err)
		}
		body, readErr := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
		_ = response.Body.Close()
		if readErr != nil {
			return fmt.Errorf("read Transmission response: %w", readErr)
		}
		if len(body) > maxResponseBytes {
			return fmt.Errorf("Transmission response exceeds %d bytes", maxResponseBytes)
		}
		if response.StatusCode == http.StatusConflict && attempt == 0 {
			sessionID := strings.TrimSpace(response.Header.Get("X-Transmission-Session-Id"))
			if sessionID == "" {
				return fmt.Errorf("Transmission session negotiation did not return a session id")
			}
			client.sessionID.Store(sessionID)
			if strings.HasPrefix(strings.TrimSpace(response.Header.Get("X-Transmission-Rpc-Version")), "6.") {
				client.protocol.Store(1)
			}
			continue
		}
		if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
			return qbittorrent.ErrUnauthorized
		}
		if response.StatusCode < 200 || response.StatusCode >= 300 {
			return fmt.Errorf("Transmission RPC returned HTTP %d: %s", response.StatusCode, safeMessage(string(body)))
		}
		var result json.RawMessage
		if protocol == 1 {
			var rpcResult jsonRPCResponse
			if err := json.Unmarshal(body, &rpcResult); err != nil {
				return fmt.Errorf("decode Transmission %s JSON-RPC response: %w", method, err)
			}
			if rpcResult.ID != tag {
				return fmt.Errorf("Transmission %s response id mismatch", method)
			}
			if rpcResult.Error != nil {
				return fmt.Errorf("Transmission %s failed (%d): %s", method, rpcResult.Error.Code, safeMessage(rpcResult.Error.Message))
			}
			result = rpcResult.Result
		} else {
			var rpcResult rpcResponse
			if err := json.Unmarshal(body, &rpcResult); err != nil {
				return fmt.Errorf("decode Transmission %s response: %w", method, err)
			}
			if rpcResult.Tag != tag {
				return fmt.Errorf("Transmission %s response tag mismatch", method)
			}
			if rpcResult.Result != "success" {
				return fmt.Errorf("Transmission %s failed: %s", method, safeMessage(rpcResult.Result))
			}
			result = rpcResult.Arguments
		}
		if target != nil && len(result) > 0 && string(result) != "null" {
			normalized, err := normalizeResponse(result)
			if err != nil {
				return fmt.Errorf("normalize Transmission %s response: %w", method, err)
			}
			if err := json.Unmarshal(normalized, target); err != nil {
				return fmt.Errorf("decode Transmission %s arguments: %w", method, err)
			}
		}
		return nil
	}
	return fmt.Errorf("Transmission session negotiation failed")
}

func jsonRPCArguments(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			newKey := camelToSnake(strings.ReplaceAll(key, "-", "_"))
			if key == "fields" {
				if fields, ok := item.([]string); ok {
					converted := make([]string, len(fields))
					for index, field := range fields {
						converted[index] = camelToSnake(strings.ReplaceAll(field, "-", "_"))
					}
					result[newKey] = converted
					continue
				}
			}
			result[newKey] = jsonRPCArguments(item)
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = jsonRPCArguments(item)
		}
		return result
	default:
		return value
	}
}

func normalizeResponse(raw json.RawMessage) ([]byte, error) {
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return nil, err
	}
	return json.Marshal(canonicalResponseValue(value))
}

func canonicalResponseValue(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			result[delimitedToCamel(key)] = canonicalResponseValue(item)
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = canonicalResponseValue(item)
		}
		return result
	default:
		return value
	}
}

func camelToSnake(value string) string {
	var builder strings.Builder
	for index, current := range value {
		if current >= 'A' && current <= 'Z' {
			if index > 0 {
				builder.WriteByte('_')
			}
			builder.WriteRune(current + ('a' - 'A'))
		} else {
			builder.WriteRune(current)
		}
	}
	return builder.String()
}

func delimitedToCamel(value string) string {
	var builder strings.Builder
	upperNext := false
	for _, current := range value {
		if current == '-' || current == '_' {
			upperNext = true
			continue
		}
		if upperNext && current >= 'a' && current <= 'z' {
			current -= 'a' - 'A'
		}
		upperNext = false
		builder.WriteRune(current)
	}
	return builder.String()
}

func normalizeTorrent(item torrent) qbittorrent.Torrent {
	state := transmissionState(item.Status)
	if strings.TrimSpace(item.ErrorString) != "" {
		state = "error: " + safeMessage(item.ErrorString)
	}
	downloadLimit, uploadLimit := int64(0), int64(0)
	if item.DownloadLimited {
		downloadLimit = item.DownloadLimit * 1000
	}
	if item.UploadLimited {
		uploadLimit = item.UploadLimit * 1000
	}
	completed := item.TotalSize - item.LeftUntilDone
	if completed < 0 {
		completed = 0
	}
	return qbittorrent.Torrent{
		Hash: strings.ToLower(item.HashString), Name: item.Name, State: state, Progress: item.PercentDone,
		Size: item.TotalSize, TotalSize: item.TotalSize, Completed: completed, AmountLeft: item.LeftUntilDone,
		Downloaded: item.DownloadedEver, Uploaded: item.UploadedEver, DownloadSpeed: item.RateDownload, UploadSpeed: item.RateUpload,
		DownloadLimit: downloadLimit, UploadLimit: uploadLimit, Ratio: item.UploadRatio,
		SavePath: item.DownloadDir, ContentPath: path.Join(item.DownloadDir, item.Name), Tags: strings.Join(item.Labels, ","),
		AddedOn: item.AddedDate, CompletionOn: item.DoneDate,
		TimeActive: item.SecondsDownloading + item.SecondsSeeding, SeedingTime: item.SecondsSeeding,
	}
}

func transmissionState(status int) string {
	switch status {
	case 0:
		return "stopped"
	case 1:
		return "check_wait"
	case 2:
		return "checking"
	case 3:
		return "download_wait"
	case 4:
		return "downloading"
	case 5:
		return "seed_wait"
	case 6:
		return "seeding"
	default:
		return fmt.Sprintf("unknown_%d", status)
	}
}

func applyLimitArguments(arguments map[string]any, prefix string, bytesPerSecond int64) {
	limited := bytesPerSecond > 0
	arguments[prefix+"-limited"] = limited
	if limited {
		kilobytes := bytesPerSecond / 1000
		if kilobytes < 1 {
			kilobytes = 1
		}
		arguments[prefix+"-limit"] = kilobytes
	}
}

func validateHash(hash string) error {
	if !hashPattern.MatchString(strings.TrimSpace(hash)) {
		return fmt.Errorf("invalid Transmission torrent hash")
	}
	return nil
}

func uniqueStrings(values []string) []string {
	result := make([]string, 0, len(values))
	seen := map[string]struct{}{}
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}

func safeMessage(message string) string {
	message = strings.TrimSpace(strings.ReplaceAll(message, "\n", " "))
	if len(message) > 300 {
		message = message[:300]
	}
	return message
}
