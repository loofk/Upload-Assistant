package rules

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
	"gopkg.in/yaml.v3"
)

// CorrectHardGate derives an immutable draft from an existing revision. Review
// comments are deliberately not interpreted as executable configuration: the
// operator must submit the corrected hard-gate value explicitly.
func (s *Store) CorrectHardGate(ctx context.Context, revisionID, expectedFingerprint, section string, data json.RawMessage, comment string, actor workflow.Actor) (Revision, error) {
	if _, err := uuid.Parse(actor.ID); err != nil {
		return Revision{}, fmt.Errorf("reviewer must be an authenticated user")
	}
	comment = strings.TrimSpace(comment)
	if comment == "" {
		return Revision{}, fmt.Errorf("correction audit comment is required")
	}
	if len(comment) > 4000 {
		return Revision{}, fmt.Errorf("correction audit comment exceeds 4000 bytes")
	}

	source, err := s.Get(ctx, revisionID)
	if err != nil {
		return Revision{}, err
	}
	if source.Fingerprint != strings.TrimSpace(expectedFingerprint) {
		return Revision{}, fmt.Errorf("%w: rule fingerprint does not match", ErrConflict)
	}
	raw, err := s.ReadMarkdown(source)
	if err != nil {
		return Revision{}, err
	}
	document, err := ParseMarkdown(raw)
	if err != nil {
		return Revision{}, fmt.Errorf("parse source rule revision: %w", err)
	}
	if err := applyHardGateCorrection(&document, section, data); err != nil {
		return Revision{}, err
	}
	document.Review = Review{Status: "draft"}
	corrected, err := RenderMarkdown(document)
	if err != nil {
		return Revision{}, err
	}
	parsed, err := ParseMarkdown(corrected)
	if err != nil {
		return Revision{}, err
	}
	targetFingerprint, err := parsed.Fingerprint()
	if err != nil {
		return Revision{}, err
	}
	if targetFingerprint == source.Fingerprint {
		return Revision{}, fmt.Errorf("%w: hard-gate correction did not change executable policy", ErrConflict)
	}
	target, err := s.Import(ctx, corrected, actor)
	if err != nil {
		return Revision{}, err
	}
	var auditData map[string]any
	if err := json.Unmarshal(data, &auditData); err != nil {
		return Revision{}, fmt.Errorf("decode corrected hard-gate audit data: %w", err)
	}
	if _, err := s.pool.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, $2, 'site_rule.correct_hard_gate', 'site_rule_revision', $3, $4)`,
		actor.Type, actor.ID, target.ID, mustJSON(map[string]any{
			"source_revision_id": source.ID, "source_fingerprint": source.Fingerprint,
			"target_revision_id": target.ID, "target_fingerprint": target.Fingerprint,
			"section": strings.TrimSpace(section), "data": auditData, "comment": comment,
		}),
	); err != nil {
		return Revision{}, fmt.Errorf("audit hard-gate correction: %w", err)
	}
	return target, nil
}

func applyHardGateCorrection(document *Document, section string, data json.RawMessage) error {
	section = strings.ToLower(strings.TrimSpace(section))
	if len(bytes.TrimSpace(data)) == 0 || string(bytes.TrimSpace(data)) == "null" {
		return fmt.Errorf("corrected hard-gate data is required")
	}
	switch section {
	case "upload_limit":
		var correction struct {
			Upload              *string `json:"upload"`
			UploadDeclared      *string `json:"upload_declared"`
			UploadSafetyMargin  *string `json:"upload_safety_margin"`
			UploadScope         *string `json:"upload_scope"`
			SeedboxUpload       *string `json:"seedbox_upload"`
			SeedboxDeclared     *string `json:"seedbox_upload_declared"`
			SeedboxSafetyMargin *string `json:"seedbox_upload_safety_margin"`
			SeedboxUploadScope  *string `json:"seedbox_upload_scope"`
		}
		if err := decodeStrictCorrection(data, &correction); err != nil {
			return fmt.Errorf("decode upload-limit correction: %w", err)
		}
		if correction.Upload == nil && correction.UploadDeclared == nil && correction.SeedboxUpload == nil && correction.SeedboxDeclared == nil {
			return fmt.Errorf("upload-limit correction requires upload or seedbox_upload")
		}
		if err := applyRateCorrection(&document.Limits.Upload, &document.Limits.UploadPolicy, correction.Upload, correction.UploadDeclared, correction.UploadSafetyMargin, correction.UploadScope, DefaultUploadSafetyMargin); err != nil {
			return fmt.Errorf("correct upload limit: %w", err)
		}
		if err := applyRateCorrection(&document.Limits.SeedboxUpload, &document.Limits.SeedboxUploadPolicy, correction.SeedboxUpload, correction.SeedboxDeclared, correction.SeedboxSafetyMargin, correction.SeedboxUploadScope, DefaultUploadSafetyMargin); err != nil {
			return fmt.Errorf("correct seedbox upload limit: %w", err)
		}
		document.Source.Conflicts = withoutSourceConflicts(document.Source.Conflicts, "upload_limit", "seedbox_upload_limit")
	case "download_limit":
		var correction struct {
			Download         *string `json:"download"`
			DownloadDeclared *string `json:"download_declared"`
			DownloadScope    *string `json:"download_scope"`
		}
		if err := decodeStrictCorrection(data, &correction); err != nil {
			return fmt.Errorf("decode download-limit correction: %w", err)
		}
		if correction.Download == nil && correction.DownloadDeclared == nil {
			return fmt.Errorf("download-limit correction requires download")
		}
		if err := applyRateCorrection(&document.Limits.Download, &document.Limits.DownloadPolicy, correction.Download, correction.DownloadDeclared, nil, correction.DownloadScope, ""); err != nil {
			return fmt.Errorf("correct download limit: %w", err)
		}
		document.Source.Conflicts = withoutSourceConflicts(document.Source.Conflicts, "download_limit")
	case "naming":
		var naming Naming
		if err := decodeStrictCorrection(data, &naming); err != nil {
			return fmt.Errorf("decode naming correction: %w", err)
		}
		document.Naming = naming
		document.Source.Conflicts = withoutSourceConflicts(document.Source.Conflicts, "naming")
	default:
		return fmt.Errorf("invalid hard-gate correction section %q", section)
	}
	if err := document.Validate(); err != nil {
		return fmt.Errorf("validate corrected hard gate: %w", err)
	}
	return nil
}

func applyRateCorrection(executable *string, policy **RateLimitPolicy, enforced, declared, margin, scope *string, defaultMargin string) error {
	if enforced == nil && declared == nil {
		return nil
	}
	value := ""
	if declared != nil {
		value = strings.TrimSpace(*declared)
	} else if enforced != nil {
		value = strings.TrimSpace(*enforced)
	}
	if value == "" {
		*executable = ""
		*policy = nil
		return nil
	}
	marginValue := ""
	if declared != nil {
		marginValue = defaultMargin
	}
	if margin != nil {
		marginValue = strings.TrimSpace(*margin)
	}
	scopeValue := "per_torrent"
	if scope != nil && strings.TrimSpace(*scope) != "" {
		scopeValue = strings.TrimSpace(*scope)
	}
	next, err := NewRateLimitPolicy(value, marginValue, scopeValue)
	if err != nil {
		return err
	}
	if *policy != nil {
		next.EvidenceRefs = append([]string(nil), (*policy).EvidenceRefs...)
	}
	if enforced != nil {
		next.Enforced = strings.TrimSpace(*enforced)
	}
	*executable = next.Enforced
	*policy = next
	return nil
}

func withoutSourceConflicts(values []SourceConflict, sections ...string) []SourceConflict {
	blocked := map[string]bool{}
	for _, section := range sections {
		blocked[section] = true
	}
	result := make([]SourceConflict, 0, len(values))
	for _, conflict := range values {
		if !blocked[conflict.Section] {
			result = append(result, conflict)
		}
	}
	return result
}

func decodeStrictCorrection(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return fmt.Errorf("only one JSON value is allowed")
		}
		return err
	}
	return nil
}

// RenderMarkdown serializes a validated rule document while preserving the
// checksum-bound original body verbatim apart from normalized outer spacing.
func RenderMarkdown(document Document) ([]byte, error) {
	document.Body = strings.TrimSpace(strings.ReplaceAll(document.Body, "\r\n", "\n"))
	if err := document.Validate(); err != nil {
		return nil, fmt.Errorf("validate rule Markdown: %w", err)
	}
	frontMatter, err := yaml.Marshal(document)
	if err != nil {
		return nil, fmt.Errorf("encode rule Markdown front matter: %w", err)
	}
	raw := []byte("---\n" + strings.TrimSpace(string(frontMatter)) + "\n---\n\n" + document.Body + "\n")
	if _, err := ParseMarkdown(raw); err != nil {
		return nil, fmt.Errorf("validate rendered rule Markdown: %w", err)
	}
	return raw, nil
}
