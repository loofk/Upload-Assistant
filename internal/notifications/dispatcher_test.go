package notifications

import (
	"context"
	"encoding/json"
	"errors"
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
	delivery    Delivery
	runtime     integrations.RuntimeNotificationChannel
	completed   map[string]any
	unknown     map[string]any
	failed      bool
	claimed     bool
	completeErr error
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
	return store.completeErr
}
func (store *fakeDeliveryStore) Fail(context.Context, string, string, time.Time, time.Duration) error {
	store.failed = true
	return nil
}
func (store *fakeDeliveryStore) MarkOutcomeUnknown(_ context.Context, _, _ string, evidence map[string]any) error {
	store.unknown = evidence
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

func TestDispatcherStopsAutomaticRetryWhenResponseIsUnknown(t *testing.T) {
	store := &fakeDeliveryStore{
		delivery: Delivery{ID: "delivery-id", ChannelName: "discord-main", Payload: json.RawMessage(`{"schedule_name":"x"}`), Attempts: 1},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 1}, Credentials: map[string]string{"webhook_url": "http://127.0.0.1:1/api/webhooks/secret"},
		},
	}
	dispatcher := NewDispatcher(store, "fixture-worker", &http.Client{Timeout: time.Second}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := dispatcher.RunOnce(context.Background()); err != nil || store.failed || store.completed != nil || store.unknown["request_sha256"] == nil {
		t.Fatalf("RunOnce() err=%v failed=%v completed=%#v unknown=%#v", err, store.failed, store.completed, store.unknown)
	}
}

func TestDispatcherRetriesKnownHTTPRejection(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusBadRequest)
	}))
	defer server.Close()
	store := &fakeDeliveryStore{
		delivery: Delivery{ID: "delivery-id", ChannelName: "discord-main", Payload: json.RawMessage(`{"schedule_name":"x"}`), Attempts: 1},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 1}, Credentials: map[string]string{"webhook_url": server.URL + "/api/webhooks/1/token"},
		},
	}
	dispatcher := NewDispatcher(store, "fixture-worker", server.Client(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := dispatcher.RunOnce(context.Background()); err != nil || !store.failed || store.unknown != nil {
		t.Fatalf("RunOnce() err=%v failed=%v unknown=%#v", err, store.failed, store.unknown)
	}
}

func TestDispatcherPreservesKnownReceiptWhenLocalCompletionFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		_, _ = response.Write([]byte(`{"id":"1234567890"}`))
	}))
	defer server.Close()
	store := &fakeDeliveryStore{
		delivery: Delivery{ID: "delivery-id", ChannelName: "discord-main", Payload: json.RawMessage(`{"schedule_name":"x"}`), Attempts: 1},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 1}, ConfigurationSHA256: strings.Repeat("c", 64), Credentials: map[string]string{"webhook_url": server.URL + "/api/webhooks/1/token"},
		},
		completeErr: errors.New("fixture completion failure"),
	}
	dispatcher := NewDispatcher(store, "fixture-worker", server.Client(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := dispatcher.RunOnce(context.Background()); err != nil || store.unknown["message_id"] != "1234567890" || store.failed {
		t.Fatalf("RunOnce() err=%v failed=%v unknown=%#v", err, store.failed, store.unknown)
	}
}

func TestNotificationAdaptersBuildBoundedTextPayloadsAndValidateReceipts(t *testing.T) {
	payload := json.RawMessage(`{"event_type":"step.blocked","title":"任务需要处理","message":"规则需要人工确认","job_id":"deadbeef-0000","job_status":"blocked","current_step":"target_rules","occurred_at":"2026-08-10T00:00:00Z"}`)
	tests := []struct {
		adapter     string
		credentials map[string]string
		bodyNeedle  string
		response    string
	}{
		{"telegram_bot", map[string]string{"bot_token": "123456:abcdefghijklmnopqrstuvwxyz_ABCD", "chat_id": "-100123"}, `"chat_id":"-100123"`, `{"ok":true,"result":{"message_id":42}}`},
		{"wecom_bot", map[string]string{"webhook_url": "https://example.invalid/wecom"}, `"msgtype":"text"`, `{"errcode":0,"errmsg":"ok"}`},
		{"feishu_bot", map[string]string{"webhook_url": "https://example.invalid/feishu"}, `"msg_type":"text"`, `{"code":0,"msg":"success"}`},
	}
	for _, test := range tests {
		t.Run(test.adapter, func(t *testing.T) {
			runtime := integrations.RuntimeNotificationChannel{
				NotificationChannel: integrations.NotificationChannel{Adapter: test.adapter},
				Credentials:         test.credentials,
			}
			endpoint, body, err := notificationRequest(runtime, payload)
			if err != nil || endpoint == "" || !strings.Contains(string(body), test.bodyNeedle) || !strings.Contains(string(body), "规则需要人工确认") {
				t.Fatalf("request endpoint/body/error = %q/%s/%v", endpoint, body, err)
			}
			if _, err := validateNotificationReceipt(test.adapter, []byte(test.response)); err != nil {
				t.Fatalf("receipt: %v", err)
			}
			if _, err := validateNotificationReceipt(test.adapter, []byte(`{}`)); err == nil {
				t.Fatal("empty receipt must not be accepted")
			}
		})
	}
}
