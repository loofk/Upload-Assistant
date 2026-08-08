package server

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/legacy"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeLegacyMigration struct {
	preview legacy.Preview
	record  legacy.ImportRecord
	err     error
}

func (fake fakeLegacyMigration) Preview(context.Context) (legacy.Preview, error) {
	return fake.preview, fake.err
}

func (fake fakeLegacyMigration) Import(_ context.Context, _ legacy.ImportRequest, _ workflow.Actor) (legacy.ImportRecord, error) {
	return fake.record, fake.err
}

func (fake fakeLegacyMigration) Get(context.Context, string) (legacy.ImportRecord, error) {
	return fake.record, fake.err
}

func (fake fakeLegacyMigration) List(context.Context, int) ([]legacy.ImportRecord, error) {
	return []legacy.ImportRecord{fake.record}, fake.err
}

func TestLegacyMigrationPreviewAndExecuteRoutes(t *testing.T) {
	preview := legacy.Preview{
		OK: true, Status: "ready", SourceFingerprint: strings.Repeat("a", 64),
		Blockers: []legacy.Issue{}, Warnings: []legacy.Issue{}, NextActions: []legacy.Issue{},
	}
	record := legacy.ImportRecord{
		ID: "01901111-1111-7111-8111-111111111111", OK: true, Status: "complete",
		SourceFingerprint: preview.SourceFingerprint, ArchiveAvailable: true,
		ArchiveExpiresAt: time.Now().UTC().Add(30 * 24 * time.Hour),
		Report:           legacy.ImportReport{Blockers: []legacy.Issue{}, NextActions: []legacy.Issue{}, Summary: "complete"},
	}
	handler := New(Dependencies{
		Auth:   fakeAuthenticator{principal: security.Principal{UserID: "01901111-1111-7111-8111-111111111112", Role: "admin", TokenScopes: []string{"*"}}},
		Legacy: fakeLegacyMigration{preview: preview, record: record},
	})

	request := httptest.NewRequest(http.MethodGet, "/api/v2/migrations/legacy/preview", nil)
	request.Header.Set("Authorization", "Bearer ua_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), preview.SourceFingerprint) || strings.Contains(response.Body.String(), "private") {
		t.Fatalf("preview response = %d %s", response.Code, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodPost, "/api/v2/migrations/legacy", strings.NewReader(`{"source_fingerprint":"`+preview.SourceFingerprint+`","confirm_import":true}`))
	request.Header.Set("Authorization", "Bearer ua_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusCreated || !strings.Contains(response.Body.String(), `"status":"complete"`) || !strings.Contains(response.Body.String(), `"archive_available":true`) {
		t.Fatalf("execute response = %d %s", response.Code, response.Body.String())
	}
}

func TestLegacyMigrationRequiresConfirmationAndFingerprint(t *testing.T) {
	principal := security.Principal{UserID: "01901111-1111-7111-8111-111111111112", Role: "admin", TokenScopes: []string{"*"}}
	for _, test := range []struct {
		err  error
		code int
		want string
	}{
		{err: legacy.ErrConfirmationRequired, code: http.StatusUnprocessableEntity, want: "confirm_import_required"},
		{err: legacy.ErrFingerprintMismatch, code: http.StatusConflict, want: "source_fingerprint_mismatch"},
	} {
		handler := New(Dependencies{Auth: fakeAuthenticator{principal: principal}, Legacy: fakeLegacyMigration{err: test.err}})
		request := httptest.NewRequest(http.MethodPost, "/api/v2/migrations/legacy", strings.NewReader(`{"source_fingerprint":"old","confirm_import":true}`))
		request.Header.Set("Authorization", "Bearer ua_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != test.code || !strings.Contains(response.Body.String(), test.want) {
			t.Fatalf("error %v response = %d %s", test.err, response.Code, response.Body.String())
		}
	}
}
