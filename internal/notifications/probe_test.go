package notifications

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeProbeStore struct {
	*fakeDeliveryStore
	probeFailed bool
}

func (store *fakeProbeStore) EnqueueProbe(context.Context, string, workflow.Actor, time.Time) (ProbeResult, error) {
	return ProbeResult{NotificationID: store.delivery.ID, ChannelName: store.delivery.ChannelName, Status: "queued"}, nil
}

func (store *fakeProbeStore) ClaimProbe(context.Context, string, string, time.Time, time.Duration) (Delivery, error) {
	return store.delivery, nil
}

func (store *fakeProbeStore) FailProbe(context.Context, string, string, time.Time) error {
	store.probeFailed = true
	return nil
}

func TestProberSendsExactlyOneDurableTestMessage(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		if string(body) == "" {
			t.Fatal("probe message body is empty")
		}
		_, _ = response.Write([]byte(`{"id":"1234567890"}`))
	}))
	defer server.Close()
	store := &fakeProbeStore{fakeDeliveryStore: &fakeDeliveryStore{
		delivery: Delivery{ID: "probe-id", ChannelName: "discord-main", Payload: json.RawMessage(`{"event_type":"configuration.test","title":"Upload Assistant 测试通知"}`), Attempts: 8},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 2},
			Credentials:         map[string]string{"webhook_url": server.URL + "/webhook"},
		},
	}}
	dispatcher := NewDispatcher(store, "worker", server.Client(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	result, err := NewProber(store, dispatcher, "probe-worker").Probe(context.Background(), "discord-main", workflow.Actor{Type: "test"})
	if err != nil || result.Status != "sent" || store.completed["message_id"] != "1234567890" || store.probeFailed {
		t.Fatalf("probe = %#v err=%v completed=%#v failed=%v", result, err, store.completed, store.probeFailed)
	}
}

func TestProberDoesNotRetryKnownRejection(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) { response.WriteHeader(http.StatusBadRequest) }))
	defer server.Close()
	store := &fakeProbeStore{fakeDeliveryStore: &fakeDeliveryStore{
		delivery: Delivery{ID: "probe-id", ChannelName: "discord-main", Payload: json.RawMessage(`{"event_type":"configuration.test"}`), Attempts: 8},
		runtime: integrations.RuntimeNotificationChannel{
			NotificationChannel: integrations.NotificationChannel{Name: "discord-main", Adapter: "discord_webhook", Enabled: true},
			ChannelConfig:       integrations.NotificationChannelConfig{TimeoutSeconds: 2},
			Credentials:         map[string]string{"webhook_url": server.URL + "/webhook"},
		},
	}}
	dispatcher := NewDispatcher(store, "worker", server.Client(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	result, err := NewProber(store, dispatcher, "probe-worker").Probe(context.Background(), "discord-main", workflow.Actor{})
	if err == nil || result.Status != "failed" || !store.probeFailed || store.unknown != nil {
		t.Fatalf("probe = %#v err=%v failed=%v unknown=%#v", result, err, store.probeFailed, store.unknown)
	}
}
