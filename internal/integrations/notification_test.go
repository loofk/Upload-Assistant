package integrations

import (
	"encoding/json"
	"errors"
	"slices"
	"testing"
)

func TestNotificationChannelConfigValidatesEventSubscriptions(t *testing.T) {
	body, err := validateNotificationChannelConfig(NotificationChannelConfig{
		EventTypes: []string{"step.failed", "job.completed", "step.failed"},
	})
	if err != nil {
		t.Fatal(err)
	}
	var config NotificationChannelConfig
	if err := json.Unmarshal(body, &config); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(config.EventTypes, []string{"job.completed", "step.failed"}) {
		t.Fatalf("event types = %#v", config.EventTypes)
	}
	if _, err := validateNotificationChannelConfig(NotificationChannelConfig{EventTypes: []string{"rules.accepted"}}); !errors.Is(err, ErrValidation) {
		t.Fatalf("unknown event error = %v", err)
	}
}

func TestNotificationCredentialContracts(t *testing.T) {
	valid := map[string]map[string]string{
		"telegram_bot": {"bot_token": "123456:abcdefghijklmnopqrstuvwxyz_ABCD", "chat_id": "-100123456"},
		"wecom_bot":    {"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture"},
		"feishu_bot":   {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/fixture"},
	}
	for adapter, credentials := range valid {
		if err := validateNotificationCredentials(adapter, credentials, true); err != nil {
			t.Fatalf("%s credentials: %v", adapter, err)
		}
	}
	if err := validateNotificationCredentials("telegram_bot", map[string]string{"bot_token": "bad", "chat_id": "1"}, true); !errors.Is(err, ErrValidation) {
		t.Fatalf("invalid token error = %v", err)
	}
}
