package candidates

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/database"
)

func TestStoreUpsertAndDailyList(t *testing.T) {
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

	store := NewStore(pool)
	date := time.Date(2099, 1, 2, 0, 0, 0, 0, time.UTC)
	rank := 1
	item, err := store.Upsert(ctx, UpsertInput{
		SourceSite: "u2", TargetSite: "mteam", SourceTorrentID: "integration-60635",
		RecommendationDate: date, Rank: &rank, Score: 88.5,
		Payload: json.RawMessage(`{"title":"fixture","ready":true}`), Status: StatusCandidate,
		ExpiresAt: time.Now().Add(24 * time.Hour),
	})
	if err != nil {
		t.Fatalf("Upsert() error = %v", err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM candidate_items WHERE id = $1", item.ID) })
	if item.SourceSite != "U2" || item.TargetSite != "MTEAM" || item.Rank == nil || *item.Rank != 1 {
		t.Fatalf("upserted candidate = %#v", item)
	}

	updatedRank := 2
	updated, err := store.Upsert(ctx, UpsertInput{
		SourceSite: "U2", TargetSite: "MTEAM", SourceTorrentID: "integration-60635",
		RecommendationDate: date, Rank: &updatedRank, Score: 77,
		Payload: json.RawMessage(`{"ready":false,"title":"fixture"}`), Status: StatusBlocked,
		ExpiresAt: time.Now().Add(48 * time.Hour),
	})
	if err != nil || updated.ID != item.ID || updated.Status != StatusBlocked || updated.Rank == nil || *updated.Rank != 2 {
		t.Fatalf("updated candidate/error = %#v/%v", updated, err)
	}

	items, err := store.List(ctx, ListFilter{
		SourceSite: "U2", TargetSite: "MTEAM", RecommendationDate: &date, Limit: 10,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 1 || items[0].ID != item.ID {
		t.Fatalf("daily candidates = %#v", items)
	}

	if _, err := pool.Exec(ctx, "UPDATE candidate_items SET status = 'candidate', expires_at = now() - interval '1 minute' WHERE id = $1", item.ID); err != nil {
		t.Fatal(err)
	}
	expired, err := store.Get(ctx, item.ID)
	if err != nil || expired.Status != StatusExpired {
		t.Fatalf("expired candidate/error = %#v/%v", expired, err)
	}
	activeItems, err := store.List(ctx, ListFilter{RecommendationDate: &date, Status: StatusCandidate, Limit: 10})
	if err != nil || len(activeItems) != 0 {
		t.Fatalf("active expired candidates/error = %#v/%v", activeItems, err)
	}
	expiredItems, err := store.List(ctx, ListFilter{RecommendationDate: &date, Status: StatusExpired, Limit: 10})
	if err != nil || len(expiredItems) != 1 || expiredItems[0].ID != item.ID {
		t.Fatalf("expired candidates/error = %#v/%v", expiredItems, err)
	}
}
