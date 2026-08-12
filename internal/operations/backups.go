package operations

import (
	"archive/tar"
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/database"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

type BackupPolicy struct {
	Enabled        bool      `json:"enabled"`
	Recipient      string    `json:"recipient,omitempty"`
	Schedule       string    `json:"schedule"`
	RetentionCount int       `json:"retention_count"`
	UpdatedAt      time.Time `json:"updated_at"`
}

type BackupRun struct {
	ID           string          `json:"id"`
	Status       string          `json:"status"`
	BundlePath   string          `json:"bundle_path,omitempty"`
	BundleSHA256 string          `json:"bundle_sha256,omitempty"`
	Manifest     json.RawMessage `json:"manifest,omitempty"`
	SizeBytes    int64           `json:"size_bytes,omitempty"`
	AppVersion   string          `json:"app_version"`
	ErrorCode    string          `json:"error_code,omitempty"`
	ErrorMessage string          `json:"error_message,omitempty"`
	StartedAt    *time.Time      `json:"started_at,omitempty"`
	FinishedAt   *time.Time      `json:"finished_at,omitempty"`
	VerifiedAt   *time.Time      `json:"verified_at,omitempty"`
	CreatedAt    time.Time       `json:"created_at"`
}

type ManifestEntry struct {
	Path      string `json:"path"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}
type BackupManifest struct {
	Schema             string          `json:"schema"`
	ApplicationVersion string          `json:"application_version"`
	CreatedAt          string          `json:"created_at"`
	Entries            []ManifestEntry `json:"entries"`
}

type BackupManager struct {
	Store                                                     *Store
	DatabaseURL, DataDir, BackupsDir, MasterKeyFile, Version  string
	PgDumpBinary, PgRestoreBinary, AgeBinary, AgeKeygenBinary string
}

func (s *Store) GetBackupPolicy(ctx context.Context) (BackupPolicy, error) {
	var p BackupPolicy
	err := s.pool.QueryRow(ctx, `SELECT enabled,COALESCE(recipient,''),schedule,retention_count,updated_at FROM backup_policy WHERE singleton`).Scan(&p.Enabled, &p.Recipient, &p.Schedule, &p.RetentionCount, &p.UpdatedAt)
	return p, err
}

func (s *Store) PutBackupPolicy(ctx context.Context, p BackupPolicy, principal security.Principal, traceID string) (BackupPolicy, error) {
	if p.Schedule == "" {
		p.Schedule = "30 3 * * *"
	}
	if p.RetentionCount < 1 || p.RetentionCount > 100 {
		return BackupPolicy{}, fmt.Errorf("%w: invalid retention count", ErrInvalid)
	}
	if p.Recipient != "" && !strings.HasPrefix(p.Recipient, "age1") {
		return BackupPolicy{}, fmt.Errorf("%w: age X25519 recipient is invalid", ErrInvalid)
	}
	if _, _, err := parseDailySchedule(p.Schedule); err != nil {
		return BackupPolicy{}, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return BackupPolicy{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	_, err = tx.Exec(ctx, `UPDATE backup_policy SET enabled=$1,recipient=NULLIF($2,''),schedule=$3,retention_count=$4,updated_by=$5,updated_at=now() WHERE singleton`, p.Enabled, p.Recipient, p.Schedule, p.RetentionCount, principal.UserID)
	if err != nil {
		return BackupPolicy{}, err
	}
	payload, _ := json.Marshal(map[string]any{"enabled": p.Enabled, "recipient_configured": p.Recipient != "", "schedule": p.Schedule, "retention_count": p.RetentionCount})
	if _, err = tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload) VALUES('user',$1,'backup.policy.update','backup_policy','singleton',NULLIF($2,'')::uuid,$3)`, principal.UserID, traceID, payload); err != nil {
		return BackupPolicy{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return BackupPolicy{}, err
	}
	return s.GetBackupPolicy(ctx)
}

func scanBackup(row rowScanner) (BackupRun, error) {
	var r BackupRun
	err := row.Scan(&r.ID, &r.Status, &r.BundlePath, &r.BundleSHA256, &r.Manifest, &r.SizeBytes, &r.AppVersion, &r.ErrorCode, &r.ErrorMessage, &r.StartedAt, &r.FinishedAt, &r.VerifiedAt, &r.CreatedAt)
	return r, err
}

const backupSelect = `SELECT id::text,status,COALESCE(bundle_path,''),COALESCE(bundle_sha256,''),COALESCE(manifest,'{}'),COALESCE(size_bytes,0),app_version,COALESCE(error_code,''),COALESCE(error_message,''),started_at,finished_at,verified_at,created_at FROM backup_runs`

func (s *Store) ListBackups(ctx context.Context, limit int) ([]BackupRun, error) {
	if limit < 1 || limit > 100 {
		return nil, ErrInvalid
	}
	rows, err := s.pool.Query(ctx, backupSelect+` ORDER BY created_at DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []BackupRun{}
	for rows.Next() {
		r, e := scanBackup(rows)
		if e != nil {
			return nil, e
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
func (s *Store) GetBackup(ctx context.Context, id string) (BackupRun, error) {
	r, err := scanBackup(s.pool.QueryRow(ctx, backupSelect+` WHERE id=$1`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return BackupRun{}, ErrNotFound
	}
	return r, err
}
func (s *Store) latestBackup(ctx context.Context) (BackupRun, error) {
	r, err := scanBackup(s.pool.QueryRow(ctx, backupSelect+` WHERE status IN('complete','verified') ORDER BY created_at DESC LIMIT 1`))
	if errors.Is(err, pgx.ErrNoRows) {
		return BackupRun{}, ErrNotFound
	}
	return r, err
}

func (m *BackupManager) defaults() {
	if m.PgDumpBinary == "" {
		m.PgDumpBinary = "pg_dump"
	}
	if m.PgRestoreBinary == "" {
		m.PgRestoreBinary = "pg_restore"
	}
	if m.AgeBinary == "" {
		m.AgeBinary = "age"
	}
	if m.AgeKeygenBinary == "" {
		m.AgeKeygenBinary = "age-keygen"
	}
}

func (m *BackupManager) GenerateIdentity(ctx context.Context) (identity, recipient string, err error) {
	m.defaults()
	command := exec.CommandContext(ctx, m.AgeKeygenBinary)
	var output, diagnostic strings.Builder
	command.Stdout = &output
	command.Stderr = &diagnostic
	if err = command.Run(); err != nil {
		return "", "", fmt.Errorf("generate age identity: %w", err)
	}
	return parseAgeIdentity(output.String(), diagnostic.String())
}

func parseAgeIdentity(output, diagnostic string) (identity, recipient string, err error) {
	scanner := bufio.NewScanner(strings.NewReader(output + "\n" + diagnostic))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "AGE-SECRET-KEY-") {
			identity = line
		}
		lower := strings.ToLower(line)
		if strings.HasPrefix(lower, "public key:") || strings.HasPrefix(lower, "# public key:") {
			recipient = strings.TrimSpace(line[strings.IndexByte(line, ':')+1:])
		}
	}
	if err = scanner.Err(); err != nil {
		return "", "", fmt.Errorf("parse age identity: %w", err)
	}
	if !strings.HasPrefix(identity, "AGE-SECRET-KEY-") || !strings.HasPrefix(recipient, "age1") {
		return "", "", errors.New("age-keygen returned an invalid X25519 identity")
	}
	return identity, recipient, nil
}

func (m *BackupManager) Create(ctx context.Context, principal security.Principal) (BackupRun, error) {
	m.defaults()
	policy, err := m.Store.GetBackupPolicy(ctx)
	if err != nil {
		return BackupRun{}, err
	}
	if policy.Recipient == "" {
		return BackupRun{}, fmt.Errorf("%w: backup recipient is not configured", ErrConflict)
	}
	var active int
	if err = m.Store.pool.QueryRow(ctx, `SELECT count(*) FROM jobs WHERE status='running'`).Scan(&active); err != nil {
		return BackupRun{}, err
	}
	status := "running"
	if active > 0 {
		status = "deferred"
	}
	var id string
	err = m.Store.pool.QueryRow(ctx, `INSERT INTO backup_runs(status,app_version,requested_by,started_at,error_code,error_message) VALUES($1,$2,NULLIF($3,'')::uuid,CASE WHEN $1='running' THEN now() END,CASE WHEN $1='deferred' THEN 'active_write_jobs' END,CASE WHEN $1='deferred' THEN 'backup delayed until write jobs reach a safe boundary' END) RETURNING id::text`, status, m.Version, principal.UserID).Scan(&id)
	if err != nil {
		return BackupRun{}, err
	}
	correlation := CorrelationFromContext(ctx)
	actorType, actorID := correlation.ActorType, correlation.ActorID
	if actorType == "" {
		actorType, actorID = "system", "backup-scheduler"
	}
	_, _ = m.Store.pool.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
		VALUES($1,NULLIF($2,''),'backup.create','backup_run',$3,NULLIF($4,'')::uuid,jsonb_build_object('status',$5::text))`, actorType, actorID, id, correlation.TraceID, status)
	if status == "deferred" {
		return m.Store.GetBackup(ctx, id)
	}
	return m.execute(ctx, id, policy)
}

func (m *BackupManager) execute(ctx context.Context, id string, policy BackupPolicy) (BackupRun, error) {
	if _, err := m.Store.pool.Exec(ctx, `UPDATE maintenance_state SET read_only=true,reason='encrypted_backup',owner=$1,updated_at=now() WHERE singleton`, id); err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "maintenance_failed", err)
	}
	defer func() {
		_, _ = m.Store.pool.Exec(context.Background(), `UPDATE maintenance_state SET read_only=false,reason=NULL,owner=NULL,updated_at=now() WHERE singleton AND owner=$1`, id)
	}()
	stage, err := os.MkdirTemp(m.BackupsDir, ".ua-backup-")
	if err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "staging_failed", err)
	}
	defer os.RemoveAll(stage)
	databasePath := filepath.Join(stage, "database.dump")
	command := exec.CommandContext(ctx, m.PgDumpBinary, "--format=custom", "--file", databasePath, "--no-owner", "--no-privileges")
	command.Env, err = databaseEnvironment(m.DatabaseURL, "")
	if err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "database_configuration_failed", err)
	}
	if output, runErr := command.CombinedOutput(); runErr != nil {
		return BackupRun{}, m.failBackup(ctx, id, "pg_dump_failed", fmt.Errorf("pg_dump: %w: %s", runErr, safeCommandOutput(output)))
	}
	if err = copyFile(m.MasterKeyFile, filepath.Join(stage, "master-keys"), 0o600); err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "master_key_copy_failed", err)
	}
	if err = copyTree(filepath.Join(m.DataDir, "rules"), filepath.Join(stage, "rules")); err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "rules_copy_failed", err)
	}
	if err = m.writeArtifactSnapshot(ctx, stage); err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "artifact_copy_failed", err)
	}
	manifest, err := buildManifest(stage, m.Version)
	if err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "manifest_failed", err)
	}
	manifestBody, _ := json.MarshalIndent(manifest, "", "  ")
	if err = os.WriteFile(filepath.Join(stage, "manifest.json"), manifestBody, 0o600); err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "manifest_failed", err)
	}
	tarPath := filepath.Join(stage, "bundle.tar")
	if err = writeTar(stage, tarPath); err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "archive_failed", err)
	}
	finalName := fmt.Sprintf("ua-%s-%s.age", time.Now().UTC().Format("20060102T150405Z"), id)
	finalPath := filepath.Join(m.BackupsDir, finalName)
	command = exec.CommandContext(ctx, m.AgeBinary, "--recipient", policy.Recipient, "--output", finalPath, tarPath)
	if output, runErr := command.CombinedOutput(); runErr != nil {
		return BackupRun{}, m.failBackup(ctx, id, "encryption_failed", fmt.Errorf("age encryption: %w: %s", runErr, safeCommandOutput(output)))
	}
	hash, size, err := fileDigest(finalPath)
	if err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "bundle_hash_failed", err)
	}
	receiptBody, _ := json.MarshalIndent(map[string]any{"schema": "upload-assistant.backup-receipt.v1", "backup_id": id, "bundle_sha256": hash, "size_bytes": size, "application_version": m.Version}, "", "  ")
	if err = os.WriteFile(finalPath+".receipt.json", receiptBody, 0o600); err != nil {
		_ = os.Remove(finalPath)
		return BackupRun{}, m.failBackup(ctx, id, "receipt_file_failed", err)
	}
	_, err = m.Store.pool.Exec(ctx, `UPDATE backup_runs SET status='complete',bundle_path=$2,bundle_sha256=$3,manifest=$4,size_bytes=$5,finished_at=now() WHERE id=$1`, id, finalPath, hash, manifestBody, size)
	if err != nil {
		return BackupRun{}, m.failBackup(ctx, id, "receipt_failed", err)
	}
	_ = m.prune(ctx, policy.RetentionCount)
	return m.Store.GetBackup(ctx, id)
}

