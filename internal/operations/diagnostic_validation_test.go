package operations

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/loofk/upload-assistant/v2/internal/security"
)

func TestCreateDiagnosticRejectsShortCorrelationPrefixBeforeDatabaseAccess(t *testing.T) {
	_, err := (&Store{}).CreateDiagnostic(context.Background(), uuid.NewString(), "", "4201f48c", security.Principal{})
	if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), "incident_id must be a full UUID") {
		t.Fatalf("CreateDiagnostic error = %v", err)
	}
}

func TestCreateDiagnosticRequiresExactlyOneTargetBeforeDatabaseAccess(t *testing.T) {
	_, err := (&Store{}).CreateDiagnosticForTarget(context.Background(), uuid.NewString(), uuid.NewString(), "", 42, security.Principal{})
	if !errors.Is(err, ErrInvalid) || !strings.Contains(err.Error(), "exactly one") {
		t.Fatalf("CreateDiagnosticForTarget error = %v", err)
	}
}
