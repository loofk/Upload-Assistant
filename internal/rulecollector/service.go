package rulecollector

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/siteaccess"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var sourceIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

type RuntimeSites interface {
	GetRuntimeSite(context.Context, string) (integrations.RuntimeSite, error)
}

type AccessGate interface {
	GetRuleCollectionPolicy(context.Context, string) (siteaccess.EffectivePolicy, error)
	AcquireRuleCollection(context.Context, sites.AccessRequest) (sites.AccessLease, error)
	Complete(context.Context, sites.AccessLease, sites.AccessResult) error
}

type Analyzer interface {
	AnalyzeRuleText(context.Context, operations.RuleAnalysisInput, security.Principal, string) (operations.RuleAnalysisResult, error)
	ProviderContractFingerprint(context.Context, string, string) (string, error)
}

type RuleImporter interface {
	Import(context.Context, []byte, workflow.Actor) (rules.Revision, error)
}

type Service struct {
	pool       *pgxpool.Pool
	dataDir    string
	sites      RuntimeSites
	access     AccessGate
	analyzer   Analyzer
	rules      RuleImporter
	httpClient *http.Client
	now        func() time.Time
}

func NewService(pool *pgxpool.Pool, dataDir string, sites RuntimeSites, access AccessGate, analyzer Analyzer, ruleStore RuleImporter) *Service {
	return &Service{
		pool: pool, dataDir: dataDir, sites: sites, access: access, analyzer: analyzer, rules: ruleStore,
		httpClient: safeHTTPClient(45 * time.Second), now: time.Now,
	}
}

