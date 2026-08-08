package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/downloaders"
	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const (
	maxContentFiles       = 200_000
	maxManifestBytes      = 32 << 20
	maxMediaCandidateList = 2_000
)

type contentResolveExecutor struct {
	provider     DownloaderProvider
	artifacts    ArtifactWriter
	recorder     ArtifactRecorder
	allowedRoots []string
}

type resolvedContentFile struct {
	Index        int     `json:"index"`
	TorrentPath  string  `json:"torrent_path"`
	LocalPath    string  `json:"local_path"`
	SizeBytes    int64   `json:"size_bytes"`
	Progress     float64 `json:"progress"`
	Priority     int     `json:"priority"`
	Availability float64 `json:"availability"`
}

type contentManifest struct {
	SchemaVersion  int                   `json:"schema_version"`
	DownloaderName string                `json:"downloader_name"`
	TorrentHash    string                `json:"torrent_hash"`
	RemoteRoot     string                `json:"remote_root"`
	LocalRoot      string                `json:"local_root"`
	FileCount      int                   `json:"file_count"`
	TotalSizeBytes int64                 `json:"total_size_bytes"`
	TorrentSize    int64                 `json:"torrent_size_bytes"`
	ResolvedFiles  []resolvedContentFile `json:"files"`
	GeneratedAt    time.Time             `json:"generated_at"`
}

