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
	markdown []byte
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
