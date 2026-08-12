package rules

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestLocalRuleDraftsUseSafeYAMLContract(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join("..", "..", "data", "site-rules", "*.md"))
	if err != nil {
		t.Fatalf("Glob() error = %v", err)
	}
	fingerprintPattern := regexp.MustCompile(`^[a-f0-9]{64}$`)
	validated := 0

	for _, path := range paths {
		if filepath.Base(path) == "README.md" {
			continue
		}
		path := path
		t.Run(filepath.Base(path), func(t *testing.T) {
			t.Parallel()
			raw, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("ReadFile(%s) error = %v", path, err)
			}
			document, err := ParseMarkdown(raw)
			if err != nil {
				t.Fatalf("ParseMarkdown(%s) error = %v", path, err)
			}
			if document.Format != "yaml" || (document.Kind != Kind && document.Kind != KindV2) {
				t.Fatalf("repository rule format=%q kind=%q", document.Format, document.Kind)
			}
			if len(document.Site.Roles) != 1 {
				t.Fatalf("repository rule site=%q roles=%v", document.Site.Code, document.Site.Roles)
			}
			if !document.Source.Complete {
				if document.Review.Status != "draft" {
					t.Fatalf("incomplete local rule review=%q", document.Review.Status)
				}
				if document.Automation.AutoPull || document.Automation.AutoUpload {
					t.Fatalf("incomplete local rule enabled automation: auto_pull=%t auto_upload=%t", document.Automation.AutoPull, document.Automation.AutoUpload)
				}
			}
			pendingManualBlocker := false
			for _, obligation := range document.Obligations {
				if obligation.Verification == "manual" && obligation.Blocking && obligation.Resolution == "pending" {
					pendingManualBlocker = true
					break
				}
			}
			if !document.Source.Complete && !pendingManualBlocker {
				t.Fatal("incomplete local rule has no pending blocking manual obligation")
			}
			fingerprint, err := document.Fingerprint()
			if err != nil {
				t.Fatalf("Fingerprint() error = %v", err)
			}
			if !fingerprintPattern.MatchString(fingerprint) {
				t.Fatalf("Fingerprint() = %q", fingerprint)
			}
			policyJSON, err := document.PolicyJSON()
			if err != nil {
				t.Fatalf("PolicyJSON() error = %v", err)
			}
			policy, err := ParsePolicy(policyJSON)
			if err != nil {
				t.Fatalf("ParsePolicy() error = %v", err)
			}
			if policy.Site.Code != document.Site.Code || policy.Source.Complete != document.Source.Complete || policy.Automation.AutoPull != document.Automation.AutoPull || policy.Automation.AutoUpload != document.Automation.AutoUpload {
				t.Fatalf("unsafe policy round trip: %#v", policy)
			}
		})
		validated++
	}
	if validated == 0 {
		t.Skip("local tracker rule evidence is intentionally gitignored")
	}
}

func TestParseYAMLRuleAndStableFingerprint(t *testing.T) {
	raw := testRuleMarkdown(false)
	document, err := ParseMarkdown([]byte(raw))
	if err != nil {
		t.Fatalf("ParseMarkdown() error = %v", err)
	}
	if document.Site.Code != "U2" || document.Source.Complete || document.Format != "yaml" {
		t.Fatalf("unexpected document: site=%s complete=%t format=%s", document.Site.Code, document.Source.Complete, document.Format)
	}
	if !document.Transfer.ForbidOriginalTorrent || !document.Transfer.PreserveContent {
		t.Fatal("hard transfer rules were not parsed")
	}
	fingerprint, err := document.Fingerprint()
	if err != nil {
		t.Fatalf("Fingerprint() error = %v", err)
	}
	document.Review.Status = "approved"
	document.Review.Reviewer = "someone"
	again, err := document.Fingerprint()
	if err != nil {
		t.Fatalf("Fingerprint() second error = %v", err)
	}
	if fingerprint != again {
		t.Fatal("review metadata changed immutable rule fingerprint")
	}
}

