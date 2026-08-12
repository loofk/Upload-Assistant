package deluge

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"path"
	"regexp"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

const (
	maxResponseBytes = 32 << 20
	maxTorrentBytes  = 40 << 20
)

var hashPattern = regexp.MustCompile(`^[a-fA-F0-9]{40}$`)

var requiredMethods = []string{
	"core.add_torrent_file", "core.get_torrent_status", "core.get_torrents_status", "core.set_torrent_options",
	"daemon.get_version", "daemon.get_method_list", "core.get_libtorrent_version",
}

var statusFields = []string{
	"hash", "name", "state", "progress", "total_size", "total_done", "total_remaining",
	"total_payload_download", "total_uploaded", "download_payload_rate", "upload_payload_rate",
	"max_download_speed", "max_upload_speed", "ratio", "save_path", "time_added", "completed_time",
	"active_time", "seeding_time", "message", "files", "file_progress", "file_priorities", "is_seed",
}

type Config struct {
	Endpoint    string
	Timeout     time.Duration
	Credentials map[string]string
	HTTPClient  *http.Client
}

// PartialAddError means Deluge returned or exposed the exact torrent hash but
// the mandatory post-add observation failed. Retrying add_torrent_file for the
// same metainfo is duplicate-safe, but callers must audit and reconcile first.
type PartialAddError struct {
	Hash string
	Err  error
}

func (err *PartialAddError) Error() string {
	return fmt.Sprintf("Deluge added torrent %s but post-add observation failed: %v", err.Hash, err.Err)
}

func (err *PartialAddError) Unwrap() error       { return err.Err }
func (err *PartialAddError) PartialHash() string { return err.Hash }

type Client struct {
	endpoint   *url.URL
	httpClient *http.Client
	password   string
	requestID  atomic.Int64
	sessionMu  sync.Mutex
	connected  bool
}

type rpcRequest struct {
	Method string `json:"method"`
	Params []any  `json:"params"`
	ID     int64  `json:"id"`
}

