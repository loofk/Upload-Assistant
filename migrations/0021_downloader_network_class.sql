-- The operator, not the application, decides whether a downloader is a
-- tracker-visible seedbox. Unknown remains the safe default for existing
-- configurations and never causes a seedbox-only cap to be inferred.
ALTER TABLE downloaders
    ADD COLUMN network_class text NOT NULL DEFAULT 'unknown'
        CHECK (network_class IN ('unknown', 'home', 'seedbox'));
