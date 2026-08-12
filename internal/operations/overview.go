package operations

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/security"
)

type Settings struct {
	LogRetentionDays            int       `json:"log_retention_days"`
	DiagnosticRetentionDays     int       `json:"diagnostic_retention_days"`
	FilesystemWarningPercent    float64   `json:"filesystem_warning_percent"`
	FilesystemCriticalPercent   float64   `json:"filesystem_critical_percent"`
	RecoveryHysteresisPercent   float64   `json:"recovery_hysteresis_percent"`
	DatabaseBudgetBytes         int64     `json:"database_budget_bytes"`
	QueueWarningCount           int       `json:"queue_warning_count"`
	QueueWarningAgeSeconds      int       `json:"queue_warning_age_seconds"`
	NotificationCooldownSeconds int       `json:"notification_cooldown_seconds"`
	AutoDiagnosticIncidentKinds []string  `json:"auto_diagnostic_incident_kinds"`
	AutoDiagnosticProviderID    string    `json:"auto_diagnostic_provider_id,omitempty"`
	UpdatedAt                   time.Time `json:"updated_at"`
}

func (s *Store) EvaluateCapacity(ctx context.Context, dataDir, downloadsDir, backupsDir, version string) error {
	settings, err := s.GetSettings(ctx)
	if err != nil {
		return err
	}
	overview, err := s.Overview(ctx, dataDir, downloadsDir, backupsDir, version, 0)
	if err != nil {
		return err
	}
	type measurement struct {
		key                      string
		value, warning, critical float64
		summary                  string
	}
	values := []measurement{}
	for _, fs := range overview.Filesystems {
		if fs.Status != "unknown" {
			values = append(values, measurement{key: "filesystem:" + fs.Name, value: fs.UsedPercent, warning: settings.FilesystemWarningPercent, critical: settings.FilesystemCriticalPercent, summary: fmt.Sprintf("%s filesystem is %.1f%% used", fs.Name, fs.UsedPercent)})
		}
	}
	values = append(values, measurement{key: "database_budget", value: float64(overview.DatabaseBytes), warning: float64(settings.DatabaseBudgetBytes) * .8, critical: float64(settings.DatabaseBudgetBytes), summary: fmt.Sprintf("database uses %d bytes", overview.DatabaseBytes)})
	values = append(values, measurement{key: "job_queue_count", value: float64(overview.QueuedJobs), warning: float64(settings.QueueWarningCount), critical: float64(settings.QueueWarningCount * 2), summary: fmt.Sprintf("%d jobs are queued", overview.QueuedJobs)})
	values = append(values, measurement{key: "job_queue_age", value: float64(overview.OldestQueuedJobSeconds), warning: float64(settings.QueueWarningAgeSeconds), critical: float64(settings.QueueWarningAgeSeconds * 2), summary: fmt.Sprintf("oldest queued job is %d seconds old", overview.OldestQueuedJobSeconds)})
	for _, item := range values {
		if err = s.evaluateMeasurement(ctx, item.key, item.value, item.warning, item.critical, settings, item.summary); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) evaluateMeasurement(ctx context.Context, key string, value, warning, critical float64, settings Settings, summary string) error {
	previous := "normal"
	var notified *time.Time
	_ = s.pool.QueryRow(ctx, `SELECT status,last_notified_at FROM capacity_alert_state WHERE fingerprint=$1`, key).Scan(&previous, &notified)
	status := capacityStatus(previous, value, warning, critical, settings.RecoveryHysteresisPercent)
	now := time.Now().UTC()
	notify := status != "normal" && (status != previous || notified == nil || now.Sub(*notified) >= time.Duration(settings.NotificationCooldownSeconds)*time.Second)
	_, err := s.pool.Exec(ctx, `INSERT INTO capacity_alert_state(fingerprint,status,current_value,last_notified_at) VALUES($1,$2,$3,CASE WHEN $4 THEN now() END) ON CONFLICT(fingerprint) DO UPDATE SET status=EXCLUDED.status,current_value=EXCLUDED.current_value,last_notified_at=CASE WHEN $4 THEN now() ELSE capacity_alert_state.last_notified_at END,updated_at=now()`, key, status, value, notify)
	if err != nil {
		return err
	}
	fingerprintHash := sha256.Sum256([]byte(key))
	fingerprint := hex.EncodeToString(fingerprintHash[:])
	if status == "normal" {
		_, _ = s.pool.Exec(ctx, `UPDATE incidents SET status='resolved',resolved_at=COALESCE(resolved_at,now()),updated_at=now() WHERE fingerprint=$1 AND status<>'resolved'`, fingerprint)
		return nil
	}
	severity := "warning"
	if status == "critical" {
		severity = "critical"
	}
	incident, err := s.UpsertIncident(ctx, IncidentInput{Severity: severity, Kind: "capacity", Fingerprint: fingerprint, Title: "容量或队列阈值告警", Summary: summary, Evidence: map[string]any{"metric": key, "value": value, "warning": warning, "critical": critical}})
	if err != nil {
		return err
	}
	if notify {
		payload, _ := json.Marshal(map[string]any{"event_type": "operations.capacity", "title": "Upload Assistant 容量告警", "message": summary, "incident_id": incident.ID, "severity": severity, "occurred_at": now})
		window := now.Unix() / int64(settings.NotificationCooldownSeconds)
		_, err = s.pool.Exec(ctx, `INSERT INTO notifications(notification_channel_id,channel,status,payload,payload_sha256,attempts,scheduled_at,event_key) SELECT c.id,c.name,'queued',$1,encode(digest(convert_to($1::jsonb::text,'UTF8'),'sha256'),'hex'),0,now(),$2 FROM notification_channels c WHERE c.enabled AND jsonb_typeof(c.config->'event_types')='array' AND ((c.config->'event_types')?'operations.capacity' OR (c.config->'event_types')?$3) ON CONFLICT(notification_channel_id,event_key) WHERE notification_channel_id IS NOT NULL AND event_key IS NOT NULL DO NOTHING`, payload, fmt.Sprintf("capacity:%s:%s:%d", key, status, window), "capacity."+status)
	}
	return err
}

func capacityStatus(previous string, value, warning, critical, hysteresis float64) string {
	status := "normal"
	if value >= critical {
		status = "critical"
	} else if value >= warning {
		status = "warning"
	}
	if previous == "critical" && value >= critical-hysteresis {
		return "critical"
	}
	if previous == "warning" && value >= warning-hysteresis && status == "normal" {
		return "warning"
	}
	return status
}

type FilesystemUsage struct {
	Path           string  `json:"path"`
	Name           string  `json:"name"`
	TotalBytes     uint64  `json:"total_bytes"`
	UsedBytes      uint64  `json:"used_bytes"`
	AvailableBytes uint64  `json:"available_bytes"`
	UsedPercent    float64 `json:"used_percent"`
	Status         string  `json:"status"`
}

type Overview struct {
	Filesystems               []FilesystemUsage `json:"filesystems"`
	DatabaseBytes             int64             `json:"database_bytes"`
	TableBytes                map[string]int64  `json:"table_bytes"`
	OperationalLogBytes30d    int64             `json:"operational_log_bytes_30d"`
	QueuedJobs                int64             `json:"queued_jobs"`
	OldestQueuedJobSeconds    int64             `json:"oldest_queued_job_seconds"`
	QueuedNotifications       int64             `json:"queued_notifications"`
	OldestNotificationSeconds int64             `json:"oldest_notification_seconds"`
	OpenIncidents             int64             `json:"open_incidents"`
	RecentFailures            []Incident        `json:"recent_failures"`
	LatestBackup              *BackupRun        `json:"latest_backup,omitempty"`
	ApplicationVersion        string            `json:"application_version"`
	LogSinkDropped            uint64            `json:"log_sink_dropped"`
	GeneratedAt               time.Time         `json:"generated_at"`
}

func (s *Store) GetSettings(ctx context.Context) (Settings, error) {
	var value Settings
	err := s.pool.QueryRow(ctx, `SELECT log_retention_days,diagnostic_retention_days,filesystem_warning_percent,
		filesystem_critical_percent,recovery_hysteresis_percent,database_budget_bytes,queue_warning_count,
		queue_warning_age_seconds,notification_cooldown_seconds,auto_diagnostic_incident_kinds,
		COALESCE(auto_diagnostic_provider_id::text,''),updated_at FROM operations_settings WHERE singleton`).Scan(
		&value.LogRetentionDays, &value.DiagnosticRetentionDays, &value.FilesystemWarningPercent,
		&value.FilesystemCriticalPercent, &value.RecoveryHysteresisPercent, &value.DatabaseBudgetBytes,
		&value.QueueWarningCount, &value.QueueWarningAgeSeconds, &value.NotificationCooldownSeconds,
		&value.AutoDiagnosticIncidentKinds, &value.AutoDiagnosticProviderID, &value.UpdatedAt)
	return value, err
}

func (s *Store) PutSettings(ctx context.Context, value Settings, principal security.Principal, traceID string) (Settings, error) {
	if value.LogRetentionDays < 1 || value.DiagnosticRetentionDays < 1 || value.FilesystemWarningPercent <= 0 ||
		value.FilesystemCriticalPercent <= value.FilesystemWarningPercent || value.FilesystemCriticalPercent > 100 ||
		value.RecoveryHysteresisPercent < 1 || value.DatabaseBudgetBytes <= 0 || value.QueueWarningCount <= 0 ||
		value.QueueWarningAgeSeconds <= 0 || value.NotificationCooldownSeconds < 60 {
		return Settings{}, fmt.Errorf("%w: invalid operations settings", ErrInvalid)
	}
	rawKinds := append([]string(nil), value.AutoDiagnosticIncidentKinds...)
	kinds := make(map[string]struct{}, len(rawKinds))
	value.AutoDiagnosticIncidentKinds = value.AutoDiagnosticIncidentKinds[:0]
	for _, raw := range rawKinds {
		kind := strings.TrimSpace(raw)
		if kind == "" || len(kind) > 100 {
			return Settings{}, fmt.Errorf("%w: invalid automatic diagnostic incident kind", ErrInvalid)
		}
		if _, exists := kinds[kind]; !exists {
			kinds[kind] = struct{}{}
			value.AutoDiagnosticIncidentKinds = append(value.AutoDiagnosticIncidentKinds, kind)
		}
	}
	if len(value.AutoDiagnosticIncidentKinds) > 32 {
		return Settings{}, fmt.Errorf("%w: too many automatic diagnostic incident kinds", ErrInvalid)
	}
	sort.Strings(value.AutoDiagnosticIncidentKinds)
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Settings{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	_, err = tx.Exec(ctx, `UPDATE operations_settings SET log_retention_days=$1,diagnostic_retention_days=$2,
		filesystem_warning_percent=$3,filesystem_critical_percent=$4,recovery_hysteresis_percent=$5,
		database_budget_bytes=$6,queue_warning_count=$7,queue_warning_age_seconds=$8,
		notification_cooldown_seconds=$9,auto_diagnostic_incident_kinds=$10,
		auto_diagnostic_provider_id=NULLIF($11,'')::uuid,updated_by=$12,updated_at=now() WHERE singleton`,
		value.LogRetentionDays, value.DiagnosticRetentionDays, value.FilesystemWarningPercent, value.FilesystemCriticalPercent,
		value.RecoveryHysteresisPercent, value.DatabaseBudgetBytes, value.QueueWarningCount, value.QueueWarningAgeSeconds,
		value.NotificationCooldownSeconds, value.AutoDiagnosticIncidentKinds, value.AutoDiagnosticProviderID, principal.UserID)
	if err != nil {
		return Settings{}, err
	}
	payload, _ := json.Marshal(Redact(value))
	if _, err = tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
		VALUES('user',$1,'operations.settings.update','operations_settings','singleton',NULLIF($2,'')::uuid,$3)`, principal.UserID, traceID, payload); err != nil {
		return Settings{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return Settings{}, err
	}
	return s.GetSettings(ctx)
}

func filesystem(path, name string, settings Settings) FilesystemUsage {
	value := FilesystemUsage{Path: path, Name: name, Status: "unknown"}
	var stat syscall.Statfs_t
	if syscall.Statfs(path, &stat) != nil {
		return value
	}
	value.TotalBytes = stat.Blocks * uint64(stat.Bsize)
	value.AvailableBytes = stat.Bavail * uint64(stat.Bsize)
	value.UsedBytes = value.TotalBytes - value.AvailableBytes
	if value.TotalBytes > 0 {
		value.UsedPercent = float64(value.UsedBytes) * 100 / float64(value.TotalBytes)
	}
	value.Status = "ready"
	if value.UsedPercent >= settings.FilesystemCriticalPercent {
		value.Status = "critical"
	} else if value.UsedPercent >= settings.FilesystemWarningPercent {
		value.Status = "warning"
	}
	return value
}

func (s *Store) Overview(ctx context.Context, dataDir, downloadsDir, backupsDir, version string, dropped uint64) (Overview, error) {
	settings, err := s.GetSettings(ctx)
	if err != nil {
		return Overview{}, err
	}
	result := Overview{Filesystems: []FilesystemUsage{filesystem(dataDir, "data", settings), filesystem(downloadsDir, "downloads", settings), filesystem(backupsDir, "backups", settings)}, TableBytes: map[string]int64{}, ApplicationVersion: version, LogSinkDropped: dropped, GeneratedAt: time.Now().UTC()}
	if err = s.pool.QueryRow(ctx, `SELECT pg_database_size(current_database())`).Scan(&result.DatabaseBytes); err != nil {
		return Overview{}, err
	}
	rows, err := s.pool.Query(ctx, `SELECT relname,pg_total_relation_size(relid) FROM pg_catalog.pg_statio_user_tables WHERE relname IN ('jobs','step_attempts','operational_logs','incidents','diagnostics','artifacts') ORDER BY relname`)
	if err != nil {
		return Overview{}, err
	}
	defer rows.Close()
	for rows.Next() {
		var n string
		var z int64
		if err = rows.Scan(&n, &z); err != nil {
			return Overview{}, err
		}
		result.TableBytes[n] = z
	}
	_ = s.pool.QueryRow(ctx, `SELECT COALESCE(sum(pg_column_size(l.*)),0) FROM operational_logs l WHERE occurred_at>=now()-interval '30 days'`).Scan(&result.OperationalLogBytes30d)
	_ = s.pool.QueryRow(ctx, `SELECT count(*),COALESCE(extract(epoch FROM now()-min(created_at))::bigint,0) FROM jobs WHERE status='queued'`).Scan(&result.QueuedJobs, &result.OldestQueuedJobSeconds)
	_ = s.pool.QueryRow(ctx, `SELECT count(*),COALESCE(extract(epoch FROM now()-min(scheduled_at))::bigint,0) FROM notifications WHERE status='queued'`).Scan(&result.QueuedNotifications, &result.OldestNotificationSeconds)
	_ = s.pool.QueryRow(ctx, `SELECT count(*) FROM incidents WHERE status<>'resolved'`).Scan(&result.OpenIncidents)
	page, _ := s.ListIncidents(ctx, IncidentFilter{Status: "open", Limit: 5})
	result.RecentFailures = page.Incidents
	if run, e := s.latestBackup(ctx); e == nil {
		result.LatestBackup = &run
	}
	return result, nil
}

func (s *Store) IsReadOnly(ctx context.Context) (bool, string, error) {
	var readOnly bool
	var reason string
	err := s.pool.QueryRow(ctx, `SELECT read_only,COALESCE(reason,'') FROM maintenance_state WHERE singleton`).Scan(&readOnly, &reason)
	return readOnly, reason, err
}

func cleanContained(base, value string) bool {
	rel, err := filepath.Rel(base, value)
	return err == nil && rel != ".." && len(rel) > 0 && rel[:1] != "/"
}
