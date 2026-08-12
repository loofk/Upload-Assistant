package downloaders

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/deluge"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/rtorrent"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/transmission"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var ErrAdapterUnavailable = errors.New("downloader adapter is not implemented")

var torrentHashPattern = regexp.MustCompile(`^[a-fA-F0-9]{40}([a-fA-F0-9]{24})?$`)

type ConfigurationStore interface {
	GetRuntimeDownloader(context.Context, string) (integrations.RuntimeDownloader, error)
	RecordDownloaderHealth(context.Context, string, string, map[string]any, workflow.Actor) error
	AuditDownloaderAction(context.Context, string, string, map[string]any, workflow.Actor) error
}

type Manager struct {
	store             ConfigurationStore
	dashboardMu       sync.Mutex
	dashboardCache    map[string]dashboardCacheEntry
	dashboardInflight map[string]*dashboardCall
}

type torrentClient interface {
	Probe(context.Context) (qbittorrent.ProbeResult, error)
	Get(context.Context, string) (qbittorrent.Torrent, error)
	List(context.Context) ([]qbittorrent.Torrent, error)
	Files(context.Context, string) ([]qbittorrent.TorrentFile, error)
	Add(context.Context, []byte, qbittorrent.AddOptions) (qbittorrent.AddResult, error)
	SetLimits(context.Context, string, int64, int64) error
	WaitComplete(context.Context, string, time.Duration) (qbittorrent.Torrent, error)
}

const dashboardCacheTTL = 3 * time.Second

type DashboardQuery struct {
	Filter string
	Query  string
	Offset int
	Limit  int
}

type DashboardSummary struct {
	Total         int   `json:"total"`
	Downloading   int   `json:"downloading"`
	Seeding       int   `json:"seeding"`
	Paused        int   `json:"paused"`
	Checking      int   `json:"checking"`
	Errors        int   `json:"errors"`
	Active        int   `json:"active"`
	DownloadSpeed int64 `json:"download_speed"`
	UploadSpeed   int64 `json:"upload_speed"`
}

type DashboardTorrent struct {
	Hash            string  `json:"hash"`
	Name            string  `json:"name"`
	State           string  `json:"state"`
	StateGroup      string  `json:"state_group"`
	Progress        float64 `json:"progress"`
	TotalSize       int64   `json:"total_size"`
	AmountLeft      int64   `json:"amount_left"`
	Downloaded      int64   `json:"downloaded"`
	Uploaded        int64   `json:"uploaded"`
	DownloadSpeed   int64   `json:"download_speed"`
	UploadSpeed     int64   `json:"upload_speed"`
	DownloadLimit   int64   `json:"download_limit"`
	UploadLimit     int64   `json:"upload_limit"`
	LimitsAvailable bool    `json:"limits_available"`
	Ratio           float64 `json:"ratio"`
	Category        string  `json:"category,omitempty"`
	Tags            string  `json:"tags,omitempty"`
	AddedOn         int64   `json:"added_on"`
	CompletionOn    int64   `json:"completion_on"`
	TimeActive      int64   `json:"time_active"`
	SeedingTime     int64   `json:"seeding_time"`
}

type DashboardSnapshot struct {
	DownloaderName string             `json:"downloader_name"`
	Adapter        string             `json:"adapter"`
	NetworkClass   string             `json:"network_class"`
	FetchedAt      time.Time          `json:"fetched_at"`
	Summary        DashboardSummary   `json:"summary"`
	Torrents       []DashboardTorrent `json:"torrents"`
	FilteredTotal  int                `json:"filtered_total"`
	Offset         int                `json:"offset"`
	Limit          int                `json:"limit"`
	HasMore        bool               `json:"has_more"`
}

type dashboardCacheEntry struct {
	fetchedAt time.Time
	runtime   integrations.RuntimeDownloader
	torrents  []qbittorrent.Torrent
}

type dashboardCall struct {
	done  chan struct{}
	entry dashboardCacheEntry
	err   error
}

type TorrentEvidence struct {
	DownloaderName      string                    `json:"downloader_name"`
	Adapter             string                    `json:"adapter"`
	NetworkClass        string                    `json:"network_class"`
	ConfigurationSHA256 string                    `json:"configuration_sha256"`
	Torrent             qbittorrent.Torrent       `json:"torrent"`
	RemoteSavePath      string                    `json:"remote_save_path"`
	RemoteContentPath   string                    `json:"remote_content_path"`
	LocalContentPath    string                    `json:"local_content_path,omitempty"`
	PathMapping         *integrations.PathMapping `json:"path_mapping,omitempty"`
}

