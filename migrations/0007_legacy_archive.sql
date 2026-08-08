ALTER TABLE legacy_imports
    ADD COLUMN archive_secret_id uuid REFERENCES secrets(id) ON DELETE SET NULL,
    ADD COLUMN archive_sha256 text CHECK (archive_sha256 IS NULL OR archive_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN archive_size_bytes bigint CHECK (archive_size_bytes IS NULL OR archive_size_bytes > 0),
    ADD COLUMN archive_deleted_at timestamptz,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX legacy_imports_source_fingerprint_idx
    ON legacy_imports(source_kind, source_sha256, created_at DESC);

CREATE INDEX legacy_imports_archive_cleanup_idx
    ON legacy_imports(expires_at)
    WHERE archive_secret_id IS NOT NULL;
