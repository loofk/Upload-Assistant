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

const maxNotificationResponseBytes = 256 << 10

var (
	ErrNoDelivery             = errors.New("no notification delivery is ready")
	ErrDeliveryOutcomeUnknown = errors.New("notification delivery outcome is unknown")
)

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
	MarkOutcomeUnknown(context.Context, string, string, map[string]any) error
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
				if retryErr := dispatcher.store.Complete(ctx, delivery.ID, dispatcher.owner, receipt); retryErr == nil {
					continue
				} else if unknownErr := dispatcher.store.MarkOutcomeUnknown(ctx, delivery.ID, dispatcher.owner, receipt); unknownErr != nil {
					return fmt.Errorf("persist notification receipt: %v; local retry: %v; preserve unknown outcome: %w", err, retryErr, unknownErr)
				}
				dispatcher.logger.Warn("notification delivered but local completion failed; automatic retry disabled", "notification_id", delivery.ID, "channel", delivery.ChannelName)
			}
			continue
		}
		if errors.Is(err, ErrDeliveryOutcomeUnknown) {
			if unknownErr := dispatcher.store.MarkOutcomeUnknown(ctx, delivery.ID, dispatcher.owner, receipt); unknownErr != nil {
				return unknownErr
			}
			dispatcher.logger.Warn("notification outcome is unknown; automatic retry disabled", "notification_id", delivery.ID, "channel", delivery.ChannelName, "attempt", delivery.Attempts)
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
	endpoint, body, err := notificationRequest(runtime, delivery.Payload)
	if err != nil {
		return nil, err
	}
	evidence := map[string]any{
		"adapter": runtime.Adapter, "payload_sha256": sha256Hex(delivery.Payload),
		"request_sha256": sha256Hex(body), "configuration_sha256": runtime.ConfigurationSHA256,
		"attempted_at": dispatcher.now().UTC().Format(time.RFC3339Nano),
	}
	requestCtx, cancel := context.WithTimeout(ctx, time.Duration(runtime.ChannelConfig.TimeoutSeconds)*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("build notification request: %w", err)
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	response, err := dispatcher.client.Do(request)
	if err != nil {
		return evidence, fmt.Errorf("%w: notification request ended without a response", ErrDeliveryOutcomeUnknown)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxNotificationResponseBytes+1))
	if err != nil {
		return evidence, fmt.Errorf("%w: notification response is unreadable", ErrDeliveryOutcomeUnknown)
	}
	if len(responseBody) > maxNotificationResponseBytes {
		return evidence, fmt.Errorf("%w: notification response is too large", ErrDeliveryOutcomeUnknown)
	}
	evidence["response_sha256"] = sha256Hex(responseBody)
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode >= 500 {
			return evidence, fmt.Errorf("%w: notification endpoint returned HTTP %d", ErrDeliveryOutcomeUnknown, response.StatusCode)
		}
		return nil, fmt.Errorf("notification endpoint returned HTTP %d", response.StatusCode)
	}
	receipt, err := validateNotificationReceipt(runtime.Adapter, responseBody)
	if err != nil {
		return evidence, fmt.Errorf("%w: notification endpoint returned an incomplete success receipt", ErrDeliveryOutcomeUnknown)
	}
	for key, value := range receipt {
		evidence[key] = value
	}
	evidence["delivered_at"] = dispatcher.now().UTC().Format(time.RFC3339Nano)
	return evidence, nil
}

func notificationRequest(runtime integrations.RuntimeNotificationChannel, payload json.RawMessage) (string, []byte, error) {
	content, err := notificationText(payload)
	if err != nil {
		return "", nil, err
	}
	var endpoint string
	var message map[string]any
	switch runtime.Adapter {
	case "discord_webhook":
		webhook, parseErr := url.Parse(runtime.Credentials["webhook_url"])
		if parseErr != nil {
			return "", nil, fmt.Errorf("invalid notification endpoint")
		}
		query := webhook.Query()
		query.Set("wait", "true")
		webhook.RawQuery = query.Encode()
		endpoint = webhook.String()
		message = map[string]any{"content": truncateText(content, 2000), "allowed_mentions": map[string]any{"parse": []string{}}}
	case "telegram_bot":
		endpoint = "https://api.telegram.org/bot" + runtime.Credentials["bot_token"] + "/sendMessage"
		message = map[string]any{"chat_id": runtime.Credentials["chat_id"], "text": truncateText(content, 4096), "disable_web_page_preview": true}
	case "wecom_bot":
		endpoint = runtime.Credentials["webhook_url"]
		message = map[string]any{"msgtype": "text", "text": map[string]any{"content": truncateText(content, 4000)}}
	case "feishu_bot":
		endpoint = runtime.Credentials["webhook_url"]
		message = map[string]any{"msg_type": "text", "content": map[string]any{"text": truncateText(content, 4000)}}
	default:
		return "", nil, fmt.Errorf("unsupported notification adapter")
	}
	body, err := json.Marshal(message)
	if err != nil {
		return "", nil, fmt.Errorf("encode notification payload: %w", err)
	}
	return endpoint, body, nil
}