// Profile is the non-secret, operator-reviewed downloader identity used by
// workflow policy. NetworkClass is never inferred from a host name or IP.
type Profile struct {
	DownloaderName      string `json:"downloader_name"`
	Adapter             string `json:"adapter"`
	NetworkClass        string `json:"network_class"`
	ConfigurationSHA256 string `json:"configuration_sha256"`
}

type AddEvidence struct {
	DownloaderName      string                 `json:"downloader_name"`
	Adapter             string                 `json:"adapter"`
	NetworkClass        string                 `json:"network_class"`
	ConfigurationSHA256 string                 `json:"configuration_sha256"`
	SeedboxUploadLimit  int64                  `json:"seedbox_upload_limit_bytes_per_second"`
	AppliedUploadLimit  int64                  `json:"applied_upload_limit_bytes_per_second"`
	TorrentBytes        int                    `json:"torrent_bytes"`
	TorrentSHA256       string                 `json:"torrent_sha256"`
	ExpectedHashes      torrentmeta.InfoHashes `json:"expected_hashes"`
	Result              qbittorrent.AddResult  `json:"result"`
	Observed            *TorrentEvidence       `json:"observed,omitempty"`
}

type TorrentFilesEvidence struct {
	DownloaderName string                    `json:"downloader_name"`
	Adapter        string                    `json:"adapter"`
	Torrent        TorrentEvidence           `json:"torrent"`
	Files          []qbittorrent.TorrentFile `json:"files"`
	FileCount      int                       `json:"file_count"`
	TotalSize      int64                     `json:"total_size"`
}

func NewManager(store ConfigurationStore) *Manager {
	return &Manager{
		store: store, dashboardCache: map[string]dashboardCacheEntry{}, dashboardInflight: map[string]*dashboardCall{},
	}
}

func (manager *Manager) Dashboard(ctx context.Context, name string, query DashboardQuery) (DashboardSnapshot, error) {
	query.Filter = strings.ToLower(strings.TrimSpace(query.Filter))
	query.Query = strings.TrimSpace(query.Query)
	if query.Filter == "" {
		query.Filter = "all"
	}
	if !isDashboardFilter(query.Filter) {
		return DashboardSnapshot{}, fmt.Errorf("invalid downloader dashboard filter")
	}
	if utf8.RuneCountInString(query.Query) > 200 {
		return DashboardSnapshot{}, fmt.Errorf("downloader dashboard query exceeds 200 characters")
	}
	if query.Offset < 0 {
		return DashboardSnapshot{}, fmt.Errorf("downloader dashboard offset must not be negative")
	}
	if query.Limit == 0 {
		query.Limit = 100
	}
	if query.Limit < 1 || query.Limit > 200 {
		return DashboardSnapshot{}, fmt.Errorf("downloader dashboard limit must be between 1 and 200")
	}

	entry, err := manager.dashboardEntry(ctx, strings.TrimSpace(name))
	if err != nil {
		return DashboardSnapshot{}, err
	}
	summary := summarizeDashboard(entry.torrents)
	needle := strings.ToLower(query.Query)
	filtered := make([]qbittorrent.Torrent, 0, len(entry.torrents))
	for _, torrent := range entry.torrents {
		group := dashboardStateGroup(torrent)
		if query.Filter != "all" && query.Filter != group && !(query.Filter == "active" && (torrent.DownloadSpeed > 0 || torrent.UploadSpeed > 0)) {
			continue
		}
		if needle != "" && !strings.Contains(strings.ToLower(strings.Join([]string{torrent.Name, torrent.Hash, torrent.Category, torrent.Tags}, " ")), needle) {
			continue
		}
		filtered = append(filtered, torrent)
	}
	sort.SliceStable(filtered, func(left, right int) bool {
		if filtered[left].AddedOn == filtered[right].AddedOn {
			return strings.ToLower(filtered[left].Name) < strings.ToLower(filtered[right].Name)
		}
		return filtered[left].AddedOn > filtered[right].AddedOn
	})
	filteredTotal := len(filtered)
	start := query.Offset
	if start > filteredTotal {
		start = filteredTotal
	}
	end := start + query.Limit
	if end > filteredTotal {
		end = filteredTotal
	}
	items := make([]DashboardTorrent, 0, end-start)
	for _, torrent := range filtered[start:end] {
		items = append(items, dashboardTorrent(torrent, entry.runtime.Adapter))
	}
	return DashboardSnapshot{
		DownloaderName: entry.runtime.Name, Adapter: entry.runtime.Adapter, NetworkClass: entry.runtime.NetworkClass,
		FetchedAt: entry.fetchedAt, Summary: summary, Torrents: items, FilteredTotal: filteredTotal,
		Offset: start, Limit: query.Limit, HasMore: end < filteredTotal,
	}, nil
}

