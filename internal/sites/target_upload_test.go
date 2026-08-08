package sites

import (
	"context"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeTargetUploadAdapter struct{ code string }

func (adapter fakeTargetUploadAdapter) SiteCode() string { return adapter.code }
func (adapter fakeTargetUploadAdapter) Upload(_ context.Context, request TargetUploadRequest, _ workflow.Actor) (TargetUploadEvidence, error) {
	return TargetUploadEvidence{SiteCode: adapter.code, TorrentID: request.JobID}, nil
}

func TestTargetUploadRegistryRoutesAndFailsClosed(t *testing.T) {
	registry, err := NewTargetUploadRegistry(fakeTargetUploadAdapter{code: "MTEAM"})
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := registry.Upload(context.Background(), "mteam", TargetUploadRequest{JobID: "job"}, workflow.Actor{})
	if err != nil || evidence.TorrentID != "job" {
		t.Fatalf("Upload() evidence/error = %#v/%v", evidence, err)
	}
	_, err = registry.Upload(context.Background(), "TTG", TargetUploadRequest{}, workflow.Actor{})
	code, _, _ := ErrorDetails(err)
	if code != "target_upload_adapter_unavailable" {
		t.Fatalf("missing adapter code/error = %q/%v", code, err)
	}
}