func (s *Service) PutSourceSet(ctx context.Context, siteCode string, input SourceSetInput, actor workflow.Actor) (SourceSet, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	sources, err := normalizeSources(input.Sources)
	if err != nil {
		return SourceSet{}, err
	}
	if _, err := uuid.Parse(actor.ID); err != nil {
		return SourceSet{}, fmt.Errorf("%w: authenticated user is required", ErrInvalid)
	}
	body, _ := json.Marshal(struct {
		Sources              []SourceInput `json:"sources"`
		ScopeConfirmed       bool          `json:"scope_confirmed"`
		CookieHostsConfirmed bool          `json:"cookie_hosts_confirmed"`
	}{sources, input.ScopeConfirmed, input.CookieHostsConfirmed})
	digest := sha256.Sum256(body)
	fingerprint := hex.EncodeToString(digest[:])
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return SourceSet{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID string
	if err := tx.QueryRow(ctx, `SELECT id::text FROM sites WHERE code=$1 FOR UPDATE`, siteCode).Scan(&siteID); errors.Is(err, pgx.ErrNoRows) {
		return SourceSet{}, ErrNotFound
	} else if err != nil {
		return SourceSet{}, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO site_rule_source_sets(site_id,sources,fingerprint,scope_confirmed,cookie_hosts_confirmed,updated_by)
		VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(site_id) DO UPDATE SET sources=EXCLUDED.sources,
		fingerprint=EXCLUDED.fingerprint,scope_confirmed=EXCLUDED.scope_confirmed,
		cookie_hosts_confirmed=EXCLUDED.cookie_hosts_confirmed,updated_by=EXCLUDED.updated_by,updated_at=now()`,
		siteID, bodyForSources(sources), fingerprint, input.ScopeConfirmed, input.CookieHostsConfirmed, actor.ID)
	if err != nil {
		return SourceSet{}, fmt.Errorf("save rule source set: %w", err)
	}
	_, err = tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,payload)
		VALUES($1,$2,'site_rule.sources_put','site',$3,$4)`, actor.Type, actor.ID, siteID, mustJSON(map[string]any{
		"site_code": siteCode, "fingerprint": fingerprint, "source_count": len(sources),
		"hosts": sourceHosts(sources), "cookie_hosts": cookieSourceHosts(sources), "scope_confirmed": input.ScopeConfirmed,
		"cookie_hosts_confirmed": input.CookieHostsConfirmed,
	}))
	if err != nil {
		return SourceSet{}, fmt.Errorf("audit rule source set: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return SourceSet{}, err
	}
	return s.GetSourceSet(ctx, siteCode)
}

func (s *Service) GetSourceSet(ctx context.Context, siteCode string) (SourceSet, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	var result SourceSet
	var raw json.RawMessage
	var updatedAt *time.Time
	err := s.pool.QueryRow(ctx, `SELECT site.code,COALESCE(set.sources,'[]'::jsonb),COALESCE(set.fingerprint,''),
		COALESCE(set.scope_confirmed,false),COALESCE(set.cookie_hosts_confirmed,false),set.updated_at,
		EXISTS(SELECT 1 FROM site_credentials credential WHERE credential.site_id=site.id AND credential.name='cookie' AND credential.enabled)
		FROM sites site LEFT JOIN site_rule_source_sets set ON set.site_id=site.id WHERE site.code=$1`, siteCode).Scan(
		&result.SiteCode, &raw, &result.Fingerprint, &result.ScopeConfirmed, &result.CookieHostsConfirmed, &updatedAt, &result.CookieConfigured)
	if errors.Is(err, pgx.ErrNoRows) {
		return SourceSet{}, ErrNotFound
	}
	if err != nil {
		return SourceSet{}, fmt.Errorf("load rule source set: %w", err)
	}
	if err := json.Unmarshal(raw, &result.Sources); err != nil {
		return SourceSet{}, fmt.Errorf("decode rule source set: %w", err)
	}
	// Source sets written before source-level authentication existed are kept
	// fail-closed: they continue to require the site's Cookie until explicitly
	// changed and fingerprinted by an operator.
	for index := range result.Sources {
		if strings.TrimSpace(result.Sources[index].AuthMode) == "" {
			result.Sources[index].AuthMode = SourceAuthSiteCookie
		}
	}
	result.CookieRequired = sourcesRequireCookie(result.Sources)
	if updatedAt != nil {
		result.UpdatedAt = *updatedAt
	}
	return result, nil
}

func (s *Service) CreateRun(ctx context.Context, siteCode string, input CreateRunInput, actor workflow.Actor) (CollectionRun, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	input.SourceSetFingerprint = strings.ToLower(strings.TrimSpace(input.SourceSetFingerprint))
	input.ProviderID = strings.TrimSpace(input.ProviderID)
	input.IdempotencyKey = strings.TrimSpace(input.IdempotencyKey)
	if _, err := uuid.Parse(actor.ID); err != nil {
		return CollectionRun{}, fmt.Errorf("%w: authenticated user is required", ErrInvalid)
	}
	if !input.Confirm {
		return CollectionRun{}, fmt.Errorf("%w: explicit confirmation of external rule-page reads and inference is required", ErrInvalid)
	}
	if _, err := uuid.Parse(input.ProviderID); err != nil || len(input.IdempotencyKey) < 8 || len(input.IdempotencyKey) > 200 {
		return CollectionRun{}, fmt.Errorf("%w: provider_id and an 8..200 byte idempotency key are required", ErrInvalid)
	}
	set, err := s.GetSourceSet(ctx, siteCode)
	if err != nil {
		return CollectionRun{}, err
	}
	if set.Fingerprint != input.SourceSetFingerprint {
		return CollectionRun{}, fmt.Errorf("%w: rule source set fingerprint changed", ErrConflict)
	}
	if !set.ScopeConfirmed || (set.CookieRequired && !set.CookieHostsConfirmed) {
		return CollectionRun{}, fmt.Errorf("%w: source completeness and any Cookie hosts must be explicitly confirmed", ErrConflict)
	}
	if set.CookieRequired && !set.CookieConfigured {
		return CollectionRun{}, ErrCredential
	}
	policy, err := s.access.GetRuleCollectionPolicy(ctx, siteCode)
	if err != nil {
		return CollectionRun{}, err
	}
	if len(policy.Blockers) > 0 {
		return CollectionRun{}, &siteaccess.DeniedError{Code: policy.Blockers[0].Code, Message: policy.Blockers[0].Message}
	}
	if existing, err := s.runByIdempotency(ctx, actor.ID, input.IdempotencyKey); err == nil {
		if existing.SiteCode == siteCode && existing.SourceSetFingerprint == input.SourceSetFingerprint && existing.ProviderID == input.ProviderID {
			return existing, nil
		}
		return CollectionRun{}, fmt.Errorf("%w: idempotency key is already bound to different input", ErrConflict)
	} else if !errors.Is(err, ErrNotFound) {
		return CollectionRun{}, err
	}
	providerConfigSHA256, err := s.analyzer.ProviderContractFingerprint(ctx, input.ProviderID, operations.ProviderUseCaseRuleAnalysis)
	if err != nil {
		return CollectionRun{}, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return CollectionRun{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID, runID string
	traceID := nullableUUID(input.TraceID)
	err = tx.QueryRow(ctx, `INSERT INTO site_rule_collection_runs(site_id,source_set_fingerprint,provider_id,provider_config_sha256,status,idempotency_key,created_by,trace_id)
		SELECT id,$2,$3,$4,'queued',$5,$6,$7 FROM sites WHERE code=$1 RETURNING id::text`,
		siteCode, input.SourceSetFingerprint, input.ProviderID, providerConfigSHA256, input.IdempotencyKey, actor.ID, traceID).Scan(&runID)
	if errors.Is(err, pgx.ErrNoRows) {
		return CollectionRun{}, ErrNotFound
	}
	if err != nil {
		return CollectionRun{}, fmt.Errorf("create rule collection run: %w", err)
	}
	if err := tx.QueryRow(ctx, `SELECT id::text FROM sites WHERE code=$1`, siteCode).Scan(&siteID); err != nil {
		return CollectionRun{}, err
	}
	for ordinal, source := range set.Sources {
		_, err = tx.Exec(ctx, `INSERT INTO site_rule_collection_documents(run_id,source_id,ordinal,source_url,scope,auth_mode,status) VALUES($1,$2,$3,$4,$5,$6,'pending')`,
			runID, source.ID, ordinal, source.URL, source.Scope, source.AuthMode)
		if err != nil {
			return CollectionRun{}, fmt.Errorf("create rule collection document: %w", err)
		}
	}
	_, err = tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
		VALUES($1,$2,'site_rule.collection_create','site_rule_collection_run',$3,$4,$5)`, actor.Type, actor.ID, runID, traceID, mustJSON(map[string]any{
		"site_code": siteCode, "source_set_fingerprint": input.SourceSetFingerprint,
		"provider_id": input.ProviderID, "source_count": len(set.Sources), "site_id": siteID,
	}))
	if err != nil {
		return CollectionRun{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return CollectionRun{}, err
	}
	return s.GetRun(ctx, runID)
}

func (s *Service) GetRun(ctx context.Context, runID string) (CollectionRun, error) {
	var result CollectionRun
	var startedAt, completedAt *time.Time
	err := s.pool.QueryRow(ctx, `SELECT run.id::text,site.code,run.source_set_fingerprint,run.provider_id::text,COALESCE(run.provider_config_sha256,''),run.status,run.not_before,
		COALESCE(run.rule_revision_id::text,''),COALESCE(run.error_code,''),COALESCE(run.error_detail,''),
		run.created_at,run.started_at,run.completed_at,run.updated_at
		FROM site_rule_collection_runs run JOIN sites site ON site.id=run.site_id WHERE run.id=$1`, runID).Scan(
		&result.ID, &result.SiteCode, &result.SourceSetFingerprint, &result.ProviderID, &result.ProviderConfigSHA256, &result.Status, &result.NotBefore,
		&result.RuleRevisionID, &result.ErrorCode, &result.ErrorDetail, &result.CreatedAt, &startedAt, &completedAt, &result.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return CollectionRun{}, ErrNotFound
	}
	if err != nil {
		return CollectionRun{}, fmt.Errorf("load rule collection run: %w", err)
	}
	result.StartedAt, result.CompletedAt = startedAt, completedAt
	rows, err := s.pool.Query(ctx, `SELECT id::text,source_id,source_url,scope,auth_mode,status,COALESCE(http_status,0),COALESCE(content_type,''),
		COALESCE(size_bytes,0),COALESCE(text_sha256,''),COALESCE(error_code,''),COALESCE(error_detail,''),captured_at
		FROM site_rule_collection_documents WHERE run_id=$1 ORDER BY ordinal`, runID)
	if err != nil {
		return CollectionRun{}, err
	}
	defer rows.Close()
	result.Documents = []CollectionDocument{}
	for rows.Next() {
		var item CollectionDocument
		if err := rows.Scan(&item.ID, &item.SourceID, &item.URL, &item.Scope, &item.AuthMode, &item.Status, &item.HTTPStatus,
			&item.ContentType, &item.SizeBytes, &item.TextSHA256, &item.ErrorCode, &item.ErrorDetail, &item.CapturedAt); err != nil {
			return CollectionRun{}, err
		}
		result.Documents = append(result.Documents, item)
	}
	return result, rows.Err()
}

func (s *Service) LatestRun(ctx context.Context, siteCode string) (CollectionRun, error) {
	var id string
	err := s.pool.QueryRow(ctx, `SELECT run.id::text FROM site_rule_collection_runs run JOIN sites site ON site.id=run.site_id
		WHERE site.code=$1 ORDER BY run.created_at DESC LIMIT 1`, strings.ToUpper(strings.TrimSpace(siteCode))).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return CollectionRun{}, ErrNotFound
	}
	if err != nil {
		return CollectionRun{}, err
	}
	return s.GetRun(ctx, id)
}

func (s *Service) Run(ctx context.Context) {
	_, _ = s.pool.Exec(ctx, `UPDATE site_rule_collection_runs SET status='queued',not_before=now(),updated_at=now()
		WHERE status IN ('fetching','analyzing') AND completed_at IS NULL`)
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		if runID, ok := s.claim(ctx); ok {
			s.process(ctx, runID)
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (s *Service) claim(ctx context.Context) (string, bool) {
	var id string
	err := s.pool.QueryRow(ctx, `UPDATE site_rule_collection_runs SET status='fetching',started_at=COALESCE(started_at,now()),updated_at=now(),error_code=NULL,error_detail=NULL
		WHERE id=(SELECT id FROM site_rule_collection_runs WHERE status='queued' AND not_before<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
		RETURNING id::text`).Scan(&id)
	return id, err == nil
}

func (s *Service) process(ctx context.Context, runID string) {
	run, err := s.GetRun(ctx, runID)
	if err != nil {
		return
	}
	var actorID, traceID string
	if err := s.pool.QueryRow(ctx, `SELECT created_by::text,COALESCE(trace_id::text,'') FROM site_rule_collection_runs WHERE id=$1`, runID).Scan(&actorID, &traceID); err != nil {
		s.fail(ctx, runID, "rule_collection_state_invalid", "采集运行的审计身份不可用")
		return
	}
	providerConfigSHA256, err := s.analyzer.ProviderContractFingerprint(ctx, run.ProviderID, operations.ProviderUseCaseRuleAnalysis)
	if err != nil || run.ProviderConfigSHA256 == "" || providerConfigSHA256 != run.ProviderConfigSHA256 {
		s.fail(ctx, runID, "provider_configuration_changed", "模型配置在采集排队后发生变化；为避免原文被发送到不同的数据边界，请重新发起采集")
		return
	}
	runtime, err := s.sites.GetRuntimeSite(ctx, run.SiteCode)
	if err != nil {
		s.fail(ctx, runID, "site_configuration_unavailable", "站点配置不可用")
		return
	}
	cookie := strings.TrimSpace(runtime.Credentials["cookie"])
	if documentsRequireCookie(run.Documents) && cookie == "" {
		s.fail(ctx, runID, "site_cookie_required", "请先在配置中心保存并启用 cookie")
		return
	}
	for _, document := range run.Documents {
		if document.Status == "ready" {
			continue
		}
		lease, err := s.access.AcquireRuleCollection(siteaccess.WithExecution(ctx, "", "", "rule-collection-"+runID), sites.AccessRequest{
			SiteCode: run.SiteCode, Operation: "rule_collection.fetch", Class: sites.AccessGeneral,
		})
		if err != nil {
			var deferred *siteaccess.DeferredError
			if errors.As(err, &deferred) {
				_, _ = s.pool.Exec(ctx, `UPDATE site_rule_collection_runs SET status='queued',not_before=$2,updated_at=now(),error_code='site_access_deferred',error_detail=$3 WHERE id=$1`,
					runID, deferred.NotBefore, bounded(deferred.Error(), 2000))
				return
			}
			s.fail(ctx, runID, "site_access_denied", bounded(err.Error(), 2000))
			return
		}
		_, _ = s.pool.Exec(ctx, `UPDATE site_rule_collection_documents SET status='fetching',updated_at=now(),error_code=NULL,error_detail=NULL WHERE id=$1`, document.ID)
		captured, fetchErr := s.fetch(ctx, runID, document, cookie)
		result := sites.AccessResult{Outcome: "completed", StatusCode: captured.HTTPStatus, ResponseSHA256: captured.ResponseSHA256}
		if fetchErr != nil {
			result.Outcome = "failed"
			if captured.HTTPStatus == http.StatusTooManyRequests {
				result.Outcome = "cooldown"
				result.RetryAfter = captured.RetryAfter
			}
		}
		_ = s.access.Complete(ctx, lease, result)
		if fetchErr != nil {
			_, _ = s.pool.Exec(ctx, `UPDATE site_rule_collection_documents SET status='failed',http_status=NULLIF($2,0),error_code=$3,error_detail=$4,updated_at=now() WHERE id=$1`,
				document.ID, captured.HTTPStatus, captured.ErrorCode, bounded(fetchErr.Error(), 2000))
			s.fail(ctx, runID, captured.ErrorCode, bounded(fetchErr.Error(), 2000))
			return
		}
	}
	if _, err := s.pool.Exec(ctx, `UPDATE site_rule_collection_runs SET status='analyzing',updated_at=now() WHERE id=$1`, runID); err != nil {
		return
	}
	run, err = s.GetRun(ctx, runID)
	if err != nil {
		s.fail(ctx, runID, "rule_collection_state_invalid", "采集结果不可读取")
		return
	}
	combined, documents, err := s.combinedSource(ctx, run)
	if err != nil {
		s.fail(ctx, runID, "rule_collection_source_invalid", bounded(err.Error(), 2000))
		return
	}
	roles := []string{"source", "target"}
	if runtime.Adapter == "mteam_api" {
		roles = []string{"target"}
	} else if runtime.Adapter != "config_only" {
		roles = []string{"source"}
	}
	analysis, err := s.analyzer.AnalyzeRuleText(ctx, operations.RuleAnalysisInput{
		ProviderID: run.ProviderID, ProviderConfigSHA256: run.ProviderConfigSHA256,
		SiteCode: run.SiteCode, DisplayName: runtime.Name, Roles: roles,
		SourceURL: run.Documents[0].URL, SourceScope: combinedScope(run.Documents), SourceComplete: true,
		SourceText: combined, SourceDocuments: documents,
	}, security.Principal{UserID: actorID}, traceID)
	if err != nil {
		code, detail := providerFailure(err)
		s.fail(ctx, runID, code, detail)
		return
	}
	revision, err := s.rules.Import(ctx, []byte(analysis.DraftMarkdown), workflow.Actor{Type: "user", ID: actorID})
	if err != nil {
		s.fail(ctx, runID, "rule_draft_import_failed", bounded(err.Error(), 2000))
		return
	}
	_, err = s.pool.Exec(ctx, `UPDATE site_rule_collection_runs SET status='ready',rule_revision_id=$2,completed_at=now(),updated_at=now(),error_code=NULL,error_detail=NULL WHERE id=$1`, runID, revision.ID)
	if err == nil {
		_, _ = s.pool.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,trace_id,payload)
			VALUES('user',$2,'site_rule.collection_ready','site_rule_collection_run',$1,NULLIF($3,'')::uuid,$4)`, runID, actorID, traceID,
			mustJSON(map[string]any{"site_code": run.SiteCode, "rule_revision_id": revision.ID, "fingerprint": revision.Fingerprint, "source_count": len(documents)}))
	}
}

func (s *Service) fail(ctx context.Context, runID, code, detail string) {
	if strings.TrimSpace(code) == "" {
		code = "rule_collection_failed"
	}
	_, _ = s.pool.Exec(ctx, `UPDATE site_rule_collection_runs SET status='failed',error_code=$2,error_detail=$3,completed_at=now(),updated_at=now() WHERE id=$1`, runID, bounded(code, 128), bounded(detail, 2000))
}

func (s *Service) combinedSource(ctx context.Context, run CollectionRun) (string, []rules.SourceDocument, error) {
	var body strings.Builder
	documents := make([]rules.SourceDocument, 0, len(run.Documents))
	for _, document := range run.Documents {
		var storagePath string
		if err := s.pool.QueryRow(ctx, `SELECT COALESCE(storage_path,'') FROM site_rule_collection_documents WHERE id=$1`, document.ID).Scan(&storagePath); err != nil {
			return "", nil, err
		}
		if storagePath == "" || filepath.IsAbs(storagePath) || strings.Contains(storagePath, "..") {
			return "", nil, fmt.Errorf("source %s storage path is invalid", document.SourceID)
		}
		raw, err := os.ReadFile(filepath.Join(s.dataDir, storagePath))
		if err != nil {
			return "", nil, err
		}
		heading := "## 来源 " + document.SourceID + " · " + document.Scope + "\n\n"
		var rendered strings.Builder
		rendered.WriteString(heading)
		for index, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
			rendered.WriteString(fmt.Sprintf("[%s:L%04d] %s\n", document.SourceID, index+1, line))
		}
		rendered.WriteString("\n")
		if rendered.Len() > MaxAggregateTextBytes-body.Len() {
			return "", nil, fmt.Errorf("combined normalized rule text with evidence references exceeds %d bytes", MaxAggregateTextBytes)
		}
		body.WriteString(rendered.String())
		capturedAt := s.now().UTC()
		if document.CapturedAt != nil {
			capturedAt = document.CapturedAt.UTC()
		}
		documents = append(documents, rules.SourceDocument{
			ID: document.SourceID, URL: document.URL, Scope: document.Scope, AuthMode: document.AuthMode,
			CapturedAt: capturedAt.Format(time.RFC3339), TextSHA256: document.TextSHA256,
			ContentType: document.ContentType, SizeBytes: document.SizeBytes,
		})
	}
	return strings.TrimSpace(body.String()), documents, nil
}

func (s *Service) runByIdempotency(ctx context.Context, actorID, key string) (CollectionRun, error) {
	var id string
	err := s.pool.QueryRow(ctx, `SELECT id::text FROM site_rule_collection_runs WHERE created_by=$1 AND idempotency_key=$2`, actorID, key).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return CollectionRun{}, ErrNotFound
	}
	if err != nil {
		return CollectionRun{}, err
	}
	return s.GetRun(ctx, id)
}

func normalizeSources(values []SourceInput) ([]SourceInput, error) {
	if len(values) < 1 || len(values) > MaxSources {
		return nil, fmt.Errorf("%w: sources must contain 1..%d items", ErrInvalid, MaxSources)
	}
	seen := map[string]bool{}
	result := make([]SourceInput, 0, len(values))
	for index, value := range values {
		value.ID = strings.ToLower(strings.TrimSpace(value.ID))
		if value.ID == "" {
			value.ID = fmt.Sprintf("source-%d", index+1)
		}
		value.URL = strings.TrimSpace(value.URL)
		value.Scope = strings.TrimSpace(value.Scope)
		value.AuthMode = strings.ToLower(strings.TrimSpace(value.AuthMode))
		if value.AuthMode == "" {
			value.AuthMode = SourceAuthSiteCookie
		}
		if !sourceIDPattern.MatchString(value.ID) || seen[value.ID] {
			return nil, fmt.Errorf("%w: source id %q is invalid or duplicated", ErrInvalid, value.ID)
		}
		seen[value.ID] = true
		if len(value.URL) > 2048 || len(value.Scope) < 1 || len(value.Scope) > 500 {
			return nil, fmt.Errorf("%w: source URL or scope is outside supported bounds", ErrInvalid)
		}
		if value.AuthMode != SourceAuthNone && value.AuthMode != SourceAuthSiteCookie {
			return nil, fmt.Errorf("%w: source auth_mode must be none or site_cookie", ErrInvalid)
		}
		parsed, err := validateSourceURL(value.URL)
		if err != nil {
			return nil, err
		}
		value.URL = parsed.String()
		result = append(result, value)
	}
	return result, nil
}

func validateSourceURL(value string) (*url.URL, error) {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
		return nil, fmt.Errorf("%w: source URL must be absolute credential-free HTTPS without a fragment", ErrInvalid)
	}
	for key := range parsed.Query() {
		lower := strings.ToLower(key)
		for _, secret := range []string{"token", "cookie", "passkey", "api_key", "apikey", "secret", "password", "auth"} {
			if strings.Contains(lower, secret) {
				return nil, fmt.Errorf("%w: source URL query contains a secret-like field", ErrInvalid)
			}
		}
	}
	return parsed, nil
}

