package integrations

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/loofk/upload-assistant/v2/internal/operations"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

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
		SELECT id::text, code, name, adapter, enabled, config, updated_at
		FROM sites WHERE code = $1`, siteCode).Scan(
		&runtime.ID, &runtime.Code, &runtime.Name, &runtime.Adapter, &enabled, &runtime.Config, &runtime.UpdatedAt,
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
		SELECT sc.name, sc.secret_id::text, sc.updated_at
		FROM site_credentials sc
		JOIN sites s ON s.id = sc.site_id
		WHERE s.code = $1 AND sc.enabled = true
		ORDER BY sc.name`, siteCode)
	if err != nil {
		return RuntimeSite{}, fmt.Errorf("load runtime site credentials: %w", err)
	}
	defer rows.Close()
	runtime.Credentials = map[string]string{}
	configurationHash := sha256.New()
	_, _ = configurationHash.Write([]byte(runtime.ID + "\x00" + runtime.Code + "\x00" + runtime.Adapter + "\x00" + runtime.UpdatedAt.UTC().Format(time.RFC3339Nano) + "\x00"))
	_, _ = configurationHash.Write(runtime.Config)
	for rows.Next() {
		var name, secretID string
		var credentialUpdatedAt time.Time
		if err := rows.Scan(&name, &secretID, &credentialUpdatedAt); err != nil {
			return RuntimeSite{}, fmt.Errorf("scan runtime site credential: %w", err)
		}
		_, _ = configurationHash.Write([]byte("\x00" + name + "\x00" + secretID + "\x00" + credentialUpdatedAt.UTC().Format(time.RFC3339Nano)))
		plaintext, err := s.secrets.Get(ctx, secretID, "sites."+siteCode+"."+name)
		if err != nil {
			return RuntimeSite{}, fmt.Errorf("decrypt runtime site credential %s: %w", name, err)
		}
		runtime.Credentials[name] = string(plaintext)
	}
	if err := rows.Err(); err != nil {
		return RuntimeSite{}, fmt.Errorf("iterate runtime site credentials: %w", err)
	}
	runtime.ConfigurationSHA256 = hex.EncodeToString(configurationHash.Sum(nil))
	return runtime, nil
}

