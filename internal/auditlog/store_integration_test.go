package auditlog

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/database"
)

func TestStoreListsFilteredAuditEventsWithStableCursor(t *testing.T) {
	databaseURL := os.Getenv("UA_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("UA_TEST_DATABASE_URL is not set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	pool, err := database.Open(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if err := database.Migrate(ctx, pool); err != nil {
		t.Fatal(err)
	}
	resourceType := "fixture_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	traceID := uuid.NewString()
	for index := 0; index < 3; index++ {
		if _, err := pool.Exec(ctx, `
			INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, trace_id, payload, created_at)
			VALUES ('test', 'audit-fixture', $1, $2, $3, $4, $5, $6)`,
			"fixture.action", resourceType, uuid.NewString(), traceID,
			json.RawMessage(`{"credential":"must-be-redacted-by-api","index":`+strconv.Itoa(index)+`}`),
			time.Date(2099, 1, 1, 0, 0, index, 0, time.UTC),
		); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), "DELETE FROM audit_events WHERE resource_type = $1", resourceType)
	})

	store := NewStore(pool)
	first, err := store.List(ctx, Filter{ResourceType: resourceType, Action: "fixture.action", Limit: 2})
	if err != nil || len(first.Events) != 2 || !first.HasMore || first.Events[0].TraceID != traceID {
		t.Fatalf("first page/error = %#v/%v", first, err)
	}
	last := first.Events[len(first.Events)-1]
	second, err := store.List(ctx, Filter{ResourceType: resourceType, BeforeCreatedAt: &last.CreatedAt, BeforeID: last.ID, Limit: 2})
	if err != nil || len(second.Events) != 1 || second.HasMore || second.Events[0].ID == last.ID {
		t.Fatalf("second page/error = %#v/%v", second, err)
	}
	if _, err := store.List(ctx, Filter{Action: "invalid action"}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("invalid filter error = %v", err)
	}
}
