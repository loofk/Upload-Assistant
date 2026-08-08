package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type metadataIdentity struct {
	Title    string `json:"title"`
	IMDbID   string `json:"imdb_id,omitempty"`
	TMDbID   string `json:"tmdb_id,omitempty"`
	TMDbType string `json:"tmdb_type,omitempty"`
	DoubanID string `json:"douban_id,omitempty"`
	AniDBID  string `json:"anidb_id,omitempty"`
}

type metadataDocument struct {
	SchemaVersion       int               `json:"schema_version"`
	Identity            metadataIdentity  `json:"identity"`
	Links               map[string]string `json:"links"`
	Provenance          []string          `json:"provenance"`
	IdentityStrength    string            `json:"identity_strength"`
	ManualReviewNeeded  bool              `json:"manual_review_required"`
	DescriptionArtifact map[string]any    `json:"description_artifact,omitempty"`
	Content             map[string]any    `json:"content"`
	GeneratedAt         time.Time         `json:"generated_at"`
}

func WithMetadata(artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["metadata"] = metadataExecutor{
			artifacts: artifactStore, recorder: runner.runtime,
		}
	}
}

type metadataExecutor struct {
	artifacts ArtifactWriter
	recorder  ArtifactRecorder
}

func (executor metadataExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("metadata artifact dependencies are unavailable")
	}
	var snapshot struct {
		ResumeState struct {
			Metadata metadataIdentity `json:"metadata"`
		} `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		return nil, invalidSnapshotBlock(fmt.Errorf("decode metadata step snapshot: %w", err))
	}
	var inspected struct {
		SourceInfo          sites.SourceInfo `json:"source_info"`
		DescriptionArtifact map[string]any   `json:"description_artifact"`
	}
	if body, exists := snapshot.PreviousSteps["source_inspect"]; !exists || json.Unmarshal(body, &inspected) != nil {
		return nil, invalidSnapshotBlock(fmt.Errorf("source_inspect metadata evidence is missing"))
	}
	var content struct {
		Resolved         bool     `json:"resolved"`
		LocalRoot        string   `json:"local_root"`
		FileCount        int      `json:"file_count"`
		TotalSizeBytes   int64    `json:"total_size_bytes"`
		ManifestArtifact string   `json:"manifest_artifact_id"`
		ManifestSHA256   string   `json:"manifest_sha256"`
		MediaCandidates  []string `json:"media_candidates"`
	}
	if body, exists := snapshot.PreviousSteps["content_resolve"]; !exists || json.Unmarshal(body, &content) != nil || !content.Resolved {
		return nil, invalidSnapshotBlock(fmt.Errorf("content_resolve evidence is missing or incomplete"))
	}
	identity := metadataIdentity{
		Title: inspected.SourceInfo.Name, IMDbID: inspected.SourceInfo.IMDbID,
		TMDbID: inspected.SourceInfo.TMDbID, TMDbType: inspected.SourceInfo.TMDbType,
		DoubanID: inspected.SourceInfo.DoubanID, AniDBID: inspected.SourceInfo.AniDBID,
	}
	mergeMetadataIdentity(&identity, snapshot.ResumeState.Metadata)
	if err := validateMetadataIdentity(identity); err != nil {
		return nil, &BlockError{
			Blockers: []Blocker{{Code: "metadata_identity_required", Message: err.Error()}},
			NextActions: []NextAction{{Action: "provide_metadata_identity", Description: "Supply corrected title and external IDs in resume_state.metadata.", Parameters: map[string]any{
				"supported_fields": []string{"title", "imdb_id", "tmdb_id", "tmdb_type", "douban_id", "anidb_id"},
			}}},
			ResumeState: map[string]any{"metadata": identity},
		}
	}
	links := metadataLinks(identity)
	strength := "title_only"
	manualReview := true
	if len(links) > 0 {
		strength = "external_id"
		manualReview = false
	}
	document := metadataDocument{
		SchemaVersion: 1, Identity: identity, Links: links,
		Provenance: []string{"source_site_details"}, IdentityStrength: strength,
		ManualReviewNeeded: manualReview, DescriptionArtifact: inspected.DescriptionArtifact,
		Content: map[string]any{
			"local_root": content.LocalRoot, "file_count": content.FileCount,
			"total_size_bytes":      content.TotalSizeBytes,
			"manifest_artifact_id":  content.ManifestArtifact,
			"manifest_sha256":       content.ManifestSHA256,
			"media_candidate_count": len(content.MediaCandidates),
		},
		GeneratedAt: time.Now().UTC(),
	}
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize metadata artifact: %w", err)
	}
	file, err := executor.artifacts.Write(ctx, artifacts.Scope{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
	}, "metadata.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist metadata artifact: %w", err)
	}
	recorded, err := executor.recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "metadata", StoragePath: file.RelativePath, Filename: file.Filename,
		MIMEType: "application/json", SizeBytes: file.SizeBytes, SHA256: file.SHA256,
		Metadata: mustJSON(map[string]any{
			"identity_strength": strength, "manual_review_required": manualReview,
			"external_id_count": len(links),
		}),
		Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register metadata artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"identity": identity, "links": links, "provenance": document.Provenance,
		"identity_strength": strength, "manual_review_required": manualReview,
		"metadata_artifact_id": recorded.ID, "metadata_sha256": recorded.SHA256,
		"metadata_storage_path": recorded.StoragePath,
	}), nil
}

func mergeMetadataIdentity(target *metadataIdentity, override metadataIdentity) {
	if strings.TrimSpace(override.Title) != "" {
		target.Title = strings.TrimSpace(override.Title)
	}
	if strings.TrimSpace(override.IMDbID) != "" {
		target.IMDbID = strings.TrimSpace(override.IMDbID)
	}
	if strings.TrimSpace(override.TMDbID) != "" {
		target.TMDbID = strings.TrimSpace(override.TMDbID)
	}
	if strings.TrimSpace(override.TMDbType) != "" {
		target.TMDbType = strings.ToLower(strings.TrimSpace(override.TMDbType))
	}
	if strings.TrimSpace(override.DoubanID) != "" {
		target.DoubanID = strings.TrimSpace(override.DoubanID)
	}
	if strings.TrimSpace(override.AniDBID) != "" {
		target.AniDBID = strings.TrimSpace(override.AniDBID)
	}
}

var (
	imdbIDPattern    = regexp.MustCompile(`^tt[0-9]{5,12}$`)
	numericIDPattern = regexp.MustCompile(`^[0-9]{1,16}$`)
)

func validateMetadataIdentity(identity metadataIdentity) error {
	if strings.TrimSpace(identity.Title) == "" {
		return fmt.Errorf("a source title is required for metadata resolution")
	}
	if identity.IMDbID != "" && !imdbIDPattern.MatchString(identity.IMDbID) {
		return fmt.Errorf("IMDb id must use tt followed by digits")
	}
	for name, value := range map[string]string{"TMDb": identity.TMDbID, "Douban": identity.DoubanID, "AniDB": identity.AniDBID} {
		if value != "" && !numericIDPattern.MatchString(value) {
			return fmt.Errorf("%s id must contain only digits", name)
		}
	}
	if identity.TMDbID != "" && identity.TMDbType != "movie" && identity.TMDbType != "tv" {
		return fmt.Errorf("TMDb type must be movie or tv when a TMDb id is present")
	}
	return nil
}

func metadataLinks(identity metadataIdentity) map[string]string {
	links := map[string]string{}
	if identity.IMDbID != "" {
		links["imdb"] = "https://www.imdb.com/title/" + identity.IMDbID + "/"
	}
	if identity.TMDbID != "" {
		links["tmdb"] = "https://www.themoviedb.org/" + identity.TMDbType + "/" + identity.TMDbID
	}
	if identity.DoubanID != "" {
		links["douban"] = "https://movie.douban.com/subject/" + identity.DoubanID + "/"
	}
	if identity.AniDBID != "" {
		links["anidb"] = "https://anidb.net/anime/" + identity.AniDBID
	}
	return links
}
