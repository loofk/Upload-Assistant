-- Keep immutable revisions for audit while presenting one effective baseline
-- and at most one pending draft per site to operators.
UPDATE site_rule_revisions revisions
SET status = 'approved'
FROM sites
WHERE revisions.id = sites.active_rule_revision_id
  AND revisions.status <> 'approved';

UPDATE site_rule_revisions revisions
SET status = 'retired'
FROM sites
WHERE revisions.site_id = sites.id
  AND revisions.status = 'approved'
  AND revisions.id IS DISTINCT FROM sites.active_rule_revision_id;

WITH ranked_drafts AS (
    SELECT id,
           row_number() OVER (PARTITION BY site_id ORDER BY revision DESC, created_at DESC, id DESC) AS position
    FROM site_rule_revisions
    WHERE status = 'draft'
)
UPDATE site_rule_revisions revisions
SET status = 'retired'
FROM ranked_drafts
WHERE revisions.id = ranked_drafts.id
  AND ranked_drafts.position > 1;
