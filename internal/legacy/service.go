package legacy

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var (
	ErrConfirmationRequired = errors.New("legacy import confirmation is required")
	ErrFingerprintMismatch  = errors.New("legacy source fingerprint mismatch")
	ErrImportNotFound       = errors.New("legacy import not found")
)

const archivePurposePrefix = "legacy_imports."

type ConfigurationWriter interface {
	PutSiteCredential(context.Context, string, string, string, workflow.Actor) (integrations.SiteCredential, error)
	UpsertDownloader(context.Context, string, integrations.DownloaderInput, workflow.Actor) (integrations.Downloader, error)
	UpsertImageHost(context.Context, string, integrations.ImageHostInput, workflow.Actor) (integrations.ImageHost, error)
	CreateScreenshotProfile(context.Context, integrations.ScreenshotProfileInput, workflow.Actor) (integrations.ScreenshotProfile, error)
	UpsertMediaManager(context.Context, string, integrations.MediaManagerInput, workflow.Actor) (integrations.MediaManager, error)
}

type ImportRequest struct {
	SourceFingerprint string `json:"source_fingerprint"`
	ConfirmImport     bool   `json:"confirm_import"`
}

type AppliedResource struct {
	Kind       string `json:"kind"`
	Name       string `json:"name"`
	ResourceID string `json:"resource_id,omitempty"`
	Status     string `json:"status"`
}

type ImportReport struct {
	Preview     Preview           `json:"preview"`
	Applied     []AppliedResource `json:"applied"`
	Blockers    []Issue           `json:"blockers"`
	NextActions []Issue           `json:"next_actions"`
	Summary     string            `json:"summary"`
}

type ImportRecord struct {
	ID                string       `json:"id"`
	OK                bool         `json:"ok"`
	Status            string       `json:"status"`
	SourceKind        string       `json:"source_kind"`
	SourcePath        string       `json:"source_path"`
	SourceFingerprint string       `json:"source_fingerprint"`
	Report            ImportReport `json:"report"`
	ArchiveAvailable  bool         `json:"archive_available"`
	ArchiveSHA256     string       `json:"archive_sha256,omitempty"`
	ArchiveSizeBytes  int64        `json:"archive_size_bytes,omitempty"`
	ArchiveExpiresAt  time.Time    `json:"archive_expires_at"`
	ArchiveDeletedAt  *time.Time   `json:"archive_deleted_at,omitempty"`
	CreatedAt         time.Time    `json:"created_at"`
	UpdatedAt         time.Time    `json:"updated_at"`
	FinishedAt        *time.Time   `json:"finished_at,omitempty"`
	IdempotentReplay  bool         `json:"idempotent_replay,omitempty"`
}

type Service struct {
	pool      *pgxpool.Pool
	secrets   *security.SecretStore
	writer    ConfigurationWriter
	legacyDir string
	logger    *slog.Logger
	now       func() time.Time
}

