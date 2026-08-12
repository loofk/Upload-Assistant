package siteaccess

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/rules"
	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var (
	ErrNotFound   = errors.New("site access policy not found")
	ErrValidation = errors.New("site access policy is invalid")
)

type PolicyInput struct {
	Enabled                   bool `json:"enabled"`
	GeneralMinIntervalSeconds int  `json:"general_min_interval_seconds"`
	GeneralMaxRequestsPerHour int  `json:"general_max_requests_per_hour"`
	SearchMinIntervalSeconds  int  `json:"search_min_interval_seconds"`
	SearchMaxRequestsPerHour  int  `json:"search_max_requests_per_hour"`
	MaxConcurrency            int  `json:"max_concurrency"`
}

type OperatorPolicy struct {
	PolicyInput
	SiteCode  string    `json:"site_code"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

type EffectivePolicy struct {
	SiteCode                  string       `json:"site_code"`
	Enabled                   bool         `json:"enabled"`
	ServiceAccess             string       `json:"service_access"`
	SearchAccess              string       `json:"search_access"`
	GeneralMinIntervalSeconds int          `json:"general_min_interval_seconds"`
	GeneralMaxRequestsPerHour int          `json:"general_max_requests_per_hour"`
	SearchMinIntervalSeconds  int          `json:"search_min_interval_seconds"`
	SearchMaxRequestsPerHour  int          `json:"search_max_requests_per_hour"`
	MaxConcurrency            int          `json:"max_concurrency"`
	RuleRevisionID            string       `json:"rule_revision_id,omitempty"`
	RuleFingerprint           string       `json:"rule_fingerprint,omitempty"`
	RuleSchemaVersion         int          `json:"rule_schema_version"`
	PolicyFingerprint         string       `json:"policy_fingerprint,omitempty"`
	OperatorPolicy            *PolicyInput `json:"operator_policy,omitempty"`
	ActiveRequests            int          `json:"active_requests"`
	GeneralUsedThisHour       int          `json:"general_used_this_hour"`
	SearchUsedThisHour        int          `json:"search_used_this_hour"`
	GeneralCooldownUntil      *time.Time   `json:"general_cooldown_until,omitempty"`
	SearchCooldownUntil       *time.Time   `json:"search_cooldown_until,omitempty"`
	Blockers                  []Blocker    `json:"blockers"`
}

type Blocker struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type DeniedError struct {
	Code    string
	Message string
}

func (e *DeniedError) Error() string { return e.Message }

type DeferredError struct {
	SiteCode     string
	Operation    string
	RequestClass sites.AccessClass
	Reason       string
	NotBefore    time.Time
	ResumeState  map[string]any
}

func (e *DeferredError) Error() string {
	return fmt.Sprintf("site access deferred until %s", e.NotBefore.UTC().Format(time.RFC3339))
}

type executionContext struct {
	JobID     string
	AttemptID string
	Owner     string
}

type executionContextKey struct{}

func WithExecution(ctx context.Context, jobID, attemptID, owner string) context.Context {
	return context.WithValue(ctx, executionContextKey{}, executionContext{
		JobID: strings.TrimSpace(jobID), AttemptID: strings.TrimSpace(attemptID), Owner: strings.TrimSpace(owner),
	})
}

type Store struct {
	pool *pgxpool.Pool
	now  func() time.Time
}

func NewStore(pool *pgxpool.Pool) *Store { return &Store{pool: pool, now: time.Now} }

func (s *Store) UpsertPolicy(ctx context.Context, siteCode string, input PolicyInput, actor workflow.Actor) (EffectivePolicy, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	if err := validateInput(input); err != nil {
		return EffectivePolicy{}, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return EffectivePolicy{}, fmt.Errorf("begin site access policy transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID string
	if err := tx.QueryRow(ctx, `SELECT id::text FROM sites WHERE code=$1 FOR UPDATE`, siteCode).Scan(&siteID); errors.Is(err, pgx.ErrNoRows) {
		return EffectivePolicy{}, ErrNotFound
	} else if err != nil {
		return EffectivePolicy{}, fmt.Errorf("lock site access policy: %w", err)
	}
	actorID := nullableUUID(actor.ID)
	_, err = tx.Exec(ctx, `
		INSERT INTO site_access_policies(
			site_id, enabled, general_min_interval_seconds, general_max_requests_per_hour,
			search_min_interval_seconds, search_max_requests_per_hour, max_concurrency, created_by, updated_by
		) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)
		ON CONFLICT (site_id) DO UPDATE SET
			enabled=EXCLUDED.enabled,
			general_min_interval_seconds=EXCLUDED.general_min_interval_seconds,
			general_max_requests_per_hour=EXCLUDED.general_max_requests_per_hour,
			search_min_interval_seconds=EXCLUDED.search_min_interval_seconds,
			search_max_requests_per_hour=EXCLUDED.search_max_requests_per_hour,
			max_concurrency=EXCLUDED.max_concurrency,
			updated_by=EXCLUDED.updated_by, updated_at=now()`,
		siteID, input.Enabled, input.GeneralMinIntervalSeconds, input.GeneralMaxRequestsPerHour,
		input.SearchMinIntervalSeconds, input.SearchMaxRequestsPerHour, input.MaxConcurrency, actorID)
	if err != nil {
		return EffectivePolicy{}, fmt.Errorf("save site access policy: %w", err)
	}
	payload, _ := json.Marshal(map[string]any{
		"site_code": siteCode, "enabled": input.Enabled,
		"general_min_interval_seconds":  input.GeneralMinIntervalSeconds,
		"general_max_requests_per_hour": input.GeneralMaxRequestsPerHour,
		"search_min_interval_seconds":   input.SearchMinIntervalSeconds,
		"search_max_requests_per_hour":  input.SearchMaxRequestsPerHour,
		"max_concurrency":               input.MaxConcurrency,
	})
	if _, err := tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,payload)
		VALUES ($1,NULLIF($2,''),'site_access.policy_upsert','site',$3,$4)`, actor.Type, actor.ID, siteID, payload); err != nil {
		return EffectivePolicy{}, fmt.Errorf("audit site access policy: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return EffectivePolicy{}, fmt.Errorf("commit site access policy: %w", err)
	}
	return s.GetPolicy(ctx, siteCode)
}

