package server

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/readiness"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

type fakeLiveReadiness struct{ input readiness.Input }

func (service *fakeLiveReadiness) Check(_ context.Context, input readiness.Input) (readiness.Report, error) {
	service.input = input
	return readiness.Report{
		OK: false, Status: "blocked", ConfigurationReady: false, ExternalCallsPerformed: false, LiveUploadAuthorized: false,
		Source: input.Source, Target: input.Target, Checks: []readiness.Check{}, RequiredConfirmations: []readiness.RuleConfirmation{},
		Blockers:    []readiness.Blocker{{Code: "active_rule_required", Component: "rules.U2", Message: "missing"}},
		NextActions: []readiness.NextAction{{Action: "import_review_activate_site_rules"}},
		ResumeState: map[string]any{"confirm_upload": false}, Summary: "blocked",
	}, nil
}

func TestLiveReadinessRouteReturnsSafeMachineReadableHandoff(t *testing.T) {
	service := &fakeLiveReadiness{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), LiveReadiness: service,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "auditor", TokenScopes: []string{"config:read"}}},
	})
	request := httptest.NewRequest(http.MethodGet, "/api/v2/readiness/live?source=U2&target=MTEAM&downloader=box&target_downloader=seedbox&image_host=imgbb&screenshot_profile=six&tmdb_provider=tmdb-main&ptgen_provider=ptgen-main", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || service.input.Source != "U2" || service.input.TargetDownloader != "seedbox" || service.input.TMDbProvider != "tmdb-main" || service.input.PTGenProvider != "ptgen-main" {
		t.Fatalf("response=%d %s input=%#v", response.Code, response.Body.String(), service.input)
	}
	for _, expected := range []string{`"status":"blocked"`, `"external_calls_performed":false`, `"live_upload_authorized":false`, `"confirm_upload":false`, `"active_rule_required"`} {
		if !strings.Contains(response.Body.String(), expected) {
			t.Fatalf("response missing %s: %s", expected, response.Body.String())
		}
	}
}
