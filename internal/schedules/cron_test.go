package schedules

import (
	"testing"
	"time"
)

func TestNextDailyRunUsesConfiguredTimezone(t *testing.T) {
	after := time.Date(2026, 8, 8, 0, 31, 0, 0, time.UTC)
	next, err := NextDailyRun("30 8 * * *", "Asia/Shanghai", after)
	if err != nil {
		t.Fatal(err)
	}
	if want := time.Date(2026, 8, 9, 0, 30, 0, 0, time.UTC); !next.Equal(want) {
		t.Fatalf("next = %s, want %s", next, want)
	}
	date, err := RecommendationDate("Asia/Shanghai", time.Date(2026, 8, 7, 17, 0, 0, 0, time.UTC))
	if err != nil || date != "2026-08-08" {
		t.Fatalf("date/error = %s/%v", date, err)
	}
}

func TestNextDailyRunRejectsUnsupportedCron(t *testing.T) {
	for _, expression := range []string{"*/5 * * * *", "0 8 * * 1", "0 25 * * *", "@daily"} {
		if _, err := NextDailyRun(expression, "Asia/Shanghai", time.Now()); err == nil {
			t.Fatalf("NextDailyRun(%q) accepted unsupported expression", expression)
		}
	}
}
