package schedules

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// NextDailyRun deliberately accepts only the auditable daily subset of cron:
// "minute hour * * *". Broader cron syntax can be added later without making
// today's scheduler silently interpret expressions it cannot prove correct.
func NextDailyRun(expression, timezone string, after time.Time) (time.Time, error) {
	fields := strings.Fields(strings.TrimSpace(expression))
	if len(fields) != 5 || fields[2] != "*" || fields[3] != "*" || fields[4] != "*" {
		return time.Time{}, fmt.Errorf("cron_expression must use the daily form 'minute hour * * *'")
	}
	minute, err := strconv.Atoi(fields[0])
	if err != nil || minute < 0 || minute > 59 {
		return time.Time{}, fmt.Errorf("cron minute must be between 0 and 59")
	}
	hour, err := strconv.Atoi(fields[1])
	if err != nil || hour < 0 || hour > 23 {
		return time.Time{}, fmt.Errorf("cron hour must be between 0 and 23")
	}
	location, err := time.LoadLocation(strings.TrimSpace(timezone))
	if err != nil || strings.TrimSpace(timezone) == "" {
		return time.Time{}, fmt.Errorf("timezone must be a valid IANA timezone")
	}
	local := after.In(location)
	next := time.Date(local.Year(), local.Month(), local.Day(), hour, minute, 0, 0, location)
	if !next.After(local) {
		next = next.AddDate(0, 0, 1)
	}
	return next.UTC(), nil
}

func RecommendationDate(timezone string, now time.Time) (string, error) {
	location, err := time.LoadLocation(strings.TrimSpace(timezone))
	if err != nil || strings.TrimSpace(timezone) == "" {
		return "", fmt.Errorf("timezone must be a valid IANA timezone")
	}
	return now.In(location).Format("2006-01-02"), nil
}