func (executor contentResolveExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil || executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("content resolution dependencies are unavailable")
	}
	downloaderName, torrentHash, err := downloaderWaitReference(execution.Step.InputSnapshot)
	if err != nil {
		return nil, invalidSnapshotBlock(err)
	}
	evidence, err := executor.provider.Files(ctx, downloaderName, torrentHash, execution.Actor)
	if err != nil {
		return nil, downloaderBlock(err, downloaderName, "resolve_downloaded_content", map[string]any{"torrent_hash": torrentHash})
	}
	if evidence.FileCount == 0 || evidence.FileCount > maxContentFiles {
		return nil, contentVerificationBlock("content_file_count_invalid", "the configured downloader returned an invalid or unsupported file count", evidence, nil)
	}
	localRoot := strings.TrimSpace(evidence.Torrent.LocalContentPath)
	if localRoot == "" {
		return nil, &BlockError{
			Blockers: []Blocker{{Code: "downloader_path_mapping_required", Message: "the downloader content path has no local path mapping"}},
			NextActions: []NextAction{{Action: "configure_downloader_path_mapping", Description: "Map the remote downloader content path into a mounted local content root.", Parameters: map[string]any{
				"downloader_name": downloaderName, "remote_content_path": evidence.Torrent.RemoteContentPath,
			}}},
			ResumeState: map[string]any{"downloader": map[string]any{"name": downloaderName}, "torrent_hash": torrentHash},
		}
	}
	resolvedRoot, err := resolveWithinRoots(localRoot, executor.allowedRoots)
	if err != nil {
		return nil, contentVerificationBlock("content_root_unsafe", err.Error(), evidence, nil)
	}
	rootInfo, err := os.Stat(resolvedRoot)
	if err != nil {
		return nil, contentVerificationBlock("content_root_unavailable", fmt.Sprintf("local content root is unavailable: %v", err), evidence, nil)
	}

	resolvedFiles := make([]resolvedContentFile, 0, len(evidence.Files))
	mediaCandidates := make([]string, 0)
	problems := make([]string, 0)
	var actualTotal int64
	for _, torrentFile := range evidence.Files {
		localPath, resolveErr := resolveTorrentFile(resolvedRoot, rootInfo, torrentFile, executor.allowedRoots)
		if resolveErr != nil {
			if len(problems) < 20 {
				problems = append(problems, resolveErr.Error())
			}
			continue
		}
		info, statErr := os.Stat(localPath)
		if statErr != nil || !info.Mode().IsRegular() {
			if len(problems) < 20 {
				problems = append(problems, fmt.Sprintf("%s is not a readable regular file", torrentFile.Name))
			}
			continue
		}
		if info.Size() != torrentFile.Size {
			if len(problems) < 20 {
				problems = append(problems, fmt.Sprintf("%s size is %d, expected %d", torrentFile.Name, info.Size(), torrentFile.Size))
			}
			continue
		}
		if torrentFile.Progress < 0.999999 {
			if len(problems) < 20 {
				problems = append(problems, fmt.Sprintf("%s progress is %.6f", torrentFile.Name, torrentFile.Progress))
			}
			continue
		}
		actualTotal += info.Size()
		resolvedFiles = append(resolvedFiles, resolvedContentFile{
			Index: torrentFile.Index, TorrentPath: torrentFile.Name, LocalPath: localPath,
			SizeBytes: info.Size(), Progress: torrentFile.Progress, Priority: torrentFile.Priority,
			Availability: torrentFile.Availability,
		})
		if len(mediaCandidates) < maxMediaCandidateList && isMediaFile(localPath) {
			mediaCandidates = append(mediaCandidates, localPath)
		}
	}
	if len(problems) > 0 || len(resolvedFiles) != evidence.FileCount || actualTotal != evidence.TotalSize {
		if len(problems) == 0 {
			problems = append(problems, fmt.Sprintf("resolved %d/%d files and %d/%d bytes", len(resolvedFiles), evidence.FileCount, actualTotal, evidence.TotalSize))
		}
		return nil, contentVerificationBlock("content_verification_failed", "downloaded content does not match downloader file evidence", evidence, problems)
	}
	if evidence.Torrent.Torrent.TotalSize > 0 && evidence.TotalSize != evidence.Torrent.Torrent.TotalSize {
		return nil, contentVerificationBlock(
			"content_size_mismatch",
			fmt.Sprintf("downloader file total %d does not match torrent total %d", evidence.TotalSize, evidence.Torrent.Torrent.TotalSize),
			evidence, nil,
		)
	}
	manifest := contentManifest{
		SchemaVersion: 1, DownloaderName: downloaderName, TorrentHash: torrentHash,
		RemoteRoot: evidence.Torrent.RemoteContentPath, LocalRoot: resolvedRoot,
		FileCount: len(resolvedFiles), TotalSizeBytes: actualTotal,
		TorrentSize: evidence.Torrent.Torrent.TotalSize, ResolvedFiles: resolvedFiles,
		GeneratedAt: time.Now().UTC(),
	}
	manifestBody, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize content manifest: %w", err)
	}
	if len(manifestBody) > maxManifestBytes {
		return nil, contentVerificationBlock("content_manifest_too_large", "content manifest exceeds the auditable artifact limit", evidence, nil)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, "content-manifest.json", bytes.NewReader(manifestBody))
	if err != nil {
		return nil, fmt.Errorf("persist content manifest artifact: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "content_manifest", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"downloader_name": downloaderName, "torrent_hash": torrentHash,
			"file_count": len(resolvedFiles), "total_size_bytes": actualTotal,
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register content manifest artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"resolved": true, "downloader_name": downloaderName, "torrent_hash": torrentHash,
		"local_root": resolvedRoot, "remote_root": evidence.Torrent.RemoteContentPath,
		"file_count": len(resolvedFiles), "total_size_bytes": actualTotal,
		"manifest_artifact_id": recorded.ID, "manifest_sha256": recorded.SHA256,
		"manifest_storage_path": recorded.StoragePath, "media_candidates": mediaCandidates,
	}), nil
}

