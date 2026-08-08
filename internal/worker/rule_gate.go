package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
)

type RuleProvider interface {
	Active(context.Context, string) (rules.Revision, error)
}

type ruleGateExecutor struct {
	provider RuleProvider
	role     string
}

type ruleAcceptance struct {
	Fingerprint string                          `json:"fingerprint"`
	Accepted    bool                            `json:"accepted"`
	Obligations map[string]obligationAcceptance `json:"obligations,omitempty"`
}

type obligationAcceptance struct {
	Confirmed bool   `json:"confirmed"`
	Evidence  string `json:"evidence"`
}

type jobControls struct {
	SourceURL   string                    `json:"source_url"`
	Target      string                    `json:"target"`
	AcceptRules map[string]ruleAcceptance `json:"accept_rules"`
}

func (executor ruleGateExecutor) Execute(ctx context.Context, execution Execution) (json.RawMessage, error) {
	if executor.provider == nil {
		return nil, fmt.Errorf("rule provider is unavailable")
	}
	controls, err := mergedJobControls(execution)
	if err != nil {
		return nil, &BlockError{
			Code: "invalid_rule_acceptance", Message: err.Error(),
			NextActions: []NextAction{{Action: "replace_rule_acceptance"}},
			ResumeState: map[string]any{},
		}
	}
	siteCode, err := controls.siteForRole(executor.role)
	if err != nil {
		return nil, &BlockError{
			Code: "invalid_rule_site", Message: err.Error(),
			NextActions: []NextAction{{Action: "provide_supported_site"}},
			ResumeState: map[string]any{},
		}
	}
	revision, err := executor.provider.Active(ctx, siteCode)
	if errors.Is(err, rules.ErrNotFound) {
		return nil, activeRuleRequired(siteCode)
	}
	if err != nil {
		return nil, fmt.Errorf("load active %s rules: %w", siteCode, err)
	}
	if revision.Status != "approved" {
		return nil, activeRuleRequired(siteCode)
	}
	policy, err := rules.ParsePolicy(revision.Policy)
	if err != nil {
		return nil, fmt.Errorf("parse active %s rule policy: %w", siteCode, err)
	}
	if !policy.Source.Complete {
		return nil, &BlockError{
			Blockers: []Blocker{{
				Code: "rule_source_incomplete", SiteCode: siteCode,
				Message: "the active rule revision does not contain a complete captured rule source",
			}},
			NextActions: []NextAction{{
				Action:      "import_complete_rule_revision",
				Description: "Import, review, approve, and activate a complete rule document before resuming.",
				Parameters:  map[string]any{"site_code": siteCode},
			}},
			ResumeState: ruleResumeTemplate(siteCode, revision, policy),
		}
	}
	if !slices.Contains(policy.Site.Roles, executor.role) {
		return nil, policyBlock(siteCode, "rule_role_not_allowed", "the active rule revision does not allow this site role", revision, policy)
	}
	if blocker := automationBlocker(executor.role, siteCode, policy); blocker != nil {
		return nil, policyBlock(siteCode, blocker.Code, blocker.Message, revision, policy)
	}

	acceptance, accepted := controls.AcceptRules[siteCode]
	if !accepted || !acceptance.Accepted {
		return nil, acceptanceRequired(siteCode, revision, policy, "rule_acceptance_required", "the active rule revision must be explicitly accepted")
	}
	if !strings.EqualFold(strings.TrimSpace(acceptance.Fingerprint), revision.Fingerprint) {
		return nil, acceptanceRequired(siteCode, revision, policy, "rule_fingerprint_mismatch", "the accepted rule fingerprint does not match the active revision")
	}

	manualBlockers := make([]Blocker, 0)
	for _, obligation := range policy.Obligations {
		if !obligation.Blocking || obligation.Verification != "manual" || obligation.Resolution == "not_applicable" {
			continue
		}
		confirmation, exists := acceptance.Obligations[obligation.ID]
		if !exists || !confirmation.Confirmed || strings.TrimSpace(confirmation.Evidence) == "" {
			manualBlockers = append(manualBlockers, Blocker{
				Code: "manual_rule_obligation_required", SiteCode: siteCode, ObligationID: obligation.ID,
				Message: obligation.Description,
			})
		}
	}
	if len(manualBlockers) > 0 {
		return nil, &BlockError{
			Blockers: manualBlockers,
			NextActions: []NextAction{{
				Action:      "confirm_manual_rule_obligations",
				Description: "Confirm every listed manual obligation with non-empty evidence, bound to the active fingerprint.",
				Parameters:  map[string]any{"site_code": siteCode, "fingerprint": revision.Fingerprint},
			}},
			ResumeState: ruleResumeTemplate(siteCode, revision, policy),
		}
	}

	acceptedObligations := make([]map[string]any, 0, len(policy.Obligations))
	for _, obligation := range policy.Obligations {
		entry := map[string]any{
			"id": obligation.ID, "verification": obligation.Verification,
			"blocking": obligation.Blocking, "resolution": obligation.Resolution,
		}
		if confirmation, exists := acceptance.Obligations[obligation.ID]; exists {
			entry["confirmed"] = confirmation.Confirmed
			entry["evidence"] = strings.TrimSpace(confirmation.Evidence)
		}
		acceptedObligations = append(acceptedObligations, entry)
	}
	acceptanceJSON, err := json.Marshal(acceptance)
	if err != nil {
		return nil, fmt.Errorf("serialize rule acceptance: %w", err)
	}
	acceptanceHash := sha256.Sum256(acceptanceJSON)
	return mustJSON(map[string]any{
		"site_code": siteCode, "role": executor.role,
		"rule_revision_id": revision.ID, "fingerprint": revision.Fingerprint,
		"accepted": true, "acceptance_sha256": hex.EncodeToString(acceptanceHash[:]),
		"automation": policy.Automation, "limits": policy.Limits,
		"seeding": policy.Seeding, "transfer": policy.Transfer,
		"obligations": acceptedObligations,
	}), nil
}

