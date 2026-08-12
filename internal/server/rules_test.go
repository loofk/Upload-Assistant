package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type ruleImportCapture struct {
	markdown           []byte
	correctionID       string
	correctionSection  string
	correctionData     json.RawMessage
	correctionComment  string
	discardID          string
	discardFingerprint string
}

func (capture *ruleImportCapture) Import(_ context.Context, markdown []byte, _ workflow.Actor) (rules.Revision, error) {
	capture.markdown = append([]byte(nil), markdown...)
	return rules.Revision{
		ID: "11111111-1111-4111-8111-111111111111", SiteCode: "U2", Revision: 1,
		Status: "draft", Fingerprint: strings.Repeat("a", 64),
	}, nil
}

func (*ruleImportCapture) Approve(context.Context, string, string, string, workflow.Actor) (rules.Revision, error) {
	return rules.Revision{}, nil
}

func (*ruleImportCapture) Activate(context.Context, string, workflow.Actor) (rules.Revision, error) {
	return rules.Revision{}, nil
}

func (capture *ruleImportCapture) DiscardDraft(_ context.Context, id, fingerprint string, _ workflow.Actor) (rules.Revision, error) {
	capture.discardID = id
	capture.discardFingerprint = fingerprint
	return rules.Revision{ID: id, SiteCode: "U2", Revision: 2, Status: "retired", Fingerprint: fingerprint}, nil
}

func (*ruleImportCapture) Active(context.Context, string) (rules.Revision, error) {
	return rules.Revision{}, nil
}

func (*ruleImportCapture) Get(context.Context, string) (rules.Revision, error) {
	return rules.Revision{}, nil
}

func (*ruleImportCapture) List(context.Context, string) ([]rules.Revision, error) {
	return nil, nil
}

func (*ruleImportCapture) ListSites(context.Context) ([]rules.SiteSummary, error) {
	return nil, nil
}

func (*ruleImportCapture) UpsertSite(context.Context, string, rules.SiteInput, workflow.Actor) (rules.SiteSummary, error) {
	return rules.SiteSummary{}, nil
}

func (*ruleImportCapture) GetReview(context.Context, string) (rules.ReviewWorkspace, error) {
	return rules.ReviewWorkspace{}, nil
}

func (*ruleImportCapture) SetReviewCheck(context.Context, string, string, string, string, string, workflow.Actor) (rules.ReviewWorkspace, error) {
	return rules.ReviewWorkspace{}, nil
}

func (capture *ruleImportCapture) CorrectHardGate(_ context.Context, revisionID, _ string, section string, data json.RawMessage, comment string, _ workflow.Actor) (rules.Revision, error) {
	capture.correctionID = revisionID
	capture.correctionSection = section
	capture.correctionData = append(json.RawMessage(nil), data...)
	capture.correctionComment = comment
	return rules.Revision{ID: "22222222-2222-4222-8222-222222222222", SiteCode: "U2", Revision: 2, Status: "draft", Fingerprint: strings.Repeat("b", 64)}, nil
}

func (*ruleImportCapture) ReadMarkdown(rules.Revision) ([]byte, error) {
	return nil, nil
}

func TestRuleImportAcceptsMarkdownAboveGenericJSONLimit(t *testing.T) {
	markdown := strings.Repeat("规", (1<<20)/len("规")+4096)
	payload, err := json.Marshal(importRuleRequest{Markdown: markdown})
	if err != nil {
		t.Fatal(err)
	}
	if len(payload) <= 1<<20 || len(markdown) > rules.MaxMarkdownBytes {
		t.Fatalf("invalid test sizes: payload=%d markdown=%d", len(payload), len(markdown))
	}

	capture := &ruleImportCapture{}
	request := httptest.NewRequest(http.MethodPost, "/api/v2/site-rules/import", bytes.NewReader(payload))
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "11111111-1111-4111-8111-111111111111", Role: "admin", TokenScopes: []string{"config:manage"},
	}))
	response := httptest.NewRecorder()
	(rulesAPI{service: capture}).importRule(response, request)

	if response.Code != http.StatusCreated {
		t.Fatalf("status = %d body=%s", response.Code, response.Body.String())
	}
	if string(capture.markdown) != markdown {
		t.Fatalf("captured Markdown size = %d, want %d", len(capture.markdown), len(markdown))
	}
}

func TestCorrectHardGatePassesExplicitDataWithoutInterpretingReviewComment(t *testing.T) {
	capture := &ruleImportCapture{}
	payload := `{"fingerprint":"` + strings.Repeat("a", 64) + `","data":{"upload":"100MB/s"},"comment":"AI 漏识别，原文明确要求限速"}`
	request := httptest.NewRequest(http.MethodPost, "/api/v2/site-rules/11111111-1111-4111-8111-111111111111/corrections/upload_limit", strings.NewReader(payload))
	request.SetPathValue("revision_id", "11111111-1111-4111-8111-111111111111")
	request.SetPathValue("section", "upload_limit")
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: "11111111-1111-4111-8111-111111111111", Role: "admin", TokenScopes: []string{"config:manage"},
	}))
	response := httptest.NewRecorder()
	(rulesAPI{service: capture}).correctHardGate(response, request)

	if response.Code != http.StatusCreated {
		t.Fatalf("status = %d body=%s", response.Code, response.Body.String())
	}
	if capture.correctionID != "11111111-1111-4111-8111-111111111111" || capture.correctionSection != "upload_limit" || string(capture.correctionData) != `{"upload":"100MB/s"}` || !strings.Contains(capture.correctionComment, "AI 漏识别") {
		t.Fatalf("captured correction = id:%q section:%q data:%s comment:%q", capture.correctionID, capture.correctionSection, capture.correctionData, capture.correctionComment)
	}
}

func TestDiscardRuleDraftRequiresConfirmationAndExactFingerprint(t *testing.T) {
	capture := &ruleImportCapture{}
	id := "11111111-1111-4111-8111-111111111111"
	fingerprint := strings.Repeat("a", 64)
	request := httptest.NewRequest(http.MethodPost, "/api/v2/site-rules/"+id+"/discard", strings.NewReader(`{"fingerprint":"`+fingerprint+`","confirm":true}`))
	request.SetPathValue("revision_id", id)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: id, Role: "admin", TokenScopes: []string{"config:manage"},
	}))
	response := httptest.NewRecorder()
	(rulesAPI{service: capture}).discardDraft(response, request)
	if response.Code != http.StatusOK || capture.discardID != id || capture.discardFingerprint != fingerprint {
		t.Fatalf("status=%d id=%q fingerprint=%q body=%s", response.Code, capture.discardID, capture.discardFingerprint, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodPost, "/api/v2/site-rules/"+id+"/discard", strings.NewReader(`{"fingerprint":"`+fingerprint+`","confirm":false}`))
	request.SetPathValue("revision_id", id)
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{
		UserID: id, Role: "admin", TokenScopes: []string{"config:manage"},
	}))
	response = httptest.NewRecorder()
	(rulesAPI{service: capture}).discardDraft(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("unconfirmed discard status=%d body=%s", response.Code, response.Body.String())
	}
}
