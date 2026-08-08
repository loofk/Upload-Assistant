package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/sites/mteam"
	"github.com/loofk/upload-assistant/v2/internal/torrentmaker"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetTorrentMaker struct {
	result  torrentmaker.Result
	err     error
	request torrentmaker.Request
	calls   int
}

func (maker *fakeTargetTorrentMaker) SanitizeAndCheck(_ context.Context, request torrentmaker.Request) (torrentmaker.Result, error) {
	maker.calls++
	maker.request = request
	return maker.result, maker.err
}

type sequenceArtifactRecorder struct {
	inputs []workflow.RegisterArtifactInput
}

func (recorder *sequenceArtifactRecorder) RegisterArtifact(_ context.Context, input workflow.RegisterArtifactInput) (workflow.Artifact, error) {
	recorder.inputs = append(recorder.inputs, input)
	return workflow.Artifact{
		ID: fmt.Sprintf("artifact-%d", len(recorder.inputs)), JobID: input.JobID, StepID: input.StepID,
		AttemptID: input.AttemptID, Kind: input.Kind, StorageBackend: "local", StoragePath: input.StoragePath,
		Filename: input.Filename, MIMEType: input.MIMEType, SizeBytes: input.SizeBytes,
		SHA256: input.SHA256, Metadata: input.Metadata,
	}, nil
}

