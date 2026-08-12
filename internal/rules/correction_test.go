package rules

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestApplyUploadLimitCorrectionChangesExecutablePolicyAndPreservesSource(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(false)))
	if err != nil {
		t.Fatal(err)
	}
	originalBody := document.Body
	originalURL := document.Source.URL
	if err := applyHardGateCorrection(&document, "upload_limit", json.RawMessage(`{"upload":"100MB/s","seedbox_upload":"20MiB/s"}`)); err != nil {
		t.Fatal(err)
	}
	if document.Limits.Upload != "100MB/s" || document.Limits.SeedboxUpload != "20MiB/s" {
		t.Fatalf("corrected limits = %#v", document.Limits)
	}
	raw, err := RenderMarkdown(document)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := ParseMarkdown(raw)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Body != originalBody || parsed.Source.URL != originalURL {
		t.Fatalf("source changed: url=%q body_equal=%t", parsed.Source.URL, parsed.Body == originalBody)
	}
	policy, err := parsed.PolicyJSON()
	if err != nil {
		t.Fatal(err)
	}
	typed, err := ParsePolicy(policy)
	if err != nil {
		t.Fatal(err)
	}
	if typed.Limits.Upload != "100MB/s" || typed.Limits.SeedboxUpload != "20MiB/s" {
		t.Fatalf("executable limits = %#v", typed.Limits)
	}
}

func TestApplyHardGateCorrectionRejectsUnknownAndInvalidFields(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(false)))
	if err != nil {
		t.Fatal(err)
	}
	for name, raw := range map[string]string{
		"unknown": `{"upload":"100MB/s","download":"1MiB/s"}`,
		"invalid": `{"upload":"100"}`,
	} {
		t.Run(name, func(t *testing.T) {
			copy := document
			if err := applyHardGateCorrection(&copy, "upload_limit", json.RawMessage(raw)); err == nil {
				t.Fatal("expected correction error")
			}
		})
	}
}

func TestApplyNamingCorrectionRequiresEnforceableAnchoredPattern(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(false)))
	if err != nil {
		t.Fatal(err)
	}
	valid := json.RawMessage(`{"release_title":{"required":true,"pattern":"^.+-[A-Za-z0-9]+$","template":"{release}-{group}"},"content_name":{"required":false}}`)
	if err := applyHardGateCorrection(&document, "naming", valid); err != nil {
		t.Fatal(err)
	}
	if !document.Naming.ReleaseTitle.Required || !strings.HasPrefix(document.Naming.ReleaseTitle.Pattern, "^") {
		t.Fatalf("corrected naming = %#v", document.Naming)
	}
	invalid := json.RawMessage(`{"release_title":{"required":true,"pattern":".*"}}`)
	if err := applyHardGateCorrection(&document, "naming", invalid); err == nil {
		t.Fatal("expected unanchored naming correction error")
	}
}
