package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/schedules"
)

type fakeScheduleService struct {
	created       schedules.CreateInput
	updated       schedules.UpdateInput
	updatedID     string
	schedule      schedules.Schedule
	schedules     []schedules.Schedule
	notifications []schedules.Notification
	reconciled    schedules.NotificationReconciliationInput
	runs          []schedules.Run
}

func (service *fakeScheduleService) Create(_ context.Context, input schedules.CreateInput, _ time.Time) (schedules.Schedule, error) {
	service.created = input
	return service.schedule, nil
}
func (service *fakeScheduleService) List(context.Context, int) ([]schedules.Schedule, error) {
	return service.schedules, nil
}
func (service *fakeScheduleService) Update(_ context.Context, id string, input schedules.UpdateInput, _ time.Time) (schedules.Schedule, error) {
	service.updatedID, service.updated = id, input
	return service.schedule, nil
}
func (service *fakeScheduleService) ListNotifications(context.Context, int) ([]schedules.Notification, error) {
	return service.notifications, nil
}
func (service *fakeScheduleService) ReconcileNotification(_ context.Context, id string, input schedules.NotificationReconciliationInput, _ time.Time) (schedules.Notification, error) {
	service.reconciled = input
	return schedules.Notification{ID: id, Channel: "discord-main", Status: "sent", Payload: json.RawMessage(`{}`), RemoteReceipt: json.RawMessage(`{"message_id":"123"}`)}, nil
}
func (service *fakeScheduleService) ListRuns(context.Context, string, int) ([]schedules.Run, error) {
	return service.runs, nil
}

func TestCreateDailyScheduleDefaultsEnabledWithoutLiveActions(t *testing.T) {
	next := time.Now().Add(time.Hour)
	service := &fakeScheduleService{schedule: schedules.Schedule{
		ID: "88888888-8888-4888-8888-888888888888", Name: "morning", Kind: "daily_candidates",
		Enabled: true, NextRunAt: &next,
	}}
	request := candidateRequest(http.MethodPost, "/api/v2/schedules/daily-candidates", `{
		"name":"morning","cron_expression":"0 9 * * *","timezone":"Asia/Shanghai",
		"config":{"source":"U2","target":"MTEAM","target_count":10,"scan_limit":30,"page":1}
	}`, "jobs:write")
	response := httptest.NewRecorder()
	(scheduleAPI{service: service}).create(response, request)
	if response.Code != http.StatusCreated || !service.created.Enabled || service.created.Config.Source != "U2" {
		t.Fatalf("response/input = %d/%s/%#v", response.Code, response.Body.String(), service.created)
	}
	if bytes.Contains(response.Body.Bytes(), []byte("confirm_upload")) || bytes.Contains(response.Body.Bytes(), []byte("accept_rules")) {
		t.Fatalf("schedule response implied live authorization: %s", response.Body.String())
	}
}

func TestUpdateDailyScheduleCanDisableIt(t *testing.T) {
	service := &fakeScheduleService{schedule: schedules.Schedule{ID: "88888888-8888-4888-8888-888888888888", Enabled: false}}
	request := candidateRequest(http.MethodPatch, "/api/v2/schedules/daily-candidates/88888888-8888-4888-8888-888888888888", `{"enabled":false}`, "jobs:write")
	request.SetPathValue("schedule_id", service.schedule.ID)
	response := httptest.NewRecorder()
	(scheduleAPI{service: service}).update(response, request)
	if response.Code != http.StatusOK || service.updated.Enabled == nil || *service.updated.Enabled || service.updatedID != service.schedule.ID {
		t.Fatalf("response/update = %d/%s/%#v", response.Code, response.Body.String(), service.updated)
	}
}

func TestNotificationsRedactEmbeddedSecrets(t *testing.T) {
	service := &fakeScheduleService{notifications: []schedules.Notification{{
		ID: "notification", Channel: "discord-main", Status: "outcome_unknown",
		Payload: json.RawMessage(`{"job_id":"job","source_url":"https://example.invalid/details?id=1&passkey=do-not-return"}`),
	}}}
	request := candidateRequest(http.MethodGet, "/api/v2/notifications", "", "jobs:read")
	response := httptest.NewRecorder()
	(scheduleAPI{service: service}).notifications(response, request)
	if response.Code != http.StatusOK || bytes.Contains(response.Body.Bytes(), []byte("do-not-return")) ||
		!bytes.Contains(response.Body.Bytes(), []byte("notification_delivery_outcome_unknown")) || !bytes.Contains(response.Body.Bytes(), []byte("reconcile_notification")) {
		t.Fatalf("notification response = %d/%s", response.Code, response.Body.String())
	}
}

func TestNotificationReconciliationRequiresExplicitEvidenceAndActor(t *testing.T) {
	id := "77777777-7777-4777-8777-777777777777"
	service := &fakeScheduleService{}
	request := candidateRequest(http.MethodPost, "/api/v2/notifications/"+id+"/reconcile", `{
		"decision":"verified_delivered","confirmed":true,"evidence_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"observed_at":"2026-08-08T12:00:00Z","message_id":"123"
	}`, "jobs:write")
	request.SetPathValue("notification_id", id)
	response := httptest.NewRecorder()
	(scheduleAPI{service: service}).reconcileNotification(response, request)
	if response.Code != http.StatusOK || service.reconciled.ActorID == "" || service.reconciled.MessageID != "123" || !bytes.Contains(response.Body.Bytes(), []byte(`"status":"sent"`)) {
		t.Fatalf("notification reconciliation = %d/%s/%#v", response.Code, response.Body.String(), service.reconciled)
	}
}

func TestScheduleRunsExposeSafeAuditState(t *testing.T) {
	service := &fakeScheduleService{runs: []schedules.Run{{
		ID: "run", ScheduleID: "88888888-8888-4888-8888-888888888888", Status: schedules.RunFailed,
		Attempts: 2, LastError: "request failed with passkey=do-not-return",
	}}}
	request := candidateRequest(http.MethodGet, "/api/v2/schedules/daily-candidates/88888888-8888-4888-8888-888888888888/runs", "", "jobs:read")
	request.SetPathValue("schedule_id", service.runs[0].ScheduleID)
	response := httptest.NewRecorder()
	(scheduleAPI{service: service}).runs(response, request)
	if response.Code != http.StatusOK || bytes.Contains(response.Body.Bytes(), []byte("do-not-return")) || !bytes.Contains(response.Body.Bytes(), []byte("\"attempts\":2")) {
		t.Fatalf("schedule run response = %d/%s", response.Code, response.Body.String())
	}
}