func (manager *Manager) dashboardEntry(ctx context.Context, name string) (dashboardCacheEntry, error) {
	now := time.Now().UTC()
	manager.dashboardMu.Lock()
	if entry, ok := manager.dashboardCache[name]; ok && now.Sub(entry.fetchedAt) < dashboardCacheTTL {
		manager.dashboardMu.Unlock()
		return entry, nil
	}
	if call, ok := manager.dashboardInflight[name]; ok {
		manager.dashboardMu.Unlock()
		select {
		case <-ctx.Done():
			return dashboardCacheEntry{}, ctx.Err()
		case <-call.done:
			return call.entry, call.err
		}
	}
	call := &dashboardCall{done: make(chan struct{})}
	manager.dashboardInflight[name] = call
	manager.dashboardMu.Unlock()

	runtime, client, err := manager.client(ctx, name)
	var torrents []qbittorrent.Torrent
	if err == nil {
		torrents, err = client.List(ctx)
	}
	entry := dashboardCacheEntry{fetchedAt: time.Now().UTC(), runtime: runtime, torrents: torrents}

	manager.dashboardMu.Lock()
	call.entry, call.err = entry, err
	if err == nil {
		manager.dashboardCache[name] = entry
	}
	delete(manager.dashboardInflight, name)
	close(call.done)
	manager.dashboardMu.Unlock()
	return entry, err
}

func isDashboardFilter(value string) bool {
	switch value {
	case "all", "downloading", "seeding", "paused", "checking", "error", "active", "completed":
		return true
	default:
		return false
	}
}

func summarizeDashboard(torrents []qbittorrent.Torrent) DashboardSummary {
	summary := DashboardSummary{Total: len(torrents)}
	for _, torrent := range torrents {
		switch dashboardStateGroup(torrent) {
		case "downloading":
			summary.Downloading++
		case "seeding":
			summary.Seeding++
		case "paused":
			summary.Paused++
		case "checking":
			summary.Checking++
		case "error":
			summary.Errors++
		}
		if torrent.DownloadSpeed > 0 || torrent.UploadSpeed > 0 {
			summary.Active++
		}
		summary.DownloadSpeed = saturatingAdd(summary.DownloadSpeed, torrent.DownloadSpeed)
		summary.UploadSpeed = saturatingAdd(summary.UploadSpeed, torrent.UploadSpeed)
	}
	return summary
}

func dashboardStateGroup(torrent qbittorrent.Torrent) string {
	state := strings.ToLower(strings.TrimSpace(torrent.State))
	switch {
	case strings.Contains(state, "error") || strings.Contains(state, "missing"):
		return "error"
	case strings.Contains(state, "check") || strings.Contains(state, "verify"):
		return "checking"
	case strings.Contains(state, "pause") || strings.Contains(state, "stop"):
		return "paused"
	case torrent.Progress >= 0.999999 || strings.Contains(state, "seed") || strings.Contains(state, "upload"):
		return "seeding"
	case strings.Contains(state, "download") || strings.Contains(state, "meta") || torrent.Progress < 0.999999:
		return "downloading"
	default:
		return "completed"
	}
}

