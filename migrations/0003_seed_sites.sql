INSERT INTO sites(code, name, adapter, enabled)
VALUES
    ('AUDIENCES', 'Audiences', 'nexusphp', true),
    ('CHD', 'CHDBits', 'nexusphp', true),
    ('HDS', 'HD-Space', 'nexusphp', true),
    ('HDSKY', 'HDSky', 'nexusphp', true),
    ('HHAN', 'HhanClub', 'nexusphp', true),
    ('MTEAM', 'M-Team', 'mteam_api', true),
    ('OB', 'OurBits', 'nexusphp', true),
    ('PTER', 'PterClub', 'nexusphp', true),
    ('TJUPT', 'TJUPT', 'nexusphp', true),
    ('TTG', 'ToTheGlory', 'ttg', true),
    ('U2', 'U2分享園@動漫花園', 'nexusphp', true)
ON CONFLICT (code) DO NOTHING;
