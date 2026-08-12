package operations

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/loofk/upload-assistant/v2/internal/rules"
)

func TestRuleAnalysisSourceSplitsOnBoundedUTF8Chunks(t *testing.T) {
	source := strings.Repeat("[rules:L0001] 中文规则内容\n", 12000)
	chunks := splitRuleAnalysisSource(source)
	if len(chunks) < 2 {
		t.Fatalf("chunk count = %d", len(chunks))
	}
	for index, chunk := range chunks {
		if len(chunk) > ruleAnalysisChunkBytes || !utf8.ValidString(chunk) || !strings.Contains(chunk, "[rules:L0001]") {
			t.Fatalf("chunk %d bytes/utf8/evidence = %d/%t/%t", index, len(chunk), utf8.ValidString(chunk), strings.Contains(chunk, "[rules:L0001]"))
		}
	}
}

func TestMergeRuleExtractionsPreservesConflictingRatesAndNamingProfiles(t *testing.T) {
	first := ruleExtraction{
		Limits:     providerLimits{Upload: providerRate{Declared: "100MB/s", Scope: "per_torrent", EvidenceRefs: []string{"rules:L0001"}}},
		Naming:     rules.Naming{Profiles: []rules.NamingProfile{{ID: "movie", Label: "电影", ReleaseTitle: rules.NamingConstraint{Required: true, Pattern: "^A$", Template: "A", EvidenceRefs: []string{"titles:L0001"}}}}},
		Confidence: .9,
	}
	second := ruleExtraction{
		Limits:     providerLimits{Upload: providerRate{Declared: "80MB/s", Scope: "per_torrent", EvidenceRefs: []string{"faq:L0002"}}},
		Naming:     rules.Naming{Profiles: []rules.NamingProfile{{ID: "movie", Label: "电影", ReleaseTitle: rules.NamingConstraint{Required: true, Pattern: "^B$", Template: "B", EvidenceRefs: []string{"titles:L0002"}}}}},
		Confidence: .8,
	}
	merged := mergeRuleExtractions([]ruleExtraction{first, second})
	if merged.Limits.Upload.Declared != "100MB/s" || len(merged.Limits.Upload.Alternatives) != 1 || merged.Limits.Upload.Alternatives[0].Declared != "80MB/s" {
		t.Fatalf("merged upload rate = %#v", merged.Limits.Upload)
	}
	if len(merged.Naming.Profiles) != 0 || !slices.ContainsFunc(merged.Conflicts, func(conflict rules.SourceConflict) bool { return conflict.Section == "naming" }) || merged.Confidence != .8 {
		t.Fatalf("merged naming/conflicts/confidence = %#v/%#v/%v", merged.Naming, merged.Conflicts, merged.Confidence)
	}
}

type fixtureRuleRevisionSource struct {
	revision rules.Revision
	markdown []byte
}

func TestRuleExtractionNormalizesStructuredObligationIntoAdvisory(t *testing.T) {
	var extraction ruleExtraction
	if err := json.Unmarshal([]byte(`{
		"automation":{},"access":{},"limits":{},"seeding":{},"transfer":{},
		"obligations":[{"id":"manual-check","scope":"upload","blocking":true,"description":"人工核对","evidence_refs":[],"enforcement":{"action":"confirm","when":"before upload"}}],
		"notes":[],"confidence":0.8,"warnings":[]
	}`), &extraction); err != nil {
		t.Fatal(err)
	}
	input := RuleAnalysisInput{SiteCode: "CHD", DisplayName: "彩虹岛", Roles: []string{"source"}, SourceURL: "https://rules.example.invalid/chd", SourceScope: "完整规则", SourceComplete: true}
	markdown, _, warnings, err := buildRuleAnalysisDraft(input, "服务端原文", extraction)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(markdown, "enforcement:") || !strings.Contains(markdown, "summary: 人工核对") {
		t.Fatalf("provider obligation was not reduced to advisory guidance:\n%s", markdown)
	}
	if !slices.ContainsFunc(warnings, func(warning string) bool { return strings.Contains(warning, "structured JSON") }) {
		t.Fatalf("normalization warnings = %#v", warnings)
	}
}