func dashboardTorrent(torrent qbittorrent.Torrent, adapter string) DashboardTorrent {
	state := strings.ToLower(strings.TrimSpace(torrent.State))
	if strings.Contains(state, "error") || strings.Contains(state, "missing") {
		state = "error"
	}
	return DashboardTorrent{
		Hash: torrent.Hash, Name: torrent.Name, State: state, StateGroup: dashboardStateGroup(torrent),
		Progress: safeProgress(torrent.Progress), TotalSize: nonNegative(torrent.TotalSize), AmountLeft: nonNegative(torrent.AmountLeft),
		Downloaded: nonNegative(torrent.Downloaded), Uploaded: nonNegative(torrent.Uploaded),
		DownloadSpeed: nonNegative(torrent.DownloadSpeed), UploadSpeed: nonNegative(torrent.UploadSpeed),
		DownloadLimit: nonNegative(torrent.DownloadLimit), UploadLimit: nonNegative(torrent.UploadLimit), LimitsAvailable: adapter != "rtorrent", Ratio: safeRatio(torrent.Ratio),
		Category: torrent.Category, Tags: torrent.Tags, AddedOn: nonNegative(torrent.AddedOn), CompletionOn: nonNegative(torrent.CompletionOn),
		TimeActive: nonNegative(torrent.TimeActive), SeedingTime: nonNegative(torrent.SeedingTime),
	}
}

func nonNegative(value int64) int64 {
	if value < 0 {
		return 0
	}
	return value
}

func safeProgress(value float64) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
		return 0
	}
	if value > 1 {
		return 1
	}
	return value
}

func safeRatio(value float64) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
		return 0
	}
	return value
}

func saturatingAdd(current, value int64) int64 {
	if value <= 0 {
		return current
	}
	const maxInt64 = int64(^uint64(0) >> 1)
	if current > maxInt64-value {
		return maxInt64
	}
	return current + value
}

func (manager *Manager) Profile(ctx context.Context, name string) (Profile, error) {
	runtime, err := manager.store.GetRuntimeDownloader(ctx, name)
	if err != nil {
		return Profile{}, err
	}
	return Profile{
		DownloaderName: runtime.Name, Adapter: runtime.Adapter, NetworkClass: runtime.NetworkClass,
		ConfigurationSHA256: runtime.ConfigurationSHA256,
	}, nil
}

func (manager *Manager) Probe(ctx context.Context, name string, actor workflow.Actor) (qbittorrent.ProbeResult, error) {
	runtime, client, err := manager.client(ctx, name)
	if err != nil {
		return qbittorrent.ProbeResult{}, err
	}
	result, probeErr := client.Probe(ctx)
	details := map[string]any{"adapter": runtime.Adapter}
	status := "ready"
	if probeErr != nil {
		status = "failed"
		details["error"] = safeError(probeErr)
	} else {
		details["application_version"] = result.ApplicationVersion
		details["webapi_version"] = result.WebAPIVersion
		details["authentication"] = result.Authentication
	}
	manager.recordHealth(ctx, name, status, details, actor)
	return result, probeErr
}

func (manager *Manager) Inspect(ctx context.Context, name, hash string, actor workflow.Actor) (TorrentEvidence, error) {
	runtime, client, err := manager.client(ctx, name)
	if err != nil {
		return TorrentEvidence{}, err
	}
	torrent, err := client.Get(ctx, hash)
	if err != nil {
		return TorrentEvidence{}, err
	}
	evidence := buildTorrentEvidence(runtime, torrent)
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.inspect", map[string]any{
		"hash": torrent.Hash, "state": torrent.State, "progress": torrent.Progress,
		"remote_content_path": evidence.RemoteContentPath, "local_content_path": evidence.LocalContentPath,
	}, actor); err != nil {
		return TorrentEvidence{}, err
	}
	return evidence, nil
}

func (manager *Manager) Files(ctx context.Context, name, hash string, actor workflow.Actor) (TorrentFilesEvidence, error) {
	runtime, client, err := manager.client(ctx, name)
	if err != nil {
		return TorrentFilesEvidence{}, err
	}
	torrent, err := client.Get(ctx, hash)
	if err != nil {
		return TorrentFilesEvidence{}, err
	}
	files, err := client.Files(ctx, hash)
	if err != nil {
		return TorrentFilesEvidence{}, err
	}
	var totalSize int64
	completeFiles := 0
	for _, file := range files {
		if file.Size < 0 || totalSize > int64(^uint64(0)>>1)-file.Size {
			return TorrentFilesEvidence{}, fmt.Errorf("downloader file sizes are invalid")
		}
		totalSize += file.Size
		if file.Progress >= 0.999999 {
			completeFiles++
		}
	}
	evidence := TorrentFilesEvidence{
		DownloaderName: runtime.Name, Adapter: runtime.Adapter,
		Torrent: buildTorrentEvidence(runtime, torrent), Files: files,
		FileCount: len(files), TotalSize: totalSize,
	}
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.files", map[string]any{
		"hash": torrent.Hash, "file_count": len(files), "complete_file_count": completeFiles,
		"total_size": totalSize, "remote_content_path": torrent.ContentPath,
		"local_content_path": evidence.Torrent.LocalContentPath,
	}, actor); err != nil {
		return TorrentFilesEvidence{}, err
	}
	return evidence, nil
}