func (s *Store) GetPolicy(ctx context.Context, siteCode string) (EffectivePolicy, error) {
	now := s.now().UTC()
	effective, siteID, err := s.loadEffective(ctx, nil, strings.ToUpper(strings.TrimSpace(siteCode)))
	if err != nil {
		return EffectivePolicy{}, err
	}
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM site_access_leases WHERE site_id=$1 AND completed_at IS NULL AND expires_at>$2`, siteID, now).Scan(&effective.ActiveRequests); err != nil {
		return EffectivePolicy{}, fmt.Errorf("count active site access requests: %w", err)
	}
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FILTER (WHERE request_class='general'), count(*) FILTER (WHERE request_class='search')
		FROM site_access_leases WHERE site_id=$1 AND acquired_at>$2`, siteID, now.Add(-time.Hour)).Scan(&effective.GeneralUsedThisHour, &effective.SearchUsedThisHour); err != nil {
		return EffectivePolicy{}, fmt.Errorf("count site access quota: %w", err)
	}
	rows, err := s.pool.Query(ctx, `SELECT request_class, until_at FROM site_access_cooldowns WHERE site_id=$1 AND until_at>$2`, siteID, now)
	if err != nil {
		return EffectivePolicy{}, fmt.Errorf("read site access cooldowns: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var class string
		var until time.Time
		if err := rows.Scan(&class, &until); err != nil {
			return EffectivePolicy{}, err
		}
		if class == string(sites.AccessSearch) {
			effective.SearchCooldownUntil = &until
		} else {
			effective.GeneralCooldownUntil = &until
		}
	}
	return effective, rows.Err()
}

func (s *Store) Acquire(ctx context.Context, request sites.AccessRequest) (sites.AccessLease, error) {
	return s.acquire(ctx, request, false)
}

// AcquireRuleCollection is the narrow bootstrap exception needed to read the
// exact rule URLs before a site has an active rule. It uses the operator's
// ordinary interval/quota/concurrency counters and never authorizes any other
// tracker operation.
func (s *Store) AcquireRuleCollection(ctx context.Context, request sites.AccessRequest) (sites.AccessLease, error) {
	request.Class = sites.AccessGeneral
	return s.acquire(ctx, request, true)
}