func NewService(pool *pgxpool.Pool, secrets *security.SecretStore, writer ConfigurationWriter, legacyDir string, logger *slog.Logger) (*Service, error) {
	legacyDir = filepath.Clean(strings.TrimSpace(legacyDir))
	if pool == nil || secrets == nil || writer == nil {
		return nil, errors.New("legacy migration dependencies are required")
	}
	if !filepath.IsAbs(legacyDir) {
		return nil, errors.New("legacy migration directory must be absolute")
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Service{pool: pool, secrets: secrets, writer: writer, legacyDir: legacyDir, logger: logger, now: time.Now}, nil
}

func (service *Service) Preview(_ context.Context) (Preview, error) {
	plan, err := Inspect(service.legacyDir, service.secrets)
	if err != nil {
		return Preview{}, err
	}
	return plan.Preview, nil
}

func (service *Service) Import(ctx context.Context, request ImportRequest, actor workflow.Actor) (ImportRecord, error) {
	if !request.ConfirmImport {
		return ImportRecord{}, ErrConfirmationRequired
	}
	plan, err := Inspect(service.legacyDir, service.secrets)
	if err != nil {
		return ImportRecord{}, err
	}
	if !plan.OK {
		return ImportRecord{}, fmt.Errorf("%w: migration preview is blocked", ErrSourceInvalid)
	}
	expected := strings.ToLower(strings.TrimSpace(request.SourceFingerprint))
	if len(expected) != len(plan.SourceFingerprint) || subtle.ConstantTimeCompare([]byte(expected), []byte(plan.SourceFingerprint)) != 1 {
		return ImportRecord{}, ErrFingerprintMismatch
	}

	connection, err := service.pool.Acquire(ctx)
	if err != nil {
		return ImportRecord{}, fmt.Errorf("acquire legacy import lock connection: %w", err)
	}
	defer connection.Release()
	if _, err := connection.Exec(ctx, "SELECT pg_advisory_lock(hashtextextended($1, 0))", plan.SourceFingerprint); err != nil {
		return ImportRecord{}, fmt.Errorf("lock legacy source fingerprint: %w", err)
	}
	defer func() {
		unlockCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = connection.Exec(unlockCtx, "SELECT pg_advisory_unlock(hashtextextended($1, 0))", plan.SourceFingerprint)
	}()

	if existing, err := service.findComplete(ctx, plan.SourceKind, plan.SourceFingerprint); err == nil {
		existing.IdempotentReplay = true
		return existing, nil
	} else if !errors.Is(err, ErrImportNotFound) {
		return ImportRecord{}, err
	}

	startedAt := service.now().UTC()
	archive, err := buildArchive(plan, startedAt)
	if err != nil {
		return ImportRecord{}, fmt.Errorf("build encrypted legacy archive: %w", err)
	}
	importID := uuid.NewString()
	purpose := archivePurpose(importID)
	storedArchive, err := service.secrets.PutDetailed(ctx, purpose, archive, actor.ID)
	if err != nil {
		return ImportRecord{}, fmt.Errorf("encrypt legacy archive: %w", err)
	}
	archiveSecretID := storedArchive.ID
	archiveSHA := storedArchive.CiphertextSHA256
	archiveSize := storedArchive.CiphertextSizeBytes
	report := ImportReport{
		Preview: plan.Preview, Applied: []AppliedResource{}, Blockers: []Issue{}, NextActions: []Issue{},
		Summary: "迁移已开始；每个资源写入后都会更新审计报告。",
	}
	record, err := service.createRecord(ctx, importID, archiveSecretID, archiveSHA, archiveSize, startedAt, report, actor)
	if err != nil {
		_ = service.secrets.Delete(context.Background(), archiveSecretID, purpose)
		return ImportRecord{}, err
	}

	apply := func(kind, name string, operation func() (string, error)) bool {
		resourceID, operationErr := operation()
		if operationErr != nil {
			report.Blockers = append(report.Blockers, Issue{Code: "legacy_resource_import_failed", Resource: kind + ":" + name, Message: "资源写入失败；已停止后续迁移，修正配置后可重新预览并执行。"})
			report.NextActions = append(report.NextActions, Issue{Code: "review_legacy_resource", Resource: kind + ":" + name, Message: "检查该资源的新配置约束和服务日志；敏感值不会出现在报告中。"})
			report.Summary = fmt.Sprintf("迁移在 %s %s 处失败；此前成功项保留且可审计。", kind, name)
			if updateErr := service.finishRecord(ctx, record.ID, "failed", report, true, actor); updateErr != nil {
				service.logger.Error("legacy import failed and report update also failed", "import_id", record.ID, "resource_kind", kind, "resource_name", name)
			}
			service.logger.Error("legacy resource import failed", "import_id", record.ID, "resource_kind", kind, "resource_name", name)
			return false
		}
		report.Applied = append(report.Applied, AppliedResource{Kind: kind, Name: name, ResourceID: resourceID, Status: "configured"})
		if updateErr := service.updateReport(ctx, record.ID, report); updateErr != nil {
			report.Blockers = append(report.Blockers, Issue{Code: "legacy_audit_update_failed", Resource: kind + ":" + name, Message: "资源已写入，但迁移进度报告持久化失败；已停止后续迁移。"})
			report.Summary = "迁移因审计进度无法持久化而停止。"
			_ = service.finishRecord(ctx, record.ID, "failed", report, true, actor)
			service.logger.Error("legacy import progress update failed", "import_id", record.ID, "resource_kind", kind, "resource_name", name)
			return false
		}
		return true
	}

	for _, operation := range plan.sites {
		operation := operation
		if !apply("site_credential", operation.siteCode+"."+operation.name, func() (string, error) {
			resource, err := service.writer.PutSiteCredential(ctx, operation.siteCode, operation.name, operation.value, actor)
			return resource.ID, err
		}) {
			return service.Get(ctx, record.ID)
		}
	}
	for _, operation := range plan.downloaders {
		operation := operation
		if !apply("downloader", operation.name, func() (string, error) {
			resource, err := service.writer.UpsertDownloader(ctx, operation.name, operation.input, actor)
			return resource.ID, err
		}) {
			return service.Get(ctx, record.ID)
		}
	}
	for _, operation := range plan.imageHosts {
		operation := operation
		if !apply("image_host", operation.name, func() (string, error) {
			resource, err := service.writer.UpsertImageHost(ctx, operation.name, operation.input, actor)
			return resource.ID, err
		}) {
			return service.Get(ctx, record.ID)
		}
	}
	for _, operation := range plan.screenshots {
		operation := operation
		if !apply("screenshot_profile", operation.input.Name, func() (string, error) {
			resource, err := service.writer.CreateScreenshotProfile(ctx, operation.input, actor)
			return resource.ID, err
		}) {
			return service.Get(ctx, record.ID)
		}
	}
	for _, operation := range plan.mediaManagers {
		operation := operation
		if !apply("media_manager", operation.name, func() (string, error) {
			resource, err := service.writer.UpsertMediaManager(ctx, operation.name, operation.input, actor)
			return resource.ID, err
		}) {
			return service.Get(ctx, record.ID)
		}
	}
	report.Summary = fmt.Sprintf("迁移完成：已配置 %d 个资源；旧文件未被修改，源快照加密保留 %d 天。", len(report.Applied), archiveRetentionDays)
	report.NextActions = []Issue{
		{Code: "review_import_warnings", Message: "逐项处理预览 warnings，尤其是规则限速、容器路径和暂未支持的集成。"},
		{Code: "probe_integrations", Message: "在 Web/API 中显式探测下载器，并用非 live 任务验证配置；迁移不会自动联网。"},
	}
	if err := service.finishRecord(ctx, record.ID, "complete", report, true, actor); err != nil {
		return ImportRecord{}, err
	}
	return service.Get(ctx, record.ID)
}

func archivePurpose(importID string) string { return archivePurposePrefix + importID + ".archive" }

func (service *Service) createRecord(
	ctx context.Context, id, archiveSecretID, archiveSHA string, archiveSize int64, startedAt time.Time,
	report ImportReport, actor workflow.Actor,
) (ImportRecord, error) {
	reportBody, err := json.Marshal(report)
	if err != nil {
		return ImportRecord{}, fmt.Errorf("encode legacy import report: %w", err)
	}
	tx, err := service.pool.Begin(ctx)
	if err != nil {
		return ImportRecord{}, fmt.Errorf("begin legacy import: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	expiresAt := startedAt.AddDate(0, 0, archiveRetentionDays)
	_, err = tx.Exec(ctx, `
		INSERT INTO legacy_imports(
			id, source_kind, source_path, source_sha256, status, report, imported_by,
			expires_at, archive_secret_id, archive_sha256, archive_size_bytes, created_at, updated_at
		) VALUES ($1, $2, $3, $4, 'running', $5, NULLIF($6, '')::uuid, $7, $8, $9, $10, $11, $11)`,
		id, "upload_assistant_python_config", filepath.ToSlash(filepath.Join(service.legacyDir, legacyConfigFilename)),
		report.Preview.SourceFingerprint, reportBody, actor.ID, expiresAt, archiveSecretID, archiveSHA, archiveSize, startedAt,
	)
	if err != nil {
		return ImportRecord{}, fmt.Errorf("create legacy import record: %w", err)
	}
	if err := insertImportAudit(ctx, tx, actor, "legacy_import.started", id, map[string]any{
		"source_fingerprint": report.Preview.SourceFingerprint,
		"source_file_count":  len(report.Preview.SourceFiles), "resource_count": len(report.Preview.Resources),
		"archive_sha256": archiveSHA, "archive_size_bytes": archiveSize, "archive_expires_at": expiresAt,
	}); err != nil {
		return ImportRecord{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return ImportRecord{}, fmt.Errorf("commit legacy import record: %w", err)
	}
	return service.Get(ctx, id)
}

func (service *Service) updateReport(ctx context.Context, id string, report ImportReport) error {
	body, err := json.Marshal(report)
	if err != nil {
		return err
	}
	command, err := service.pool.Exec(ctx, `
		UPDATE legacy_imports SET report = $2, updated_at = now()
		WHERE id = $1 AND status = 'running'`, id, body)
	if err != nil {
		return fmt.Errorf("update legacy import report: %w", err)
	}
	if command.RowsAffected() != 1 {
		return ErrImportNotFound
	}
	return nil
}

func (service *Service) finishRecord(ctx context.Context, id, status string, report ImportReport, finished bool, actor workflow.Actor) error {
	if status != "complete" && status != "failed" && status != "blocked" {
		return errors.New("invalid legacy import terminal status")
	}
	body, err := json.Marshal(report)
	if err != nil {
		return err
	}
	tx, err := service.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin legacy import completion: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var finishedAt any
	if finished {
		finishedAt = service.now().UTC()
	}
	command, err := tx.Exec(ctx, `
		UPDATE legacy_imports SET status = $2, report = $3, finished_at = $4, updated_at = now()
		WHERE id = $1 AND status = 'running'`, id, status, body, finishedAt)
	if err != nil {
		return fmt.Errorf("finish legacy import: %w", err)
	}
	if command.RowsAffected() != 1 {
		return ErrImportNotFound
	}
	if err := insertImportAudit(ctx, tx, actor, "legacy_import."+status, id, map[string]any{
		"status": status, "applied_resource_count": len(report.Applied),
		"blocker_codes": issueCodes(report.Blockers), "warning_codes": issueCodes(report.Preview.Warnings),
	}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (service *Service) Get(ctx context.Context, id string) (ImportRecord, error) {
	if _, err := uuid.Parse(id); err != nil {
		return ImportRecord{}, ErrImportNotFound
	}
	return scanImport(service.pool.QueryRow(ctx, importSelect+" WHERE id = $1", id))
}

func (service *Service) List(ctx context.Context, limit int) ([]ImportRecord, error) {
	if limit <= 0 {
		limit = 25
	}
	if limit > 100 {
		limit = 100
	}
	rows, err := service.pool.Query(ctx, importSelect+" ORDER BY created_at DESC LIMIT $1", limit)
	if err != nil {
		return nil, fmt.Errorf("list legacy imports: %w", err)
	}
	defer rows.Close()
	result := make([]ImportRecord, 0)
	for rows.Next() {
		record, err := scanImport(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, record)
	}
	return result, rows.Err()
}

func (service *Service) findComplete(ctx context.Context, sourceKind, fingerprint string) (ImportRecord, error) {
	return scanImport(service.pool.QueryRow(ctx, importSelect+`
		WHERE source_kind = $1 AND source_sha256 = $2 AND status = 'complete'
		ORDER BY created_at DESC LIMIT 1`, sourceKind, fingerprint))
}

const importSelect = `
	SELECT id::text, source_kind, COALESCE(source_path, ''), COALESCE(source_sha256, ''), status, report,
	       archive_secret_id IS NOT NULL, COALESCE(archive_sha256, ''), COALESCE(archive_size_bytes, 0),
	       expires_at, archive_deleted_at, created_at, updated_at, finished_at
	FROM legacy_imports`

type rowScanner interface{ Scan(...any) error }

func scanImport(row rowScanner) (ImportRecord, error) {
	var record ImportRecord
	var reportBody []byte
	err := row.Scan(
		&record.ID, &record.SourceKind, &record.SourcePath, &record.SourceFingerprint, &record.Status, &reportBody,
		&record.ArchiveAvailable, &record.ArchiveSHA256, &record.ArchiveSizeBytes, &record.ArchiveExpiresAt,
		&record.ArchiveDeletedAt, &record.CreatedAt, &record.UpdatedAt, &record.FinishedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return ImportRecord{}, ErrImportNotFound
	}
	if err != nil {
		return ImportRecord{}, fmt.Errorf("scan legacy import: %w", err)
	}
	if err := json.Unmarshal(reportBody, &record.Report); err != nil {
		return ImportRecord{}, fmt.Errorf("decode legacy import report: %w", err)
	}
	record.OK = record.Status == "complete"
	return record, nil
}

func (service *Service) CleanupExpired(ctx context.Context, actor workflow.Actor) (int, error) {
	tx, err := service.pool.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin legacy archive cleanup: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows, err := tx.Query(ctx, `
		SELECT id::text, archive_secret_id::text FROM legacy_imports
		WHERE archive_secret_id IS NOT NULL AND expires_at <= now()
		ORDER BY expires_at FOR UPDATE SKIP LOCKED`)
	if err != nil {
		return 0, fmt.Errorf("select expired legacy archives: %w", err)
	}
	type expiredArchive struct{ importID, secretID string }
	archives := []expiredArchive{}
	for rows.Next() {
		var archive expiredArchive
		if err := rows.Scan(&archive.importID, &archive.secretID); err != nil {
			rows.Close()
			return 0, fmt.Errorf("scan expired legacy archive: %w", err)
		}
		archives = append(archives, archive)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, fmt.Errorf("iterate expired legacy archives: %w", err)
	}
	for _, archive := range archives {
		command, err := tx.Exec(ctx, "DELETE FROM secrets WHERE id = $1 AND purpose = $2", archive.secretID, archivePurpose(archive.importID))
		if err != nil {
			return 0, fmt.Errorf("delete expired encrypted legacy archive: %w", err)
		}
		if command.RowsAffected() != 1 {
			return 0, errors.New("expired legacy archive purpose mismatch")
		}
		if _, err := tx.Exec(ctx, "UPDATE legacy_imports SET archive_deleted_at = now(), updated_at = now() WHERE id = $1", archive.importID); err != nil {
			return 0, fmt.Errorf("mark expired legacy archive deleted: %w", err)
		}
		if err := insertImportAudit(ctx, tx, actor, "legacy_import.archive_expired", archive.importID, map[string]any{"retention_days": archiveRetentionDays}); err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit legacy archive cleanup: %w", err)
	}
	return len(archives), nil
}

func insertImportAudit(ctx context.Context, tx pgx.Tx, actor workflow.Actor, action, resourceID string, payload map[string]any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, NULLIF($2, ''), $3, 'legacy_import', $4, $5)`, actor.Type, actor.ID, action, resourceID, body); err != nil {
		return fmt.Errorf("audit legacy import: %w", err)
	}
	return nil
}

func issueCodes(issues []Issue) []string {
	result := make([]string, 0, len(issues))
	for _, issue := range issues {
		result = append(result, issue.Code)
	}
	return result
}
