package legacy

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"
)

type archiveDocument struct {
	SchemaVersion     int           `json:"schema_version"`
	CreatedAt         time.Time     `json:"created_at"`
	SourceFingerprint string        `json:"source_fingerprint"`
	Files             []archiveFile `json:"files"`
}

type archiveFile struct {
	Path      string `json:"path"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
	Content   []byte `json:"content_base64"`
}

func buildArchive(plan Plan, createdAt time.Time) ([]byte, error) {
	document := archiveDocument{
		SchemaVersion: 1, CreatedAt: createdAt.UTC(), SourceFingerprint: plan.SourceFingerprint,
		Files: make([]archiveFile, 0, len(plan.files)),
	}
	for _, file := range plan.files {
		digest := sha256.Sum256(file.body)
		document.Files = append(document.Files, archiveFile{
			Path: file.path, SHA256: hex.EncodeToString(digest[:]), SizeBytes: int64(len(file.body)),
			Content: append([]byte(nil), file.body...),
		})
	}
	body, err := json.Marshal(document)
	if err != nil {
		return nil, err
	}
	return body, nil
}