func validateNotificationReceipt(adapter string, body []byte) (map[string]any, error) {
	switch adapter {
	case "discord_webhook":
		var remote struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(body, &remote); err != nil || !numericDiscordMessageID(remote.ID) {
			return nil, errors.New("Discord receipt is incomplete")
		}
		return map[string]any{"message_id": cleanID(remote.ID)}, nil
	case "telegram_bot":
		var remote struct {
			OK     bool `json:"ok"`
			Result struct {
				MessageID int64 `json:"message_id"`
			} `json:"result"`
		}
		if err := json.Unmarshal(body, &remote); err != nil || !remote.OK || remote.Result.MessageID <= 0 {
			return nil, errors.New("Telegram receipt is incomplete")
		}
		return map[string]any{"message_id": fmt.Sprint(remote.Result.MessageID)}, nil
	case "wecom_bot":
		var remote struct {
			ErrorCode *int `json:"errcode"`
		}
		if err := json.Unmarshal(body, &remote); err != nil || remote.ErrorCode == nil || *remote.ErrorCode != 0 {
			return nil, errors.New("WeCom receipt is incomplete")
		}
		return map[string]any{"remote_acknowledged": true}, nil
	case "feishu_bot":
		var remote struct {
			Code *int `json:"code"`
		}
		if err := json.Unmarshal(body, &remote); err != nil || remote.Code == nil || *remote.Code != 0 {
			return nil, errors.New("Feishu receipt is incomplete")
		}
		return map[string]any{"remote_acknowledged": true}, nil
	default:
		return nil, errors.New("unsupported notification adapter")
	}
}

func numericDiscordMessageID(value string) bool {
	value = strings.TrimSpace(value)
	if value == "" || len(value) > 30 {
		return false
	}
	for _, character := range value {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}

func notificationText(payload json.RawMessage) (string, error) {
	var value struct {
		EventType    string          `json:"event_type"`
		Title        string          `json:"title"`
		Message      string          `json:"message"`
		ScheduleName string          `json:"schedule_name"`
		JobID        string          `json:"job_id"`
		JobStatus    string          `json:"job_status"`
		CurrentStep  string          `json:"current_step"`
		OccurredAt   string          `json:"occurred_at"`
		Summary      json.RawMessage `json:"summary"`
	}
	if err := json.Unmarshal(payload, &value); err != nil {
		return "", fmt.Errorf("decode notification payload: %w", err)
	}
	var summary struct {
		ReadyCount    int `json:"ready_count"`
		SelectedCount int `json:"selected_count"`
		TargetCount   int `json:"target_count"`
		BlockedCount  int `json:"blocked_count"`
	}
	_ = json.Unmarshal(value.Summary, &summary)
	if strings.TrimSpace(value.EventType) != "" {
		title := cleanText(value.Title, 120)
		if title == "" {
			title = "Upload Assistant 系统事件"
		}
		lines := []string{title, "事件：" + cleanText(value.EventType, 80)}
		if status := cleanText(value.JobStatus, 30); status != "" {
			lines = append(lines, "状态："+status)
		}
		if step := cleanText(value.CurrentStep, 80); step != "" {
			lines = append(lines, "环节："+step)
		}
		if message := cleanText(value.Message, 500); message != "" {
			lines = append(lines, "说明："+message)
		}
		if jobID := cleanID(value.JobID); jobID != "invalid" && jobID != "" {
			lines = append(lines, "任务："+jobID)
		}
		if occurredAt := cleanText(value.OccurredAt, 60); occurredAt != "" {
			lines = append(lines, "时间："+occurredAt)
		}
		lines = append(lines, "请在本地控制台处理；通知不会自动批准规则、忽略查重或确认上传。")
		return strings.Join(lines, "\n"), nil
	}
	name := cleanText(value.ScheduleName, 100)
	status := cleanText(value.JobStatus, 30)
	jobID := cleanID(value.JobID)
	content := fmt.Sprintf("Upload Assistant 每日候选 · %s\n状态：%s · 可转：%d/%d · 已选：%d · 阻塞：%d\n任务：%s\n请在本地控制台审阅；此通知不代表批准候选或上传种子。",
		name, status, summary.ReadyCount, summary.TargetCount, summary.SelectedCount, summary.BlockedCount, jobID)
	return content, nil
}

func truncateText(value string, limit int) string {
	characters := []rune(value)
	if len(characters) <= limit {
		return value
	}
	return string(characters[:limit])
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