func (s *Store) acquire(ctx context.Context, request sites.AccessRequest, ruleCollection bool) (sites.AccessLease, error) {
	request.SiteCode = strings.ToUpper(strings.TrimSpace(request.SiteCode))
	request.Operation = strings.TrimSpace(request.Operation)
	if request.SiteCode == "" || request.Operation == "" || (request.Class != sites.AccessGeneral && request.Class != sites.AccessSearch) {
		return sites.AccessLease{}, fmt.Errorf("%w: site, operation, and request class are required", ErrValidation)
	}
	now := s.now().UTC()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return sites.AccessLease{}, fmt.Errorf("begin site access acquisition: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var effective EffectivePolicy
	var siteID string
	if ruleCollection {
		effective, siteID, err = s.loadCollectionEffective(ctx, tx, request.SiteCode)
	} else {
		effective, siteID, err = s.loadEffective(ctx, tx, request.SiteCode)
	}
	if err != nil {
		return sites.AccessLease{}, err
	}
	var blocker *Blocker
	if ruleCollection {
		if len(effective.Blockers) > 0 {
			blocker = &effective.Blockers[0]
		}
	} else {
		blocker = accessBlocker(effective, request.Class)
	}
	if blocker != nil {
		_ = auditDecision(ctx, tx, siteID, "site_access.denied", request, map[string]any{
			"code": blocker.Code, "message": blocker.Message, "rule_fingerprint": effective.RuleFingerprint,
			"purpose": map[bool]string{true: "rule_collection", false: "runtime"}[ruleCollection],
		})
		if err := tx.Commit(ctx); err != nil {
			return sites.AccessLease{}, err
		}
		return sites.AccessLease{}, &DeniedError{Code: blocker.Code, Message: blocker.Message}
	}
	interval, quota := effective.GeneralMinIntervalSeconds, effective.GeneralMaxRequestsPerHour
	if request.Class == sites.AccessSearch {
		interval, quota = effective.SearchMinIntervalSeconds, effective.SearchMaxRequestsPerHour
	}
	notBefore, reason, err := nextPermit(ctx, tx, siteID, request.Class, now, time.Duration(interval)*time.Second, quota, effective.MaxConcurrency)
	if err != nil {
		return sites.AccessLease{}, err
	}
	if notBefore.After(now) {
		_ = auditDecision(ctx, tx, siteID, "site_access.deferred", request, map[string]any{
			"reason": reason, "not_before": notBefore, "policy_fingerprint": effective.PolicyFingerprint,
			"purpose": map[bool]string{true: "rule_collection", false: "runtime"}[ruleCollection],
		})
		if err := tx.Commit(ctx); err != nil {
			return sites.AccessLease{}, err
		}
		return sites.AccessLease{}, &DeferredError{
			SiteCode: request.SiteCode, Operation: request.Operation, RequestClass: request.Class,
			Reason: reason, NotBefore: notBefore,
		}
	}
	execution, _ := ctx.Value(executionContextKey{}).(executionContext)
	owner := execution.Owner
	if owner == "" {
		owner = "site-access"
	}
	var lease sites.AccessLease
	err = tx.QueryRow(ctx, `INSERT INTO site_access_leases(
		site_id,operation,request_class,job_id,attempt_id,owner,policy_fingerprint,expires_at
	) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id::text`, siteID, request.Operation, request.Class,
		nullableUUID(execution.JobID), nullableUUID(execution.AttemptID), owner, effective.PolicyFingerprint, now.Add(2*time.Minute)).Scan(&lease.ID)
	if err != nil {
		return sites.AccessLease{}, fmt.Errorf("create site access lease: %w", err)
	}
	lease.SiteCode, lease.Operation, lease.Class, lease.PolicyFingerprint = request.SiteCode, request.Operation, request.Class, effective.PolicyFingerprint
	if err := auditDecision(ctx, tx, siteID, "site_access.acquired", request, map[string]any{
		"lease_id": lease.ID, "job_id": execution.JobID, "attempt_id": execution.AttemptID,
		"policy_fingerprint": effective.PolicyFingerprint,
		"purpose":            map[bool]string{true: "rule_collection", false: "runtime"}[ruleCollection],
	}); err != nil {
		return sites.AccessLease{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return sites.AccessLease{}, fmt.Errorf("commit site access acquisition: %w", err)
	}
	return lease, nil
}

// GetRuleCollectionPolicy exposes whether the operator-side ordinary policy is
// ready for the collection-only bootstrap path. Active rule numeric limits are
// merged when available, but service_access does not authorize this purpose.
func (s *Store) GetRuleCollectionPolicy(ctx context.Context, siteCode string) (EffectivePolicy, error) {
	effective, _, err := s.loadCollectionEffective(ctx, nil, strings.ToUpper(strings.TrimSpace(siteCode)))
	return effective, err
}

func (s *Store) Complete(ctx context.Context, lease sites.AccessLease, result sites.AccessResult) error {
	if _, err := uuid.Parse(lease.ID); err != nil {
		return fmt.Errorf("%w: lease id is invalid", ErrValidation)
	}
	outcome := strings.TrimSpace(result.Outcome)
	if outcome != "completed" && outcome != "failed" && outcome != "cooldown" {
		outcome = "completed"
	}
	now := s.now().UTC()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID, siteCode, operation, requestClass string
	err = tx.QueryRow(ctx, `UPDATE site_access_leases lease SET completed_at=$2,outcome=$3,status_code=NULLIF($4,0),response_sha256=NULLIF($5,'')
		FROM sites site WHERE lease.id=$1 AND lease.completed_at IS NULL AND site.id=lease.site_id
		RETURNING site.id::text,site.code,lease.operation,lease.request_class`, lease.ID, now, outcome, result.StatusCode, strings.ToLower(result.ResponseSHA256)).Scan(&siteID, &siteCode, &operation, &requestClass)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("complete site access lease: %w", err)
	}
	if result.StatusCode == 429 || result.RetryAfter > 0 || outcome == "cooldown" {
		delay := result.RetryAfter
		if delay <= 0 {
			delay = 15 * time.Minute
		}
		if delay > 7*24*time.Hour {
			delay = 7 * 24 * time.Hour
		}
		_, err = tx.Exec(ctx, `INSERT INTO site_access_cooldowns(site_id,request_class,until_at,reason)
			VALUES ($1,$2,$3,'remote_rate_limit') ON CONFLICT(site_id,request_class) DO UPDATE
			SET until_at=GREATEST(site_access_cooldowns.until_at,EXCLUDED.until_at),reason=EXCLUDED.reason,updated_at=now()`, siteID, requestClass, now.Add(delay))
		if err != nil {
			return fmt.Errorf("save site access cooldown: %w", err)
		}
	}
	payload, _ := json.Marshal(map[string]any{
		"site_code": siteCode, "operation": operation, "request_class": requestClass,
		"lease_id": lease.ID, "outcome": outcome, "status_code": result.StatusCode,
		"response_sha256": strings.ToLower(result.ResponseSHA256),
	})
	if _, err := tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,payload)
		VALUES ('worker',NULL,'site_access.completed','site',$1,$2)`, siteID, payload); err != nil {
		return fmt.Errorf("audit completed site access: %w", err)
	}
	return tx.Commit(ctx)
}

func (s *Store) loadEffective(ctx context.Context, tx pgx.Tx, siteCode string) (EffectivePolicy, string, error) {
	query := `SELECT site.id::text,site.enabled,COALESCE(rule.id::text,''),COALESCE(rule.fingerprint,''),COALESCE(rule.parsed_policy,'{}'::jsonb),
		policy.enabled,policy.general_min_interval_seconds,policy.general_max_requests_per_hour,
		policy.search_min_interval_seconds,policy.search_max_requests_per_hour,policy.max_concurrency
		FROM sites site
		LEFT JOIN site_rule_revisions rule ON rule.id=site.active_rule_revision_id AND rule.status='approved'
		LEFT JOIN site_access_policies policy ON policy.site_id=site.id WHERE site.code=$1`
	if tx != nil {
		query += ` FOR UPDATE OF site`
	}
	var row pgx.Row
	if tx != nil {
		row = tx.QueryRow(ctx, query, siteCode)
	} else {
		row = s.pool.QueryRow(ctx, query, siteCode)
	}
	var siteID, revisionID, fingerprint string
	var siteEnabled bool
	var raw json.RawMessage
	var enabled sql.NullBool
	var generalInterval, generalQuota, searchInterval, searchQuota, concurrency sql.NullInt64
	if err := row.Scan(&siteID, &siteEnabled, &revisionID, &fingerprint, &raw, &enabled,
		&generalInterval, &generalQuota, &searchInterval, &searchQuota, &concurrency); errors.Is(err, pgx.ErrNoRows) {
		return EffectivePolicy{}, "", ErrNotFound
	} else if err != nil {
		return EffectivePolicy{}, "", fmt.Errorf("load effective site access policy: %w", err)
	}
	effective := EffectivePolicy{SiteCode: siteCode, RuleRevisionID: revisionID, RuleFingerprint: fingerprint, Blockers: []Blocker{}}
	if !siteEnabled {
		effective.Blockers = append(effective.Blockers, Blocker{Code: "site_disabled", Message: "站点配置已禁用"})
	}
	if !enabled.Valid {
		effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_policy_required", Message: "尚未配置人工站点访问策略"})
	} else {
		effective.Enabled = enabled.Bool
		effective.GeneralMinIntervalSeconds = int(generalInterval.Int64)
		effective.GeneralMaxRequestsPerHour = int(generalQuota.Int64)
		effective.SearchMinIntervalSeconds = int(searchInterval.Int64)
		effective.SearchMaxRequestsPerHour = int(searchQuota.Int64)
		effective.MaxConcurrency = int(concurrency.Int64)
		effective.OperatorPolicy = &PolicyInput{
			Enabled:                   enabled.Bool,
			GeneralMinIntervalSeconds: int(generalInterval.Int64),
			GeneralMaxRequestsPerHour: int(generalQuota.Int64),
			SearchMinIntervalSeconds:  int(searchInterval.Int64),
			SearchMaxRequestsPerHour:  int(searchQuota.Int64),
			MaxConcurrency:            int(concurrency.Int64),
		}
		if !enabled.Bool {
			effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_disabled", Message: "人工站点访问策略已关闭"})
		}
	}
	if revisionID == "" {
		effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_rule_required", Message: "没有已激活的审批规则"})
	} else {
		policy, err := rules.ParsePolicy(raw)
		if err != nil {
			effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_rule_invalid", Message: "活动规则访问策略不可用"})
		} else {
			effective.RuleSchemaVersion = policy.SchemaVersion
			effective.ServiceAccess = policy.Access.ServiceAccess
			effective.SearchAccess = policy.Access.SearchAccess
			if policy.SchemaVersion < 2 {
				effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_rule_v2_required", Message: "活动规则是 v1，不能授权服务网络访问"})
			} else {
				effective.GeneralMinIntervalSeconds = stricterMinimum(effective.GeneralMinIntervalSeconds, policy.Access.GeneralMinIntervalSeconds)
				effective.GeneralMaxRequestsPerHour = stricterMaximum(effective.GeneralMaxRequestsPerHour, policy.Access.GeneralMaxRequestsPerHour)
				effective.SearchMinIntervalSeconds = stricterMinimum(effective.SearchMinIntervalSeconds, policy.Access.SearchMinIntervalSeconds)
				effective.SearchMaxRequestsPerHour = stricterMaximum(effective.SearchMaxRequestsPerHour, policy.Access.SearchMaxRequestsPerHour)
				effective.MaxConcurrency = stricterMaximum(effective.MaxConcurrency, policy.Access.MaxConcurrency)
				if effective.ServiceAccess != "allowed" {
					effective.Blockers = append(effective.Blockers, Blocker{Code: "site_service_access_forbidden", Message: "活动规则未明确允许服务访问该站点"})
				}
				if effective.SearchAccess != "allowed" {
					effective.Blockers = append(effective.Blockers, Blocker{Code: "site_search_access_forbidden", Message: "活动规则未明确允许服务搜索该站点"})
				}
			}
		}
	}
	effective.PolicyFingerprint = effectiveFingerprint(effective)
	return effective, siteID, nil
}

func (s *Store) loadCollectionEffective(ctx context.Context, tx pgx.Tx, siteCode string) (EffectivePolicy, string, error) {
	query := `SELECT site.id::text,site.enabled,COALESCE(rule.id::text,''),COALESCE(rule.fingerprint,''),COALESCE(rule.parsed_policy,'{}'::jsonb),
		policy.enabled,policy.general_min_interval_seconds,policy.general_max_requests_per_hour,
		policy.search_min_interval_seconds,policy.search_max_requests_per_hour,policy.max_concurrency
		FROM sites site
		LEFT JOIN site_rule_revisions rule ON rule.id=site.active_rule_revision_id AND rule.status='approved'
		LEFT JOIN site_access_policies policy ON policy.site_id=site.id WHERE site.code=$1`
	if tx != nil {
		query += ` FOR UPDATE OF site`
	}
	var row pgx.Row
	if tx != nil {
		row = tx.QueryRow(ctx, query, siteCode)
	} else {
		row = s.pool.QueryRow(ctx, query, siteCode)
	}
	var siteID, revisionID, fingerprint string
	var siteEnabled bool
	var raw json.RawMessage
	var enabled sql.NullBool
	var generalInterval, generalQuota, searchInterval, searchQuota, concurrency sql.NullInt64
	if err := row.Scan(&siteID, &siteEnabled, &revisionID, &fingerprint, &raw, &enabled,
		&generalInterval, &generalQuota, &searchInterval, &searchQuota, &concurrency); errors.Is(err, pgx.ErrNoRows) {
		return EffectivePolicy{}, "", ErrNotFound
	} else if err != nil {
		return EffectivePolicy{}, "", fmt.Errorf("load rule collection access policy: %w", err)
	}
	effective := EffectivePolicy{SiteCode: siteCode, RuleRevisionID: revisionID, RuleFingerprint: fingerprint, Blockers: []Blocker{}}
	if !siteEnabled {
		effective.Blockers = append(effective.Blockers, Blocker{Code: "site_disabled", Message: "站点配置已禁用"})
	}
	if !enabled.Valid {
		effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_policy_required", Message: "尚未配置人工站点访问策略"})
	} else {
		effective.Enabled = enabled.Bool
		effective.GeneralMinIntervalSeconds = int(generalInterval.Int64)
		effective.GeneralMaxRequestsPerHour = int(generalQuota.Int64)
		effective.SearchMinIntervalSeconds = int(searchInterval.Int64)
		effective.SearchMaxRequestsPerHour = int(searchQuota.Int64)
		effective.MaxConcurrency = int(concurrency.Int64)
		effective.OperatorPolicy = &PolicyInput{
			Enabled: enabled.Bool, GeneralMinIntervalSeconds: int(generalInterval.Int64),
			GeneralMaxRequestsPerHour: int(generalQuota.Int64), SearchMinIntervalSeconds: int(searchInterval.Int64),
			SearchMaxRequestsPerHour: int(searchQuota.Int64), MaxConcurrency: int(concurrency.Int64),
		}
		if !enabled.Bool {
			effective.Blockers = append(effective.Blockers, Blocker{Code: "site_access_disabled", Message: "人工站点访问策略已关闭"})
		}
	}
	if revisionID != "" {
		if policy, err := rules.ParsePolicy(raw); err == nil && policy.SchemaVersion >= 2 {
			effective.RuleSchemaVersion = policy.SchemaVersion
			effective.GeneralMinIntervalSeconds = stricterMinimum(effective.GeneralMinIntervalSeconds, policy.Access.GeneralMinIntervalSeconds)
			effective.GeneralMaxRequestsPerHour = stricterMaximum(effective.GeneralMaxRequestsPerHour, policy.Access.GeneralMaxRequestsPerHour)
			effective.MaxConcurrency = stricterMaximum(effective.MaxConcurrency, policy.Access.MaxConcurrency)
		}
	}
	effective.PolicyFingerprint = effectiveFingerprint(effective)
	return effective, siteID, nil
}

func nextPermit(ctx context.Context, tx pgx.Tx, siteID string, class sites.AccessClass, now time.Time, interval time.Duration, quota, maxConcurrency int) (time.Time, string, error) {
	notBefore, reason := now, "ready"
	var active int
	var earliestExpiry sql.NullTime
	if err := tx.QueryRow(ctx, `SELECT count(*),min(expires_at) FROM site_access_leases WHERE site_id=$1 AND completed_at IS NULL AND expires_at>$2`, siteID, now).Scan(&active, &earliestExpiry); err != nil {
		return time.Time{}, "", err
	}
	if active >= maxConcurrency && earliestExpiry.Valid && earliestExpiry.Time.After(notBefore) {
		notBefore, reason = earliestExpiry.Time, "concurrency"
	}
	var last sql.NullTime
	if err := tx.QueryRow(ctx, `SELECT max(acquired_at) FROM site_access_leases WHERE site_id=$1 AND request_class=$2`, siteID, class).Scan(&last); err != nil {
		return time.Time{}, "", err
	}
	if last.Valid && last.Time.Add(interval).After(notBefore) {
		notBefore, reason = last.Time.Add(interval), "minimum_interval"
	}
	var used int
	var oldest sql.NullTime
	if err := tx.QueryRow(ctx, `SELECT count(*),min(acquired_at) FROM site_access_leases WHERE site_id=$1 AND request_class=$2 AND acquired_at>$3`, siteID, class, now.Add(-time.Hour)).Scan(&used, &oldest); err != nil {
		return time.Time{}, "", err
	}
	if used >= quota && oldest.Valid && oldest.Time.Add(time.Hour).After(notBefore) {
		notBefore, reason = oldest.Time.Add(time.Hour), "hourly_quota"
	}
	var cooldown sql.NullTime
	if err := tx.QueryRow(ctx, `SELECT until_at FROM site_access_cooldowns WHERE site_id=$1 AND request_class=$2 AND until_at>$3`, siteID, class, now).Scan(&cooldown); err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return time.Time{}, "", err
	}
	if cooldown.Valid && cooldown.Time.After(notBefore) {
		notBefore, reason = cooldown.Time, "remote_cooldown"
	}
	return notBefore, reason, nil
}

func accessBlocker(policy EffectivePolicy, class sites.AccessClass) *Blocker {
	if len(policy.Blockers) > 0 {
		return &policy.Blockers[0]
	}
	if policy.ServiceAccess != "allowed" {
		return &Blocker{Code: "site_service_access_forbidden", Message: "活动规则未允许服务访问该站点"}
	}
	if class == sites.AccessSearch && policy.SearchAccess != "allowed" {
		return &Blocker{Code: "site_search_access_forbidden", Message: "活动规则未允许服务搜索该站点"}
	}
	return nil
}

func validateInput(input PolicyInput) error {
	if input.GeneralMinIntervalSeconds < 1 || input.GeneralMinIntervalSeconds > 86400 ||
		input.SearchMinIntervalSeconds < 1 || input.SearchMinIntervalSeconds > 86400 ||
		input.GeneralMaxRequestsPerHour < 1 || input.GeneralMaxRequestsPerHour > 3600 ||
		input.SearchMaxRequestsPerHour < 1 || input.SearchMaxRequestsPerHour > 3600 ||
		input.MaxConcurrency < 1 || input.MaxConcurrency > 4 {
		return fmt.Errorf("%w: intervals must be 1..86400, hourly quotas 1..3600, and concurrency 1..4", ErrValidation)
	}
	return nil
}

func stricterMinimum(operator, rule int) int {
	if rule > operator {
		return rule
	}
	return operator
}

func stricterMaximum(operator, rule int) int {
	if rule > 0 && rule < operator {
		return rule
	}
	return operator
}

func effectiveFingerprint(policy EffectivePolicy) string {
	body, _ := json.Marshal(struct {
		SiteCode                  string `json:"site_code"`
		Enabled                   bool   `json:"enabled"`
		RuleFingerprint           string `json:"rule_fingerprint"`
		ServiceAccess             string `json:"service_access"`
		SearchAccess              string `json:"search_access"`
		GeneralMinIntervalSeconds int    `json:"general_min_interval_seconds"`
		GeneralMaxRequestsPerHour int    `json:"general_max_requests_per_hour"`
		SearchMinIntervalSeconds  int    `json:"search_min_interval_seconds"`
		SearchMaxRequestsPerHour  int    `json:"search_max_requests_per_hour"`
		MaxConcurrency            int    `json:"max_concurrency"`
	}{policy.SiteCode, policy.Enabled, policy.RuleFingerprint, policy.ServiceAccess, policy.SearchAccess,
		policy.GeneralMinIntervalSeconds, policy.GeneralMaxRequestsPerHour,
		policy.SearchMinIntervalSeconds, policy.SearchMaxRequestsPerHour, policy.MaxConcurrency})
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}

func auditDecision(ctx context.Context, tx pgx.Tx, siteID, action string, request sites.AccessRequest, extra map[string]any) error {
	payload := map[string]any{"site_code": request.SiteCode, "operation": request.Operation, "request_class": request.Class}
	for key, value := range extra {
		payload[key] = value
	}
	body, _ := json.Marshal(payload)
	_, err := tx.Exec(ctx, `INSERT INTO audit_events(actor_type,actor_id,action,resource_type,resource_id,payload)
		VALUES ('system',NULL,$1,'site',$2,$3)`, action, siteID, body)
	return err
}

func nullableUUID(value string) any {
	parsed, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil {
		return nil
	}
	return parsed
}
