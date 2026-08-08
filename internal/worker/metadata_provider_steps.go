package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/artifacts"
	"github.com/loofk/upload-assistant/v2/internal/metadataproviders"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const maxPTGenDescriptionBytes = 1 << 20

type MetadataProviderResolver interface {
	Resolve(context.Context, string, metadataproviders.ResolveRequest, workflow.Actor) (metadataproviders.ResolveResult, error)
}

type metadataProviderNames struct {
	TMDb  string `json:"tmdb,omitempty"`
	PTGen string `json:"ptgen,omitempty"`
}

type metadataTMDbRecovery struct {
	Provider string `json:"provider,omitempty"`
	IMDbID   string `json:"imdb_id,omitempty"`
	TMDbID   string `json:"tmdb_id,omitempty"`
	TMDbType string `json:"tmdb_type,omitempty"`
}

type metadataPTGenRecovery struct {
	Provider    string `json:"provider,omitempty"`
	DoubanID    string `json:"douban_id,omitempty"`
	Description string `json:"description,omitempty"`
}

type metadataTMDbDocument struct {
	SchemaVersion       int                              `json:"schema_version"`
	Source              string                           `json:"source"`
	Provider            string                           `json:"provider,omitempty"`
	Adapter             string                           `json:"adapter"`
	Identity            metadataIdentity                 `json:"identity"`
	ConfigurationSHA256 string                           `json:"configuration_sha256,omitempty"`
	QuerySHA256         string                           `json:"query_sha256,omitempty"`
	Calls               []metadataproviders.CallEvidence `json:"calls"`
	GeneratedAt         time.Time                        `json:"generated_at"`
}

type metadataPTGenDocument struct {
	SchemaVersion       int                              `json:"schema_version"`
	Source              string                           `json:"source"`
	Provider            string                           `json:"provider,omitempty"`
	Adapter             string                           `json:"adapter"`
	Identity            metadataIdentity                 `json:"identity"`
	Description         string                           `json:"description"`
	DescriptionSHA256   string                           `json:"description_sha256"`
	ConfigurationSHA256 string                           `json:"configuration_sha256,omitempty"`
	QuerySHA256         string                           `json:"query_sha256,omitempty"`
	Calls               []metadataproviders.CallEvidence `json:"calls"`
	GeneratedAt         time.Time                        `json:"generated_at"`
}

func WithMetadataProviders(resolver MetadataProviderResolver, artifactStore ArtifactWriter) Option {
	return func(runner *Runner) {
		runner.executors["metadata_tmdb"] = metadataTMDbExecutor{resolver: resolver, artifacts: artifactStore, recorder: runner.runtime}
		runner.executors["metadata_ptgen"] = metadataPTGenExecutor{resolver: resolver, artifacts: artifactStore, recorder: runner.runtime}
	}
}

type metadataTMDbExecutor struct {
	resolver  MetadataProviderResolver
	artifacts ArtifactWriter
	recorder  ArtifactRecorder
}

func (executor metadataTMDbExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("TMDb metadata artifact dependencies are unavailable")
	}
	var snapshot struct {
		JobInput struct {
			Providers metadataProviderNames `json:"metadata_providers"`
		} `json:"job_input"`
		ResumeState struct {
			Providers metadataProviderNames `json:"metadata_providers"`
			Recovery  metadataTMDbRecovery  `json:"metadata_tmdb"`
		} `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		return nil, invalidSnapshotBlock(fmt.Errorf("decode TMDb metadata snapshot: %w", err))
	}
	var previous struct {
		Identity metadataIdentity `json:"identity"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "metadata", &previous) {
		return nil, invalidSnapshotBlock(fmt.Errorf("metadata identity evidence is missing"))
	}
	identity := previous.Identity
	mergeMetadataIdentity(&identity, metadataIdentity{
		IMDbID: snapshot.ResumeState.Recovery.IMDbID, TMDbID: snapshot.ResumeState.Recovery.TMDbID,
		TMDbType: snapshot.ResumeState.Recovery.TMDbType,
	})
	if err := validateMetadataIdentity(identity); err != nil {
		return nil, metadataTMDbBlock(identity, err.Error())
	}
	providerName := firstMetadataProvider(snapshot.ResumeState.Recovery.Provider, snapshot.ResumeState.Providers.TMDb, snapshot.JobInput.Providers.TMDb)
	document := metadataTMDbDocument{SchemaVersion: 1, Source: "frozen_identity", Adapter: "manual", Identity: identity, Calls: []metadataproviders.CallEvidence{}, GeneratedAt: time.Now().UTC()}
	if providerName != "" {
		if executor.resolver == nil {
			return nil, metadataTMDbBlock(identity, "TMDb metadata resolver is unavailable")
		}
		result, err := executor.resolver.Resolve(ctx, providerName, metadataproviders.ResolveRequest{
			IMDbID: identity.IMDbID, TMDbID: identity.TMDbID, TMDbType: identity.TMDbType,
		}, execution.Actor)
		if err != nil {
			return nil, metadataTMDbBlock(identity, "configured TMDb provider request failed")
		}
		if result.Adapter != "tmdb" {
			return nil, metadataTMDbBlock(identity, "selected provider is not a TMDb adapter")
		}
		if err := mergeProviderIdentity(&identity, result.Identity); err != nil {
			return nil, metadataTMDbBlock(identity, err.Error())
		}
		document.Source, document.Provider, document.Adapter = "provider", providerName, result.Adapter
		document.ConfigurationSHA256, document.QuerySHA256, document.Calls = result.ConfigurationSHA256, result.QuerySHA256, result.Calls
	}
	if identity.IMDbID == "" || identity.TMDbID == "" || (identity.TMDbType != "movie" && identity.TMDbType != "tv") {
		return nil, metadataTMDbBlock(identity, "both IMDb and typed TMDb identities are required before target packaging")
	}
	document.Identity = identity
	return persistMetadataTMDb(ctx, execution, executor.artifacts, executor.recorder, document)
}

