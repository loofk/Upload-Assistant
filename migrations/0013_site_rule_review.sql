CREATE TABLE site_aliases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    alias text NOT NULL,
    normalized_alias text NOT NULL UNIQUE,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, normalized_alias)
);

CREATE TABLE site_tags (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    tag text NOT NULL,
    normalized_tag text NOT NULL,
    created_by uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, normalized_tag)
);

CREATE TABLE site_rule_review_checks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_revision_id uuid NOT NULL REFERENCES site_rule_revisions(id) ON DELETE CASCADE,
    section text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('confirmed', 'needs_changes')),
    comment text,
    fingerprint text NOT NULL,
    reviewer_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_revision_id, section)
);

CREATE INDEX site_aliases_site_idx ON site_aliases(site_id);
CREATE INDEX site_tags_site_idx ON site_tags(site_id);
CREATE INDEX site_rule_review_checks_revision_idx ON site_rule_review_checks(rule_revision_id);

INSERT INTO site_tags(site_id, tag, normalized_tag)
SELECT id, '中文 PT', '中文 pt' FROM sites
ON CONFLICT (site_id, normalized_tag) DO NOTHING;

INSERT INTO site_tags(site_id, tag, normalized_tag)
SELECT id, '动漫', '动漫' FROM sites WHERE code = 'U2'
ON CONFLICT (site_id, normalized_tag) DO NOTHING;

INSERT INTO site_aliases(site_id, alias, normalized_alias)
SELECT id, alias, lower(alias)
FROM sites
JOIN (VALUES
    ('U2', 'U2分享园'),
    ('CHD', 'CHDBits'),
    ('MTEAM', 'MT'),
    ('MTEAM', 'M-Team')
) AS seeded(code, alias) USING (code)
ON CONFLICT (normalized_alias) DO NOTHING;