func TestRuleExtractionAcceptsNumericZeroForUnknownProviderRate(t *testing.T) {
	var extraction ruleExtraction
	if err := json.Unmarshal([]byte(`{
		"automation":{},
		"access":{"service_access":"undetermined","search_access":"undetermined"},
		"limits":{"upload":"125MB/s","seedbox_upload":0},
		"naming":{},"seeding":{},"transfer":{},
		"advisories":[],"obligations":[],"notes":[],
		"confidence":0.78,"warnings":[]
	}`), &extraction); err != nil {
		t.Fatal(err)
	}

	input := RuleAnalysisInput{
		SiteCode: "MTEAM", DisplayName: "馒头", Roles: []string{"target"},
		SourceURL: "https://rules.example.invalid/mteam", SourceScope: "完整规则", SourceComplete: true,
	}
	markdown, _, _, err := buildRuleAnalysisDraft(input, "规则正文", extraction)
	if err != nil {
		t.Fatal(err)
	}
	document, err := rules.ParseMarkdown([]byte(markdown))
	if err != nil {
		t.Fatal(err)
	}
	if document.Limits.Upload != "105MB/s" || document.Limits.SeedboxUpload != "" || document.Limits.UploadPolicy == nil ||
		document.Limits.UploadPolicy.Declared != "125MB/s" || document.Limits.UploadPolicy.SafetyMargin != "20MB/s" {
		t.Fatalf("limits = %#v", document.Limits)
	}
}

func TestRuleExtractionRejectsNonZeroNumericProviderRate(t *testing.T) {
	var extraction ruleExtraction
	err := json.Unmarshal([]byte(`{"limits":{"seedbox_upload":10}}`), &extraction)
	if err == nil || !strings.Contains(err.Error(), "unit-bearing string") {
		t.Fatalf("error = %v", err)
	}
}

func TestDecodeRuleExtractionJSONAcceptsFenceAndBoundedProse(t *testing.T) {
	for _, value := range []string{
		"```json\n{\"confidence\":0.75,\"warnings\":[]}\n```",
		"result follows:\n{\"confidence\":0.75,\"warnings\":[]}\nend",
	} {
		var extraction ruleExtraction
		if err := decodeRuleExtractionJSON(value, &extraction); err != nil {
			t.Fatalf("decode %q: %v", value, err)
		}
		if extraction.Confidence != 0.75 {
			t.Fatalf("confidence = %v", extraction.Confidence)
		}
	}
}

func TestDecodeRuleExtractionJSONRejectsIncompleteObject(t *testing.T) {
	var extraction ruleExtraction
	if err := decodeRuleExtractionJSON(`{"confidence":0.75`, &extraction); err == nil {
		t.Fatal("expected incomplete JSON to fail")
	}
}

func TestMergeRuleExtractionsKeepsAccessConflictUndetermined(t *testing.T) {
	merged := mergeRuleExtractions([]ruleExtraction{
		{Access: rules.Access{ServiceAccess: "allowed"}},
		{Access: rules.Access{ServiceAccess: "forbidden"}},
		{Access: rules.Access{ServiceAccess: "allowed"}},
	})
	if merged.Access.ServiceAccess != "undetermined" || !containsMergeConflictWarning(merged.Warnings, "access.service_access") {
		t.Fatalf("access conflict was overwritten: %#v", merged)
	}
}

func TestRuleExtractionBlocksConflictingMultiSourceRates(t *testing.T) {
	var extraction ruleExtraction
	if err := json.Unmarshal([]byte(`{
		"automation":{},"access":{},
		"limits":{"upload":{"declared":"100MB/s","scope":"per_torrent","evidence_refs":["rules:L0010"],"alternatives":[{"declared":"80MB/s","scope":"per_torrent","evidence_refs":["faq:L0020"]}]}},
		"naming":{},"seeding":{},"transfer":{},"advisories":[],"obligations":[],"notes":[],"confidence":0.9,"warnings":[]
	}`), &extraction); err != nil {
		t.Fatal(err)
	}
	input := RuleAnalysisInput{
		SiteCode: "CHD", DisplayName: "彩虹岛", Roles: []string{"source"}, SourceURL: "https://rules.example.invalid/chd", SourceScope: "多来源", SourceComplete: true,
		SourceDocuments: []rules.SourceDocument{
			{ID: "rules", URL: "https://rules.example.invalid/chd", Scope: "规则", CapturedAt: "2026-08-10T12:00:00Z", TextSHA256: strings.Repeat("a", 64), ContentType: "text/html", SizeBytes: 100},
			{ID: "faq", URL: "https://rules.example.invalid/faq", Scope: "FAQ", CapturedAt: "2026-08-10T12:01:00Z", TextSHA256: strings.Repeat("b", 64), ContentType: "text/html", SizeBytes: 120},
		},
	}
	markdown, _, _, err := buildRuleAnalysisDraft(input, "[rules:L0010] 100MB/s\n[faq:L0020] 80MB/s", extraction)
	if err != nil {
		t.Fatal(err)
	}
	document, err := rules.ParseMarkdown([]byte(markdown))
	if err != nil {
		t.Fatal(err)
	}
	if document.Limits.Upload != "" || len(document.Source.Documents) != 2 || len(document.Source.Conflicts) != 1 || document.Source.Conflicts[0].Section != "upload_limit" {
		t.Fatalf("document conflicts/limits = %#v/%#v", document.Source.Conflicts, document.Limits)
	}
}