func mergedJobControls(execution Execution) (jobControls, error) {
	var input, resumed jobControls
	if err := json.Unmarshal(execution.Job.Input, &input); err != nil {
		return jobControls{}, fmt.Errorf("job input is not valid JSON: %w", err)
	}
	if len(execution.Job.ResumeState) > 0 && string(execution.Job.ResumeState) != "null" {
		if err := json.Unmarshal(execution.Job.ResumeState, &resumed); err != nil {
			return jobControls{}, fmt.Errorf("job resume_state is not valid JSON: %w", err)
		}
	}
	input.AcceptRules = normalizedAcceptances(input.AcceptRules)
	for siteCode, acceptance := range normalizedAcceptances(resumed.AcceptRules) {
		input.AcceptRules[siteCode] = acceptance
	}
	if resumed.SourceURL != "" {
		input.SourceURL = resumed.SourceURL
	}
	if resumed.Target != "" {
		input.Target = resumed.Target
	}
	return input, nil
}

func normalizedAcceptances(input map[string]ruleAcceptance) map[string]ruleAcceptance {
	result := make(map[string]ruleAcceptance, len(input))
	for siteCode, acceptance := range input {
		siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
		if siteCode == "" {
			continue
		}
		if acceptance.Obligations == nil {
			acceptance.Obligations = map[string]obligationAcceptance{}
		}
		result[siteCode] = acceptance
	}
	return result
}

func (controls jobControls) siteForRole(role string) (string, error) {
	switch role {
	case "source":
		reference, err := sites.ParseSourceReference(controls.SourceURL)
		if err != nil {
			return "", err
		}
		return reference.Tracker, nil
	case "target":
		target := strings.ToUpper(strings.TrimSpace(controls.Target))
		if target == "" {
			return "", fmt.Errorf("target site is required")
		}
		return target, nil
	default:
		return "", fmt.Errorf("unsupported rule role %q", role)
	}
}

func automationBlocker(role, siteCode string, policy rules.Policy) *Blocker {
	if !policy.Automation.Retorrent {
		return &Blocker{Code: "site_retorrent_forbidden", SiteCode: siteCode, Message: "the active rules do not allow retorrenting"}
	}
	if role == "source" && !policy.Automation.Download {
		return &Blocker{Code: "site_download_forbidden", SiteCode: siteCode, Message: "the active rules do not allow source downloads"}
	}
	if role == "source" && !policy.Automation.AutoPull {
		return &Blocker{Code: "site_auto_pull_forbidden", SiteCode: siteCode, Message: "the active rules do not allow automatic source torrent retrieval"}
	}
	if role == "target" && !policy.Automation.Upload {
		return &Blocker{Code: "site_upload_forbidden", SiteCode: siteCode, Message: "the active rules do not allow uploads"}
	}
	if role == "target" && !policy.Automation.AutoUpload {
		return &Blocker{Code: "site_auto_upload_forbidden", SiteCode: siteCode, Message: "the active rules do not allow automated uploads"}
	}
	return nil
}

func activeRuleRequired(siteCode string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{
			Code: "active_approved_rule_required", SiteCode: siteCode,
			Message: "an active approved rule revision is required before this step can run",
		}},
		NextActions: []NextAction{{
			Action:      "activate_approved_rule_revision",
			Description: "Import, approve, and activate a complete rule revision.",
			Parameters:  map[string]any{"site_code": siteCode},
		}},
		ResumeState: map[string]any{"accept_rules": map[string]any{siteCode: map[string]any{}}},
	}
}

func acceptanceRequired(siteCode string, revision rules.Revision, policy rules.Policy, code, message string) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: code, SiteCode: siteCode, Message: message}},
		NextActions: []NextAction{{
			Action:      "accept_active_rule_revision",
			Description: "Submit accepted=true, the exact fingerprint, and evidence for every manual blocking obligation.",
			Parameters:  map[string]any{"site_code": siteCode, "fingerprint": revision.Fingerprint},
		}},
		ResumeState: ruleResumeTemplate(siteCode, revision, policy),
	}
}

func policyBlock(siteCode, code, message string, revision rules.Revision, policy rules.Policy) *BlockError {
	return &BlockError{
		Blockers: []Blocker{{Code: code, SiteCode: siteCode, Message: message}},
		NextActions: []NextAction{{
			Action:      "review_site_automation_policy",
			Description: "Do not resume until a newly reviewed rule revision explicitly permits this operation.",
			Parameters:  map[string]any{"site_code": siteCode, "active_fingerprint": revision.Fingerprint},
		}},
		ResumeState: ruleResumeTemplate(siteCode, revision, policy),
	}
}

func ruleResumeTemplate(siteCode string, revision rules.Revision, policy rules.Policy) map[string]any {
	obligations := make(map[string]any)
	for _, obligation := range policy.Obligations {
		if obligation.Blocking && obligation.Verification == "manual" && obligation.Resolution != "not_applicable" {
			obligations[obligation.ID] = map[string]any{"confirmed": false, "evidence": ""}
		}
	}
	return map[string]any{
		"accept_rules": map[string]any{
			siteCode: map[string]any{
				"accepted": false, "fingerprint": revision.Fingerprint, "obligations": obligations,
			},
		},
	}
}