func (s *Store) AuditSiteAction(ctx context.Context, siteCode, action string, details map[string]any, actor workflow.Actor) error {
	siteCode = strings.ToUpper(strings.TrimSpace(siteCode))
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin site action audit: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var siteID string
	if err := tx.QueryRow(ctx, "SELECT id::text FROM sites WHERE code = $1 FOR UPDATE", siteCode).Scan(&siteID); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("lock site for action audit: %w", err)
	}
	if err := audit(ctx, tx, actor, "site."+action, "site", siteID, copyMap(details)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) UpsertDownloader(ctx context.Context, name string, input DownloaderInput, actor workflow.Actor) (Downloader, error) {
	name = strings.TrimSpace(name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	input.NetworkClass = strings.ToLower(strings.TrimSpace(input.NetworkClass))
	if input.NetworkClass == "" {
		input.NetworkClass = "unknown"
	}
	if input.NetworkClass != "unknown" && input.NetworkClass != "home" && input.NetworkClass != "seedbox" {
		return Downloader{}, fmt.Errorf("%w: downloader network_class must be unknown, home, or seedbox", ErrValidation)
	}
	if err := validateResourceName("downloader", name); err != nil {
		return Downloader{}, err
	}
	capability, exists := DownloaderAdapterCapabilityFor(input.Adapter)
	if !exists {
		return Downloader{}, fmt.Errorf("%w: unsupported downloader adapter %q", ErrValidation, input.Adapter)
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if enabled && !capability.RuntimeSupported {
		return Downloader{}, fmt.Errorf("%w: downloader adapter %q cannot be enabled: %s", ErrValidation, input.Adapter, capability.UnavailableReason)
	}
	if err := validateDownloaderCredentialContract(capability, input.Credentials); err != nil {
		return Downloader{}, err
	}
	if err := validateDownloaderOptionContract(capability, input.Config.Options); err != nil {
		return Downloader{}, err
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
	if err := s.ensureDownloaderRequiredCredentials(ctx, name, input.Adapter, enabled, credentialFields); err != nil {
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
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return Downloader{}, fmt.Errorf("begin downloader transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var downloader Downloader
	var secretID string
	var healthChecked pgtype.Timestamptz
	err = tx.QueryRow(ctx, `
		INSERT INTO downloaders(name, adapter, enabled, network_class, config, secret_id)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (name) DO UPDATE SET
			adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled, network_class = EXCLUDED.network_class, config = EXCLUDED.config,
			secret_id = CASE
				WHEN downloaders.adapter IS DISTINCT FROM EXCLUDED.adapter THEN EXCLUDED.secret_id
				ELSE COALESCE(EXCLUDED.secret_id, downloaders.secret_id)
			END,
			updated_at = now()
		RETURNING id::text, name, adapter, enabled, network_class, config, COALESCE(secret_id::text, ''),
		          health_status, last_health_check_at, created_at, updated_at`,
		name, input.Adapter, enabled, input.NetworkClass, config, newSecretID,
	).Scan(
		&downloader.ID, &downloader.Name, &downloader.Adapter, &downloader.Enabled,
		&downloader.NetworkClass, &downloader.Config, &secretID, &downloader.HealthStatus, &healthChecked,
		&downloader.CreatedAt, &downloader.UpdatedAt,
	)
	if err != nil {
		return Downloader{}, fmt.Errorf("upsert downloader: %w", err)
	}
	if enabled && input.Adapter == "deluge" && secretID == "" {
		return Downloader{}, fmt.Errorf("%w: Deluge requires a Web password credential when enabled", ErrValidation)
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
		"name": name, "adapter": input.Adapter, "enabled": enabled, "network_class": input.NetworkClass,
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
	attachDownloaderCapability(&downloader)
	return downloader, err
}

func (s *Store) ListDownloaders(ctx context.Context) ([]Downloader, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, adapter, enabled, network_class, config, COALESCE(secret_id::text, ''),
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
			&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.NetworkClass, &item.Config, &secretID,
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
		attachDownloaderCapability(&item)
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
		SELECT id::text, name, adapter, enabled, network_class, config, COALESCE(secret_id::text, ''),
		       health_status, last_health_check_at, created_at, updated_at
		FROM downloaders WHERE name = $1`, strings.TrimSpace(name)).Scan(
		&runtime.ID, &runtime.Name, &runtime.Adapter, &runtime.Enabled, &runtime.NetworkClass, &runtime.Config,
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
	capability, exists := DownloaderAdapterCapabilityFor(runtime.Adapter)
	if !exists || !capability.RuntimeSupported {
		return RuntimeDownloader{}, fmt.Errorf("%w: downloader adapter %q has no native runtime", ErrValidation, runtime.Adapter)
	}
	runtime.AdapterCapability = capability
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
	if err := validateDownloaderCredentialContract(capability, runtime.Credentials); err != nil {
		return RuntimeDownloader{}, fmt.Errorf("%w: stored downloader credentials no longer match adapter contract", ErrValidation)
	}
	if runtime.Adapter == "deluge" && runtime.Credentials["password"] == "" {
		return RuntimeDownloader{}, fmt.Errorf("%w: Deluge Web password is required", ErrValidation)
	}
	runtime.CredentialFields = make([]string, 0, len(runtime.Credentials))
	for field := range runtime.Credentials {
		runtime.CredentialFields = append(runtime.CredentialFields, field)
	}
	slices.Sort(runtime.CredentialFields)
	configurationHash := sha256.New()
	_, _ = configurationHash.Write([]byte(runtime.ID + "\x00" + runtime.Name + "\x00" + runtime.Adapter + "\x00" + runtime.NetworkClass + "\x00" + runtime.UpdatedAt.UTC().Format(time.RFC3339Nano) + "\x00" + secretID + "\x00"))
	_, _ = configurationHash.Write(runtime.Config)
	for _, mapping := range runtime.PathMappings {
		_, _ = configurationHash.Write([]byte("\x00" + mapping.RemotePath + "\x00" + mapping.LocalPath + "\x00" + strconv.Itoa(mapping.Priority)))
	}
	runtime.ConfigurationSHA256 = hex.EncodeToString(configurationHash.Sum(nil))
	return runtime, nil
}

func (s *Store) ensureDownloaderRequiredCredentials(ctx context.Context, name, adapter string, enabled bool, suppliedFields []string) error {
	if !enabled || adapter != "deluge" {
		return nil
	}
	if slices.Equal(suppliedFields, []string{"password"}) {
		return nil
	}
	if len(suppliedFields) > 0 {
		return fmt.Errorf("%w: Deluge requires a Web password credential", ErrValidation)
	}
	var existingAdapter, secretID string
	err := s.pool.QueryRow(ctx, `SELECT adapter, COALESCE(secret_id::text, '') FROM downloaders WHERE name = $1`, name).Scan(&existingAdapter, &secretID)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("%w: Deluge requires a Web password credential when enabled", ErrValidation)
	}
	if err != nil {
		return fmt.Errorf("inspect existing downloader credentials: %w", err)
	}
	if existingAdapter != "deluge" || secretID == "" {
		return fmt.Errorf("%w: Deluge requires a new Web password when enabling or changing adapters", ErrValidation)
	}
	fields, err := s.loadCredentialFields(ctx, secretID, "downloaders."+name+".credentials")
	if err != nil {
		return err
	}
	if !slices.Equal(fields, []string{"password"}) {
		return fmt.Errorf("%w: stored Deluge credentials are not a Web password; replace them explicitly", ErrValidation)
	}
	return nil
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
		UPDATE downloaders SET health_status = $2, last_health_check_at = now()
		WHERE name = $1 RETURNING id::text`, name, status).Scan(&downloaderID)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("update downloader health: %w", err)
	}
	payload := copyMap(details)
	payload["status"] = status
	if err := audit(ctx, tx, actor, "downloader.health", "downloader", downloaderID, payload); err != nil {
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
	requiredCredentialFields, supported := imageHostCredentialFields(input.Adapter)
	if !supported {
		return ImageHost{}, fmt.Errorf("%w: unsupported image host adapter %q", ErrValidation, input.Adapter)
	}
	config, err := validateEndpointConfig(input.Config)
	if err != nil {
		return ImageHost{}, err
	}
	if err := ValidateImageHostEndpoint(input.Adapter, input.Config.Endpoint); err != nil {
		return ImageHost{}, err
	}
	credentialFields, credentialPayload, err := validateCredentials(input.Credentials)
	if err != nil {
		return ImageHost{}, err
	}
	if len(credentialFields) > 0 && !slices.Equal(credentialFields, requiredCredentialFields) {
		if len(requiredCredentialFields) == 0 {
			return ImageHost{}, fmt.Errorf("%w: %s does not accept credentials", ErrValidation, input.Adapter)
		}
		return ImageHost{}, fmt.Errorf("%w: %s only accepts %s credentials", ErrValidation, input.Adapter, strings.Join(requiredCredentialFields, ", "))
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if enabled && len(requiredCredentialFields) > 0 && len(credentialFields) == 0 {
		var exists bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM image_hosts WHERE name = $1 AND adapter = $2 AND secret_id IS NOT NULL)`, name, input.Adapter).Scan(&exists); err != nil {
			return ImageHost{}, fmt.Errorf("check image host credentials: %w", err)
		}
		if !exists {
			return ImageHost{}, fmt.Errorf("%w: enabled %s requires %s", ErrValidation, input.Adapter, strings.Join(requiredCredentialFields, " and "))
		}
	}
	var newSecretID any
	if credentialPayload != nil {
		secretID, err := s.secrets.Put(ctx, "image_hosts."+name+".credentials", credentialPayload, actor.ID)
		if err != nil {
			return ImageHost{}, err
		}
		newSecretID = secretID
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
			config = EXCLUDED.config,
			secret_id = CASE
				WHEN image_hosts.adapter IS DISTINCT FROM EXCLUDED.adapter THEN EXCLUDED.secret_id
				ELSE COALESCE(EXCLUDED.secret_id, image_hosts.secret_id)
			END,
			updated_at = now()
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

func imageHostCredentialFields(adapter string) ([]string, bool) {
	switch adapter {
	case "imgbb", "ptpimg":
		return []string{"api_key"}, true
	case "imgbox", "pixhost":
		return []string{}, true
	default:
		return nil, false
	}
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

func (s *Store) GetRuntimeImageHost(ctx context.Context, name string) (RuntimeImageHost, error) {
	name = strings.TrimSpace(name)
	var runtime RuntimeImageHost
	var secretID string
	var healthChecked pgtype.Timestamptz
	err := s.pool.QueryRow(ctx, `
		SELECT id::text, name, adapter, enabled, priority, config, COALESCE(secret_id::text, ''),
		       health_status, last_health_check_at, created_at, updated_at
		FROM image_hosts WHERE name = $1`, name).Scan(
		&runtime.ID, &runtime.Name, &runtime.Adapter, &runtime.Enabled, &runtime.Priority,
		&runtime.Config, &secretID, &runtime.HealthStatus, &healthChecked,
		&runtime.CreatedAt, &runtime.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeImageHost{}, ErrNotFound
	}
	if err != nil {
		return RuntimeImageHost{}, fmt.Errorf("load runtime image host: %w", err)
	}
	if !runtime.Enabled {
		return RuntimeImageHost{}, fmt.Errorf("%w: image host is disabled", ErrValidation)
	}
	runtime.LastHealthCheck = timePointer(healthChecked)
	if err := json.Unmarshal(runtime.Config, &runtime.EndpointConfig); err != nil {
		return RuntimeImageHost{}, fmt.Errorf("decode runtime image host config: %w", err)
	}
	if secretID != "" {
		plaintext, err := s.secrets.Get(ctx, secretID, "image_hosts."+runtime.Name+".credentials")
		if err != nil {
			return RuntimeImageHost{}, err
		}
		if err := json.Unmarshal(plaintext, &runtime.Credentials); err != nil {
			return RuntimeImageHost{}, fmt.Errorf("decode runtime image host credentials: %w", err)
		}
	} else {
		runtime.Credentials = map[string]string{}
	}
	return runtime, nil
}

func (s *Store) RecordImageHostHealth(ctx context.Context, name, status string, details map[string]any, actor workflow.Actor) error {
	if status != "ready" && status != "failed" && status != "unknown" {
		return fmt.Errorf("%w: invalid image host health status", ErrValidation)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin image host health update: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var imageHostID string
	err = tx.QueryRow(ctx, `
		UPDATE image_hosts SET health_status = $2, last_health_check_at = now()
		WHERE name = $1 RETURNING id::text`, name, status).Scan(&imageHostID)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("update image host health: %w", err)
	}
	payload := copyMap(details)
	payload["status"] = status
	if err := audit(ctx, tx, actor, "image_host.health", "image_host", imageHostID, payload); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) AuditImageHostAction(ctx context.Context, name, action string, details map[string]any, actor workflow.Actor) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin image host action audit: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var imageHostID string
	if err := tx.QueryRow(ctx, "SELECT id::text FROM image_hosts WHERE name = $1 FOR UPDATE", name).Scan(&imageHostID); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("lock image host for action audit: %w", err)
	}
	if err := audit(ctx, tx, actor, "image_host."+action, "image_host", imageHostID, copyMap(details)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) UpsertNotificationChannel(ctx context.Context, name string, input NotificationChannelInput, actor workflow.Actor) (NotificationChannel, error) {
	name = strings.TrimSpace(name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	if err := validateResourceName("notification channel", name); err != nil {
		return NotificationChannel{}, err
	}
	if !slices.Contains([]string{"discord_webhook", "telegram_bot", "wecom_bot", "feishu_bot"}, input.Adapter) {
		return NotificationChannel{}, fmt.Errorf("%w: unsupported notification adapter %q", ErrValidation, input.Adapter)
	}
	config, err := validateNotificationChannelConfig(input.Config)
	if err != nil {
		return NotificationChannel{}, err
	}
	fields, payload, err := validateCredentials(input.Credentials)
	if err != nil {
		return NotificationChannel{}, err
	}
	requiredFields := notificationCredentialFields(input.Adapter)
	if len(fields) > 0 && !slices.Equal(fields, requiredFields) {
		return NotificationChannel{}, fmt.Errorf("%w: %s only accepts %s credentials", ErrValidation, input.Adapter, strings.Join(requiredFields, ", "))
	}
	if err := validateNotificationCredentials(input.Adapter, input.Credentials, false); err != nil {
		return NotificationChannel{}, err
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if enabled && len(fields) == 0 {
		var exists bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM notification_channels WHERE name = $1 AND adapter = $2 AND secret_id IS NOT NULL)`, name, input.Adapter).Scan(&exists); err != nil {
			return NotificationChannel{}, fmt.Errorf("check notification credentials: %w", err)
		}
		if !exists {
			return NotificationChannel{}, fmt.Errorf("%w: enabled %s requires %s", ErrValidation, input.Adapter, strings.Join(requiredFields, " and "))
		}
	}
	var newSecretID any
	if payload != nil {
		secretID, err := s.secrets.Put(ctx, "notification_channels."+name+".credentials", payload, actor.ID)
		if err != nil {
			return NotificationChannel{}, err
		}
		newSecretID = secretID
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return NotificationChannel{}, fmt.Errorf("begin notification channel transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var item NotificationChannel
	var secretID string
	var healthChecked pgtype.Timestamptz
	err = tx.QueryRow(ctx, `
		INSERT INTO notification_channels(name, adapter, enabled, config, secret_id)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (name) DO UPDATE SET
			adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled, config = EXCLUDED.config,
			secret_id = CASE WHEN notification_channels.adapter IS DISTINCT FROM EXCLUDED.adapter
			                 THEN EXCLUDED.secret_id ELSE COALESCE(EXCLUDED.secret_id, notification_channels.secret_id) END,
			updated_at = now()
		RETURNING id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''),
		          health_status, last_health_check_at, created_at, updated_at`,
		name, input.Adapter, enabled, config, newSecretID,
	).Scan(&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
		&item.HealthStatus, &healthChecked, &item.CreatedAt, &item.UpdatedAt)
	if err != nil {
		return NotificationChannel{}, fmt.Errorf("upsert notification channel: %w", err)
	}
	item.LastHealthCheck = timePointer(healthChecked)
	if err := audit(ctx, tx, actor, "notification_channel.upsert", "notification_channel", item.ID, map[string]any{
		"name": name, "adapter": input.Adapter, "enabled": enabled, "credential_fields": fields,
	}); err != nil {
		return NotificationChannel{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return NotificationChannel{}, fmt.Errorf("commit notification channel: %w", err)
	}
	if newSecretID == nil {
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "notification_channels."+name+".credentials")
	} else {
		item.CredentialFields = fields
	}
	return item, err
}

func (s *Store) ListNotificationChannels(ctx context.Context) ([]NotificationChannel, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''),
		       health_status, last_health_check_at, created_at, updated_at
		FROM notification_channels ORDER BY name`)
	if err != nil {
		return nil, fmt.Errorf("list notification channels: %w", err)
	}
	defer rows.Close()
	result := make([]NotificationChannel, 0)
	for rows.Next() {
		var item NotificationChannel
		var secretID string
		var checked pgtype.Timestamptz
		if err := rows.Scan(&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
			&item.HealthStatus, &checked, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan notification channel: %w", err)
		}
		item.LastHealthCheck = timePointer(checked)
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "notification_channels."+item.Name+".credentials")
		if err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

func (s *Store) GetRuntimeNotificationChannel(ctx context.Context, name string) (RuntimeNotificationChannel, error) {
	var runtime RuntimeNotificationChannel
	var secretID string
	var checked pgtype.Timestamptz
	err := s.pool.QueryRow(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		       last_health_check_at, created_at, updated_at
		FROM notification_channels WHERE name = $1`, strings.TrimSpace(name)).Scan(
		&runtime.ID, &runtime.Name, &runtime.Adapter, &runtime.Enabled, &runtime.Config, &secretID,
		&runtime.HealthStatus, &checked, &runtime.CreatedAt, &runtime.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeNotificationChannel{}, ErrNotFound
	}
	if err != nil {
		return RuntimeNotificationChannel{}, fmt.Errorf("load runtime notification channel: %w", err)
	}
	if !runtime.Enabled {
		return RuntimeNotificationChannel{}, fmt.Errorf("%w: notification channel is disabled", ErrValidation)
	}
	runtime.LastHealthCheck = timePointer(checked)
	if err := json.Unmarshal(runtime.Config, &runtime.ChannelConfig); err != nil {
		return RuntimeNotificationChannel{}, fmt.Errorf("decode runtime notification channel config: %w", err)
	}
	runtime.Credentials = map[string]string{}
	if secretID != "" {
		plaintext, err := s.secrets.Get(ctx, secretID, "notification_channels."+runtime.Name+".credentials")
		if err != nil {
			return RuntimeNotificationChannel{}, err
		}
		if err := json.Unmarshal(plaintext, &runtime.Credentials); err != nil {
			return RuntimeNotificationChannel{}, fmt.Errorf("decode runtime notification credentials: %w", err)
		}
	}
	if err := validateNotificationCredentials(runtime.Adapter, runtime.Credentials, true); err != nil {
		return RuntimeNotificationChannel{}, err
	}
	runtime.ConfigurationSHA256 = integrationConfigurationSHA(runtime.ID, runtime.Adapter, runtime.Config, secretID, runtime.UpdatedAt)
	return runtime, nil
}

func notificationCredentialFields(adapter string) []string {
	if adapter == "telegram_bot" {
		return []string{"bot_token", "chat_id"}
	}
	return []string{"webhook_url"}
}

func validateNotificationCredentials(adapter string, credentials map[string]string, required bool) error {
	fields := notificationCredentialFields(adapter)
	if required {
		for _, field := range fields {
			if strings.TrimSpace(credentials[field]) == "" {
				return fmt.Errorf("%w: stored %s credential %s is missing", ErrValidation, adapter, field)
			}
		}
	}
	if adapter == "telegram_bot" {
		if token := strings.TrimSpace(credentials["bot_token"]); token != "" {
			parts := strings.Split(token, ":")
			if len(parts) != 2 || len(parts[0]) < 5 || len(parts[1]) < 20 {
				return fmt.Errorf("%w: invalid Telegram bot token", ErrValidation)
			}
			for _, character := range parts[0] {
				if character < '0' || character > '9' {
					return fmt.Errorf("%w: invalid Telegram bot token", ErrValidation)
				}
			}
			for _, character := range parts[1] {
				if (character < 'a' || character > 'z') &&
					(character < 'A' || character > 'Z') &&
					(character < '0' || character > '9') && character != '_' && character != '-' {
					return fmt.Errorf("%w: invalid Telegram bot token", ErrValidation)
				}
			}
		}
		if chatID := strings.TrimSpace(credentials["chat_id"]); chatID != "" {
			if len(chatID) > 128 || strings.ContainsAny(chatID, "\r\n\x00") {
				return fmt.Errorf("%w: invalid Telegram chat id", ErrValidation)
			}
		}
		return nil
	}
	if value := strings.TrimSpace(credentials["webhook_url"]); value != "" {
		if err := validateNotificationWebhookURL(value); err != nil {
			return fmt.Errorf("%w: invalid %s webhook URL", ErrValidation, adapter)
		}
	}
	return nil
}

func validateNotificationWebhookURL(value string) error {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return ErrValidation
	}
	if parsed.User != nil || parsed.Fragment != "" || strings.Contains(parsed.EscapedPath(), "..") {
		return ErrValidation
	}
	if parsed.Scheme == "http" {
		hostname := strings.ToLower(parsed.Hostname())
		address := net.ParseIP(hostname)
		if hostname != "localhost" && (address == nil || !address.IsLoopback()) {
			return ErrValidation
		}
	}
	return nil
}

func (s *Store) UpsertMediaManager(ctx context.Context, name string, input MediaManagerInput, actor workflow.Actor) (MediaManager, error) {
	name = strings.TrimSpace(name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	if err := validateResourceName("media manager", name); err != nil {
		return MediaManager{}, err
	}
	if input.Adapter != "sonarr" && input.Adapter != "radarr" {
		return MediaManager{}, fmt.Errorf("%w: media manager adapter must be sonarr or radarr", ErrValidation)
	}
	config, err := validateEndpointConfig(input.Config)
	if err != nil {
		return MediaManager{}, err
	}
	fields, payload, err := validateCredentials(input.Credentials)
	if err != nil {
		return MediaManager{}, err
	}
	if len(fields) > 0 && !slices.Equal(fields, []string{"api_key"}) {
		return MediaManager{}, fmt.Errorf("%w: media managers only accept api_key credentials", ErrValidation)
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if enabled && len(fields) == 0 {
		var exists bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM media_managers WHERE name = $1 AND adapter = $2 AND secret_id IS NOT NULL)`, name, input.Adapter).Scan(&exists); err != nil {
			return MediaManager{}, fmt.Errorf("check media manager credentials: %w", err)
		}
		if !exists {
			return MediaManager{}, fmt.Errorf("%w: enabled media manager requires api_key", ErrValidation)
		}
	}
	var newSecretID any
	if payload != nil {
		secretID, err := s.secrets.Put(ctx, "media_managers."+name+".credentials", payload, actor.ID)
		if err != nil {
			return MediaManager{}, err
		}
		newSecretID = secretID
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return MediaManager{}, fmt.Errorf("begin media manager transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var item MediaManager
	var secretID string
	var checked pgtype.Timestamptz
	err = tx.QueryRow(ctx, `
		INSERT INTO media_managers(name, adapter, enabled, config, secret_id)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (name) DO UPDATE SET adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled,
			config = EXCLUDED.config,
			secret_id = CASE WHEN media_managers.adapter IS DISTINCT FROM EXCLUDED.adapter
			                 THEN EXCLUDED.secret_id ELSE COALESCE(EXCLUDED.secret_id, media_managers.secret_id) END,
			updated_at = now()
		RETURNING id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		          last_health_check_at, created_at, updated_at`,
		name, input.Adapter, enabled, config, newSecretID,
	).Scan(&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
		&item.HealthStatus, &checked, &item.CreatedAt, &item.UpdatedAt)
	if err != nil {
		return MediaManager{}, fmt.Errorf("upsert media manager: %w", err)
	}
	item.LastHealthCheck = timePointer(checked)
	if err := audit(ctx, tx, actor, "media_manager.upsert", "media_manager", item.ID, map[string]any{
		"name": name, "adapter": input.Adapter, "enabled": enabled, "credential_fields": fields,
	}); err != nil {
		return MediaManager{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return MediaManager{}, fmt.Errorf("commit media manager: %w", err)
	}
	if newSecretID == nil {
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "media_managers."+name+".credentials")
	} else {
		item.CredentialFields = fields
	}
	return item, err
}

func (s *Store) ListMediaManagers(ctx context.Context) ([]MediaManager, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		       last_health_check_at, created_at, updated_at FROM media_managers ORDER BY adapter, name`)
	if err != nil {
		return nil, fmt.Errorf("list media managers: %w", err)
	}
	defer rows.Close()
	result := make([]MediaManager, 0)
	for rows.Next() {
		var item MediaManager
		var secretID string
		var checked pgtype.Timestamptz
		if err := rows.Scan(&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
			&item.HealthStatus, &checked, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan media manager: %w", err)
		}
		item.LastHealthCheck = timePointer(checked)
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "media_managers."+item.Name+".credentials")
		if err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

func (s *Store) GetRuntimeMediaManager(ctx context.Context, name string) (RuntimeMediaManager, error) {
	var runtime RuntimeMediaManager
	var secretID string
	var checked pgtype.Timestamptz
	err := s.pool.QueryRow(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		       last_health_check_at, created_at, updated_at FROM media_managers WHERE name = $1`, strings.TrimSpace(name)).Scan(
		&runtime.ID, &runtime.Name, &runtime.Adapter, &runtime.Enabled, &runtime.Config, &secretID,
		&runtime.HealthStatus, &checked, &runtime.CreatedAt, &runtime.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeMediaManager{}, ErrNotFound
	}
	if err != nil {
		return RuntimeMediaManager{}, fmt.Errorf("load runtime media manager: %w", err)
	}
	if !runtime.Enabled {
		return RuntimeMediaManager{}, fmt.Errorf("%w: media manager is disabled", ErrValidation)
	}
	runtime.LastHealthCheck = timePointer(checked)
	if err := json.Unmarshal(runtime.Config, &runtime.EndpointConfig); err != nil {
		return RuntimeMediaManager{}, fmt.Errorf("decode runtime media manager config: %w", err)
	}
	runtime.Credentials = map[string]string{}
	if secretID != "" {
		plaintext, err := s.secrets.Get(ctx, secretID, "media_managers."+runtime.Name+".credentials")
		if err != nil {
			return RuntimeMediaManager{}, err
		}
		if err := json.Unmarshal(plaintext, &runtime.Credentials); err != nil {
			return RuntimeMediaManager{}, fmt.Errorf("decode runtime media manager credentials: %w", err)
		}
	}
	if strings.TrimSpace(runtime.Credentials["api_key"]) == "" {
		return RuntimeMediaManager{}, fmt.Errorf("%w: media manager api_key is missing", ErrValidation)
	}
	runtime.ConfigurationSHA256 = integrationConfigurationSHA(runtime.ID, runtime.Adapter, runtime.Config, secretID, runtime.UpdatedAt)
	return runtime, nil
}

func (s *Store) RecordMediaManagerHealth(ctx context.Context, name, status string, details map[string]any, actor workflow.Actor) error {
	if status != "ready" && status != "failed" && status != "unknown" {
		return fmt.Errorf("%w: invalid media manager health status", ErrValidation)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin media manager health update: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var id string
	if err := tx.QueryRow(ctx, `UPDATE media_managers SET health_status = $2, last_health_check_at = now() WHERE name = $1 RETURNING id::text`, name, status).Scan(&id); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("update media manager health: %w", err)
	}
	payload := copyMap(details)
	payload["status"] = status
	if err := audit(ctx, tx, actor, "media_manager.health", "media_manager", id, payload); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) AuditMediaManagerAction(ctx context.Context, name, action string, details map[string]any, actor workflow.Actor) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin media manager action audit: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var id string
	if err := tx.QueryRow(ctx, `SELECT id::text FROM media_managers WHERE name = $1 FOR UPDATE`, name).Scan(&id); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("lock media manager for action audit: %w", err)
	}
	if err := audit(ctx, tx, actor, "media_manager."+action, "media_manager", id, copyMap(details)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) UpsertMetadataProvider(ctx context.Context, name string, input MetadataProviderInput, actor workflow.Actor) (MetadataProvider, error) {
	name = strings.TrimSpace(name)
	input.Adapter = strings.ToLower(strings.TrimSpace(input.Adapter))
	if err := validateResourceName("metadata provider", name); err != nil {
		return MetadataProvider{}, err
	}
	if input.Adapter != "tmdb" && input.Adapter != "ptgen" {
		return MetadataProvider{}, fmt.Errorf("%w: metadata provider adapter must be tmdb or ptgen", ErrValidation)
	}
	config, err := validateEndpointConfig(input.Config)
	if err != nil {
		return MetadataProvider{}, err
	}
	fields, payload, err := validateCredentials(input.Credentials)
	if err != nil {
		return MetadataProvider{}, err
	}
	if len(fields) > 0 && !slices.Equal(fields, []string{"api_key"}) {
		return MetadataProvider{}, fmt.Errorf("%w: metadata providers only accept api_key credentials", ErrValidation)
	}
	enabled := true
	if input.Enabled != nil {
		enabled = *input.Enabled
	}
	if enabled && input.Adapter == "tmdb" && len(fields) == 0 {
		var exists bool
		if err := s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM metadata_providers WHERE name = $1 AND adapter = $2 AND secret_id IS NOT NULL)`, name, input.Adapter).Scan(&exists); err != nil {
			return MetadataProvider{}, fmt.Errorf("check metadata provider credentials: %w", err)
		}
		if !exists {
			return MetadataProvider{}, fmt.Errorf("%w: enabled TMDb provider requires api_key", ErrValidation)
		}
	}
	var newSecretID any
	if payload != nil {
		secretID, err := s.secrets.Put(ctx, "metadata_providers."+name+".credentials", payload, actor.ID)
		if err != nil {
			return MetadataProvider{}, err
		}
		newSecretID = secretID
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return MetadataProvider{}, fmt.Errorf("begin metadata provider transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var item MetadataProvider
	var secretID string
	var checked pgtype.Timestamptz
	err = tx.QueryRow(ctx, `
		INSERT INTO metadata_providers(name, adapter, enabled, config, secret_id)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (name) DO UPDATE SET adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled,
			config = EXCLUDED.config,
			secret_id = CASE WHEN metadata_providers.adapter IS DISTINCT FROM EXCLUDED.adapter
			                 THEN EXCLUDED.secret_id ELSE COALESCE(EXCLUDED.secret_id, metadata_providers.secret_id) END,
			updated_at = now()
		RETURNING id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		          last_health_check_at, created_at, updated_at`,
		name, input.Adapter, enabled, config, newSecretID,
	).Scan(&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
		&item.HealthStatus, &checked, &item.CreatedAt, &item.UpdatedAt)
	if err != nil {
		return MetadataProvider{}, fmt.Errorf("upsert metadata provider: %w", err)
	}
	item.LastHealthCheck = timePointer(checked)
	if err := audit(ctx, tx, actor, "metadata_provider.upsert", "metadata_provider", item.ID, map[string]any{
		"name": name, "adapter": input.Adapter, "enabled": enabled, "credential_fields": fields,
	}); err != nil {
		return MetadataProvider{}, err
	}
	if err := tx.Commit(ctx); err != nil {
		return MetadataProvider{}, fmt.Errorf("commit metadata provider: %w", err)
	}
	if newSecretID == nil {
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "metadata_providers."+name+".credentials")
	} else {
		item.CredentialFields = fields
	}
	return item, err
}

func (s *Store) ListMetadataProviders(ctx context.Context) ([]MetadataProvider, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		       last_health_check_at, created_at, updated_at FROM metadata_providers ORDER BY adapter, name`)
	if err != nil {
		return nil, fmt.Errorf("list metadata providers: %w", err)
	}
	defer rows.Close()
	result := make([]MetadataProvider, 0)
	for rows.Next() {
		var item MetadataProvider
		var secretID string
		var checked pgtype.Timestamptz
		if err := rows.Scan(&item.ID, &item.Name, &item.Adapter, &item.Enabled, &item.Config, &secretID,
			&item.HealthStatus, &checked, &item.CreatedAt, &item.UpdatedAt); err != nil {
			return nil, fmt.Errorf("scan metadata provider: %w", err)
		}
		item.LastHealthCheck = timePointer(checked)
		item.CredentialFields, err = s.loadCredentialFields(ctx, secretID, "metadata_providers."+item.Name+".credentials")
		if err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

func (s *Store) GetRuntimeMetadataProvider(ctx context.Context, name string) (RuntimeMetadataProvider, error) {
	var runtime RuntimeMetadataProvider
	var secretID string
	var checked pgtype.Timestamptz
	err := s.pool.QueryRow(ctx, `
		SELECT id::text, name, adapter, enabled, config, COALESCE(secret_id::text, ''), health_status,
		       last_health_check_at, created_at, updated_at FROM metadata_providers WHERE name = $1`, strings.TrimSpace(name)).Scan(
		&runtime.ID, &runtime.Name, &runtime.Adapter, &runtime.Enabled, &runtime.Config, &secretID,
		&runtime.HealthStatus, &checked, &runtime.CreatedAt, &runtime.UpdatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeMetadataProvider{}, ErrNotFound
	}
	if err != nil {
		return RuntimeMetadataProvider{}, fmt.Errorf("load runtime metadata provider: %w", err)
	}
	if !runtime.Enabled {
		return RuntimeMetadataProvider{}, fmt.Errorf("%w: metadata provider is disabled", ErrValidation)
	}
	runtime.LastHealthCheck = timePointer(checked)
	if err := json.Unmarshal(runtime.Config, &runtime.EndpointConfig); err != nil {
		return RuntimeMetadataProvider{}, fmt.Errorf("decode runtime metadata provider config: %w", err)
	}
	runtime.Credentials = map[string]string{}
	if secretID != "" {
		plaintext, err := s.secrets.Get(ctx, secretID, "metadata_providers."+runtime.Name+".credentials")
		if err != nil {
			return RuntimeMetadataProvider{}, err
		}
		if err := json.Unmarshal(plaintext, &runtime.Credentials); err != nil {
			return RuntimeMetadataProvider{}, fmt.Errorf("decode runtime metadata provider credentials: %w", err)
		}
	}
	if runtime.Adapter == "tmdb" && strings.TrimSpace(runtime.Credentials["api_key"]) == "" {
		return RuntimeMetadataProvider{}, fmt.Errorf("%w: TMDb provider api_key is missing", ErrValidation)
	}
	runtime.ConfigurationSHA256 = integrationConfigurationSHA(runtime.ID, runtime.Adapter, runtime.Config, secretID, runtime.UpdatedAt)
	return runtime, nil
}

func (s *Store) RecordMetadataProviderHealth(ctx context.Context, name, status string, details map[string]any, actor workflow.Actor) error {
	if status != "ready" && status != "failed" && status != "unknown" {
		return fmt.Errorf("%w: invalid metadata provider health status", ErrValidation)
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin metadata provider health update: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var id string
	if err := tx.QueryRow(ctx, `UPDATE metadata_providers SET health_status = $2, last_health_check_at = now() WHERE name = $1 RETURNING id::text`, name, status).Scan(&id); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("update metadata provider health: %w", err)
	}
	payload := copyMap(details)
	payload["status"] = status
	if err := audit(ctx, tx, actor, "metadata_provider.health", "metadata_provider", id, payload); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (s *Store) AuditMetadataProviderAction(ctx context.Context, name, action string, details map[string]any, actor workflow.Actor) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin metadata provider action audit: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	var id string
	if err := tx.QueryRow(ctx, `SELECT id::text FROM metadata_providers WHERE name = $1 FOR UPDATE`, name).Scan(&id); errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	} else if err != nil {
		return fmt.Errorf("lock metadata provider for action audit: %w", err)
	}
	if err := audit(ctx, tx, actor, "metadata_provider."+action, "metadata_provider", id, copyMap(details)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func validateSecretHTTPURL(value string) error {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return ErrValidation
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || strings.Contains(parsed.EscapedPath(), "..") {
		return ErrValidation
	}
	return nil
}

func integrationConfigurationSHA(id, adapter string, config []byte, secretID string, updatedAt time.Time) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte(id + "\x00" + adapter + "\x00" + secretID + "\x00" + updatedAt.UTC().Format(time.RFC3339Nano) + "\x00"))
	_, _ = hash.Write(config)
	return hex.EncodeToString(hash.Sum(nil))
}

func copyMap(input map[string]any) map[string]any {
	result := make(map[string]any, len(input)+1)
	for key, value := range input {
		result[key] = value
	}
	return result
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
	_, config, err := normalizeScreenshotConfig(input.Config)
	if err != nil {
		return ScreenshotProfile{}, err
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

func (s *Store) GetRuntimeScreenshotProfile(ctx context.Context, name string) (RuntimeScreenshotProfile, error) {
	name = strings.TrimSpace(name)
	var runtime RuntimeScreenshotProfile
	err := s.pool.QueryRow(ctx, `
		SELECT id::text, name, revision, enabled, config, created_at
		FROM screenshot_profiles
		WHERE name = $1 AND enabled = true
		ORDER BY revision DESC LIMIT 1`, name).Scan(
		&runtime.ID, &runtime.Name, &runtime.Revision, &runtime.Enabled, &runtime.Config, &runtime.CreatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return RuntimeScreenshotProfile{}, ErrNotFound
	}
	if err != nil {
		return RuntimeScreenshotProfile{}, fmt.Errorf("load runtime screenshot profile: %w", err)
	}
	if err := json.Unmarshal(runtime.Config, &runtime.ScreenshotConfig); err != nil {
		return RuntimeScreenshotProfile{}, fmt.Errorf("decode runtime screenshot profile: %w", err)
	}
	return runtime, nil
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
	traceID := operations.CorrelationFromContext(ctx).TraceID
	if _, err := tx.Exec(ctx, `
		INSERT INTO audit_events(actor_type, actor_id, action, resource_type, resource_id, trace_id, payload)
		VALUES ($1, NULLIF($2, ''), $3, $4, $5, NULLIF($6,'')::uuid, $7)`,
		actor.Type, actor.ID, action, resourceType, resourceID, traceID, body,
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
