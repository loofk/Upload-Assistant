package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/rulecollector"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeRuleCollectionService struct {
	set      rulecollector.SourceSet
	run      rulecollector.CollectionRun
	putInput rulecollector.SourceSetInput
	create   rulecollector.CreateRunInput
	actor    workflow.Actor
}

func (service *fakeRuleCollectionService) GetSourceSet(context.Context, string) (rulecollector.SourceSet, error) {
	return service.set, nil
}

func (service *fakeRuleCollectionService) PutSourceSet(_ context.Context, site string, input rulecollector.SourceSetInput, actor workflow.Actor) (rulecollector.SourceSet, error) {
	service.putInput, service.actor = input, actor
	service.set.SiteCode = site
	service.set.Sources = input.Sources
	service.set.ScopeConfirmed = input.ScopeConfirmed
	service.set.CookieHostsConfirmed = input.CookieHostsConfirmed
	return service.set, nil
}

func (service *fakeRuleCollectionService) CreateRun(_ context.Context, site string, input rulecollector.CreateRunInput, actor workflow.Actor) (rulecollector.CollectionRun, error) {
	service.create, service.actor = input, actor
	service.run.SiteCode = site
	return service.run, nil
}

func (service *fakeRuleCollectionService) GetRun(context.Context, string) (rulecollector.CollectionRun, error) {
	return service.run, nil
}

func (service *fakeRuleCollectionService) LatestRun(context.Context, string) (rulecollector.CollectionRun, error) {
	return service.run, nil
}

func TestRuleCollectionAPIRequiresExactSourceFingerprintAndIdempotency(t *testing.T) {
	fingerprint := strings.Repeat("a", 64)
	service := &fakeRuleCollectionService{
		set: rulecollector.SourceSet{Fingerprint: fingerprint, CookieConfigured: true},
		run: rulecollector.CollectionRun{ID: "33333333-3333-4333-8333-333333333333", Status: "queued", NotBefore: time.Now(), Documents: []rulecollector.CollectionDocument{}, CreatedAt: time.Now(), UpdatedAt: time.Now()},
	}
	principal := security.Principal{UserID: "11111111-1111-4111-8111-111111111111", Role: "admin", TokenScopes: []string{"config:manage"}}
	sources := `{"sources":[{"id":"titles","url":"https://wiki.example.invalid/title-rules","scope":"标题规则"}],"scope_confirmed":true,"cookie_hosts_confirmed":true}`
	putRequest := httptest.NewRequest(http.MethodPut, "/api/v2/sites/MTEAM/rule-sources", strings.NewReader(sources))
	putRequest.SetPathValue("site_code", "mteam")
	putRequest = putRequest.WithContext(security.WithPrincipal(putRequest.Context(), principal))
	putResponse := httptest.NewRecorder()
	(ruleCollectionAPI{service: service}).putSources(putResponse, putRequest)
	if putResponse.Code != http.StatusOK || len(service.putInput.Sources) != 1 || !service.putInput.ScopeConfirmed || service.actor.ID != principal.UserID {
		t.Fatalf("put source set = %d/%s input=%#v actor=%#v", putResponse.Code, putResponse.Body.String(), service.putInput, service.actor)
	}

	createRequest := httptest.NewRequest(http.MethodPost, "/api/v2/sites/MTEAM/rule-collection-runs", strings.NewReader(`{"source_set_fingerprint":"`+fingerprint+`","provider_id":"22222222-2222-4222-8222-222222222222","confirm":true}`))
	createRequest.SetPathValue("site_code", "mteam")
	createRequest.Header.Set("Idempotency-Key", "rule-collection-test")
	createRequest = createRequest.WithContext(security.WithPrincipal(createRequest.Context(), principal))
	createResponse := httptest.NewRecorder()
	(ruleCollectionAPI{service: service}).createRun(createResponse, createRequest)
	if createResponse.Code != http.StatusAccepted || service.create.SourceSetFingerprint != fingerprint || service.create.IdempotencyKey != "rule-collection-test" || !service.create.Confirm {
		t.Fatalf("create run = %d/%s input=%#v", createResponse.Code, createResponse.Body.String(), service.create)
	}
	var envelope map[string]json.RawMessage
	if json.Unmarshal(createResponse.Body.Bytes(), &envelope) != nil || len(envelope["run"]) == 0 || len(envelope["blockers"]) == 0 {
		t.Fatalf("collection envelope = %s", createResponse.Body.String())
	}
}

func TestRuleCollectionAPIRequiresExplicitConfirmation(t *testing.T) {
	service := &fakeRuleCollectionService{}
	request := httptest.NewRequest(http.MethodPost, "/api/v2/sites/CHD/rule-collection-runs", strings.NewReader(`{"source_set_fingerprint":"`+strings.Repeat("a", 64)+`","provider_id":"22222222-2222-4222-8222-222222222222"}`))
	request.SetPathValue("site_code", "CHD")
	request.Header.Set("Idempotency-Key", "rule-collection-test")
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{UserID: "11111111-1111-4111-8111-111111111111", Role: "admin", TokenScopes: []string{"config:manage"}}))
	response := httptest.NewRecorder()
	(ruleCollectionAPI{service: service}).createRun(response, request)
	if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), "confirmation_required") || service.create.ProviderID != "" {
		t.Fatalf("confirmation forwarding = %d/%s input=%#v", response.Code, response.Body.String(), service.create)
	}
}

func TestRuleCollectionAPIMissingIdempotencyKeyFailsBeforeWork(t *testing.T) {
	service := &fakeRuleCollectionService{}
	request := httptest.NewRequest(http.MethodPost, "/api/v2/sites/CHD/rule-collection-runs", strings.NewReader(`{"source_set_fingerprint":"`+strings.Repeat("a", 64)+`","provider_id":"22222222-2222-4222-8222-222222222222","confirm":true}`))
	request.SetPathValue("site_code", "CHD")
	request = request.WithContext(security.WithPrincipal(request.Context(), security.Principal{UserID: "11111111-1111-4111-8111-111111111111", Role: "admin", TokenScopes: []string{"config:manage"}}))
	response := httptest.NewRecorder()
	(ruleCollectionAPI{service: service}).createRun(response, request)
	if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), "idempotency_key_required") || service.create.ProviderID != "" {
		t.Fatalf("missing idempotency response = %d/%s", response.Code, response.Body.String())
	}
}

func TestPublicRuleSourcesDoNotRequireCookieOrCookieHostConfirmation(t *testing.T) {
	set := rulecollector.SourceSet{
		Sources:        []rulecollector.SourceInput{{ID: "wiki", URL: "https://wiki.example.invalid/rules", Scope: "公开规则", AuthMode: rulecollector.SourceAuthNone}},
		ScopeConfirmed: true, CookieRequired: false, CookieConfigured: false, CookieHostsConfirmed: false,
	}
	if blockers := sourceSetBlockers(set); len(blockers) != 0 {
		t.Fatalf("public source blockers = %#v", blockers)
	}
	set.Sources[0].AuthMode = rulecollector.SourceAuthSiteCookie
	set.CookieRequired = true
	if blockers := sourceSetBlockers(set); len(blockers) != 2 {
		t.Fatalf("Cookie source blockers = %#v", blockers)
	}
}