func (m *BackupManager) failBackup(ctx context.Context, id, code string, cause error) error {
	message, _ := Redact(cause.Error()).(string)
	_, err := m.Store.pool.Exec(ctx, `UPDATE backup_runs SET status='failed',error_code=$2,error_message=$3,finished_at=now() WHERE id=$1`, id, code, message)
	if err != nil {
		return err
	}
	_, _ = m.Store.UpsertIncident(ctx, IncidentInput{Severity: "critical", Kind: "backup_failed", Fingerprint: "backup_failed:" + id, Title: "加密备份失败", Summary: message, Evidence: map[string]any{"backup_id": id, "error_code": code}})
	return cause
}

func (m *BackupManager) writeArtifactSnapshot(ctx context.Context, stage string) error {
	rows, err := m.Store.pool.Query(ctx, `SELECT id::text,storage_path,filename,size_bytes,sha256 FROM artifacts ORDER BY id`)
	if err != nil {
		return err
	}
	defer rows.Close()
	metadata := []map[string]any{}
	for rows.Next() {
		var id, path, name, hash string
		var size int64
		if err = rows.Scan(&id, &path, &name, &size, &hash); err != nil {
			return err
		}
		absolute := path
		if !filepath.IsAbs(absolute) {
			absolute = filepath.Join(m.DataDir, path)
		}
		if !pathContained(m.DataDir, absolute) {
			metadata = append(metadata, map[string]any{"id": id, "filename": name, "size_bytes": size, "sha256": hash, "included": false})
			continue
		}
		relative, relErr := filepath.Rel(m.DataDir, absolute)
		if relErr != nil || relative == "." || strings.HasPrefix(relative, "..") {
			continue
		}
		metadata = append(metadata, map[string]any{"id": id, "filename": name, "size_bytes": size, "sha256": hash, "storage_path": filepath.ToSlash(relative), "included": true})
		if _, err = os.Stat(absolute); err == nil {
			target := filepath.Join(stage, "artifacts", id)
			if err = copyFile(absolute, target, 0o600); err != nil {
				return err
			}
		}
	}
	body, _ := json.MarshalIndent(metadata, "", "  ")
	return os.WriteFile(filepath.Join(stage, "artifacts.json"), body, 0o600)
}

