package sites

import (
	"context"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetDuplicateAdapter struct{ code string }

func (adapter fakeTargetDuplicateAdapter) SiteCode() string { return adapter.code }
func (adapter fakeTargetDuplicateAdapter) DuplicateCheck(_ context.Context, query TargetDuplicateQuery, _ workflow.Actor) (TargetDuplicateEvidence, error) {
	return TargetDuplicateEvidence{SiteCode: adapter.code, Query: query}, nil
}

func TestTargetDuplicateRegistryRoutesAndFailsClosed(t *testing.T) {
	registry, err := NewTargetDuplicateRegistry(fakeTargetDuplicateAdapter{code: "MTEAM"})
	if err != nil {
		t.Fatal(err)
	}
	result, err := registry.DuplicateCheck(context.Background(), "mteam", TargetDuplicateQuery{IMDbID: "tt1234567"}, workflow.Actor{})
	if err != nil || result.Query.IMDbID != "tt1234567" {
		t.Fatalf("DuplicateCheck() result/error = %#v/%v", result, err)
	}
	_, err = registry.DuplicateCheck(context.Background(), "TTG", TargetDuplicateQuery{}, workflow.Actor{})
	code, _, _ := ErrorDetails(err)
	if code != "target_duplicate_adapter_unavailable" {
		t.Fatalf("missing duplicate adapter error = %q/%v", code, err)
	}
}