func downloaderWaitReference(snapshotBody json.RawMessage) (string, string, error) {
	var snapshot struct {
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(snapshotBody, &snapshot); err != nil {
		return "", "", fmt.Errorf("decode content resolution snapshot: %w", err)
	}
	var waited struct {
		Completed      bool   `json:"completed"`
		DownloaderName string `json:"downloader_name"`
		TorrentHash    string `json:"torrent_hash"`
	}
	body, exists := snapshot.PreviousSteps["downloader_wait"]
	if !exists || json.Unmarshal(body, &waited) != nil || !waited.Completed || waited.DownloaderName == "" || waited.TorrentHash == "" {
		return "", "", fmt.Errorf("completed downloader_wait evidence is missing or incomplete")
	}
	return waited.DownloaderName, waited.TorrentHash, nil
}

func resolveWithinRoots(candidate string, allowedRoots []string) (string, error) {
	if !filepath.IsAbs(candidate) || filepath.Clean(candidate) != candidate {
		return "", fmt.Errorf("local content path must be a normalized absolute path")
	}
	resolved, err := filepath.EvalSymlinks(candidate)
	if err != nil {
		return "", fmt.Errorf("resolve local content path: %w", err)
	}
	resolved = filepath.Clean(resolved)
	for _, root := range allowedRoots {
		if !filepath.IsAbs(root) {
			continue
		}
		resolvedRoot, rootErr := filepath.EvalSymlinks(filepath.Clean(root))
		if rootErr == nil && pathWithin(resolved, resolvedRoot) {
			return resolved, nil
		}
	}
	return "", fmt.Errorf("local content path is outside configured content roots")
}

func resolveTorrentFile(root string, rootInfo os.FileInfo, torrentFile qbittorrent.TorrentFile, allowedRoots []string) (string, error) {
	if rootInfo.Mode().IsRegular() {
		return resolveWithinRoots(root, allowedRoots)
	}
	if !rootInfo.IsDir() {
		return "", fmt.Errorf("content root has unsupported file type")
	}
	cleanName := path.Clean(strings.ReplaceAll(torrentFile.Name, "\\", "/"))
	if cleanName == "." || strings.HasPrefix(cleanName, "../") || path.IsAbs(cleanName) {
		return "", fmt.Errorf("torrent file path %q is unsafe", torrentFile.Name)
	}
	candidates := make([]string, 0, 2)
	firstSegment, _, _ := strings.Cut(cleanName, "/")
	if firstSegment == filepath.Base(root) {
		candidates = append(candidates, filepath.Join(filepath.Dir(root), filepath.FromSlash(cleanName)))
	}
	candidates = append(candidates, filepath.Join(root, filepath.FromSlash(cleanName)))
	for _, candidate := range candidates {
		info, err := os.Lstat(candidate)
		if err != nil || info.Mode()&os.ModeSymlink != 0 {
			continue
		}
		resolved, err := resolveWithinRoots(candidate, allowedRoots)
		if err == nil && pathWithin(resolved, root) {
			return resolved, nil
		}
	}
	return "", fmt.Errorf("torrent file %q is missing or outside the resolved content root", torrentFile.Name)
}

func pathWithin(candidate, root string) bool {
	relative, err := filepath.Rel(filepath.Clean(root), filepath.Clean(candidate))
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func isMediaFile(filename string) bool {
	switch strings.ToLower(filepath.Ext(filename)) {
	case ".mkv", ".mp4", ".m2ts", ".ts", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg", ".vob":
		return true
	default:
		return false
	}
}

func contentVerificationBlock(code, message string, evidence downloaders.TorrentFilesEvidence, problems []string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: code, Message: message}},
		NextActions: []NextAction{{Action: "repair_or_recheck_downloaded_content", Description: "Correct path mappings or finish/repair the source download, then resume content resolution.", Parameters: map[string]any{
			"downloader_name": evidence.DownloaderName,
			"torrent_hash":    evidence.Torrent.Torrent.Hash,
		}}},
		ResumeState: map[string]any{"content_resolution": map[string]any{
			"downloader_name": evidence.DownloaderName, "torrent_hash": evidence.Torrent.Torrent.Hash,
			"remote_content_path": evidence.Torrent.RemoteContentPath,
			"local_content_path":  evidence.Torrent.LocalContentPath,
			"file_count":          evidence.FileCount, "total_size": evidence.TotalSize,
			"problems": problems,
		}},
	}
}
