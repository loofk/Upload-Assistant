package downloaders

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"path"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/rtorrent"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/transmission"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var ErrAdapterUnavailable = errors.New("downloader adapter is not implemented")

type ConfigurationStore interface {
	GetRuntimeDownloader(context.Context, string) (integrations.RuntimeDownloader, error)
	RecordDownloaderHealth(context.Context, string, string, map[string]any, workflow.Actor) error
	AuditDownloaderAction(context.Context, string, string, map[string]any, workflow.Actor) error
}

type Manager struct {
	store ConfigurationStore
}

type torrentClient interface {
	Probe(context.Context) (qbittorrent.ProbeResult, error)
	Get(context.Context, string) (qbittorrent.Torrent, error)
	Files(context.Context, string) ([]qbittorrent.TorrentFile, error)
	Add(context.Context, []byte, qbittorrent.AddOptions) (qbittorrent.AddResult, error)
	SetLimits(context.Context, string, int64, int64) error
	WaitComplete(context.Context, string, time.Duration) (qbittorrent.Torrent, error)
}

type TorrentEvidence struct {
	DownloaderName      string                    `json:"downloader_name"`
	Adapter             string                    `json:"adapter"`
	ConfigurationSHA256 string                    `json:"configuration_sha256"`
	Torrent             qbittorrent.Torrent       `json:"torrent"`
	RemoteSavePath      string                    `json:"remote_save_path"`
	RemoteContentPath   string                    `json:"remote_content_path"`
	LocalContentPath    string                    `json:"local_content_path,omitempty"`
	PathMapping         *integrations.PathMapping `json:"path_mapping,omitempty"`
}

type AddEvidence struct {
	DownloaderName      string                `json:"downloader_name"`
	Adapter             string                `json:"adapter"`
	ConfigurationSHA256 string                `json:"configuration_sha256"`
	TorrentBytes        int                   `json:"torrent_bytes"`
	TorrentSHA256       string                `json:"torrent_sha256"`
	Result              qbittorrent.AddResult `json:"result"`
	Observed            *TorrentEvidence      `json:"observed,omitempty"`
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
	return &Manager{store: store}
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
	applyConfiguredDefaults(runtime.EndpointConfig.Options, &options)
	result, err := client.Add(ctx, metainfo, options)
	if err != nil {
		if partialHash, partial := PartialAddHash(err); partial {
			auditCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 3*time.Second)
			defer cancel()
			_ = manager.store.AuditDownloaderAction(auditCtx, name, "torrent.add_partial", map[string]any{
				"observed_hash": partialHash, "v1_infohash": result.Hashes.V1SHA1, "v2_infohash": result.Hashes.V2SHA256,
				"download_limit": options.DownloadLimit, "upload_limit": options.UploadLimit,
				"reconciliation_required": true, "error": safeError(err),
			}, actor)
		}
		return AddEvidence{}, err
	}
	torrentSHA := sha256.Sum256(metainfo)
	evidence := AddEvidence{
		DownloaderName: runtime.Name, Adapter: runtime.Adapter, ConfigurationSHA256: runtime.ConfigurationSHA256, TorrentBytes: len(metainfo),
		TorrentSHA256: hex.EncodeToString(torrentSHA[:]), Result: result,
	}
	if result.Observed != nil {
		observed := buildTorrentEvidence(runtime, *result.Observed)
		evidence.Observed = &observed
	}
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.add", map[string]any{
		"torrent_bytes": evidence.TorrentBytes, "torrent_sha256": evidence.TorrentSHA256,
		"configuration_sha256": evidence.ConfigurationSHA256,
		"v1_infohash":          result.Hashes.V1SHA1, "v2_infohash": result.Hashes.V2SHA256,
		"observed_hash": observedHash(result.Observed), "save_path": options.SavePath,
		"category": options.Category, "tags": options.Tags,
		"download_limit": options.DownloadLimit, "upload_limit": options.UploadLimit,
	}, actor); err != nil {
		return AddEvidence{}, err
	}
	return evidence, nil
}

func applyConfiguredDefaults(configured map[string]any, options *qbittorrent.AddOptions) {
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
	if err := client.SetLimits(ctx, hash, downloadBytesPerSecond, uploadBytesPerSecond); err != nil {
		return TorrentEvidence{}, err
	}
	torrent, err := client.Get(ctx, hash)
	if err != nil {
		return TorrentEvidence{}, err
	}
	evidence := buildTorrentEvidence(runtime, torrent)
	if err := manager.store.AuditDownloaderAction(ctx, name, "torrent.set_limits", map[string]any{
		"hash": torrent.Hash, "requested_download_limit": downloadBytesPerSecond,
		"requested_upload_limit": uploadBytesPerSecond, "observed_download_limit": torrent.DownloadLimit,
		"observed_upload_limit": torrent.UploadLimit,
	}, actor); err != nil {
		return TorrentEvidence{}, err
	}
	return evidence, nil
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
		DownloaderName: runtime.Name, Adapter: runtime.Adapter, ConfigurationSHA256: runtime.ConfigurationSHA256, Torrent: torrent,
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
