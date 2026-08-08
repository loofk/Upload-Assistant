package notifications

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

type fakeDeliveryStore struct {
	delivery  Delivery
	runtime   integrations.RuntimeNotificationChannel
	completed map[string]any
	failed    bool
	claimed   bool
}

func (store *fakeDeliveryStore) Claim(context.Context, string, time.Time, time.Duration) (Delivery, error) {
	if store.claimed {
		return Delivery{}, ErrNoDelivery
	}
	store.claimed = true
	return store.delivery, nil
}
func (store *fakeDeliveryStore) Complete(_ context.Context, _, _ string, receipt map[string]any) error {
	store.completed = receipt
	return nil
}
func (store *fakeDeliveryStore) Fail(context.Context, string, string, time.Time, time.Duration) error {
	store.failed = true
	return nil
}
func (store *fakeDeliveryStore) GetRuntimeNotificationChannel(context.Context, string) (integrations.RuntimeNotificationChannel, error) {
	return store.runtime, nil
}

func TestDispatcherDeliversDiscordWithReceipt(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Query().Get("wait") != "true" {
			t.Fatalf("request = %s %s", request.Method, request.URL.String())
		}
		body, _ := io.ReadAll(request.Body)
		if strings.Contains(string(body), `"parse":["everyone"]`) || !strings.Contains(string(body), `"parse":[]`) {
			t.Fatalf("unsafe Discord payload: %s", body)
		}
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{"id":"1234567890"}`))
	}))
	defer server.Close()
	payload := json.RawMessage(`{"schedule_name":"u2-daily","job_id":"deadbeef-0000","job_status":"complete","summary":{"ready_count":10,"selected_count":10,"target_count":10,"blocked_count":0}}`)
	store := &fakeDeliveryStore{
		delivery: Delivery{ID: "delivery-id", ChannelName: "discord-main", Payload: payload, Attempts: 1},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 5}, ConfigurationSHA256: strings.Repeat("c", 64),
			Credentials: map[string]string{"webhook_url": server.URL + "/api/webhooks/1/token"},
		},
	}
	dispatcher := NewDispatcher(store, "fixture-worker", server.Client(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := dispatcher.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if store.completed["message_id"] != "1234567890" || store.completed["payload_sha256"] == "" || store.failed {
		t.Fatalf("receipt=%#v failed=%v", store.completed, store.failed)
	}
}

func TestDispatcherSchedulesSanitizedRetry(t *testing.T) {
	store := &fakeDeliveryStore{
		delivery: Delivery{ID: "delivery-id", ChannelName: "discord-main", Payload: json.RawMessage(`{"schedule_name":"x"}`), Attempts: 1},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 1}, Credentials: map[string]string{"webhook_url": "http://127.0.0.1:1/api/webhooks/secret"},
		},
	}
	dispatcher := NewDispatcher(store, "fixture-worker", &http.Client{Timeout: time.Second}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := dispatcher.RunOnce(context.Background()); err != nil || !store.failed || store.completed != nil {
		t.Fatalf("RunOnce() err=%v failed=%v completed=%#v", err, store.failed, store.completed)
	}
}