func (m *BackupManager) Verify(ctx context.Context, id string) (BackupRun, error) {
	run, err := m.Store.GetBackup(ctx, id)
	if err != nil {
		return BackupRun{}, err
	}
	hash, size, err := fileDigest(run.BundlePath)
	if err != nil {
		return BackupRun{}, m.failVerification(ctx, run, "bundle_unreadable", err)
	}
	if hash != run.BundleSHA256 || size != run.SizeBytes {
		return BackupRun{}, m.failVerification(ctx, run, "bundle_checksum_mismatch", fmt.Errorf("%w: encrypted bundle checksum mismatch", ErrConflict))
	}
	file, err := os.Open(run.BundlePath)
	if err != nil {
		return BackupRun{}, m.failVerification(ctx, run, "bundle_unreadable", err)
	}
	defer file.Close()
	header := make([]byte, 21)
	_, err = io.ReadFull(file, header)
	if err != nil || !strings.HasPrefix(string(header), "age-encryption.org/v1") {
		return BackupRun{}, m.failVerification(ctx, run, "age_envelope_invalid", fmt.Errorf("%w: bundle is not an age v1 envelope", ErrConflict))
	}
	_, err = m.Store.pool.Exec(ctx, `UPDATE backup_runs SET status='verified',verified_at=now() WHERE id=$1 AND status IN('complete','verified')`, id)
	if err != nil {
		return BackupRun{}, err
	}
	correlation := CorrelationFromContext(ctx)
	_, _ = m.Store.pool.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
		VALUES(COALESCE(NULLIF($1,''),'system'),NULLIF($2,''),'backup.verify','backup_run',$3,NULLIF($4,'')::uuid,jsonb_build_object('bundle_sha256',$5::text))`, correlation.ActorType, correlation.ActorID, id, correlation.TraceID, hash)
	return m.Store.GetBackup(ctx, id)
}

func (m *BackupManager) failVerification(ctx context.Context, run BackupRun, code string, cause error) error {
	message, _ := Redact(cause.Error()).(string)
	_, _ = m.Store.pool.Exec(ctx, `UPDATE backup_runs SET status='failed',error_code=$2,error_message=$3,finished_at=COALESCE(finished_at,now()) WHERE id=$1`, run.ID, code, message)
	_, _ = m.Store.UpsertIncident(ctx, IncidentInput{Severity: "critical", Kind: "backup_integrity", Fingerprint: "backup_integrity:" + run.ID, Title: "加密备份完整性校验失败", Summary: message, Evidence: map[string]any{"backup_id": run.ID, "error_code": code, "expected_sha256": run.BundleSHA256}})
	return cause
}

func (m *BackupManager) prune(ctx context.Context, keep int) error {
	rows, err := m.Store.pool.Query(ctx, `SELECT id::text,bundle_path FROM backup_runs WHERE status IN('complete','verified') ORDER BY created_at DESC OFFSET $1`, keep)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id, path string
		if rows.Scan(&id, &path) == nil && pathContained(m.BackupsDir, path) {
			_ = os.Remove(path)
			_ = os.Remove(path + ".receipt.json")
			_, _ = m.Store.pool.Exec(ctx, `UPDATE backup_runs SET bundle_path=NULL,error_code='retention_pruned',error_message='encrypted bundle removed by retention policy' WHERE id=$1`, id)
		}
	}
	return rows.Err()
}

func parseDailySchedule(value string) (int, int, error) {
	fields := strings.Fields(value)
	if len(fields) != 5 || fields[2] != "*" || fields[3] != "*" || fields[4] != "*" {
		return 0, 0, fmt.Errorf("%w: backup schedule must be a daily five-field cron expression", ErrInvalid)
	}
	minute, minuteErr := strconv.Atoi(fields[0])
	hour, hourErr := strconv.Atoi(fields[1])
	if minuteErr != nil || hourErr != nil || minute < 0 || minute > 59 || hour < 0 || hour > 23 {
		return 0, 0, fmt.Errorf("%w: backup schedule hour or minute is invalid", ErrInvalid)
	}
	return hour, minute, nil
}

// RunScheduler evaluates the database policy locally. It never stores a
// private age identity and retries a deferred run only after active write jobs
// have reached a safe boundary.
func (m *BackupManager) RunScheduler(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		_ = m.runScheduledOnce(ctx, time.Now())
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (m *BackupManager) runScheduledOnce(ctx context.Context, now time.Time) error {
	policy, err := m.Store.GetBackupPolicy(ctx)
	if err != nil || !policy.Enabled || policy.Recipient == "" {
		return err
	}
	hour, minute, err := parseDailySchedule(policy.Schedule)
	if err != nil || now.Hour() < hour || (now.Hour() == hour && now.Minute() < minute) {
		return err
	}
	var id, status string
	err = m.Store.pool.QueryRow(ctx, `SELECT id::text,status FROM backup_runs
		WHERE created_at >= date_trunc('day',now()) ORDER BY created_at DESC LIMIT 1`).Scan(&id, &status)
	if errors.Is(err, pgx.ErrNoRows) {
		_, err = m.Create(ctx, security.Principal{})
		return err
	}
	if err != nil || status != "deferred" {
		return err
	}
	var active int
	if err = m.Store.pool.QueryRow(ctx, `SELECT count(*) FROM jobs WHERE status='running'`).Scan(&active); err != nil || active > 0 {
		return err
	}
	command, err := m.Store.pool.Exec(ctx, `UPDATE backup_runs SET status='running',started_at=now(),error_code=NULL,error_message=NULL
		WHERE id=$1 AND status='deferred'`, id)
	if err != nil || command.RowsAffected() == 0 {
		return err
	}
	_, err = m.execute(ctx, id, policy)
	return err
}

type RestoreOptions struct{ BundlePath, IdentityFile, DatabaseURL, DataDir, MasterKeyFile, AgeBinary, PgRestoreBinary, ExpectedVersion string }

func RestoreOffline(ctx context.Context, options RestoreOptions) error {
	if options.AgeBinary == "" {
		options.AgeBinary = "age"
	}
	if options.PgRestoreBinary == "" {
		options.PgRestoreBinary = "pg_restore"
	}
	if !filepath.IsAbs(options.BundlePath) || !filepath.IsAbs(options.IdentityFile) || !filepath.IsAbs(options.DataDir) || !filepath.IsAbs(options.MasterKeyFile) {
		return errors.New("restore paths must be absolute")
	}
	var receipt struct {
		Schema             string `json:"schema"`
		BundleSHA256       string `json:"bundle_sha256"`
		SizeBytes          int64  `json:"size_bytes"`
		ApplicationVersion string `json:"application_version"`
	}
	receiptBody, err := os.ReadFile(options.BundlePath + ".receipt.json")
	if err != nil || json.Unmarshal(receiptBody, &receipt) != nil || receipt.Schema != "upload-assistant.backup-receipt.v1" {
		return errors.New("backup receipt is missing or invalid")
	}
	hash, size, err := fileDigest(options.BundlePath)
	if err != nil || hash != receipt.BundleSHA256 || size != receipt.SizeBytes {
		return errors.New("encrypted backup bundle SHA-256 or size does not match its receipt")
	}
	if receipt.ApplicationVersion != options.ExpectedVersion {
		return errors.New("backup receipt application version is incompatible")
	}
	stage, err := os.MkdirTemp(options.DataDir, ".ua-restore-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(stage)
	tarPath := filepath.Join(stage, "bundle.tar")
	command := exec.CommandContext(ctx, options.AgeBinary, "--decrypt", "--identity", options.IdentityFile, "--output", tarPath, options.BundlePath)
	if output, runErr := command.CombinedOutput(); runErr != nil {
		return fmt.Errorf("decrypt backup: %w: %s", runErr, safeCommandOutput(output))
	}
	extractDir := filepath.Join(stage, "contents")
	if err = os.Mkdir(extractDir, 0o700); err != nil {
		return err
	}
	if err = extractTar(tarPath, extractDir); err != nil {
		return err
	}
	manifestBody, err := os.ReadFile(filepath.Join(extractDir, "manifest.json"))
	if err != nil {
		return err
	}
	var manifest BackupManifest
	if err = json.Unmarshal(manifestBody, &manifest); err != nil {
		return err
	}
	if manifest.Schema != "upload-assistant.backup.v1" || manifest.ApplicationVersion != options.ExpectedVersion {
		return fmt.Errorf("backup application version is incompatible")
	}
	for _, entry := range manifest.Entries {
		path := filepath.Join(extractDir, filepath.FromSlash(entry.Path))
		if !pathContained(extractDir, path) {
			return errors.New("backup manifest contains unsafe path")
		}
		hash, size, e := fileDigest(path)
		if e != nil || hash != entry.SHA256 || size != entry.SizeBytes {
			return fmt.Errorf("backup entry integrity failed: %s", entry.Path)
		}
	}
	targetConfig, err := pgx.ParseConfig(options.DatabaseURL)
	if err != nil || targetConfig.Database == "" || targetConfig.Database == "postgres" || len(targetConfig.Database) > 40 {
		return errors.New("restore target database URL is invalid")
	}
	adminConfig, err := pgx.ParseConfig(options.DatabaseURL)
	if err != nil {
		return err
	}
	adminConfig.Database = "postgres"
	admin, err := pgx.ConnectConfig(ctx, adminConfig)
	if err != nil {
		return fmt.Errorf("connect database control plane: %w", err)
	}
	defer admin.Close(context.Background())
	suffix := strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	temporaryDatabase := "ua_restore_" + suffix
	rollbackDatabase := targetConfig.Database + "_rollback_" + suffix
	if _, err = admin.Exec(ctx, "CREATE DATABASE "+pgx.Identifier{temporaryDatabase}.Sanitize()+" TEMPLATE template0"); err != nil {
		return fmt.Errorf("create temporary restore database: %w", err)
	}
	temporaryExists := true
	defer func() {
		if temporaryExists {
			_, _ = admin.Exec(context.Background(), "DROP DATABASE IF EXISTS "+pgx.Identifier{temporaryDatabase}.Sanitize()+" WITH (FORCE)")
		}
	}()
	command = exec.CommandContext(ctx, options.PgRestoreBinary, "--exit-on-error", "--no-owner", "--no-privileges", "--dbname", temporaryDatabase, filepath.Join(extractDir, "database.dump"))
	command.Env, err = databaseEnvironment(options.DatabaseURL, temporaryDatabase)
	if err != nil {
		return err
	}
	if output, runErr := command.CombinedOutput(); runErr != nil {
		return fmt.Errorf("restore temporary database: %w: %s", runErr, safeCommandOutput(output))
	}
	temporaryPoolConfig, err := pgxpool.ParseConfig(options.DatabaseURL)
	if err != nil {
		return fmt.Errorf("parse temporary database configuration: %w", err)
	}
	temporaryPoolConfig.ConnConfig.Database = temporaryDatabase
	temporaryPool, err := pgxpool.NewWithConfig(ctx, temporaryPoolConfig)
	if err != nil {
		return fmt.Errorf("create temporary database pool: %w", err)
	}
	if err = temporaryPool.Ping(ctx); err != nil {
		temporaryPool.Close()
		return fmt.Errorf("ping temporary database: %w", err)
	}
	if err = database.Migrate(ctx, temporaryPool); err == nil {
		var migrationCount, invalidHashCount int
		err = temporaryPool.QueryRow(ctx, `SELECT (SELECT count(*) FROM schema_migrations),
			(SELECT count(*) FROM job_events WHERE length(event_hash)<>64 OR (previous_hash IS NOT NULL AND length(previous_hash)<>64))`).Scan(&migrationCount, &invalidHashCount)
		if err == nil && (migrationCount == 0 || invalidHashCount != 0) {
			err = errors.New("temporary restore database integrity checks failed")
		}
	}
	temporaryPool.Close()
	if err != nil {
		return fmt.Errorf("validate temporary restore database: %w", err)
	}
	rollbackRoot := filepath.Join(options.DataDir, "restore-rollbacks", time.Now().UTC().Format("20060102T150405Z")+"-"+suffix)
	if err = os.MkdirAll(rollbackRoot, 0o700); err != nil {
		return err
	}
	filesInstalled, err := installRestoredFiles(extractDir, options, rollbackRoot)
	if err != nil {
		return fmt.Errorf("stage restored files: %w", err)
	}
	rollbackFiles := func() {
		if filesInstalled {
			_ = restoreOriginalFiles(options, rollbackRoot)
		}
	}
	if err = quiesceAndRenameDatabase(ctx, admin, targetConfig.Database, rollbackDatabase); err != nil {
		rollbackFiles()
		return fmt.Errorf("preserve original database: %w", err)
	}
	if err = quiesceAndRenameDatabase(ctx, admin, temporaryDatabase, targetConfig.Database); err != nil {
		_ = quiesceAndRenameDatabase(context.Background(), admin, rollbackDatabase, targetConfig.Database)
		rollbackFiles()
		return fmt.Errorf("activate restored database: %w", err)
	}
	temporaryExists = false
	restoreReceipt, _ := json.MarshalIndent(map[string]any{"schema": "upload-assistant.restore-receipt.v1", "restored_at": time.Now().UTC(), "bundle_sha256": hash, "rollback_database": rollbackDatabase, "rollback_files": rollbackRoot}, "", "  ")
	_ = os.WriteFile(filepath.Join(rollbackRoot, "restore-receipt.json"), restoreReceipt, 0o600)
	return nil
}

func quiesceAndRenameDatabase(ctx context.Context, admin *pgx.Conn, source, target string) error {
	var last error
	for attempt := 0; attempt < 10; attempt++ {
		if _, err := admin.Exec(ctx, `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid<>pg_backend_pid()`, source); err != nil {
			return err
		}
		if _, err := admin.Exec(ctx, "ALTER DATABASE "+pgx.Identifier{source}.Sanitize()+" RENAME TO "+pgx.Identifier{target}.Sanitize()); err == nil {
			return nil
		} else {
			last = err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
	return last
}

type artifactBackupMetadata struct {
	ID          string `json:"id"`
	StoragePath string `json:"storage_path"`
}

func installRestoredFiles(extractDir string, options RestoreOptions, rollbackRoot string) (bool, error) {
	if !pathContained(options.DataDir, options.MasterKeyFile) {
		return false, errors.New("master key target must be inside the data directory")
	}
	if _, err := os.Stat(options.MasterKeyFile); err == nil {
		if err = copyFile(options.MasterKeyFile, filepath.Join(rollbackRoot, "master-keys"), 0o600); err != nil {
			return false, err
		}
	}
	rulesTarget := filepath.Join(options.DataDir, "rules")
	if err := copyTree(rulesTarget, filepath.Join(rollbackRoot, "rules")); err != nil {
		return false, err
	}
	metadataBody, err := os.ReadFile(filepath.Join(extractDir, "artifacts.json"))
	if err != nil {
		return false, err
	}
	var artifacts []artifactBackupMetadata
	if err = json.Unmarshal(metadataBody, &artifacts); err != nil {
		return false, err
	}
	if err = os.WriteFile(filepath.Join(rollbackRoot, "artifacts.json"), metadataBody, 0o600); err != nil {
		return false, err
	}
	for _, item := range artifacts {
		if item.ID == "" || item.StoragePath == "" {
			continue
		}
		target := filepath.Join(options.DataDir, filepath.FromSlash(item.StoragePath))
		if !pathContained(options.DataDir, target) {
			return false, errors.New("artifact restore target escapes the data directory")
		}
		if _, statErr := os.Stat(target); statErr == nil {
			if err = copyFile(target, filepath.Join(rollbackRoot, "artifacts", item.ID), 0o600); err != nil {
				return false, err
			}
		}
	}
	if err = copyFile(filepath.Join(extractDir, "master-keys"), options.MasterKeyFile, 0o600); err != nil {
		return false, err
	}
	if err = os.RemoveAll(rulesTarget); err != nil {
		return true, err
	}
	if err = copyTree(filepath.Join(extractDir, "rules"), rulesTarget); err != nil {
		return true, err
	}
	for _, item := range artifacts {
		source := filepath.Join(extractDir, "artifacts", item.ID)
		if _, statErr := os.Stat(source); statErr != nil {
			continue
		}
		target := filepath.Join(options.DataDir, filepath.FromSlash(item.StoragePath))
		if err = copyFile(source, target, 0o600); err != nil {
			return true, err
		}
	}
	return true, nil
}

func restoreOriginalFiles(options RestoreOptions, rollbackRoot string) error {
	if _, err := os.Stat(filepath.Join(rollbackRoot, "master-keys")); err == nil {
		_ = copyFile(filepath.Join(rollbackRoot, "master-keys"), options.MasterKeyFile, 0o600)
	}
	rulesTarget := filepath.Join(options.DataDir, "rules")
	_ = os.RemoveAll(rulesTarget)
	_ = copyTree(filepath.Join(rollbackRoot, "rules"), rulesTarget)
	metadataBody, _ := os.ReadFile(filepath.Join(rollbackRoot, "artifacts.json"))
	var artifacts []artifactBackupMetadata
	_ = json.Unmarshal(metadataBody, &artifacts)
	for _, item := range artifacts {
		target := filepath.Join(options.DataDir, filepath.FromSlash(item.StoragePath))
		if item.StoragePath == "" || !pathContained(options.DataDir, target) {
			continue
		}
		source := filepath.Join(rollbackRoot, "artifacts", item.ID)
		if _, err := os.Stat(source); err == nil {
			_ = copyFile(source, target, 0o600)
		} else {
			_ = os.Remove(target)
		}
	}
	return nil
}

func buildManifest(root, version string) (BackupManifest, error) {
	manifest := BackupManifest{Schema: "upload-assistant.backup.v1", ApplicationVersion: version, CreatedAt: time.Now().UTC().Format(time.RFC3339Nano)}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() || info.Name() == "manifest.json" || info.Name() == "bundle.tar" {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		hash, size, err := fileDigest(path)
		if err != nil {
			return err
		}
		manifest.Entries = append(manifest.Entries, ManifestEntry{Path: filepath.ToSlash(rel), SHA256: hash, SizeBytes: size})
		return nil
	})
	return manifest, err
}
func fileDigest(path string) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, file)
	return hex.EncodeToString(hash.Sum(nil)), size, err
}
func copyFile(source, target string, mode os.FileMode) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	if err = os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
		return err
	}
	output, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	if copyErr != nil {
		return copyErr
	}
	return closeErr
}
func copyTree(source, target string) error {
	if _, err := os.Stat(source); errors.Is(err, os.ErrNotExist) {
		return os.MkdirAll(target, 0o750)
	}
	return filepath.Walk(source, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(source, path)
		destination := filepath.Join(target, rel)
		if info.IsDir() {
			return os.MkdirAll(destination, 0o750)
		}
		if info.Mode().IsRegular() {
			return copyFile(path, destination, 0o600)
		}
		return nil
	})
}
func pathContained(base, path string) bool {
	baseAbs, e1 := filepath.Abs(base)
	pathAbs, e2 := filepath.Abs(path)
	if e1 != nil || e2 != nil {
		return false
	}
	rel, err := filepath.Rel(baseAbs, pathAbs)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
func writeTar(root, target string) error {
	file, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	writer := tar.NewWriter(file)
	err = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if path == target || info.IsDir() {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		header.Name = filepath.ToSlash(rel)
		header.Mode = 0600
		if err = writer.WriteHeader(header); err != nil {
			return err
		}
		input, err := os.Open(path)
		if err != nil {
			return err
		}
		_, copyErr := io.Copy(writer, input)
		closeErr := input.Close()
		if copyErr != nil {
			return copyErr
		}
		return closeErr
	})
	closeWriter := writer.Close()
	closeFile := file.Close()
	if err != nil {
		return err
	}
	if closeWriter != nil {
		return closeWriter
	}
	return closeFile
}
func extractTar(source, target string) error {
	file, err := os.Open(source)
	if err != nil {
		return err
	}
	defer file.Close()
	reader := tar.NewReader(file)
	for {
		header, err := reader.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		path := filepath.Join(target, filepath.FromSlash(header.Name))
		if !pathContained(target, path) {
			return errors.New("archive contains unsafe path")
		}
		if header.Typeflag != tar.TypeReg {
			return errors.New("archive contains unsupported entry")
		}
		if err = os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		out, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
		if err != nil {
			return err
		}
		_, copyErr := io.Copy(out, reader)
		closeErr := out.Close()
		if copyErr != nil {
			return copyErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
}
func databaseEnvironment(databaseURL, databaseOverride string) ([]string, error) {
	config, err := pgx.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse backup database URL: %w", err)
	}
	if databaseOverride != "" {
		config.Database = databaseOverride
	}
	sslMode := "require"
	if config.TLSConfig == nil {
		sslMode = "disable"
	} else {
		for _, fallback := range config.Fallbacks {
			if fallback.TLSConfig == nil {
				sslMode = "prefer"
				break
			}
		}
	}
	values := map[string]string{
		"PGHOST": config.Host, "PGPORT": strconv.Itoa(int(config.Port)), "PGUSER": config.User,
		"PGPASSWORD": config.Password, "PGDATABASE": config.Database, "PGSSLMODE": sslMode,
		"PGAPPNAME": "upload-assistant-backup",
	}
	if parsed, parseErr := url.Parse(databaseURL); parseErr == nil && (parsed.Scheme == "postgres" || parsed.Scheme == "postgresql") {
		for queryName, environmentName := range map[string]string{
			"sslmode": "PGSSLMODE", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY", "sslrootcert": "PGSSLROOTCERT",
			"sslcrl": "PGSSLCRL", "connect_timeout": "PGCONNECT_TIMEOUT", "target_session_attrs": "PGTARGETSESSIONATTRS",
		} {
			if value := parsed.Query().Get(queryName); value != "" {
				values[environmentName] = value
			}
		}
	}
	environment := make([]string, 0, len(os.Environ())+len(values))
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if _, replaced := values[name]; !replaced {
			environment = append(environment, item)
		}
	}
	for _, name := range []string{"PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE", "PGSSLCERT", "PGSSLKEY", "PGSSLROOTCERT", "PGSSLCRL", "PGCONNECT_TIMEOUT", "PGTARGETSESSIONATTRS", "PGAPPNAME"} {
		if value, ok := values[name]; ok {
			environment = append(environment, name+"="+value)
		}
	}
	return environment, nil
}
func safeCommandOutput(body []byte) string {
	value := strings.TrimSpace(string(body))
	if len(value) > 1000 {
		value = value[:1000]
	}
	redacted, _ := Redact(value).(string)
	return redacted
}