type rpcResponse struct {
	Result json.RawMessage `json:"result"`
	Error  json.RawMessage `json:"error"`
	ID     int64           `json:"id"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type torrentStatus struct {
	Hash                 string        `json:"hash"`
	Name                 string        `json:"name"`
	State                string        `json:"state"`
	Progress             float64       `json:"progress"`
	TotalSize            int64         `json:"total_size"`
	TotalDone            int64         `json:"total_done"`
	TotalRemaining       int64         `json:"total_remaining"`
	TotalPayloadDownload int64         `json:"total_payload_download"`
	TotalUploaded        int64         `json:"total_uploaded"`
	DownloadPayloadRate  int64         `json:"download_payload_rate"`
	UploadPayloadRate    int64         `json:"upload_payload_rate"`
	MaxDownloadSpeed     float64       `json:"max_download_speed"`
	MaxUploadSpeed       float64       `json:"max_upload_speed"`
	Ratio                float64       `json:"ratio"`
	SavePath             string        `json:"save_path"`
	TimeAdded            int64         `json:"time_added"`
	CompletedTime        int64         `json:"completed_time"`
	ActiveTime           int64         `json:"active_time"`
	SeedingTime          int64         `json:"seeding_time"`
	Message              string        `json:"message"`
	Files                []torrentFile `json:"files"`
	FileProgress         []float64     `json:"file_progress"`
	FilePriorities       []int         `json:"file_priorities"`
	IsSeed               bool          `json:"is_seed"`
}

type torrentFile struct {
	Index  int    `json:"index"`
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	Offset int64  `json:"offset"`
}

func New(config Config) (*Client, error) {
	endpoint, err := url.Parse(strings.TrimSpace(config.Endpoint))
	if err != nil || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.Host == "" {
		return nil, fmt.Errorf("invalid Deluge Web endpoint")
	}
	if endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return nil, fmt.Errorf("Deluge Web endpoint must not contain credentials, query, or fragment")
	}
	endpoint.Path = strings.TrimRight(endpoint.Path, "/")
	if endpoint.Path != "" && (!path.IsAbs(endpoint.Path) || path.Clean(endpoint.Path) != endpoint.Path) {
		return nil, fmt.Errorf("Deluge Web endpoint path must be normalized")
	}
	if !strings.HasSuffix(endpoint.Path, "/json") {
		endpoint.Path += "/json"
	}
	for field := range config.Credentials {
		if field != "password" {
			return nil, fmt.Errorf("Deluge Web credential field %q is unsupported", field)
		}
	}
	password := config.Credentials["password"]
	if password == "" {
		return nil, fmt.Errorf("%w: Deluge Web password is required", qbittorrent.ErrUnauthorized)
	}
	if config.Timeout <= 0 {
		config.Timeout = 30 * time.Second
	}
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, fmt.Errorf("create Deluge Web cookie jar: %w", err)
	}
	if config.HTTPClient == nil {
		config.HTTPClient = &http.Client{
			Timeout: config.Timeout, Jar: jar,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return errors.New("Deluge Web endpoint redirect is not allowed")
			},
		}
	} else {
		copied := *config.HTTPClient
		copied.Jar = jar
		if copied.Timeout <= 0 {
			copied.Timeout = config.Timeout
		}
		copied.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
			return errors.New("Deluge Web endpoint redirect is not allowed")
		}
		config.HTTPClient = &copied
	}
	return &Client{endpoint: endpoint, httpClient: config.HTTPClient, password: password}, nil
}

func (client *Client) Probe(ctx context.Context) (qbittorrent.ProbeResult, error) {
	if err := client.ensureSession(ctx); err != nil {
		return qbittorrent.ProbeResult{}, err
	}
	var methods []string
	if err := client.callConnected(ctx, "daemon.get_method_list", []any{}, &methods); err != nil {
		return qbittorrent.ProbeResult{}, err
	}
	missing := make([]string, 0)
	for _, method := range requiredMethods {
		if !slices.Contains(methods, method) {
			missing = append(missing, method)
		}
	}
	if len(missing) > 0 {
		return qbittorrent.ProbeResult{}, fmt.Errorf("Deluge daemon is missing required methods: %s", strings.Join(missing, ", "))
	}
	var version, library string
	if err := client.callConnected(ctx, "daemon.get_version", []any{}, &version); err != nil {
		return qbittorrent.ProbeResult{}, err
	}
	if err := client.callConnected(ctx, "core.get_libtorrent_version", []any{}, &library); err != nil {
		return qbittorrent.ProbeResult{}, err
	}
	if strings.TrimSpace(version) == "" || strings.TrimSpace(library) == "" {
		return qbittorrent.ProbeResult{}, fmt.Errorf("Deluge daemon returned incomplete version evidence")
	}
	return qbittorrent.ProbeResult{
		ApplicationVersion: "Deluge " + version,
		WebAPIVersion:      "JSON-RPC v1 (libtorrent " + library + ")",
		Authentication:     "deluge-web-cookie",
	}, nil
}

func (client *Client) Get(ctx context.Context, hash string) (qbittorrent.Torrent, error) {
	status, err := client.status(ctx, hash, statusFields)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	return normalizeTorrent(status)
}

func (client *Client) List(ctx context.Context) ([]qbittorrent.Torrent, error) {
	var statuses map[string]torrentStatus
	if err := client.call(ctx, "core.get_torrents_status", []any{map[string]any{}, statusFields}, &statuses); err != nil {
		return nil, err
	}
	if len(statuses) > 100_000 {
		return nil, fmt.Errorf("Deluge torrent count exceeds 100000")
	}
	result := make([]qbittorrent.Torrent, 0, len(statuses))
	for hash, status := range statuses {
		if status.Hash == "" {
			status.Hash = hash
		}
		item, err := normalizeTorrent(status)
		if err != nil {
			return nil, fmt.Errorf("normalize Deluge torrent: %w", err)
		}
		result = append(result, item)
	}
	return result, nil
}

func (client *Client) Files(ctx context.Context, hash string) ([]qbittorrent.TorrentFile, error) {
	status, err := client.status(ctx, hash, []string{"hash", "files", "file_progress", "file_priorities"})
	if err != nil {
		return nil, err
	}
	if len(status.Files) == 0 || len(status.Files) > 200_000 {
		return nil, fmt.Errorf("Deluge torrent file count is invalid")
	}
	if len(status.FileProgress) != len(status.Files) || len(status.FilePriorities) != len(status.Files) {
		return nil, fmt.Errorf("Deluge file evidence arrays have inconsistent lengths")
	}
	result := make([]qbittorrent.TorrentFile, 0, len(status.Files))
	seen := make(map[int]struct{}, len(status.Files))
	for position, item := range status.Files {
		if item.Index < 0 || item.Size < 0 || item.Offset < 0 || item.Index >= len(status.Files) {
			return nil, fmt.Errorf("Deluge file row %d is invalid", position)
		}
		if _, exists := seen[item.Index]; exists {
			return nil, fmt.Errorf("Deluge returned duplicate file index %d", item.Index)
		}
		seen[item.Index] = struct{}{}
		name, err := normalizeFilePath(item.Path)
		if err != nil {
			return nil, fmt.Errorf("Deluge file row %d: %w", position, err)
		}
		progress := status.FileProgress[item.Index]
		if math.IsNaN(progress) || math.IsInf(progress, 0) || progress < 0 || progress > 1 {
			return nil, fmt.Errorf("Deluge file row %d has invalid progress", position)
		}
		priority := status.FilePriorities[item.Index]
		if priority < 0 || priority > 7 {
			return nil, fmt.Errorf("Deluge file row %d has invalid priority", position)
		}
		result = append(result, qbittorrent.TorrentFile{
			Index: item.Index, Name: name, Size: item.Size, Progress: progress,
			Priority: priority, Seed: progress >= 0.999999,
		})
	}
	slices.SortFunc(result, func(left, right qbittorrent.TorrentFile) int { return left.Index - right.Index })
	return result, nil
}

func (client *Client) Add(ctx context.Context, metainfo []byte, options qbittorrent.AddOptions) (qbittorrent.AddResult, error) {
	if len(metainfo) > maxTorrentBytes {
		return qbittorrent.AddResult{}, fmt.Errorf("Deluge torrent metainfo exceeds %d bytes", maxTorrentBytes)
	}
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		return qbittorrent.AddResult{}, err
	}
	result := qbittorrent.AddResult{Hashes: hashes}
	if hashes.V1SHA1 == "" {
		return result, fmt.Errorf("Deluge runtime requires a v1 infohash; v2-only torrents are not supported")
	}
	if options.DownloadLimit < 0 || options.UploadLimit < 0 {
		return result, fmt.Errorf("Deluge limits must not be negative")
	}
	if strings.TrimSpace(options.SavePath) != "" && (!path.IsAbs(options.SavePath) || path.Clean(options.SavePath) != options.SavePath) {
		return result, fmt.Errorf("Deluge save path must be a normalized absolute path")
	}
	if options.SkipChecking {
		return result, fmt.Errorf("Deluge seed_mode is never enabled; skip_checking cannot be requested")
	}
	if strings.TrimSpace(options.Category) != "" || len(nonEmptyStrings(options.Tags)) > 0 {
		return result, fmt.Errorf("Deluge core has no category or tag contract; configure a workflow without those fields")
	}
	downloadLimit, err := bytesToKiB(options.DownloadLimit)
	if err != nil {
		return result, err
	}
	uploadLimit, err := bytesToKiB(options.UploadLimit)
	if err != nil {
		return result, err
	}
	addOptions := map[string]any{
		"add_paused": options.Paused, "seed_mode": false,
		"max_download_speed": downloadLimit, "max_upload_speed": uploadLimit,
	}
	if strings.TrimSpace(options.SavePath) != "" {
		addOptions["download_location"] = options.SavePath
	}
	var observedHash *string
	if err := client.call(ctx, "core.add_torrent_file", []any{
		strings.ToLower(hashes.V1SHA1) + ".torrent", base64.StdEncoding.EncodeToString(metainfo), addOptions,
	}, &observedHash); err != nil {
		return result, err
	}
	hash := strings.ToLower(hashes.V1SHA1)
	if observedHash != nil && *observedHash != "" && !strings.EqualFold(*observedHash, hash) {
		return result, &PartialAddError{Hash: hash, Err: fmt.Errorf("Deluge returned an unexpected torrent hash")}
	}
	observed, err := client.Get(ctx, hash)
	if err != nil {
		return result, &PartialAddError{Hash: hash, Err: err}
	}
	if strings.TrimSpace(options.SavePath) != "" && observed.SavePath != options.SavePath {
		return result, &PartialAddError{Hash: hash, Err: fmt.Errorf("Deluge observed save path does not match the requested path")}
	}
	if err := verifyLimit("download", options.DownloadLimit, observed.DownloadLimit); err != nil {
		return result, &PartialAddError{Hash: hash, Err: err}
	}
	if err := verifyLimit("upload", options.UploadLimit, observed.UploadLimit); err != nil {
		return result, &PartialAddError{Hash: hash, Err: err}
	}
	result.Observed = &observed
	return result, nil
}

func (client *Client) SetLimits(ctx context.Context, hash string, downloadBytesPerSecond, uploadBytesPerSecond int64) error {
	if err := validateHash(hash); err != nil {
		return err
	}
	if downloadBytesPerSecond < 0 || uploadBytesPerSecond < 0 {
		return fmt.Errorf("Deluge limits must not be negative")
	}
	downloadLimit, err := bytesToKiB(downloadBytesPerSecond)
	if err != nil {
		return err
	}
	uploadLimit, err := bytesToKiB(uploadBytesPerSecond)
	if err != nil {
		return err
	}
	if err := client.call(ctx, "core.set_torrent_options", []any{
		[]string{strings.ToLower(hash)}, map[string]any{"max_download_speed": downloadLimit, "max_upload_speed": uploadLimit},
	}, nil); err != nil {
		return err
	}
	observed, err := client.Get(ctx, hash)
	if err != nil {
		return err
	}
	if err := verifyLimit("download", downloadBytesPerSecond, observed.DownloadLimit); err != nil {
		return err
	}
	return verifyLimit("upload", uploadBytesPerSecond, observed.UploadLimit)
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
			return qbittorrent.Torrent{}, fmt.Errorf("Deluge torrent entered state %s", item.State)
		}
		select {
		case <-ctx.Done():
			return qbittorrent.Torrent{}, ctx.Err()
		case <-time.After(interval):
		}
	}
}

func (client *Client) status(ctx context.Context, hash string, fields []string) (torrentStatus, error) {
	if err := validateHash(hash); err != nil {
		return torrentStatus{}, err
	}
	var status torrentStatus
	if err := client.call(ctx, "core.get_torrent_status", []any{strings.ToLower(hash), fields}, &status); err != nil {
		return torrentStatus{}, err
	}
	if status.Hash == "" {
		return torrentStatus{}, qbittorrent.ErrNotFound
	}
	if !strings.EqualFold(status.Hash, hash) {
		return torrentStatus{}, fmt.Errorf("Deluge returned status for an unexpected torrent hash")
	}
	return status, nil
}

func normalizeTorrent(status torrentStatus) (qbittorrent.Torrent, error) {
	if status.TotalSize < 0 || status.TotalDone < 0 || status.TotalRemaining < 0 || status.TotalDone > status.TotalSize ||
		status.Progress < 0 || status.Progress > 100 || math.IsNaN(status.Progress) || math.IsInf(status.Progress, 0) {
		return qbittorrent.Torrent{}, fmt.Errorf("Deluge returned invalid torrent size or progress evidence")
	}
	downloadLimit, err := kiBToBytes(status.MaxDownloadSpeed)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	uploadLimit, err := kiBToBytes(status.MaxUploadSpeed)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	state := strings.ToLower(strings.TrimSpace(status.State))
	if state == "" {
		state = "unknown"
	}
	if state == "error" {
		state = "error: " + safeMessage(status.Message)
	}
	contentPath, err := contentPath(status)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	ratio := status.Ratio
	if math.IsNaN(ratio) || math.IsInf(ratio, 0) {
		return qbittorrent.Torrent{}, fmt.Errorf("Deluge returned invalid ratio evidence")
	}
	if ratio < 0 {
		ratio = 0
	}
	return qbittorrent.Torrent{
		Hash: strings.ToLower(status.Hash), Name: status.Name, State: state, Progress: status.Progress / 100,
		Size: status.TotalSize, TotalSize: status.TotalSize, Completed: status.TotalDone, AmountLeft: status.TotalRemaining,
		Downloaded: status.TotalPayloadDownload, Uploaded: status.TotalUploaded,
		DownloadSpeed: status.DownloadPayloadRate, UploadSpeed: status.UploadPayloadRate,
		DownloadLimit: downloadLimit, UploadLimit: uploadLimit, Ratio: ratio,
		SavePath: status.SavePath, ContentPath: contentPath,
		AddedOn: status.TimeAdded, CompletionOn: status.CompletedTime,
		TimeActive: status.ActiveTime, SeedingTime: status.SeedingTime,
	}, nil
}

func contentPath(status torrentStatus) (string, error) {
	if !path.IsAbs(status.SavePath) || path.Clean(status.SavePath) != status.SavePath {
		return "", fmt.Errorf("Deluge save path is not a normalized absolute path")
	}
	for _, item := range status.Files {
		if _, err := normalizeFilePath(item.Path); err != nil {
			return "", err
		}
	}
	if strings.TrimSpace(status.Name) == "" || path.Base(status.Name) != status.Name {
		return "", fmt.Errorf("Deluge torrent name is invalid")
	}
	return path.Join(status.SavePath, status.Name), nil
}

func (client *Client) call(ctx context.Context, method string, params []any, target any) error {
	if err := client.ensureSession(ctx); err != nil {
		return err
	}
	return client.callConnected(ctx, method, params, target)
}

func (client *Client) ensureSession(ctx context.Context) error {
	client.sessionMu.Lock()
	defer client.sessionMu.Unlock()
	if client.connected {
		return nil
	}
	var authenticated bool
	if err := client.callRaw(ctx, "auth.login", []any{client.password}, &authenticated); err != nil {
		return err
	}
	if !authenticated {
		return qbittorrent.ErrUnauthorized
	}
	var connected bool
	if err := client.callRaw(ctx, "web.connected", []any{}, &connected); err != nil {
		return err
	}
	if !connected {
		return fmt.Errorf("Deluge Web is authenticated but not connected to a daemon")
	}
	client.connected = true
	return nil
}

func (client *Client) callConnected(ctx context.Context, method string, params []any, target any) error {
	return client.callRaw(ctx, method, params, target)
}

func (client *Client) callRaw(ctx context.Context, method string, params []any, target any) error {
	id := client.requestID.Add(1)
	payload, err := json.Marshal(rpcRequest{Method: method, Params: params, ID: id})
	if err != nil {
		return fmt.Errorf("encode Deluge %s request: %w", method, err)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, client.endpoint.String(), bytes.NewReader(payload))
	if err != nil {
		return fmt.Errorf("create Deluge request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", "Upload-Assistant/2")
	response, err := client.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("Deluge request failed: %w", err)
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	_ = response.Body.Close()
	if readErr != nil {
		return fmt.Errorf("read Deluge response: %w", readErr)
	}
	if len(body) > maxResponseBytes {
		return fmt.Errorf("Deluge response exceeds %d bytes", maxResponseBytes)
	}
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return qbittorrent.ErrUnauthorized
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("Deluge JSON-RPC returned HTTP %d: %s", response.StatusCode, safeMessage(string(body)))
	}
	var rpcResult rpcResponse
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&rpcResult); err != nil {
		return fmt.Errorf("decode Deluge %s response: %w", method, err)
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return fmt.Errorf("decode Deluge %s response: trailing JSON value", method)
	}
	if rpcResult.ID != id {
		return fmt.Errorf("Deluge %s response id mismatch", method)
	}
	if len(rpcResult.Error) > 0 && string(rpcResult.Error) != "null" {
		var detail rpcError
		if err := json.Unmarshal(rpcResult.Error, &detail); err != nil {
			return fmt.Errorf("Deluge %s returned an invalid error", method)
		}
		return mapRPCError(method, detail)
	}
	if target != nil {
		if len(rpcResult.Result) == 0 {
			return fmt.Errorf("Deluge %s response omitted result", method)
		}
		if err := json.Unmarshal(rpcResult.Result, target); err != nil {
			return fmt.Errorf("decode Deluge %s result: %w", method, err)
		}
	}
	return nil
}

func mapRPCError(method string, detail rpcError) error {
	message := strings.ToLower(detail.Message)
	if strings.Contains(message, "unknown torrent") || strings.Contains(message, "torrent") && strings.Contains(message, "not found") || strings.Contains(message, "keyerror") {
		return fmt.Errorf("%w: %s", qbittorrent.ErrNotFound, safeMessage(detail.Message))
	}
	if strings.Contains(message, "not authenticated") || strings.Contains(message, "authentication") {
		return qbittorrent.ErrUnauthorized
	}
	return fmt.Errorf("Deluge %s failed (%d): %s", method, detail.Code, safeMessage(detail.Message))
}

func bytesToKiB(value int64) (float64, error) {
	if value == 0 {
		return -1, nil
	}
	if value < 0 || value > 1<<53 {
		return 0, fmt.Errorf("Deluge limit is outside the exactly representable range")
	}
	return float64(value) / 1024, nil
}

func kiBToBytes(value float64) (int64, error) {
	if value < 0 {
		return 0, nil
	}
	if math.IsNaN(value) || math.IsInf(value, 0) || value > float64(int64(1)<<53)/1024 {
		return 0, fmt.Errorf("Deluge returned an invalid rate limit")
	}
	return int64(math.Round(value * 1024)), nil
}

func verifyLimit(kind string, requested, observed int64) error {
	if requested == 0 {
		if observed != 0 {
			return fmt.Errorf("Deluge %s limit expected unlimited but observed %d bytes per second", kind, observed)
		}
		return nil
	}
	if observed <= 0 || observed > requested {
		return fmt.Errorf("Deluge %s limit observed %d bytes per second, outside requested cap %d", kind, observed, requested)
	}
	return nil
}

func normalizeFilePath(value string) (string, error) {
	value = strings.ReplaceAll(strings.TrimSpace(value), "\\", "/")
	if value == "" || strings.HasPrefix(value, "/") || path.Clean(value) != value || value == "." || strings.HasPrefix(value, "../") {
		return "", fmt.Errorf("file path is not a normalized relative path")
	}
	return value, nil
}

func validateHash(hash string) error {
	if !hashPattern.MatchString(strings.TrimSpace(hash)) {
		return fmt.Errorf("invalid Deluge v1 torrent hash")
	}
	return nil
}

func nonEmptyStrings(values []string) []string {
	result := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			result = append(result, strings.TrimSpace(value))
		}
	}
	return result
}

func safeMessage(message string) string {
	message = strings.TrimSpace(strings.NewReplacer("\r", " ", "\n", " ", "\t", " ").Replace(message))
	if len(message) > 300 {
		message = message[:300]
	}
	return message
}