type metadataPTGenExecutor struct {
	resolver  MetadataProviderResolver
	artifacts ArtifactWriter
	recorder  ArtifactRecorder
}

func (executor metadataPTGenExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.artifacts == nil || executor.recorder == nil {
		return nil, fmt.Errorf("PTGen metadata artifact dependencies are unavailable")
	}
	var snapshot struct {
		JobInput struct {
			Providers metadataProviderNames `json:"metadata_providers"`
		} `json:"job_input"`
		ResumeState struct {
			Providers metadataProviderNames `json:"metadata_providers"`
			Recovery  metadataPTGenRecovery `json:"metadata_ptgen"`
		} `json:"resume_state"`
		PreviousSteps map[string]json.RawMessage `json:"previous_steps"`
	}
	if err := json.Unmarshal(execution.Step.InputSnapshot, &snapshot); err != nil {
		return nil, invalidSnapshotBlock(fmt.Errorf("decode PTGen metadata snapshot: %w", err))
	}
	var previous struct {
		Identity metadataIdentity `json:"identity"`
	}
	if !decodePrevious(snapshot.PreviousSteps, "metadata_tmdb", &previous) {
		return nil, invalidSnapshotBlock(fmt.Errorf("metadata_tmdb evidence is missing"))
	}
	identity := previous.Identity
	if value := strings.TrimSpace(snapshot.ResumeState.Recovery.DoubanID); value != "" {
		identity.DoubanID = value
	}
	description := strings.TrimSpace(snapshot.ResumeState.Recovery.Description)
	providerName := firstMetadataProvider(snapshot.ResumeState.Recovery.Provider, snapshot.ResumeState.Providers.PTGen, snapshot.JobInput.Providers.PTGen)
	document := metadataPTGenDocument{SchemaVersion: 1, Source: "manual", Adapter: "manual", Identity: identity, Description: description, Calls: []metadataproviders.CallEvidence{}, GeneratedAt: time.Now().UTC()}
	if providerName != "" {
		if executor.resolver == nil {
			return nil, metadataPTGenBlock(identity, "PTGen metadata resolver is unavailable")
		}
		result, err := executor.resolver.Resolve(ctx, providerName, metadataproviders.ResolveRequest{
			IMDbID: identity.IMDbID, TMDbID: identity.TMDbID, TMDbType: identity.TMDbType, DoubanID: identity.DoubanID,
		}, execution.Actor)
		if err != nil {
			return nil, metadataPTGenBlock(identity, "configured PTGen provider request failed")
		}
		if result.Adapter != "ptgen" {
			return nil, metadataPTGenBlock(identity, "selected provider is not a PTGen adapter")
		}
		if err := mergeProviderIdentity(&identity, result.Identity); err != nil {
			return nil, metadataPTGenBlock(identity, err.Error())
		}
		description = strings.TrimSpace(result.Description)
		document.Source, document.Provider, document.Adapter = "provider", providerName, result.Adapter
		document.ConfigurationSHA256, document.QuerySHA256, document.Calls = result.ConfigurationSHA256, result.QuerySHA256, result.Calls
	}
	if identity.DoubanID == "" || !numericIDPattern.MatchString(identity.DoubanID) {
		return nil, metadataPTGenBlock(identity, "a numeric Douban subject id is required before target packaging")
	}
	if description == "" || len([]byte(description)) > maxPTGenDescriptionBytes || !utf8.ValidString(description) || strings.ContainsRune(description, '\x00') {
		return nil, metadataPTGenBlock(identity, "a non-empty bounded UTF-8 PTGen/Douban description is required before target packaging")
	}
	document.Identity, document.Description, document.DescriptionSHA256 = identity, description, sha256Bytes([]byte(description))
	return persistMetadataPTGen(ctx, execution, executor.artifacts, executor.recorder, document)
}

