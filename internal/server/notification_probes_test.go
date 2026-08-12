package server

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/notifications"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeNotificationProbeService struct{ calls int }

func (service *fakeNotificationProbeService) Probe(_ context.Context, name string, _ workflow.Actor) (notifications.ProbeResult, error) {
	service.calls++
	return notifications.ProbeResult{NotificationID: "probe-id", ChannelName: name, Status: "sent"}, nil
}

func TestNotificationProbeRequiresExplicitDeliveryConfirmation(t *testing.T) {
	service := &fakeNotificationProbeService{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), Notifications: service,
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "user", Role: "admin", TokenScopes: []string{"config:manage"}}},
	})
	request := httptest.NewRequest(http.MethodPost, "/api/v2/notification-channels/alerts/probe", bytes.NewBufferString(`{"confirm_delivery":false}`))
	request.Header.Set("Authorization", "Bearer fixture")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || service.calls != 0 {
		t.Fatalf("unconfirmed probe = %d calls=%d", response.Code, service.calls)
	}

	request = httptest.NewRequest(http.MethodPost, "/api/v2/notification-channels/alerts/probe", bytes.NewBufferString(`{"confirm_delivery":true}`))
	request.Header.Set("Authorization", "Bearer fixture")
	request.Header.Set("Content-Type", "application/json")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || service.calls != 1 || !bytes.Contains(response.Body.Bytes(), []byte(`"status":"sent"`)) {
		t.Fatalf("confirmed probe = %d calls=%d body=%s", response.Code, service.calls, response.Body.String())
	}
}