func (manager *Manager) Add(ctx context.Context, name string, metainfo []byte, options qbittorrent.AddOptions, actor workflow.Actor) (AddEvidence, error) {
	runtime, client, err := manager.client(ctx, name)
	if err != nil {
		return AddEvidence{}, err
	}
	if options.SeedboxUploadLimit < 0 {
		return AddEvidence{}, fmt.Errorf("seedbox upload limit must not be negative")
	}
	if runtime.NetworkClass == "seedbox" && options.SeedboxUploadLimit > 0 &&
		(options.UploadLimit == 0 || options.SeedboxUploadLimit < options.UploadLimit) {
		options.UploadLimit = options.SeedboxUploadLimit
	}
	applyConfiguredDefaults(runtime.EndpointConfig.Options, &options)
	if err := validateAddCapabilities(runtime.AdapterCapability, options); err != nil {
		return AddEvidence{}, err
	}
	inspection, err := torrentmeta.Inspect(metainfo)
	if err != nil {
		return AddEvidence{}, err
	}
	torrentSHA := sha256.Sum256(metainfo)
	evidence := AddEvidence{
		DownloaderName: runtime.Name, Adapter: runtime.Adapter, NetworkClass: runtime.NetworkClass,
		ConfigurationSHA256: runtime.ConfigurationSHA256,
		SeedboxUploadLimit:  options.SeedboxUploadLimit, AppliedUploadLimit: options.UploadLimit,
		TorrentBytes: len(metainfo), TorrentSHA256: hex.EncodeToString(torrentSHA[:]),
		ExpectedHashes: inspection.Hashes, Result: qbittorrent.AddResult{Hashes: inspection.Hashes},
	}
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.add_intent", map[string]any{
		"torrent_bytes": evidence.TorrentBytes, "torrent_sha256": evidence.TorrentSHA256,
		"configuration_sha256": evidence.ConfigurationSHA256,
		"v1_infohash":          inspection.Hashes.V1SHA1, "v2_infohash": inspection.Hashes.V2SHA256,
		"save_path": options.SavePath, "category": options.Category, "tags": options.Tags,
		"apply_labels": addLabelsEnabled(options), "skip_checking": options.SkipChecking, "paused": options.Paused,
		"download_limit": options.DownloadLimit, "upload_limit": options.UploadLimit,
		"network_class": runtime.NetworkClass, "seedbox_upload_limit": options.SeedboxUploadLimit,
	}, actor); err != nil {
		return AddEvidence{}, fmt.Errorf("persist downloader add intent: %w", err)
	}
	result, err := client.Add(ctx, metainfo, options)
	if err != nil {
		evidence.Result = mergeAddResult(evidence.Result, result)
		if partialHash, partial := PartialAddHash(err); partial {
			auditCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
			defer cancel()
			_ = manager.store.AuditDownloaderAction(auditCtx, name, "torrent.add_partial", map[string]any{
				"observed_hash": partialHash, "v1_infohash": result.Hashes.V1SHA1, "v2_infohash": result.Hashes.V2SHA256,
				"download_limit": options.DownloadLimit, "upload_limit": options.UploadLimit,
				"apply_labels":            addLabelsEnabled(options),
				"reconciliation_required": true, "error": safeError(err),
			}, actor)
			return evidence, err
		}
		if errors.Is(err, qbittorrent.ErrUnauthorized) {
			return AddEvidence{}, err
		}
		return evidence, fmt.Errorf("%w: downloader request ended without a trustworthy add result: %w", ErrAddOutcomeUnknown, err)
	}
	evidence.Result = result
	if evidence.Result.Hashes != inspection.Hashes {
		return evidence, fmt.Errorf("%w: downloader returned hashes that do not match the submitted metainfo", ErrAddOutcomeUnknown)
	}
	if result.Observed != nil {
		observed := buildTorrentEvidence(runtime, *result.Observed)
		evidence.Observed = &observed
	}
	if options.UploadLimit > 0 {
		hash := inspection.Hashes.V1SHA1
		if hash == "" {
			hash = inspection.Hashes.V2SHA256
		}
		if result.Observed == nil {
			return evidence, &postAddVerificationError{hash: strings.ToLower(hash), err: errors.New("upload limit could not be read back after torrent add")}
		}
		if result.Observed.UploadLimit != options.UploadLimit {
			return evidence, &postAddVerificationError{hash: strings.ToLower(hash), err: fmt.Errorf("upload limit readback is %d bytes/s, want %d bytes/s", result.Observed.UploadLimit, options.UploadLimit)}
		}
	}
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.add", map[string]any{
		"torrent_bytes": evidence.TorrentBytes, "torrent_sha256": evidence.TorrentSHA256,
		"configuration_sha256": evidence.ConfigurationSHA256,
		"v1_infohash":          result.Hashes.V1SHA1, "v2_infohash": result.Hashes.V2SHA256,
		"observed_hash": observedHash(result.Observed), "save_path": options.SavePath,
		"category": options.Category, "tags": options.Tags, "apply_labels": addLabelsEnabled(options),
		"download_limit": options.DownloadLimit, "upload_limit": options.UploadLimit,
	}, actor); err != nil {
		return evidence, fmt.Errorf("%w: persist downloader add result audit", ErrAddOutcomeUnknown)
	}
	return evidence, nil
}