func persistMetadataTMDb(ctx context.Context, execution Execution, store ArtifactWriter, recorder ArtifactRecorder, document metadataTMDbDocument) (json.RawMessage, error) {
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize TMDb metadata artifact: %w", err)
	}
	file, err := store.Write(ctx, artifacts.Scope{JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID}, "metadata-tmdb.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist TMDb metadata artifact: %w", err)
	}
	recorded, err := recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "metadata_tmdb", StoragePath: file.RelativePath, Filename: file.Filename, MIMEType: "application/json",
		SizeBytes: file.SizeBytes, SHA256: file.SHA256, Metadata: mustJSON(map[string]any{
			"source": document.Source, "provider": document.Provider, "adapter": document.Adapter,
			"configuration_sha256": document.ConfigurationSHA256, "call_count": len(document.Calls),
		}), Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register TMDb metadata artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"resolved": true, "source": document.Source, "provider": document.Provider, "adapter": document.Adapter,
		"identity": document.Identity, "links": metadataLinks(document.Identity), "configuration_sha256": document.ConfigurationSHA256,
		"query_sha256": document.QuerySHA256, "calls": document.Calls, "artifact_id": recorded.ID,
		"artifact_sha256": recorded.SHA256, "artifact_storage_path": recorded.StoragePath,
	}), nil
}

func persistMetadataPTGen(ctx context.Context, execution Execution, store ArtifactWriter, recorder ArtifactRecorder, document metadataPTGenDocument) (json.RawMessage, error) {
	body, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("serialize PTGen metadata artifact: %w", err)
	}
	file, err := store.Write(ctx, artifacts.Scope{JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID}, "metadata-ptgen.json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("persist PTGen metadata artifact: %w", err)
	}
	recorded, err := recorder.RegisterArtifact(ctx, workflow.RegisterArtifactInput{
		JobID: execution.Job.ID, StepID: execution.Step.ID, AttemptID: execution.Attempt.ID,
		Kind: "metadata_ptgen", StoragePath: file.RelativePath, Filename: file.Filename, MIMEType: "application/json",
		SizeBytes: file.SizeBytes, SHA256: file.SHA256, Metadata: mustJSON(map[string]any{
			"source": document.Source, "provider": document.Provider, "adapter": document.Adapter,
			"configuration_sha256": document.ConfigurationSHA256, "call_count": len(document.Calls),
			"description_sha256": document.DescriptionSHA256, "description_size_bytes": len([]byte(document.Description)),
		}), Retention: artifactRetention, Actor: execution.Actor,
	})
	if err != nil {
		return nil, fmt.Errorf("register PTGen metadata artifact: %w", err)
	}
	return mustJSON(map[string]any{
		"resolved": true, "source": document.Source, "provider": document.Provider, "adapter": document.Adapter,
		"identity": document.Identity, "links": metadataLinks(document.Identity), "configuration_sha256": document.ConfigurationSHA256,
		"query_sha256": document.QuerySHA256, "calls": document.Calls, "description_sha256": document.DescriptionSHA256,
		"description_size_bytes": len([]byte(document.Description)), "artifact_id": recorded.ID,
		"artifact_sha256": recorded.SHA256, "artifact_storage_path": recorded.StoragePath,
	}), nil
}

func metadataTMDbBlock(identity metadataIdentity, message string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "metadata_tmdb_required", Message: message}},
		NextActions: []NextAction{{Action: "configure_or_select_tmdb_provider", Description: "Configure a TMDb provider or provide reviewed IMDb/TMDb IDs, then resume this exact step.", Parameters: map[string]any{
			"resume_fields": []string{"metadata_providers.tmdb", "metadata_tmdb.provider", "metadata_tmdb.imdb_id", "metadata_tmdb.tmdb_id", "metadata_tmdb.tmdb_type"},
		}}},
		ResumeState: map[string]any{"metadata_tmdb": map[string]any{"imdb_id": identity.IMDbID, "tmdb_id": identity.TMDbID, "tmdb_type": identity.TMDbType}},
	}
}

func metadataPTGenBlock(identity metadataIdentity, message string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: "metadata_ptgen_required", Message: message}},
		NextActions: []NextAction{{Action: "configure_or_select_ptgen_provider", Description: "Configure a PTGen provider or provide reviewed Douban ID and PTGen/Douban text, then resume this exact step.", Parameters: map[string]any{
			"resume_fields": []string{"metadata_providers.ptgen", "metadata_ptgen.provider", "metadata_ptgen.douban_id", "metadata_ptgen.description"},
		}}},
		ResumeState: map[string]any{"metadata_ptgen": map[string]any{"douban_id": identity.DoubanID, "description": ""}},
	}
}

func mergeProviderIdentity(target *metadataIdentity, source metadataproviders.Identity) error {
	values := []struct {
		name   string
		target *string
		value  string
	}{
		{"IMDb", &target.IMDbID, source.IMDbID}, {"TMDb", &target.TMDbID, source.TMDbID},
		{"TMDb type", &target.TMDbType, source.TMDbType}, {"Douban", &target.DoubanID, source.DoubanID},
	}
	for _, item := range values {
		value := strings.TrimSpace(item.value)
		if value == "" {
			continue
		}
		if strings.TrimSpace(*item.target) != "" && !strings.EqualFold(strings.TrimSpace(*item.target), value) {
			return fmt.Errorf("provider %s identity conflicts with frozen workflow evidence", item.name)
		}
		*item.target = value
	}
	return validateMetadataIdentity(*target)
}

func firstMetadataProvider(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func sha256Bytes(value []byte) string {
	return sha256Hex(value)
}
