package notifications

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
)

const maxDiscordResponseBytes = 256 << 10

var ErrNoDelivery = errors.New("no notification delivery is ready")

type Delivery struct {
	ID          string
	ChannelName string
	Payload     json.RawMessage
	Attempts    int
}

type DeliveryStore interface {
	Claim(context.Context, string, time.Time, time.Duration) (Delivery, error)
	Complete(context.Context, string, string, map[string]any) error
	Fail(context.Context, string, string, time.Time, time.Duration) error
	GetRuntimeNotificationChannel(context.Context, string) (integrations.RuntimeNotificationChannel, error)
}

type Dispatcher struct {
	store  DeliveryStore
	owner  string
	client *http.Client
	logger *slog.Logger
	now    func() time.Time
	poll   time.Duration
	lease  time.Duration
}

func NewDispatcher(store DeliveryStore, owner string, client *http.Client, logger *slog.Logger) *Dispatcher {
	if client == nil {
		client = &http.Client{}
	}
	clone := *client
	clone.CheckRedirect = func(*http.Request, []*http.Request) error { return errors.New("redirects are disabled") }
	if logger == nil {
		logger = slog.Default()
	}
	return &Dispatcher{store: store, owner: strings.TrimSpace(owner), client: &clone, logger: logger, now: time.Now, poll: 20 * time.Second, lease: 90 * time.Second}
}

func (dispatcher *Dispatcher) Run(ctx context.Context) {
	dispatcher.tick(ctx)
	ticker := time.NewTicker(dispatcher.poll)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			dispatcher.tick(ctx)
		}
	}
}

func (dispatcher *Dispatcher) tick(ctx context.Context) {
	if err := dispatcher.RunOnce(ctx); err != nil && !errors.Is(err, context.Canceled) {
		dispatcher.logger.Error("notification delivery tick failed", "error", err)
	}
}

func (dispatcher *Dispatcher) RunOnce(ctx context.Context) error {
	if dispatcher.store == nil || dispatcher.owner == "" {
		return fmt.Errorf("notification dispatcher dependencies are incomplete")
	}
	for processed := 0; processed < 25; processed++ {
		now := dispatcher.now().UTC()
		delivery, err := dispatcher.store.Claim(ctx, dispatcher.owner, now, dispatcher.lease)
		if errors.Is(err, ErrNoDelivery) {
			return nil
		}
		if err != nil {
			return err
		}
		receipt, err := dispatcher.deliver(ctx, delivery)
		if err == nil {
			if err := dispatcher.store.Complete(ctx, delivery.ID, dispatcher.owner, receipt); err != nil {
				return err
			}
			continue
		}
		retry := time.Duration(1<<min(delivery.Attempts-1, 6)) * time.Minute
		if err := dispatcher.store.Fail(ctx, delivery.ID, dispatcher.owner, now, retry); err != nil {
			return err
		}
		dispatcher.logger.Warn("external notification will retry", "notification_id", delivery.ID, "channel", delivery.ChannelName, "attempt", delivery.Attempts)
	}
	return nil
}

func (dispatcher *Dispatcher) deliver(ctx context.Context, delivery Delivery) (map[string]any, error) {
	runtime, err := dispatcher.store.GetRuntimeNotificationChannel(ctx, delivery.ChannelName)
	if err != nil {
		return nil, err
	}
	if runtime.Adapter != "discord_webhook" {
		return nil, fmt.Errorf("unsupported notification adapter")
	}
	webhook, err := url.Parse(runtime.Credentials["webhook_url"])
	if err != nil {
		return nil, fmt.Errorf("invalid notification endpoint")
	}
	query := webhook.Query()
	query.Set("wait", "true")
	webhook.RawQuery = query.Encode()
	message, err := discordMessage(delivery.Payload)
	if err != nil {
		return nil, err
	}
	body, _ := json.Marshal(message)
	requestCtx, cancel := context.WithTimeout(ctx, time.Duration(runtime.ChannelConfig.TimeoutSeconds)*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, webhook.String(), bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build notification request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	response, err := dispatcher.client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("notification request failed")
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxDiscordResponseBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read notification response: %w", err)
	}
	if len(responseBody) > maxDiscordResponseBytes {
		return nil, fmt.Errorf("notification response is too large")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("notification endpoint returned HTTP %d", response.StatusCode)
	}
	var remote struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(responseBody, &remote); err != nil || strings.TrimSpace(remote.ID) == "" {
		return nil, fmt.Errorf("notification endpoint returned an incomplete receipt")
	}
	return map[string]any{
		"adapter": runtime.Adapter, "message_id": cleanID(remote.ID),
		"payload_sha256": sha256Hex(delivery.Payload), "request_sha256": sha256Hex(body),
		"response_sha256": sha256Hex(responseBody), "configuration_sha256": runtime.ConfigurationSHA256,
		"delivered_at": dispatcher.now().UTC().Format(time.RFC3339Nano),
	}, nil
}

func discordMessage(payload json.RawMessage) (map[string]any, error) {
	var value struct {
		ScheduleName string          `json:"schedule_name"`
		JobID        string          `json:"job_id"`
		JobStatus    string          `json:"job_status"`
		Summary      json.RawMessage `json:"summary"`
	}
	if err := json.Unmarshal(payload, &value); err != nil {
		return nil, fmt.Errorf("decode notification payload: %w", err)
	}
	var summary struct {
		ReadyCount    int `json:"ready_count"`
		SelectedCount int `json:"selected_count"`
		TargetCount   int `json:"target_count"`
		BlockedCount  int `json:"blocked_count"`
	}
	_ = json.Unmarshal(value.Summary, &summary)
	name := cleanText(value.ScheduleName, 100)
	status := cleanText(value.JobStatus, 30)
	jobID := cleanID(value.JobID)
	content := fmt.Sprintf("Upload Assistant 每日候选 · %s\n状态：%s · 可转：%d/%d · 已选：%d · 阻塞：%d\n任务：%s\n请在本地控制台审阅；此通知不代表批准候选或上传种子。",
		name, status, summary.ReadyCount, summary.TargetCount, summary.SelectedCount, summary.BlockedCount, jobID)
	if len(content) > 2000 {
		content = content[:2000]
	}
	return map[string]any{
		"content":          content,
		"allowed_mentions": map[string]any{"parse": []string{}},
	}, nil
}

func cleanText(value string, limit int) string {
	value = strings.Map(func(character rune) rune {
		if character == '\r' || character == '\n' || character == '\x00' || character == '@' {
			return ' '
		}
		return character
	}, strings.TrimSpace(value))
	if len(value) > limit {
		value = value[:limit]
	}
	return value
}

func cleanID(value string) string {
	value = strings.TrimSpace(value)
	for _, character := range value {
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') && character != '-' {
			return "invalid"
		}
	}
	if len(value) > 80 {
		return value[:80]
	}
	return value
}

func sha256Hex(body []byte) string {
	hash := sha256.Sum256(body)
	return hex.EncodeToString(hash[:])
}
