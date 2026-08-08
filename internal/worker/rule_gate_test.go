package worker

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeRuleProvider struct {
	revision rules.Revision
	err      error
}

func (provider fakeRuleProvider) Active(_ context.Context, siteCode string) (rules.Revision, error) {
	if provider.err != nil {
		return rules.Revision{}, provider.err
	}
	if provider.revision.SiteCode != siteCode {
		return rules.Revision{}, rules.ErrNotFound
	}
	return provider.revision, nil
}

func TestRuleGateRequiresActiveRule(t *testing.T) {
	executor := ruleGateExecutor{provider: fakeRuleProvider{err: rules.ErrNotFound}, role: "source"}
	_, err := executor.Execute(context.Background(), Execution{Job: workflow.Job{
		Input: json.RawMessage(`{"source_url":"https://u2.dmhy.org/details.php?id=60635","target":"MTEAM"}`),
	}})
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "active_approved_rule_required" {
		t.Fatalf("blocker code = %s", blocked.Blockers[0].Code)
	}
}

func TestRuleGateRequiresExactFingerprintAndManualEvidence(t *testing.T) {
	revision := testRuleRevision(t, "source", true)
	executor := ruleGateExecutor{provider: fakeRuleProvider{revision: revision}, role: "source"}

	_, err := executor.Execute(context.Background(), ruleExecution(map[string]any{}))
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "rule_acceptance_required" {
		t.Fatalf("missing acceptance blocker = %s", blocked.Blockers[0].Code)
	}

	_, err = executor.Execute(context.Background(), ruleExecution(map[string]any{
		"U2": map[string]any{"accepted": true, "fingerprint": "stale"},
	}))
	blocked = requireBlockError(t, err)
	if blocked.Blockers[0].Code != "rule_fingerprint_mismatch" {
		t.Fatalf("fingerprint blocker = %s", blocked.Blockers[0].Code)
	}

	_, err = executor.Execute(context.Background(), ruleExecution(map[string]any{
		"U2": map[string]any{
			"accepted": true, "fingerprint": revision.Fingerprint,
			"obligations": map[string]any{"repost-permission": map[string]any{"confirmed": true, "evidence": ""}},
		},
	}))
	blocked = requireBlockError(t, err)
	if blocked.Blockers[0].Code != "manual_rule_obligation_required" || blocked.Blockers[0].ObligationID != "repost-permission" {
		t.Fatalf("manual blocker = %#v", blocked.Blockers[0])
	}
}

func TestRuleGateAcceptsResumeStateBoundToActiveFingerprint(t *testing.T) {
	revision := testRuleRevision(t, "source", true)
	executor := ruleGateExecutor{provider: fakeRuleProvider{revision: revision}, role: "source"}
	execution := ruleExecution(map[string]any{})
	execution.Job.ResumeState = mustJSON(map[string]any{
		"accept_rules": map[string]any{
			"u2": map[string]any{
				"accepted": true, "fingerprint": revision.Fingerprint,
				"obligations": map[string]any{
					"repost-permission": map[string]any{"confirmed": true, "evidence": "No source-side prohibition marker was present."},
				},
			},
		},
	})
	output, err := executor.Execute(context.Background(), execution)
	if err != nil {
		t.Fatalf("Execute() error = %v", err)
	}
	var result struct {
		SiteCode         string `json:"site_code"`
		Fingerprint      string `json:"fingerprint"`
		Accepted         bool   `json:"accepted"`
		AcceptanceSHA256 string `json:"acceptance_sha256"`
	}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if result.SiteCode != "U2" || result.Fingerprint != revision.Fingerprint || !result.Accepted || len(result.AcceptanceSHA256) != 64 {
		t.Fatalf("unexpected gate output: %#v", result)
	}
}

func TestRuleGateBlocksDisallowedTargetAutomation(t *testing.T) {
	revision := testRuleRevision(t, "target", false)
	executor := ruleGateExecutor{provider: fakeRuleProvider{revision: revision}, role: "target"}
	_, err := executor.Execute(context.Background(), Execution{Job: workflow.Job{
		Input: mustJSON(map[string]any{
			"source_url": "https://u2.dmhy.org/details.php?id=60635", "target": "U2",
		}),
		ResumeState: json.RawMessage(`{}`),
	}})
	blocked := requireBlockError(t, err)
	if blocked.Blockers[0].Code != "site_auto_upload_forbidden" {
		t.Fatalf("automation blocker = %#v", blocked.Blockers[0])
	}
}

func testRuleRevision(t *testing.T, role string, allowAutomation bool) rules.Revision {
	t.Helper()
	policy := rules.Policy{
		SchemaVersion: 1,
		Site:          rules.Site{Code: "U2", DisplayName: "U2", Roles: []string{role}},
		Source:        rules.Source{URL: "https://u2.dmhy.org/rules.php", Complete: true},
		Automation: rules.Automation{
			Download: true, Upload: true, Retorrent: true,
			AutoPull: true, AutoUpload: allowAutomation,
		},
		Obligations: []rules.Obligation{{
			ID: "repost-permission", Scope: "retorrent", Verification: "manual",
			Blocking: true, Resolution: "pending", Description: "Verify source-side repost permission.",
			Enforcement: "Require per-job evidence.",
		}},
	}
	body, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	return rules.Revision{
		ID: "3ae52d0b-7c03-4e2c-b9c8-610999fe329f", SiteCode: "U2", Status: "approved",
		Fingerprint: "7e027ba6785be404549f2f52f2511974929899c81c5a64d88c9a46fe54f48d09", Policy: body,
	}
}

func ruleExecution(acceptRules map[string]any) Execution {
	return Execution{Job: workflow.Job{
		Input: mustJSON(map[string]any{
			"source_url": "https://u2.dmhy.org/details.php?id=60635",
			"target":     "MTEAM", "accept_rules": acceptRules,
		}),
		ResumeState: json.RawMessage(`{}`),
	}}
}

func requireBlockError(t *testing.T, err error) *BlockError {
	t.Helper()
	var blocked *BlockError
	if !errors.As(err, &blocked) {
		t.Fatalf("error = %v, want BlockError", err)
	}
	if len(blocked.Blockers) == 0 {
		t.Fatal("BlockError has no blockers")
	}
	return blocked
}