func mergeAddResult(expected, observed qbittorrent.AddResult) qbittorrent.AddResult {
	if observed.Hashes.V1SHA1 != "" || observed.Hashes.V2SHA256 != "" {
		expected.Hashes = observed.Hashes
	}
	if observed.Observed != nil {
		expected.Observed = observed.Observed
	}
	return expected
}

func validateAddCapabilities(capability integrations.DownloaderAdapterCapability, options qbittorrent.AddOptions) error {
	if !addLabelsEnabled(options) && (strings.TrimSpace(options.Category) != "" || len(nonEmptyOptionStrings(options.Tags)) > 0) {
		return fmt.Errorf("%w: category and tags must be empty when apply_labels=false", integrations.ErrValidation)
	}
	if addLabelsEnabled(options) && (!capability.Operations.Category || !capability.Operations.Tags) {
		return fmt.Errorf("%w: downloader adapter %q requires explicit apply_labels=false because its core runtime cannot apply category and tags", integrations.ErrValidation, capability.Adapter)
	}
	if strings.TrimSpace(options.Category) != "" && !capability.Operations.Category {
		return fmt.Errorf("%w: downloader adapter %q does not support category", integrations.ErrValidation, capability.Adapter)
	}
	if len(nonEmptyOptionStrings(options.Tags)) > 0 && !capability.Operations.Tags {
		return fmt.Errorf("%w: downloader adapter %q does not support tags", integrations.ErrValidation, capability.Adapter)
	}
	if options.SkipChecking && !capability.Operations.SkipChecking {
		return fmt.Errorf("%w: downloader adapter %q does not support skip_checking", integrations.ErrValidation, capability.Adapter)
	}
	return nil
}

func addLabelsEnabled(options qbittorrent.AddOptions) bool {
	return options.ApplyLabels == nil || *options.ApplyLabels
}

func nonEmptyOptionStrings(values []string) []string {
	result := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			result = append(result, value)
		}
	}
	return result
}

func applyConfiguredDefaults(configured map[string]any, options *qbittorrent.AddOptions) {
	if !addLabelsEnabled(*options) {
		return
	}
	if options.Category == "" {
		if category, ok := configured["category"].(string); ok {
			options.Category = strings.TrimSpace(category)
		}
	}
	defaults := make([]string, 0, 2)
	for _, key := range []string{"tag", "label"} {
		if value, ok := configured[key].(string); ok && strings.TrimSpace(value) != "" {
			defaults = append(defaults, strings.TrimSpace(value))
		}
	}
	seen := make(map[string]struct{}, len(options.Tags)+len(defaults))
	merged := make([]string, 0, len(options.Tags)+len(defaults))
	for _, value := range append(append([]string(nil), options.Tags...), defaults...) {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		merged = append(merged, value)
	}
	options.Tags = merged
}