func TestRuleExtractionPreservesCategorySpecificNamingProfiles(t *testing.T) {
	var extraction ruleExtraction
	if err := json.Unmarshal([]byte(`{
		"automation":{},"access":{},"limits":{},
		"naming":{"release_title":{"required":false},"profiles":[
			{"id":"movie","label":"电影","release_title":{"required":true,"pattern":"^.+ [0-9]{4} .+-.+$","template":"英文名 年份 参数-小组","evidence_refs":["标题格式/电影"]}},
			{"id":"tv_episode","label":"电视单集","release_title":{"required":true,"pattern":"^.+ S[0-9]{2}E[0-9]{2} .+-.+$","template":"英文名 季集 参数-小组","evidence_refs":["标题格式/电视"]}}
		]},
		"seeding":{},"transfer":{},"advisories":[],"obligations":[],"notes":[],"confidence":0.9,"warnings":[]
	}`), &extraction); err != nil {
		t.Fatal(err)
	}
	input := RuleAnalysisInput{SiteCode: "MTEAM", DisplayName: "馒头", Roles: []string{"target"}, SourceURL: "https://rules.example.invalid/mteam", SourceScope: "命名规范", SourceComplete: true}
	markdown, _, _, err := buildRuleAnalysisDraft(input, "规则正文", extraction)
	if err != nil {
		t.Fatal(err)
	}
	document, err := rules.ParseMarkdown([]byte(markdown))
	if err != nil {
		t.Fatal(err)
	}
	if len(document.Naming.Profiles) != 2 || document.Naming.Profiles[0].ID != "movie" || !document.Naming.Profiles[1].ReleaseTitle.Required {
		t.Fatalf("naming profiles = %#v", document.Naming.Profiles)
	}
}

func (f fixtureRuleRevisionSource) Get(context.Context, string) (rules.Revision, error) {
	return f.revision, nil
}

func (f fixtureRuleRevisionSource) ReadMarkdown(rules.Revision) ([]byte, error) {
	return f.markdown, nil
}

func TestResolveRuleAnalysisInputBindsStoredRevisionOriginal(t *testing.T) {
	stored := RuleAnalysisInput{
		SiteCode: "CHD", DisplayName: "彩虹岛", Roles: []string{"source"},
		SourceURL: "https://rules.example.invalid/chd", SourceScope: "完整上传规则",
		SourceComplete: true,
	}
	markdown, _, _, err := buildRuleAnalysisDraft(stored, "服务端保存的不可变原文", ruleExtraction{})
	if err != nil {
		t.Fatal(err)
	}
	service := &DiagnosticService{RuleSource: fixtureRuleRevisionSource{
		revision: rules.Revision{ID: "11111111-1111-4111-8111-111111111111"},
		markdown: []byte(markdown),
	}}
	resolved, err := service.resolveRuleAnalysisInput(context.Background(), RuleAnalysisInput{
		ProviderID: "provider", SourceRevisionID: "11111111-1111-4111-8111-111111111111",
		SiteCode: "MTEAM", SourceText: "客户端试图替换的原文", SourceComplete: false,
	})
	if err != nil {
		t.Fatal(err)
	}
	if resolved.SiteCode != "CHD" || resolved.DisplayName != "彩虹岛" || resolved.SourceText != "服务端保存的不可变原文" || !resolved.SourceComplete {
		t.Fatalf("stored revision did not override client source fields: %#v", resolved)
	}
}