func TestParseLegacyTOMLRule(t *testing.T) {
	raw := `+++
schema_version = 1
kind = "ptcli.site_rule_document.v1"
tracker = "CHD"
display_name = "CHDBits"
roles = ["source"]
rules_url = "https://ptchdbits.co/rules.php"
captured_at = "2026-08-08"
source_complete = false
source_scope = "User supplied rules"
review_status = "draft"

[automation]
manual_review_required = true
download = true
upload = false
retorrent = true

[qbit_limits]
download_limit = "20MiB/s"

[seeding_requirements]
min_seed_time_hours = 72

[transfer_rules]
freeleech_required = false

[[obligations]]
id = "chd-no-direct-torrent-reuse"
scope = "retorrent"
verification = "programmatic"
blocking = true
resolution = "pending"
description = "Do not reuse source torrent"
enforcement = "Generate a new torrent"
+++

# Original rules

Do not upload the source torrent to another tracker.
`
	document, err := ParseMarkdown([]byte(raw))
	if err != nil {
		t.Fatalf("ParseMarkdown() legacy error = %v", err)
	}
	if document.Kind != Kind || document.Site.Code != "CHD" || document.Format != "toml" {
		t.Fatalf("unexpected legacy document: kind=%s site=%s format=%s", document.Kind, document.Site.Code, document.Format)
	}
	if len(document.Obligations) != 1 || document.Obligations[0].ID != "chd-no-direct-torrent-reuse" {
		t.Fatalf("legacy obligations = %#v", document.Obligations)
	}
}

func TestRuleTextSHA256MismatchIsRejected(t *testing.T) {
	raw := strings.Replace(testRuleMarkdown(false), "text_sha256: \"\"", "text_sha256: deadbeef", 1)
	if _, err := ParseMarkdown([]byte(raw)); err == nil {
		t.Fatal("ParseMarkdown() checksum error = nil")
	}
}

func TestRuleMarkdownSizeLimitIsEnforced(t *testing.T) {
	raw := make([]byte, MaxMarkdownBytes+1)
	if _, err := ParseMarkdown(raw); err == nil || !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("ParseMarkdown() size error = %v", err)
	}
}

func TestNegativeSeedingRequirementIsRejectedInDocumentAndPolicy(t *testing.T) {
	raw := strings.Replace(testRuleMarkdown(false), "minimum_time_hours: 72", "minimum_time_hours: -1", 1)
	if _, err := ParseMarkdown([]byte(raw)); err == nil {
		t.Fatal("ParseMarkdown() accepted a negative minimum seeding time")
	}
	if _, err := ParsePolicy([]byte(`{"schema_version":1,"site":{"code":"MTEAM"},"seeding":{"minimum_ratio":-1}}`)); err == nil {
		t.Fatal("ParsePolicy() accepted a negative minimum seeding ratio")
	}
}

func testRuleMarkdown(complete bool) string {
	completeValue := "false"
	if complete {
		completeValue = "true"
	}
	return `---
schema_version: 1
kind: upload-assistant.site-rule.v1
site:
  code: U2
  display_name: U2
  roles: [source]
source:
  url: https://u2.dmhy.org/rules.php
  captured_at: "2026-08-08"
  complete: ` + completeValue + `
  scope: User supplied source and transfer rules
  text_sha256: ""
automation:
  manual_review_required: true
  download: true
  upload: false
  retorrent: true
  auto_pull: false
  auto_upload: false
limits:
  download: 20MiB/s
seeding:
  minimum_time_hours: 72
transfer:
  freeleech_required: false
  forbid_original_torrent: true
  preserve_content: true
obligations:
  - id: u2-origin-repost-restrictions
    scope: source_download
    verification: manual
    blocking: true
    resolution: pending
    description: Verify original repost restrictions
    evidence_refs: [rule-6]
    enforcement: Stop when the source permission cannot be established
notes: []
review:
  status: draft
---

# Original rules

Do not upload the source torrent to another tracker.
`
}
