package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/siteaccess"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeSiteAccessService struct {
	policy siteaccess.EffectivePolicy
	input  siteaccess.PolicyInput
}

func (service *fakeSiteAccessService) GetPolicy(context.Context, string) (siteaccess.EffectivePolicy, error) {
	return service.policy, nil
}

func (service *fakeSiteAccessService) UpsertPolicy(_ context.Context, code string, input siteaccess.PolicyInput, _ workflow.Actor) (siteaccess.EffectivePolicy, error) {
	service.input = input
	service.policy.SiteCode = code
	service.policy.OperatorPolicy = &input
	return service.policy, nil
}

func TestSiteAccessPolicyIsFailClosedAndReturnsRecoveryAction(t *testing.T) {
	service := &fakeSiteAccessService{policy: siteaccess.EffectivePolicy{
		SiteCode: "CHD", ServiceAccess: "undetermined", SearchAccess: "undetermined",
		Blockers: []siteaccess.Blocker{{Code: "site_access_rule_v2_required", Message: "v2 rule required"}},
	}}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/sites/CHD/access-policy", nil)
	request.SetPathValue("site_code", "chd")
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{UserID: "operator", Role: "admin", TokenScopes: []string{"config:read"}}))
	response := httptest.NewRecorder()
	(siteAccessAPI{service: service}).getPolicy(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"blocked"`) || !strings.Contains(response.Body.String(), "review_and_activate_site_rule_v2") {
		t.Fatalf("policy response = %d/%s", response.Code, response.Body.String())
	}
}

func TestSiteAccessPolicyUpdateUsesExplicitLimits(t *testing.T) {
	service := &fakeSiteAccessService{policy: siteaccess.EffectivePolicy{ServiceAccess: "allowed", SearchAccess: "allowed", Blockers: []siteaccess.Blocker{}}}
	body := `{"enabled":true,"general_min_interval_seconds":5,"general_max_requests_per_hour":120,"search_min_interval_seconds":30,"search_max_requests_per_hour":20,"max_concurrency":1}`
	request := httptest.NewRequest(http.MethodPut, "/api/v2/sites/U2/access-policy", strings.NewReader(body))
	request.SetPathValue("site_code", "u2")
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{UserID: "operator", Role: "admin", TokenScopes: []string{"config:manage"}}))
	response := httptest.NewRecorder()
	(siteAccessAPI{service: service}).putPolicy(response, request)
	if response.Code != http.StatusOK || service.input.SearchMinIntervalSeconds != 30 || service.input.MaxConcurrency != 1 {
		t.Fatalf("policy input/response = %#v/%d/%s", service.input, response.Code, response.Body.String())
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || len(envelope["access_policy"]) == 0 {
		t.Fatalf("policy envelope = %#v/%v", envelope, err)
	}
}