func (manager *Manager) SetLimits(ctx context.Context, name, hash string, downloadBytesPerSecond, uploadBytesPerSecond int64, actor workflow.Actor) (TorrentEvidence, error) {
	runtime, client, err := manager.client(ctx, name)
	if err != nil {
		return TorrentEvidence{}, err
	}
	hash = strings.ToLower(strings.TrimSpace(hash))
	if !torrentHashPattern.MatchString(hash) {
		return TorrentEvidence{}, fmt.Errorf("%w: torrent hash must be a 40 or 64 character hexadecimal infohash", integrations.ErrValidation)
	}
	if downloadBytesPerSecond < 0 || uploadBytesPerSecond < 0 {
		return TorrentEvidence{}, fmt.Errorf("%w: downloader limits must not be negative", integrations.ErrValidation)
	}
	if runtime.Adapter == "rtorrent" && ((downloadBytesPerSecond > 0 && downloadBytesPerSecond < 1024) || (uploadBytesPerSecond > 0 && uploadBytesPerSecond < 1024)) {
		return TorrentEvidence{}, fmt.Errorf("%w: rTorrent named throttle granularity is 1024 bytes per second", integrations.ErrValidation)
	}
	if runtime.Adapter == "transmission" && ((downloadBytesPerSecond > 0 && downloadBytesPerSecond < 1000) || (uploadBytesPerSecond > 0 && uploadBytesPerSecond < 1000)) {
		return TorrentEvidence{}, fmt.Errorf("%w: Transmission limit granularity is 1000 bytes per second", integrations.ErrValidation)
	}
	if runtime.Adapter == "deluge" && (downloadBytesPerSecond > 1<<53 || uploadBytesPerSecond > 1<<53) {
		return TorrentEvidence{}, fmt.Errorf("%w: Deluge limit is outside the exactly representable range", integrations.ErrValidation)
	}
	intent := map[string]any{
		"hash": hash, "configuration_sha256": runtime.ConfigurationSHA256,
		"requested_download_limit": downloadBytesPerSecond, "requested_upload_limit": uploadBytesPerSecond,
	}
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.set_limits_intent", intent, actor); err != nil {
		return TorrentEvidence{}, fmt.Errorf("persist downloader limit intent: %w", err)
	}
	if err := client.SetLimits(ctx, hash, downloadBytesPerSecond, uploadBytesPerSecond); err != nil {
		return TorrentEvidence{}, fmt.Errorf("%w: downloader request ended without trustworthy limit evidence: %w", ErrLimitsOutcomeUnknown, err)
	}
	torrent, err := client.Get(ctx, hash)
	if err != nil {
		return TorrentEvidence{}, fmt.Errorf("%w: read back downloader limits: %w", ErrLimitsOutcomeUnknown, err)
	}
	evidence := buildTorrentEvidence(runtime, torrent)
	if !observedLimitMatches(downloadBytesPerSecond, torrent.DownloadLimit) || !observedLimitMatches(uploadBytesPerSecond, torrent.UploadLimit) {
		return evidence, fmt.Errorf("%w: downloader read-back limits do not satisfy the requested caps", ErrLimitsOutcomeUnknown)
	}
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.set_limits", map[string]any{
		"hash": torrent.Hash, "requested_download_limit": downloadBytesPerSecond,
		"requested_upload_limit": uploadBytesPerSecond, "observed_download_limit": torrent.DownloadLimit,
		"observed_upload_limit": torrent.UploadLimit,
	}, actor); err != nil {
		return evidence, fmt.Errorf("%w: persist downloader limit result audit", ErrLimitsOutcomeUnknown)
	}
	return evidence, nil
}

func observedLimitMatches(requested, observed int64) bool {
	if requested == 0 {
		return observed == 0
	}
	return observed > 0 && observed <= requested
}

