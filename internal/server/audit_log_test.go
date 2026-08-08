package server

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/auditlog"
	"github.com/loofk/upload-assistant/v2/internal/buildinfo"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

type fakeAuditLog struct {
	filter auditlog.Filter
	calls  int
}

func (store *fakeAuditLog) List(_ context.Context, filter auditlog.Filter) (auditlog.Page, error) {
	store.calls++
	store.filter = filter
	return auditlog.Page{Events: []auditlog.Event{{
		ID: "00000000-0000-0000-0000-000000000001", ActorType: "worker", ActorID: "fixture",
		Action: "downloader.torrent.add", ResourceType: "downloader", ResourceID: "box-id",
		Payload:   json.RawMessage(`{"api_key":"must-not-leak","configuration_sha256":"` + strings.Repeat("a", 64) + `"}`),
		CreatedAt: time.Date(2026, 8, 8, 1, 2, 3, 0, time.UTC),
	}}, HasMore: true}, nil
}

func TestAuditLogRouteRequiresAuditReadAndRejectsMalformedCursor(t *testing.T) {
	service := &fakeAuditLog{}
	newHandler := func(principal security.Principal) http.Handler {
		return New(Dependencies{
			Database: fakeDatabase{}, DataDir: t.TempDir(), AuditLog: service,
			Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
			Auth: fakeAuthenticator{principal: principal},
		})
	}
	request := httptest.NewRequest(http.MethodGet, "/api/v2/audit-events", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response := httptest.NewRecorder()
	newHandler(security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"config:read"}}).ServeHTTP(response, request)
	if response.Code != http.StatusForbidden || service.calls != 0 {
		t.Fatalf("forbidden response=%d %s calls=%d", response.Code, response.Body.String(), service.calls)
	}

	request = httptest.NewRequest(http.MethodGet, "/api/v2/audit-events?cursor=not-a-cursor", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response = httptest.NewRecorder()
	newHandler(security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"audit:read"}}).ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || service.calls != 0 || !bytes.Contains(response.Body.Bytes(), []byte(`"code":"invalid_cursor"`)) {
		t.Fatalf("invalid cursor response=%d %s calls=%d", response.Code, response.Body.String(), service.calls)
	}
}

func TestAuditLogRouteFiltersPaginatesAndRedacts(t *testing.T) {
	service := &fakeAuditLog{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), AuditLog: service,
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)), Build: buildinfo.Info{Version: "test"},
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"*"}}},
	})
	request := httptest.NewRequest(http.MethodGet, "/api/v2/audit-events?resource_type=downloader&action=downloader.torrent.add&limit=25", nil)
	request.Header.Set("Authorization", "Bearer fixture")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || service.filter.ResourceType != "downloader" || service.filter.Action != "downloader.torrent.add" || service.filter.Limit != 25 {
		t.Fatalf("response=%d %s filter=%#v", response.Code, response.Body.String(), service.filter)
	}
	if bytes.Contains(response.Body.Bytes(), []byte("must-not-leak")) || !bytes.Contains(response.Body.Bytes(), []byte(`"api_key":"[REDACTED]"`)) || !bytes.Contains(response.Body.Bytes(), []byte(`"has_more":true`)) {
		t.Fatalf("unsafe audit response: %s", response.Body.String())
	}
	var envelope struct {
		NextCursor string `json:"next_cursor"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil || envelope.NextCursor == "" {
		t.Fatalf("cursor/error = %q/%v", envelope.NextCursor, err)
	}
}