func sourceHosts(values []SourceInput) []string {
	result := make([]string, 0, len(values))
	seen := map[string]bool{}
	for _, value := range values {
		parsed, _ := url.Parse(value.URL)
		host := strings.ToLower(parsed.Hostname())
		if host != "" && !seen[host] {
			seen[host] = true
			result = append(result, host)
		}
	}
	return result
}

func cookieSourceHosts(values []SourceInput) []string {
	filtered := make([]SourceInput, 0, len(values))
	for _, value := range values {
		if value.AuthMode == SourceAuthSiteCookie {
			filtered = append(filtered, value)
		}
	}
	return sourceHosts(filtered)
}

func sourcesRequireCookie(values []SourceInput) bool {
	for _, value := range values {
		if value.AuthMode == SourceAuthSiteCookie {
			return true
		}
	}
	return false
}

func documentsRequireCookie(values []CollectionDocument) bool {
	for _, value := range values {
		if value.AuthMode == SourceAuthSiteCookie {
			return true
		}
	}
	return false
}

func bodyForSources(values []SourceInput) []byte { body, _ := json.Marshal(values); return body }
func mustJSON(value any) []byte                  { body, _ := json.Marshal(value); return body }
func bounded(value string, maximum int) string {
	value = strings.TrimSpace(value)
	if len(value) > maximum {
		return value[:maximum]
	}
	return value
}
func nullableUUID(value string) any {
	parsed, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil {
		return nil
	}
	return parsed
}
func combinedScope(values []CollectionDocument) string {
	parts := make([]string, 0, len(values))
	for _, value := range values {
		parts = append(parts, value.Scope)
	}
	return strings.Join(parts, "；")
}
func providerFailure(err error) (string, string) {
	if failure, ok := operations.DescribeProviderCallFailure(err); ok {
		return failure.Code, bounded(failure.Detail, 2000)
	}
	return "rule_analysis_failed", bounded(err.Error(), 2000)
}
