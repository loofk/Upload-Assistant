ALTER TABLE site_rule_collection_documents
    ADD COLUMN auth_mode text NOT NULL DEFAULT 'site_cookie',
    ADD CONSTRAINT site_rule_collection_documents_auth_mode_check
        CHECK (auth_mode IN ('none', 'site_cookie'));