func (manager *Manager) WaitComplete(ctx context.Context, name, hash string, interval time.Duration, actor workflow.Actor) (TorrentEvidence, error) {
	runtime, client, err := manager.client(ctx, name)
	if err != nil {
		return TorrentEvidence{}, err
	}
	torrent, err := client.WaitComplete(ctx, hash, interval)
	if err != nil {
		return TorrentEvidence{}, err
	}
	evidence := buildTorrentEvidence(runtime, torrent)
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.wait_complete", map[string]any{
		"hash": torrent.Hash, "state": torrent.State, "progress": torrent.Progress,
		"completed": torrent.Completed, "total_size": torrent.TotalSize,
		"remote_content_path": evidence.RemoteContentPath, "local_content_path": evidence.LocalContentPath,
	}, actor); err != nil {
		return TorrentEvidence{}, err
	}
	return evidence, nil
}

func (manager *Manager) client(ctx context.Context, name string) (integrations.RuntimeDownloader, torrentClient, error) {
	runtime, err := manager.store.GetRuntimeDownloader(ctx, name)
	if err != nil {
		return integrations.RuntimeDownloader{}, nil, err
	}
	capability, supported := integrations.DownloaderAdapterCapabilityFor(runtime.Adapter)
	if !supported || !capability.RuntimeSupported {
		return integrations.RuntimeDownloader{}, nil, fmt.Errorf("%w: %s", ErrAdapterUnavailable, runtime.Adapter)
	}
	runtime.AdapterCapability = capability
	timeout := time.Duration(runtime.EndpointConfig.TimeoutSeconds) * time.Second
	var client torrentClient
	switch runtime.Adapter {
	case "qbittorrent":
		client, err = qbittorrent.New(qbittorrent.Config{
			Endpoint: runtime.EndpointConfig.Endpoint, Timeout: timeout, Credentials: runtime.Credentials,
		})
	case "transmission":
		client, err = transmission.New(transmission.Config{
			Endpoint: runtime.EndpointConfig.Endpoint, Timeout: timeout, Credentials: runtime.Credentials,
		})
	case "rtorrent":
		client, err = rtorrent.New(rtorrent.Config{
			Endpoint: runtime.EndpointConfig.Endpoint, Timeout: timeout, Credentials: runtime.Credentials,
		})
	case "deluge":
		client, err = deluge.New(deluge.Config{
			Endpoint: runtime.EndpointConfig.Endpoint, Timeout: timeout, Credentials: runtime.Credentials,
		})
	default:
		return integrations.RuntimeDownloader{}, nil, fmt.Errorf("%w: %s", ErrAdapterUnavailable, runtime.Adapter)
	}
	if err != nil {
		return integrations.RuntimeDownloader{}, nil, err
	}
	return runtime, client, nil
}

func buildTorrentEvidence(runtime integrations.RuntimeDownloader, torrent qbittorrent.Torrent) TorrentEvidence {
	evidence := TorrentEvidence{
		DownloaderName: runtime.Name, Adapter: runtime.Adapter, NetworkClass: runtime.NetworkClass,
		ConfigurationSHA256: runtime.ConfigurationSHA256, Torrent: torrent,
		RemoteSavePath: torrent.SavePath, RemoteContentPath: torrent.ContentPath,
	}
	for _, mapping := range runtime.PathMappings {
		local, ok := mapPath(torrent.ContentPath, mapping)
		if ok {
			mappingCopy := mapping
			evidence.LocalContentPath = local
			evidence.PathMapping = &mappingCopy
			break
		}
	}
	return evidence
}

func mapPath(remote string, mapping integrations.PathMapping) (string, bool) {
	remote = path.Clean(remote)
	base := path.Clean(mapping.RemotePath)
	if remote != base && !strings.HasPrefix(remote, base+"/") {
		return "", false
	}
	relative := strings.TrimPrefix(remote, base)
	local := path.Clean(mapping.LocalPath + "/" + strings.TrimPrefix(relative, "/"))
	localBase := path.Clean(mapping.LocalPath)
	if local != localBase && !strings.HasPrefix(local, localBase+"/") {
		return "", false
	}
	return local, true
}

func (manager *Manager) recordHealth(ctx context.Context, name, status string, details map[string]any, actor workflow.Actor) {
	recordCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
	defer cancel()
	_ = manager.store.RecordDownloaderHealth(recordCtx, name, status, details, actor)
}

func observedHash(torrent *qbittorrent.Torrent) string {
	if torrent == nil {
		return ""
	}
	return torrent.Hash
}

func safeError(err error) string {
	message := strings.ReplaceAll(err.Error(), "\n", " ")
	if len(message) > 300 {
		message = message[:300]
	}
	return message
}