func TestTargetTorrentStepSanitizesChecksAndBindsEveryGate(t *testing.T) {
	store := mustArtifactStore(t)
	source := targetTorrentMetainfo("https://source.example/announce/secret-passkey", "U2", 3, nil)
	target := targetTorrentMetainfo("https://fake.tracker", "MTEAM", 3, nil)
	execution := targetTorrentExecution(t, store, source)
	maker := &fakeTargetTorrentMaker{result: torrentmaker.Result{
		Torrent: target, Tool: "mkbrr", Version: "mkbrr version: v1.23.0", Verification: "100.00%",
		ModifyDurationMS: 2, CheckDurationMS: 3,
	}}
	profiles, err := sites.NewTargetTorrentRegistry(mteam.NewTorrentAdapter())
	if err != nil {
		t.Fatal(err)
	}
	recorder := &sequenceArtifactRecorder{}
	output, err := (targetTorrentExecutor{profiles: profiles, maker: maker, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	if err != nil {
		t.Fatal(err)
	}
	if maker.calls != 1 || maker.request.AnnounceURL != "https://fake.tracker" || maker.request.SourceTag != "MTEAM" ||
		maker.request.ContentPath == "" || len(recorder.inputs) != 2 || recorder.inputs[0].Kind != "target_torrent" ||
		recorder.inputs[1].Kind != "target_torrent_receipt" {
		t.Fatalf("maker/recorded = %#v/%#v", maker, recorder.inputs)
	}
	var result struct {
		Prepared   bool   `json:"prepared"`
		Verified   bool   `json:"verified"`
		Status     string `json:"status"`
		ArtifactID string `json:"target_torrent_artifact_id"`
		ReceiptID  string `json:"receipt_artifact_id"`
	}
	if json.Unmarshal(output, &result) != nil || !result.Prepared || !result.Verified || result.Status != "ready_for_upload" ||
		result.ArtifactID != "artifact-1" || result.ReceiptID != "artifact-2" {
		t.Fatalf("target torrent output = %s", output)
	}
	receiptFile, err := store.Open(recorder.inputs[1].StoragePath)
	if err != nil {
		t.Fatal(err)
	}
	receiptBody, _ := io.ReadAll(receiptFile)
	receiptFile.Close()
	if bytes.Contains(receiptBody, []byte("secret-passkey")) || bytes.Contains(receiptBody, []byte("source.example")) ||
		!bytes.Contains(receiptBody, []byte(`"source_torrent_sha256"`)) || !bytes.Contains(receiptBody, []byte(`"rule_fingerprint"`)) {
		t.Fatalf("receipt redaction/bindings = %s", receiptBody)
	}
}

func TestTargetTorrentStepBlocksPayloadMutationAndToolMismatch(t *testing.T) {
	store := mustArtifactStore(t)
	source := targetTorrentMetainfo("https://source.example/announce/passkey", "U2", 3, nil)
	mutated := targetTorrentMetainfo("https://fake.tracker", "MTEAM", 4, nil)
	execution := targetTorrentExecution(t, store, source)
	profiles, _ := sites.NewTargetTorrentRegistry(mteam.NewTorrentAdapter())
	recorder := &sequenceArtifactRecorder{}
	maker := &fakeTargetTorrentMaker{result: torrentmaker.Result{Torrent: mutated, Tool: "mkbrr", Version: "v1.23.0"}}
	_, err := (targetTorrentExecutor{profiles: profiles, maker: maker, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_torrent_profile_violation" || len(recorder.inputs) != 0 {
		t.Fatalf("payload blocker/records = %#v/%#v", blocked, recorder.inputs)
	}

	maker = &fakeTargetTorrentMaker{err: &torrentmaker.ToolError{
		Code: "target_torrent_content_mismatch", Message: "piece verification failed",
	}}
	_, err = (targetTorrentExecutor{profiles: profiles, maker: maker, artifacts: store, recorder: recorder}).Execute(context.Background(), execution)
	blocked = requireBlockError(t, err)
	if blocked.Blockers[0].Code != "target_torrent_content_mismatch" || blocked.NextActions[0].Action != "recheck_source_content" {
		t.Fatalf("tool blocker = %#v", blocked)
	}
}

func targetTorrentExecution(t *testing.T, store *artifacts.LocalStore, sourceTorrent []byte) Execution {
	t.Helper()
	contentRoot := filepath.Join(t.TempDir(), "video.mkv")
	if err := os.WriteFile(contentRoot, []byte("abc"), 0o600); err != nil {
		t.Fatal(err)
	}
	sourceFile := writeTargetTorrentFixture(t, store, "source-step", "source-attempt", "source.torrent", sourceTorrent)
	sourceHashes, err := torrentmeta.Hashes(sourceTorrent)
	if err != nil {
		t.Fatal(err)
	}
	manifest := contentManifest{
		SchemaVersion: 1, DownloaderName: "box", TorrentHash: "source-hash", LocalRoot: contentRoot,
		FileCount: 1, TotalSizeBytes: 3, TorrentSize: 3,
		ResolvedFiles: []resolvedContentFile{{Index: 0, TorrentPath: "video.mkv", LocalPath: contentRoot, SizeBytes: 3, Progress: 1}},
		GeneratedAt:   time.Unix(1, 0).UTC(),
	}
	manifestBody, _ := json.Marshal(manifest)
	manifestFile := writeTargetTorrentFixture(t, store, "content-step", "content-attempt", "content-manifest.json", manifestBody)
	prepared := sites.PreparedTargetPackage{
		SchemaVersion: 1, Target: "MTEAM", Adapter: "mteam_api",
		Content:     sites.TargetContentEvidence{LocalRoot: contentRoot, FileCount: 1, TotalSizeBytes: 3, ManifestSHA256: manifestFile.SHA256},
		GeneratedAt: time.Unix(1, 0).UTC(),
	}
	packageBody, _ := json.Marshal(prepared)
	packageFile := writeTargetTorrentFixture(t, store, "package-step", "package-attempt", "mteam-target-package.json", packageBody)
	duplicateDocument := duplicateCheckDocument{
		SchemaVersion: 1,
		TargetPackage: sites.TargetArtifactEvidence{ArtifactID: "package-id", StoragePath: packageFile.RelativePath, SHA256: packageFile.SHA256, SizeBytes: packageFile.SizeBytes},
		Evidence: sites.TargetDuplicateEvidence{
			SiteCode: "MTEAM", Adapter: "mteam_api", ConfigurationSHA256: strings.Repeat("c", 64),
			Query: sites.TargetDuplicateQuery{IMDbID: "tt1234567"}, CheckedAt: time.Unix(1, 0).UTC(),
		},
	}
	duplicateBody, _ := json.Marshal(duplicateDocument)
	duplicateFile := writeTargetTorrentFixture(t, store, "duplicate-step", "duplicate-attempt", "mteam-duplicate-check.json", duplicateBody)
	return Execution{
		Job: workflow.Job{ID: "job-id"}, Step: workflow.Step{ID: "target-torrent-step", InputSnapshot: mustJSON(map[string]any{
			"previous_steps": map[string]any{
				"source_parse": map[string]any{"target": "MTEAM"},
				"source_torrent": map[string]any{
					"artifact_id": "source-id", "storage_path": sourceFile.RelativePath, "size_bytes": sourceFile.SizeBytes,
					"sha256": sourceFile.SHA256, "hashes": sourceHashes,
				},
				"content_resolve": map[string]any{
					"resolved": true, "downloader_name": "box", "remote_root": "/remote/downloads/video.mkv",
					"local_root": contentRoot, "file_count": 1, "total_size_bytes": 3,
					"manifest_artifact_id": "manifest-id", "manifest_sha256": manifestFile.SHA256,
					"manifest_storage_path": manifestFile.RelativePath,
				},
				"target_package": map[string]any{
					"prepared": true, "target": "MTEAM", "package_artifact_id": "package-id",
					"package_sha256": packageFile.SHA256, "package_storage_path": packageFile.RelativePath,
					"package_size_bytes": packageFile.SizeBytes,
				},
				"target_duplicate_check": map[string]any{
					"checked": true, "status": "clean", "target": "MTEAM", "duplicate": false,
					"duplicate_check_artifact_id": "duplicate-id", "duplicate_check_sha256": duplicateFile.SHA256,
					"duplicate_check_storage_path": duplicateFile.RelativePath, "target_package_sha256": packageFile.SHA256,
				},
				"target_rules": map[string]any{
					"site_code": "MTEAM", "role": "target", "rule_revision_id": "rule-id",
					"fingerprint": strings.Repeat("f", 64), "accepted": true, "acceptance_sha256": strings.Repeat("a", 64),
					"limits": map[string]any{"upload": "2MiB/s"}, "seeding": map[string]any{"minimum_time_hours": 1, "minimum_ratio": 1.0},
				},
			},
		})},
		Attempt: workflow.Attempt{ID: "target-torrent-attempt"}, Actor: workflow.Actor{Type: "worker", ID: "fixture"},
	}
}

func writeTargetTorrentFixture(t *testing.T, store *artifacts.LocalStore, step, attempt, name string, body []byte) artifacts.File {
	t.Helper()
	file, err := store.Write(context.Background(), artifacts.Scope{JobID: "job-id", StepID: step, AttemptID: attempt}, name, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	return file
}

func targetTorrentMetainfo(announce, source string, length int64, extra map[string][]byte) []byte {
	info := map[string][]byte{
		"length": ttBencodeInt(length), "name": ttBencodeBytes([]byte("video.mkv")),
		"piece length": ttBencodeInt(16384), "pieces": ttBencodeBytes(bytes.Repeat([]byte{0x42}, 20)),
		"private": ttBencodeInt(1), "source": ttBencodeBytes([]byte(source)),
	}
	top := map[string][]byte{"announce": ttBencodeBytes([]byte(announce)), "info": ttBencodeDict(info)}
	for key, value := range extra {
		top[key] = value
	}
	return ttBencodeDict(top)
}

func ttBencodeBytes(value []byte) []byte {
	return append([]byte(strconv.Itoa(len(value))+":"), value...)
}
func ttBencodeInt(value int64) []byte { return []byte("i" + strconv.FormatInt(value, 10) + "e") }
func ttBencodeDict(values map[string][]byte) []byte {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := []byte{'d'}
	for _, key := range keys {
		result = append(result, ttBencodeBytes([]byte(key))...)
		result = append(result, values[key]...)
	}
	return append(result, 'e')
}
