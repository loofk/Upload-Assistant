package artifacts

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

type LocalStore struct {
	root string
}

type Scope struct {
	JobID     string
	StepID    string
	AttemptID string
}

type File struct {
	RelativePath string
	Filename     string
	SizeBytes    int64
	SHA256       string
}

func NewLocalStore(dataDir string) (*LocalStore, error) {
	root := filepath.Join(dataDir, "artifacts")
	if !filepath.IsAbs(root) {
		return nil, fmt.Errorf("artifact root must be absolute")
	}
	if err := os.MkdirAll(root, 0o750); err != nil {
		return nil, fmt.Errorf("create artifact root: %w", err)
	}
	return &LocalStore{root: root}, nil
}

func (s *LocalStore) Write(ctx context.Context, scope Scope, filename string, source io.Reader) (File, error) {
	if err := validateScope(scope); err != nil {
		return File{}, err
	}
	filename, err := safeFilename(filename)
	if err != nil {
		return File{}, err
	}
	directory := filepath.Join(s.root, scope.JobID, scope.StepID, scope.AttemptID)
	if err := os.MkdirAll(directory, 0o750); err != nil {
		return File{}, fmt.Errorf("create artifact directory: %w", err)
	}
	temporary, err := os.CreateTemp(directory, ".upload-*")
	if err != nil {
		return File{}, fmt.Errorf("create artifact temporary file: %w", err)
	}
	temporaryPath := temporary.Name()
	keepTemporary := true
	defer func() {
		_ = temporary.Close()
		if keepTemporary {
			_ = os.Remove(temporaryPath)
		}
	}()
	if err := temporary.Chmod(0o640); err != nil {
		return File{}, fmt.Errorf("set artifact permissions: %w", err)
	}
	hasher := sha256.New()
	size, err := copyContext(ctx, io.MultiWriter(temporary, hasher), source)
	if err != nil {
		return File{}, fmt.Errorf("write artifact: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		return File{}, fmt.Errorf("sync artifact: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return File{}, fmt.Errorf("close artifact: %w", err)
	}
	digest := hex.EncodeToString(hasher.Sum(nil))
	finalName := digest + "-" + filename
	finalPath := filepath.Join(directory, finalName)
	if _, statErr := os.Stat(finalPath); statErr == nil {
		if err := os.Remove(temporaryPath); err != nil {
			return File{}, fmt.Errorf("remove duplicate artifact temporary file: %w", err)
		}
	} else if !os.IsNotExist(statErr) {
		return File{}, fmt.Errorf("inspect artifact destination: %w", statErr)
	} else if err := os.Rename(temporaryPath, finalPath); err != nil {
		return File{}, fmt.Errorf("commit artifact: %w", err)
	}
	keepTemporary = false
	relative, err := filepath.Rel(s.root, finalPath)
	if err != nil {
		return File{}, fmt.Errorf("resolve artifact relative path: %w", err)
	}
	return File{
		RelativePath: filepath.ToSlash(relative), Filename: filename, SizeBytes: size, SHA256: digest,
	}, nil
}

func (s *LocalStore) Open(relativePath string) (*os.File, error) {
	cleaned := filepath.Clean(filepath.FromSlash(relativePath))
	if cleaned == "." || filepath.IsAbs(cleaned) || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) || cleaned == ".." {
		return nil, fmt.Errorf("invalid artifact path")
	}
	path := filepath.Join(s.root, cleaned)
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open artifact: %w", err)
	}
	return file, nil
}

func validateScope(scope Scope) error {
	for name, value := range map[string]string{"job_id": scope.JobID, "step_id": scope.StepID, "attempt_id": scope.AttemptID} {
		if strings.TrimSpace(value) == "" || strings.ContainsAny(value, `/\\`) || value == "." || value == ".." {
			return fmt.Errorf("invalid artifact %s", name)
		}
	}
	return nil
}

func safeFilename(value string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" || trimmed == "." || trimmed == ".." || filepath.Base(trimmed) != trimmed || strings.ContainsAny(trimmed, `/\\`) {
		return "", fmt.Errorf("invalid artifact filename")
	}
	return trimmed, nil
}

func copyContext(ctx context.Context, destination io.Writer, source io.Reader) (int64, error) {
	buffer := make([]byte, 128*1024)
	var total int64
	for {
		select {
		case <-ctx.Done():
			return total, ctx.Err()
		default:
		}
		read, readErr := source.Read(buffer)
		if read > 0 {
			written, writeErr := destination.Write(buffer[:read])
			total += int64(written)
			if writeErr != nil {
				return total, writeErr
			}
			if written != read {
				return total, io.ErrShortWrite
			}
		}
		if readErr != nil {
			if readErr == io.EOF {
				return total, nil
			}
			return total, readErr
		}
	}
}
