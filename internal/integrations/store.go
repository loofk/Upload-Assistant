package integrations

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

var downloaderAdapters = []string{"qbittorrent", "rtorrent", "deluge", "transmission"}

type Store struct {
	pool    *pgxpool.Pool
	secrets *security.SecretStore
}

func NewStore(pool *pgxpool.Pool, secrets *security.SecretStore) *Store {
	return &Store{pool: pool, secrets: secrets}
}

func (s *Store) PutSiteCredential(ctx context.Context, siteCode, name, value string, actor workflow.Actor) (SiteCredential, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	name = strings.ToLower(strings.TrimSpace(name))
	if err := validateResourceName("credential", name); err != nil {
		return SiteCredential{}, err
	}
	if strings.TrimSpace(value) == "" {
		return SiteCredential{}, fmt.Errorf("%w: credential value is required", ErrValidation)
	}
	var siteID string
	if err := s.pool.QueryRow(ctx, "SELECT id::text FROM sites WHERE code = $1", siteCode).Scan(&siteID); errors.Is(err, pgx.ErrNoRows) {
		return SiteCredential{}, ErrNotFound
	} else if err != nil {
		return SiteCredential{}, fmt.Errorf("find credential site: %w", err)
	}
	purpose := "sites." + siteCode + "." + name
	secretID, err := s.secrets.Put(ctx, purpose, []byte(value), actor.ID)
	if err != nil {
		return SiteCredential{}, err
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return SiteCredential{}, fmt.Errorf("begin site credential transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var credential SiteCredential
	err = tx.QueryRow(ctx, `
		INSERT INTO site_credentials(site_id, name, secret_id, enabled)
		VALUES ($1, $2, $3, true)
		ON CONFLICT (site_id, name) DO UPDATE
		SET secret_id = EXCLUDED.secret_id, enabled = true, updated_at = now()
		RETURNING id::text, enabled, created_at, updated_at`, siteID, name, secretID).Scan(
		&credential.ID, &credential.Enabled, &credential.CreatedAt, &credential.UpdatedAt,
	)
	if err != nil {
		return SiteCredential{}, fmt.Errorf("upsert site credential: %w", err)
	}
	credential.SiteCode = siteCode
	credential.Name = name
	if err := audit(ctx, tx, actor, "site_credential.put", "site_credential", credential.ID, map[string]any{
		"site_code": siteCode, "name": name, "secret_id": secretID,
	}); err != nil {
		return SiteCredential{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return SiteCredential{}, fmt.Errorf("commit site credential: %w", err)
	}
	return credential, nil
}

func (s *Store) ListSiteCredentials(ctx context.Context, siteCode string) ([]SiteCredential, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	rows, err := s.pool.Query(ctx, `
		SELECT sc.id::text, s.code, sc.name, sc.enabled, sc.created_at, sc.updated_at
		FROM site_credentials sc JOIN sites s ON s.id = sc.site_id
		WHERE s.code = $1 ORDER BY sc.name`, siteCode)
	if err != nil {
		return nil, fmt.Errorf("list site credentials: %w", err)
	}
	defer rows.Close()
	result := make([]SiteCredential, 0)
	for rows.Next() {
		var item SiteCredential
		if err := rows.Scan(&item.ID, &item.SiteCode, &item.Name, &item.Enabled, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan site credential: %w", err)
		}
		result = append(result, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate site credentials: %w", err)
	}
	return result, nil
}

func (s *Store) DisableSiteCredential(ctx context.Context, siteCode, name string, actor workflow.Actor) (SiteCredential, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	name = strings.ToLower(strings.TrimSpace(name))
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return SiteCredential{}, fmt.Errorf("begin disable site credential: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var credential SiteCredential
	err = tx.QueryRow(ctx, `
		UPDATE site_credentials sc SET enabled = false, updated_at = now()
		FROM sites s WHERE sc.site_id = s.id AND s.code = $1 AND sc.name = $2
		RETURNING sc.id::text, s.code, sc.name, sc.enabled, sc.created_at, sc.updated_at`, siteCode, name).Scan(
		&credential.ID, &credential.SiteCode, &credential.Name, &credential.Enabled,
		&credential.CreatedAt, &credential.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return SiteCredential{}, ErrNotFound
	}
	if err != nil {
		return SiteCredential{}, fmt.Errorf("disable site credential: %w", err)
	}
	if err := audit(ctx, tx, actor, "site_credential.disable", "site_credential", credential.ID, map[string]any{
		"site_code": siteCode, "name": name,
	}); err != nil {
		return SiteCredential{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return SiteCredential{}, fmt.Errorf("commit disabled site credential: %w", err)
	}
	return credential, nil
}

func (s *Store) GetRuntimeSite(ctx context.Context, siteCode string) (RuntimeSite, error) {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	var runtime RuntimeSite
	var enabled bool
	err := s.pool.QueryRow(ctx, `
		SELECT code, name, adapter, enabled, config
		FROM sites WHERE code = $1`, siteCode).Scan(
		&runtime.Code, &runtime.Name, &runtime.Adapter, &enabled, &runtime.Config,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeSite{}, ErrNotFound
	}
	if err != nil {
		return RuntimeSite{}, fmt.Errorf("load runtime site: %w", err)
	}
	if !enabled {
		return RuntimeSite{}, fmt.Errorf("%w: site is disabled", ErrValidation)
	}

	rows, err := s.pool.Query(ctx, `
		SELECT sc.name, sc.secret_id::text
		FROM site_credentials sc
		JOIN sites s ON s.id = sc.site_id
		WHERE s.code = $1 AND sc.enabled = true
		ORDER BY sc.name`, siteCode)
	if err != nil {
		return RuntimeSite{}, fmt.Errorf("load runtime site credentials: %w", err)
	}
	defer rows.Close()
	runtime.Credentials = map[string]string{}
	for rows.Next() {
		var name, secretID string
		if err := rows.Scan(&name, &secretID); err != nil {
			return RuntimeSite{}, fmt.Errorf("scan runtime site credential: %w", err)
		}
		plaintext, err := s.secrets.Get(ctx, secretID, "sites."+siteCode+"."+name)
		if err != nil {
			return RuntimeSite{}, fmt.Errorf("decrypt runtime site credential %s: %w", name, err)
		}
		runtime.Credentials[name] = string(plaintext)
	}
	if err := rows.Err(); err != nil {
		return RuntimeSite{}, fmt.Errorf("iterate runtime site credentials: %w", err)
	}
	return runtime, nil
}

func (s *Store) UpsertDownloader(ctx context.Context, name string, input DownloaderInput, actor workflow.Actor) (Downloader, error) {
	name = strings.TrimSpace(name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	if err := validateResourceName("downloader", name); err != nil {
		return Downloader{}, err
	}
	if !slices.Contains(downloaderAdapters, input.Adapter) {
		return Downloader{}, fmt.Errorf("%w: unsupported downloader adapter %q", ErrValidation, input.Adapter)
	}
	config, err := validateEndpointConfig(input.Config)
	if err != nil {
		return Downloader{}, err
	}
	if input.PathMappings != nil {
		if err := validateMappings(input.PathMappings); err != nil {
			return Downloader{}, err
		}
	}
	credentialFields, credentialPayload, err := validateCredentials(input.Credentials)
	if err != nil {
		return Downloader{}, err
	}
	var newSecretID any
	if credentialPayload != nil {
		secretID, err := s.secrets.Put(ctx, "downloaders."+name+".credentials", credentialPayload, actor.ID)
		if err != nil {
			return Downloader{}, err
		}
		newSecretID = secretID
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Downloader{}, fmt.Errorf("begin downloader transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var downloader Downloader
	var secretID string
	var healthChecked pgtype.Timestamptz
	err = tx.QueryRow(ctx, `
		INSERT INTO downloaders(name, adapter, enabled, config, secret_id)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (name) DO UPDATE SET
			adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled, config = EXCLUDED.config,
			secret_id = COALESCE(EXCLUDED.secret_id, downloaders.secret_id), updated_at = now()
		RETURNING id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''),
		          health_status, last_health_check_at, created_at, updated_at`,
		name, input.Adapter, enabled, config, newSecretID,
	).Scan(
		&downloader.ID, &downloader.Name, &downloader.Adapter, &downloader.Enabled,
		&downloader.Config, &secretID, &downloader.HealthStatus, &healthChecked,
		&downloader.CreatedAt, &downloader.UpdatedAt,
	)
	if err != nil {
		return Downloader{}, fmt.Errorf("upsert downloader: %w", err)
	}
	downloader.LastHealthCheck = timePointer(healthChecked)
	if input.PathMappings != nil {
		if _, err := tx.Exec(ctx, "DELETE FROM downloader_path_mappings WHERE downloader_id = $1", downloader.ID); err != nil {
			return Downloader{}, fmt.Errorf("replace downloader path mappings: %w", err)
		}
		for _, mapping := range input.PathMappings {
			if _, err := tx.Exec(ctx, `
				INSERT INTO downloader_path_mappings(downloader_id, remote_path, local_path, priority)
				VALUES ($1, $2, $3, $4)`, downloader.ID, mapping.RemotePath, mapping.LocalPath, mapping.Priority); err != nil {
				return Downloader{}, fmt.Errorf("insert downloader path mapping: %w", err)
			}
		}
	}
	if err := audit(ctx, tx, actor, "downloader.upsert", "downloader", downloader.ID, map[string]any{
		"name": name, "adapter": input.Adapter, "enabled": enabled,
		"credential_fields": credentialFields, "path_mapping_count": len(input.PathMappings),
	}); err != nil {
		return Downloader{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return Downloader{}, fmt.Errorf("commit downloader: %w", err)
	}
	downloader.PathMappings, err = s.loadMappings(ctx, downloader.ID)
	if err != nil {
		return Downloader{}, err
	}
	if newSecretID == nil {
		downloader.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "downloaders."+name+".credentials")
	} else {
		downloader.CredentialFields = credentialFields
	}
	return downloader, err
}

func (s *Store) ListDownloaders(ctx context.Context) ([]Downloader, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''),
		       health_status, last_health_check_at, created_at, updated_at
		FROM downloaders ORDER BY name`)
	if err != nil {
		return nil, fmt.Errorf("list downloaders: %w", err)
	}
	defer rows.Close()
	result := make([]Downloader, 0)
	for rows.Next() {
		var item Downloader
		var secretID string
		var healthChecked pgtype.Timestamptz
		if err := rows.Scan(
			&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
			&item.HealthStatus, &healthChecked, &item.CreatedAt, &item.UpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan downloader: %w", err)
		}
		item.LastHealthCheck = timePointer(healthChecked)
		item.PathMappings, err = s.loadMappings(ctx, item.ID)
		if err != nil {
			return nil, err
		}
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "downloaders."+item.Name+".credentials")
		if err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate downloaders: %w", err)
	}
	return result, nil
}

func (s *Store) GetRuntimeDownloader(ctx context.Context, name string) (RuntimeDownloader, error) {
	var runtime RuntimeDownloader
	var secretID string
	var healthChecked pgtype.Timestamptz
	err := s.pool.QueryRow(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''),
		       health_status, last_health_check_at, created_at, updated_at
		FROM downloaders WHERE name = $1`, strings.TrimSpace(name)).Scan(
		&runtime.ID, &runtime.Name, &runtime.Adapter, &runtime.Enabled, &runtime.Config,
		&secretID, &runtime.HealthStatus, &healthChecked, &runtime.CreatedAt, &runtime.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeDownloader{}, ErrNotFound
	}
	if err != nil {
		return RuntimeDownloader{}, fmt.Errorf("load runtime downloader: %w", err)
	}
	if !runtime.Enabled {
		return RuntimeDownloader{}, fmt.Errorf("%w: downloader is disabled", ErrValidation)
	}
	runtime.LastHealthCheck = timePointer(healthChecked)
	if err := json.Unmarshal(runtime.Config, &runtime.EndpointConfig); err != nil {
		return RuntimeDownloader{}, fmt.Errorf("decode runtime downloader config: %w", err)
	}
	runtime.PathMappings, err = s.loadMappings(ctx, runtime.ID)
	if err != nil {
		return RuntimeDownloader{}, err
	}
	if secretID != "" {
		plaintext, err := s.secrets.Get(ctx, secretID, "downloaders."+runtime.Name+".credentials")
		if err != nil {
			return RuntimeDownloader{}, err
		}
		if err := json.Unmarshal(plaintext, &runtime.Credentials); err != nil {
			return RuntimeDownloader{}, fmt.Errorf("decode runtime downloader credentials: %w", err)
		}
	} else {
		runtime.Credentials = map[string]string{}
	}
	runtime.CredentialFields = make([]string, 0, len(runtime.Credentials))
	for field := range runtime.Credentials {
		runtime.CredentialFields = append(runtime.CredentialFields, field)
	}
	slices.Sort(runtime.CredentialFields)
	return runtime, nil
}

func (s *Store) RecordDownloaderHealth(ctx context.Context, name, status string, details map[string]any, actor workflow.Actor) error {
	if status != "ready" && status != "failed" && status != "unknown" {
		return fmt.Errorf("%w: invalid downloader health status", ErrValidation)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin downloader health update: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var downloaderID string
	err = tx.QueryRow(ctx, `
		UPDATE downloaders SET health_status = $2, last_health_check_at = now(), updated_at = now()
		WHERE name = $1 RETURNING id::text`, name, status).Scan(&downloaderID)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("update downloader health: %w", err)
	}
	if details == nil {
		details = map[string]any{}
	}
	details["status"] = status
	if err := audit(ctx, tx, actor, "downloader.health", "downloader", downloaderID, details); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) AuditDownloaderAction(ctx context.Context, name, action string, details map[string]any, actor workflow.Actor) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin downloader action audit: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var downloaderID string
	if err := tx.QueryRow(ctx, "SELECT id::text FROM downloaders WHERE name = $1 FOR UPDATE", name).Scan(&downloaderID); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("lock downloader for action audit: %w", err)
	}
	if details == nil {
		details = map[string]any{}
	}
	if err := audit(ctx, tx, actor, "downloader."+action, "downloader", downloaderID, details); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) UpsertImageHost(ctx context.Context, name string, input ImageHostInput, actor workflow.Actor) (ImageHost, error) {
	name = strings.TrimSpace(name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	if err := validateResourceName("image host", name); err != nil {
		return ImageHost{}, err
	}
	if err := validateResourceName("image host adapter", input.Adapter); err != nil {
		return ImageHost{}, err
	}
	config, err := validateEndpointConfig(input.Config)
	if err != nil {
		return ImageHost{}, err
	}
	credentialFields, credentialPayload, err := validateCredentials(input.Credentials)
	if err != nil {
		return ImageHost{}, err
	}
	var newSecretID any
	if credentialPayload != nil {
		secretID, err := s.secrets.Put(ctx, "image_hosts."+name+".credentials", credentialPayload, actor.ID)
		if err != nil {
			return ImageHost{}, err
		}
		newSecretID = secretID
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if input.Priority == 0 {
		input.Priority = 100
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return ImageHost{}, fmt.Errorf("begin image host transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var imageHost ImageHost
	var secretID string
	var healthChecked pgtype.Timestamptz
	err = tx.QueryRow(ctx, `
		INSERT INTO image_hosts(name, adapter, enabled, priority, config, secret_id)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (name) DO UPDATE SET
			adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled, priority = EXCLUDED.priority,
			config = EXCLUDED.config, secret_id = COALESCE(EXCLUDED.secret_id, image_hosts.secret_id), updated_at = now()
		RETURNING id::text, name, adapter, enabled, priority, config, COALESCE(secret_id::text, ''),
		          health_status, last_health_check_at, created_at, updated_at`,
		name, input.Adapter, enabled, input.Priority, config, newSecretID,
	).Scan(
		&imageHost.ID, &imageHost.Name, &imageHost.Adapter, &imageHost.Enabled,
		&imageHost.Priority, &imageHost.Config, &secretID, &imageHost.HealthStatus,
		&healthChecked, &imageHost.CreatedAt, &imageHost.UpdatedAt,
	)
	if err != nil {
		return ImageHost{}, fmt.Errorf("upsert image host: %w", err)
	}
	imageHost.LastHealthCheck = timePointer(healthChecked)
	if err := audit(ctx, tx, actor, "image_host.upsert", "image_host", imageHost.ID, map[string]any{
		"name": name, "adapter": input.Adapter, "enabled": enabled,
		"priority": input.Priority, "credential_fields": credentialFields,
	}); err != nil {
		return ImageHost{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return ImageHost{}, fmt.Errorf("commit image host: %w", err)
	}
	if newSecretID == nil {
		imageHost.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "image_hosts."+name+".credentials")
	} else {
		imageHost.CredentialFields = credentialFields
	}
	return imageHost, err
}

func (s *Store) ListImageHosts(ctx context.Context) ([]ImageHost, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, adapter, enabled, priority, config, COALESCE(secret_id::text, ''),
		       health_status, last_health_check_at, created_at, updated_at
		FROM image_hosts ORDER BY priority, name`)
	if err != nil {
		return nil, fmt.Errorf("list image hosts: %w", err)
	}
	defer rows.Close()
	result := make([]ImageHost, 0)
	for rows.Next() {
		var item ImageHost
		var secretID string
		var healthChecked pgtype.Timestamptz
		if err := rows.Scan(
			&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Priority,
			&item.Config, &secretID, &item.HealthStatus, &healthChecked, &item.CreatedAt, &item.UpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan image host: %w", err)
		}
		item.LastHealthCheck = timePointer(healthChecked)
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "image_hosts."+item.Name+".credentials")
		if err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate image hosts: %w", err)
	}
	return result, nil
}

func (s *Store) CreateScreenshotProfile(ctx context.Context, input ScreenshotProfileInput, actor workflow.Actor) (ScreenshotProfile, error) {
	input.Name = strings.TrimSpace(input.Name)
	if err := validateResourceName("screenshot profile", input.Name); err != nil {
		return ScreenshotProfile{}, err
	}
	if input.Config == nil {
		return ScreenshotProfile{}, fmt.Errorf("%w: screenshot config is required", ErrValidation)
	}
	if containsSecretLikeKey(input.Config) {
		return ScreenshotProfile{}, fmt.Errorf("%w: screenshot config must not contain secrets", ErrValidation)
	}
	config, err := json.Marshal(input.Config)
	if err != nil {
		return ScreenshotProfile{}, fmt.Errorf("%w: serialize screenshot config: %v", ErrValidation, err)
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
	if err != nil {
		return ScreenshotProfile{}, fmt.Errorf("begin screenshot profile: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var revision int
	if err := tx.QueryRow(ctx, "SELECT COALESCE(max(revision), 0) + 1 FROM screenshot_profiles WHERE name = $1", input.Name).Scan(&revision); err != nil {
		return ScreenshotProfile{}, fmt.Errorf("allocate screenshot profile revision: %w", err)
	}
	var profile ScreenshotProfile
	err = tx.QueryRow(ctx, `
		INSERT INTO screenshot_profiles(name, revision, enabled, config, created_by)
		VALUES ($1, $2, $3, $4, NULLIF($5, '')::uuid)
		RETURNING id::text, created_at`, input.Name, revision, enabled, config, actor.ID).Scan(&profile.ID, &profile.CreatedAt)
	if err != nil {
		return ScreenshotProfile{}, fmt.Errorf("insert screenshot profile: %w", err)
	}
	profile.Name, profile.Revision, profile.Enabled, profile.Config = input.Name, revision, enabled, config
	if err := audit(ctx, tx, actor, "screenshot_profile.create", "screenshot_profile", profile.ID, map[string]any{
		"name": input.Name, "revision": revision, "enabled": enabled,
	}); err != nil {
		return ScreenshotProfile{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return ScreenshotProfile{}, fmt.Errorf("commit screenshot profile: %w", err)
	}
	return profile, nil
}

func (s *Store) ListScreenshotProfiles(ctx context.Context) ([]ScreenshotProfile, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, revision, enabled, config, created_at
		FROM screenshot_profiles ORDER BY name, revision DESC`)
	if err != nil {
		return nil, fmt.Errorf("list screenshot profiles: %w", err)
	}
	defer rows.Close()
	result := make([]ScreenshotProfile, 0)
	for rows.Next() {
		var item ScreenshotProfile
		if err := rows.Scan(&item.ID, &item.Name, &item.Revision, &item.Enabled, &item.Config, &item.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan screenshot profile: %w", err)
		}
		result = append(result, item)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate screenshot profiles: %w", err)
	}
	return result, nil
}

func (s *Store) loadMappings(ctx context.Context, downloaderID string) ([]PathMapping, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT remote_path, local_path, priority FROM downloader_path_mappings
		WHERE downloader_id = $1 ORDER BY priority DESC, remote_path`, downloaderID)
	if err != nil {
		return nil, fmt.Errorf("load downloader path mappings: %w", err)
	}
	defer rows.Close()
	result := make([]PathMapping, 0)
	for rows.Next() {
		var mapping PathMapping
		if err := rows.Scan(&mapping.RemotePath, &mapping.LocalPath, &mapping.Priority); err != nil {
			return nil, fmt.Errorf("scan downloader path mapping: %w", err)
		}
		result = append(result, mapping)
	}
	return result, rows.Err()
}

func (s *Store) loadCredentialFields(ctx context.Context, secretID, purpose string) ([]string, error) {
	if secretID == "" {
		return []string{}, nil
	}
	plaintext, err := s.secrets.Get(ctx, secretID, purpose)
	if err != nil {
		return nil, fmt.Errorf("read integration credential metadata: %w", err)
	}
	var credentials map[string]string
	if err := json.Unmarshal(plaintext, &credentials); err != nil {
		return nil, fmt.Errorf("decode integration credential metadata: %w", err)
	}
	fields := make([]string, 0, len(credentials))
	for field := range credentials {
		fields = append(fields, field)
	}
	slices.Sort(fields)
	return fields, nil
}

func audit(ctx context.Context, tx pgx.Tx, actor workflow.Actor, action, resourceType, resourceID string, payload any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("serialize integration audit event: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, payload)
		VALUES ($1, NULLIF($2, ''), $3, $4, $5, $6)`,
		actor.Type, actor.ID, action, resourceType, resourceID, body,
	); err != nil {
		return fmt.Errorf("write integration audit event: %w", err)
	}
	return nil
}

func timePointer(value pgtype.Timestamptz) *time.Time {
	if !value.Valid {
		return nil
	}
	result := value.Time
	return &result
}
