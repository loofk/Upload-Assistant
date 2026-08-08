package migrations

import "embed"

// Files contains the immutable, ordered PostgreSQL migrations.
//
//go:embed *.sql
var Files embed.FS
