package rtorrent

import (
	"context"
	"errors"
	"fmt"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

var hashPattern = regexp.MustCompile(`^[a-fA-F0-9]{40}$`)

// PartialAddError means rTorrent loaded the metainfo but a mandatory direct
// follow-up (directory, labels, limits, start, or observation) failed. Direct
// hash-addressed retries are safe and must reconcile this exact hash.
type PartialAddError struct {
	Hash string
	Err  error
}

func (err *PartialAddError) Error() string {
	return fmt.Sprintf("rTorrent added torrent %s but post-add configuration failed: %v", err.Hash, err.Err)
}

func (err *PartialAddError) Unwrap() error       { return err.Err }
func (err *PartialAddError) PartialHash() string { return err.Hash }

type Client struct {
	rpc *rpcClient
}

type torrentSnapshot struct {
	Hash, Name, Directory, Label, Throttle, Message  string
	Complete, State, Active, MultiFile, HashChecking int64
	Size, Completed, Left                            int64
	DownloadRate, Downloaded, UploadRate, Uploaded   int64
	Ratio, AddedOn, StartedOn, CompletionOn          int64
}

var snapshotCalls = []string{
	"d.hash", "d.name", "d.directory", "d.custom1", "d.throttle_name", "d.message",
	"d.complete", "d.state", "d.is_active", "d.is_multi_file", "d.is_hash_checking",
	"d.size_bytes", "d.completed_bytes", "d.left_bytes",
	"d.down.rate", "d.down.total", "d.up.rate", "d.up.total", "d.ratio",
	"d.load_date", "d.timestamp.started", "d.timestamp.finished",
}

var requiredMethods = []string{
	"system.multicall", "load.raw", "d.hash", "d.directory.set", "d.custom1.set", "d.start",
	"f.multicall", "d.throttle_name.set", "throttle.down", "throttle.up", "throttle.down.max", "throttle.up.max",
	"d.is_active", "d.is_multi_file", "d.is_hash_checking", "d.timestamp.started", "d.timestamp.finished",
}

func New(config Config) (*Client, error) {
	rpc, err := newRPCClient(config)
	if err != nil {
		return nil, err
	}
	return &Client{rpc: rpc}, nil
}

func (client *Client) Probe(ctx context.Context) (qbittorrent.ProbeResult, error) {
	version, err := client.rpc.call(ctx, "system.client_version")
	if err != nil {
		return qbittorrent.ProbeResult{}, mapRPCError(err)
	}
	library, err := client.rpc.call(ctx, "system.library_version")
	if err != nil {
		return qbittorrent.ProbeResult{}, mapRPCError(err)
	}
	authentication := "none"
	if client.rpc.credentials["username"] != "" {
		authentication = "basic"
	}
	applicationVersion, ok := version.(string)
	if !ok || strings.TrimSpace(applicationVersion) == "" {
		return qbittorrent.ProbeResult{}, fmt.Errorf("rTorrent system.client_version returned an invalid value")
	}
	libraryVersion, ok := library.(string)
	if !ok || strings.TrimSpace(libraryVersion) == "" {
		return qbittorrent.ProbeResult{}, fmt.Errorf("rTorrent system.library_version returned an invalid value")
	}
	missing := make([]string, 0)
	for _, method := range requiredMethods {
		exists, err := client.rpc.call(ctx, "system.methodExist", method)
		if err != nil {
			return qbittorrent.ProbeResult{}, mapRPCError(err)
		}
		available := false
		switch value := exists.(type) {
		case bool:
			available = value
		case int64:
			available = value == 1
		default:
			return qbittorrent.ProbeResult{}, fmt.Errorf("rTorrent system.methodExist returned an invalid value for %s", method)
		}
		if !available {
			missing = append(missing, method)
		}
	}
	if len(missing) > 0 {
		return qbittorrent.ProbeResult{}, fmt.Errorf("rTorrent XML-RPC endpoint is missing required methods: %s", strings.Join(missing, ", "))
	}
	return qbittorrent.ProbeResult{
		ApplicationVersion: applicationVersion,
		WebAPIVersion:      "XML-RPC (libtorrent " + libraryVersion + ")",
		Authentication:     authentication,
	}, nil
}

func (client *Client) Get(ctx context.Context, hash string) (qbittorrent.Torrent, error) {
	if err := validateHash(hash); err != nil {
		return qbittorrent.Torrent{}, err
	}
	hash = strings.ToUpper(hash)
	calls := make([]any, 0, len(snapshotCalls))
	for _, method := range snapshotCalls {
		calls = append(calls, map[string]any{"methodName": method, "params": []any{hash}})
	}
	response, err := client.rpc.call(ctx, "system.multicall", calls)
	if err != nil {
		return qbittorrent.Torrent{}, mapRPCError(err)
	}
	values, err := multicallValues(response, len(snapshotCalls))
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	snapshot, err := decodeSnapshot(values)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	if snapshot.Hash == "" || !strings.EqualFold(snapshot.Hash, hash) {
		return qbittorrent.Torrent{}, qbittorrent.ErrNotFound
	}
	downloadLimit, uploadLimit, err := client.throttleLimits(ctx, snapshot.Throttle)
	if err != nil {
		return qbittorrent.Torrent{}, err
	}
	return normalizeTorrent(snapshot, downloadLimit, uploadLimit), nil
}

func (client *Client) Files(ctx context.Context, hash string) ([]qbittorrent.TorrentFile, error) {
	if err := validateHash(hash); err != nil {
		return nil, err
	}
	response, err := client.rpc.call(ctx, "f.multicall", strings.ToUpper(hash), "",
		"f.path=", "f.size_bytes=", "f.completed_chunks=", "f.size_chunks=", "f.priority=")
	if err != nil {
		return nil, mapRPCError(err)
	}
	rows, ok := response.([]any)
	if !ok {
		return nil, fmt.Errorf("rTorrent f.multicall returned an invalid response")
	}
	if len(rows) > 200_000 {
		return nil, fmt.Errorf("rTorrent torrent file count exceeds 200000")
	}
	if len(rows) == 0 {
		return nil, qbittorrent.ErrNotFound
	}
	result := make([]qbittorrent.TorrentFile, 0, len(rows))
	for index, rawRow := range rows {
		row, ok := rawRow.([]any)
		if !ok || len(row) != 5 {
			return nil, fmt.Errorf("rTorrent file row %d is invalid", index)
		}
		name, err := requiredString(row[0], fmt.Sprintf("file row %d path", index))
		if err != nil {
			return nil, err
		}
		size, err := requiredInt64(row[1], fmt.Sprintf("file row %d size", index))
		if err != nil || size < 0 {
			return nil, fmt.Errorf("rTorrent file row %d has an invalid size", index)
		}
		completedChunks, err := requiredInt64(row[2], fmt.Sprintf("file row %d completed chunks", index))
		if err != nil || completedChunks < 0 {
			return nil, fmt.Errorf("rTorrent file row %d has invalid completed chunks", index)
		}
		sizeChunks, err := requiredInt64(row[3], fmt.Sprintf("file row %d size chunks", index))
		if err != nil || sizeChunks < 0 {
			return nil, fmt.Errorf("rTorrent file row %d has invalid size chunks", index)
		}
		progress := float64(0)
		if size == 0 || (sizeChunks > 0 && completedChunks >= sizeChunks) {
			progress = 1
		} else if completedChunks > 0 && sizeChunks > 0 {
			progress = float64(completedChunks) / float64(sizeChunks)
		}
		priorityValue, err := requiredInt64(row[4], fmt.Sprintf("file row %d priority", index))
		if err != nil || priorityValue < 0 || priorityValue > 3 {
			return nil, fmt.Errorf("rTorrent file row %d has invalid priority", index)
		}
		priority := int(priorityValue)
		result = append(result, qbittorrent.TorrentFile{
			Index: index, Name: name, Size: size, Progress: progress,
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
	result := qbittorrent.AddResult{Hashes: hashes}
	if hashes.V1SHA1 == "" {
		return result, fmt.Errorf("rTorrent requires a v1 infohash; v2-only torrents are not supported")
	}
	if options.DownloadLimit < 0 || options.UploadLimit < 0 {
		return result, fmt.Errorf("rTorrent limits must not be negative")
	}
	if options.SkipChecking {
		return result, fmt.Errorf("rTorrent does not support skip_checking; verification cannot be bypassed")
	}
	labels := uniqueStrings(append([]string{options.Category}, options.Tags...))
	if err := validateLabels(labels); err != nil {
		return result, err
	}
	hash := strings.ToUpper(hashes.V1SHA1)
	// rTorrent's XML-RPC compatibility form requires an empty command target
	// before the raw binary metainfo, even though the command reference renders
	// the logical signature starting with the metainfo value.
	_, loadErr := client.rpc.call(ctx, "load.raw", "", metainfo)
	if loadErr != nil {
		if _, observedErr := client.Get(ctx, hash); observedErr != nil {
			return result, mapRPCError(loadErr)
		}
	}
	postAdd := func(method string, params ...any) error {
		_, err := client.rpc.call(ctx, method, params...)
		if err != nil {
			return mapRPCError(err)
		}
		return nil
	}
	if options.SavePath != "" {
		if err := postAdd("d.directory.set", hash, options.SavePath); err != nil {
			return result, &PartialAddError{Hash: strings.ToLower(hash), Err: err}
		}
	}
	if len(labels) > 0 {
		if err := postAdd("d.custom1.set", hash, strings.Join(labels, ",")); err != nil {
			return result, &PartialAddError{Hash: strings.ToLower(hash), Err: err}
		}
	}
	if options.DownloadLimit > 0 || options.UploadLimit > 0 {
		if err := client.SetLimits(ctx, hash, options.DownloadLimit, options.UploadLimit); err != nil {
			return result, &PartialAddError{Hash: strings.ToLower(hash), Err: err}
		}
	}
	if !options.Paused {
		if err := postAdd("d.start", hash); err != nil {
			return result, &PartialAddError{Hash: strings.ToLower(hash), Err: err}
		}
	}
	observed, err := client.Get(ctx, hash)
	if err != nil {
		return result, &PartialAddError{Hash: strings.ToLower(hash), Err: err}
	}
	result.Observed = &observed
	return result, nil
}

func (client *Client) SetLimits(ctx context.Context, hash string, downloadBytesPerSecond, uploadBytesPerSecond int64) error {
	if err := validateHash(hash); err != nil {
		return err
	}
	if downloadBytesPerSecond < 0 || uploadBytesPerSecond < 0 {
		return fmt.Errorf("rTorrent limits must not be negative")
	}
	if (downloadBytesPerSecond > 0 && downloadBytesPerSecond < 1024) || (uploadBytesPerSecond > 0 && uploadBytesPerSecond < 1024) {
		return fmt.Errorf("rTorrent named throttle granularity is 1024 bytes per second")
	}
	hash = strings.ToUpper(hash)
	if downloadBytesPerSecond == 0 && uploadBytesPerSecond == 0 {
		if _, err := client.rpc.call(ctx, "d.throttle_name.set", hash, "NULL"); err != nil {
			return mapRPCError(err)
		}
		observed, err := client.rpc.call(ctx, "d.throttle_name", hash)
		if err != nil {
			return mapRPCError(err)
		}
		name, err := requiredString(observed, "d.throttle_name")
		if err != nil {
			return err
		}
		if name = strings.TrimSpace(name); name != "" && !strings.EqualFold(name, "NULL") {
			return fmt.Errorf("rTorrent did not clear the per-torrent throttle")
		}
		return nil
	}
	name := "ua_" + strings.ToLower(hash[:16])
	if _, err := client.rpc.call(ctx, "throttle.down", name, throttleRate(downloadBytesPerSecond)); err != nil {
		return mapRPCError(err)
	}
	if _, err := client.rpc.call(ctx, "throttle.up", name, throttleRate(uploadBytesPerSecond)); err != nil {
		return mapRPCError(err)
	}
	if _, err := client.rpc.call(ctx, "d.throttle_name.set", hash, name); err != nil {
		return mapRPCError(err)
	}
	observedDownload, observedUpload, err := client.throttleLimits(ctx, name)
	if err != nil {
		return err
	}
	if err := verifyLimit("download", downloadBytesPerSecond, observedDownload); err != nil {
		return err
	}
	if err := verifyLimit("upload", uploadBytesPerSecond, observedUpload); err != nil {
		return err
	}
	return nil
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
			return qbittorrent.Torrent{}, fmt.Errorf("rTorrent torrent entered state %s", item.State)
		}
		select {
		case <-ctx.Done():
			return qbittorrent.Torrent{}, ctx.Err()
		case <-time.After(interval):
		}
	}
}

func (client *Client) throttleLimits(ctx context.Context, name string) (int64, int64, error) {
	name = strings.TrimSpace(name)
	if name == "" || strings.EqualFold(name, "NULL") {
		return 0, 0, nil
	}
	down, err := client.rpc.call(ctx, "throttle.down.max", name)
	if err != nil {
		return 0, 0, mapRPCError(err)
	}
	up, err := client.rpc.call(ctx, "throttle.up.max", name)
	if err != nil {
		return 0, 0, mapRPCError(err)
	}
	download, err := requiredInt64(down, "throttle.down.max")
	if err != nil {
		return 0, 0, err
	}
	upload, err := requiredInt64(up, "throttle.up.max")
	if err != nil {
		return 0, 0, err
	}
	return download, upload, nil
}

func multicallValues(value any, expected int) ([]any, error) {
	rows, ok := value.([]any)
	if !ok || len(rows) != expected {
		return nil, fmt.Errorf("rTorrent system.multicall returned %d values; expected %d", len(rows), expected)
	}
	result := make([]any, len(rows))
	for index, rawRow := range rows {
		row, ok := rawRow.([]any)
		if !ok || len(row) != 1 {
			if fault, faultOK := rawRow.(map[string]any); faultOK {
				return nil, mapRPCError(&faultError{Code: integerValue(fault["faultCode"]), Message: stringValue(fault["faultString"])})
			}
			return nil, fmt.Errorf("rTorrent system.multicall row %d is invalid", index)
		}
		result[index] = row[0]
	}
	return result, nil
}

func normalizeTorrent(item torrentSnapshot, downloadLimit, uploadLimit int64) qbittorrent.Torrent {
	progress := float64(0)
	if item.Complete != 0 || (item.Size > 0 && item.Completed >= item.Size) {
		progress = 1
	} else if item.Completed > 0 {
		progress = float64(item.Completed) / float64(item.Size)
	}
	state := "stopped"
	if item.HashChecking != 0 {
		state = "checking"
	} else if item.State != 0 && item.Active == 0 {
		state = "paused"
	} else if item.State != 0 && item.Complete != 0 {
		state = "seeding"
	} else if item.State != 0 {
		state = "downloading"
	} else if item.Complete != 0 {
		state = "completed"
	}
	if item.State == 0 && item.Complete == 0 && strings.TrimSpace(item.Message) != "" {
		state = "stopped: " + safeMessage(item.Message)
	}
	labels := uniqueStrings(strings.Split(item.Label, ","))
	category, tags := "", ""
	if len(labels) > 0 {
		category = labels[0]
	}
	if len(labels) > 1 {
		tags = strings.Join(labels[1:], ",")
	}
	left := item.Left
	if left < 0 {
		left = 0
	}
	completed := item.Completed
	if completed < 0 {
		completed = 0
	}
	savePath := item.Directory
	contentPath := path.Join(item.Directory, item.Name)
	if item.MultiFile != 0 {
		contentPath = path.Clean(item.Directory)
		savePath = path.Dir(contentPath)
	}
	activeSeconds := int64(0)
	if item.Active != 0 {
		activeSince := item.StartedOn
		if item.Complete != 0 && item.CompletionOn > activeSince {
			activeSince = item.CompletionOn
		}
		now := time.Now().Unix()
		if activeSince > 0 && activeSince <= now {
			activeSeconds = now - activeSince
		}
	}
	seedingSeconds := int64(0)
	if item.Complete != 0 {
		seedingSeconds = activeSeconds
	}
	return qbittorrent.Torrent{
		Hash: strings.ToLower(item.Hash), Name: item.Name, State: state, Progress: progress,
		Size: item.Size, TotalSize: item.Size, Completed: completed, AmountLeft: left,
		Downloaded: item.Downloaded, Uploaded: item.Uploaded, DownloadSpeed: item.DownloadRate, UploadSpeed: item.UploadRate,
		DownloadLimit: downloadLimit, UploadLimit: uploadLimit, Ratio: float64(item.Ratio) / 1000,
		SavePath: savePath, ContentPath: contentPath, Category: category, Tags: tags,
		AddedOn: item.AddedOn, CompletionOn: item.CompletionOn, TimeActive: activeSeconds, SeedingTime: seedingSeconds,
	}
}

func throttleRate(bytesPerSecond int64) string {
	if bytesPerSecond <= 0 {
		return "0"
	}
	// The XML-RPC value must be a string and is already interpreted as KiB/s.
	return strconv.FormatInt(bytesPerSecond/1024, 10)
}

func verifyLimit(kind string, requested, observed int64) error {
	if requested == 0 {
		if observed != 0 {
			return fmt.Errorf("rTorrent %s throttle expected unlimited but observed %d bytes per second", kind, observed)
		}
		return nil
	}
	if observed <= 0 {
		return fmt.Errorf("rTorrent %s named throttle is ineffective; configure a non-zero global throttle before resuming", kind)
	}
	if observed > requested {
		return fmt.Errorf("rTorrent %s named throttle observed %d bytes per second, above requested %d", kind, observed, requested)
	}
	return nil
}

func mapRPCError(err error) error {
	var fault *faultError
	if errors.As(err, &fault) {
		message := strings.ToLower(fault.Message)
		if strings.Contains(message, "not found") || strings.Contains(message, "info-hash") || strings.Contains(message, "unknown download") {
			return fmt.Errorf("%w: %s", qbittorrent.ErrNotFound, safeMessage(fault.Message))
		}
	}
	return err
}

func validateHash(hash string) error {
	if !hashPattern.MatchString(strings.TrimSpace(hash)) {
		return fmt.Errorf("invalid rTorrent v1 torrent hash")
	}
	return nil
}

func stringValue(value any) string {
	text, _ := value.(string)
	return text
}

func integerValue(value any) int64 {
	integer, _ := asInt64(value)
	return integer
}

func decodeSnapshot(values []any) (torrentSnapshot, error) {
	if len(values) != len(snapshotCalls) {
		return torrentSnapshot{}, fmt.Errorf("rTorrent snapshot value count is invalid")
	}
	texts := make([]string, 6)
	for index := range texts {
		value, err := requiredString(values[index], snapshotCalls[index])
		if err != nil {
			return torrentSnapshot{}, err
		}
		texts[index] = value
	}
	integers := make([]int64, len(values)-6)
	for index := 6; index < len(values); index++ {
		value, err := requiredInt64(values[index], snapshotCalls[index])
		if err != nil {
			return torrentSnapshot{}, err
		}
		integers[index-6] = value
	}
	for index := 0; index < 5; index++ {
		if integers[index] < 0 || integers[index] > 1 {
			return torrentSnapshot{}, fmt.Errorf("rTorrent snapshot contains invalid completion or state evidence")
		}
	}
	if integers[5] < 0 || integers[6] < 0 || integers[7] < 0 || (integers[5] > 0 && integers[6] > integers[5]) {
		return torrentSnapshot{}, fmt.Errorf("rTorrent snapshot contains invalid size evidence")
	}
	return torrentSnapshot{
		Hash: texts[0], Name: texts[1], Directory: texts[2], Label: texts[3], Throttle: texts[4], Message: texts[5],
		Complete: integers[0], State: integers[1], Active: integers[2], MultiFile: integers[3], HashChecking: integers[4],
		Size: integers[5], Completed: integers[6], Left: integers[7], DownloadRate: integers[8], Downloaded: integers[9],
		UploadRate: integers[10], Uploaded: integers[11], Ratio: integers[12], AddedOn: integers[13], StartedOn: integers[14], CompletionOn: integers[15],
	}, nil
}

func requiredString(value any, field string) (string, error) {
	text, ok := value.(string)
	if !ok {
		return "", fmt.Errorf("rTorrent %s returned an invalid string value", field)
	}
	return text, nil
}

func requiredInt64(value any, field string) (int64, error) {
	integer, ok := value.(int64)
	if !ok {
		return 0, fmt.Errorf("rTorrent %s returned an invalid integer value", field)
	}
	return integer, nil
}

func validateLabels(labels []string) error {
	for _, label := range labels {
		if strings.ContainsAny(label, ",\r\n\x00") {
			return fmt.Errorf("rTorrent category and tags must not contain commas or control characters")
		}
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
