package imagehosts

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeConfigurationStore struct {
	runtime integrations.RuntimeImageHost
	health  string
	actions []string
}

func (store *fakeConfigurationStore) GetRuntimeImageHost(context.Context, string) (integrations.RuntimeImageHost, error) {
	return store.runtime, nil
}
func (store *fakeConfigurationStore) RecordImageHostHealth(_ context.Context, _ string, status string, _ map[string]any, _ workflow.Actor) error {
	store.health = status
	return nil
}
func (store *fakeConfigurationStore) AuditImageHostAction(_ context.Context, _ string, action string, _ map[string]any, _ workflow.Actor) error {
	store.actions = append(store.actions, action)
	return nil
}

func TestManagerUploadsImgBBWithoutLeakingKeyIntoURL(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.RawQuery != "" {
			t.Fatalf("request query exposed credentials: %s", r.URL.RawQuery)
		}
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Fatal(err)
		}
		if r.FormValue("key") != "imgbb-secret" {
			t.Fatalf("key form field missing")
		}
		file, _, err := r.FormFile("image")
		if err != nil {
			t.Fatal(err)
		}
		_ = file.Close()
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true, "status": 200,
			"data": map[string]any{
				"id": "image-id", "url": "https://i.ibb.co/path/image.png",
				"url_viewer": "https://ibb.co/image-id",
				"image":      map[string]any{"url": "https://i.ibb.co/path/image.png"},
				"thumb":      map[string]any{"url": "https://i.ibb.co/path/thumb.png"},
			},
		})
	}))
	defer server.Close()
	store := runtimeStore("imgbb", server.URL, map[string]string{"api_key": "imgbb-secret"})
	evidence, err := NewManager(store, nil).Upload(context.Background(), "primary", fixtureImage(), workflow.Actor{Type: "test", ID: "image"})
	if err != nil {
		t.Fatal(err)
	}
	if evidence.Result.URL != "https://i.ibb.co/path/image.png" || evidence.Result.ViewerURL != "https://ibb.co/image-id" || store.health != "ready" || len(store.actions) != 1 {
		t.Fatalf("upload evidence/store = %#v/%#v", evidence, store)
	}
}

func TestManagerUploadsPTPImgContract(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Fatal(err)
		}
		if r.FormValue("api_key") != "ptp-secret" || r.FormValue("format") != "json" {
			t.Fatalf("ptpimg fields = %#v", r.MultipartForm.Value)
		}
		file, _, err := r.FormFile("file-upload[0]")
		if err != nil {
			t.Fatal(err)
		}
		_ = file.Close()
		_ = json.NewEncoder(w).Encode([]map[string]string{{"code": "abc123", "ext": "png"}})
	}))
	defer server.Close()
	store := runtimeStore("ptpimg", server.URL, map[string]string{"api_key": "ptp-secret"})
	evidence, err := NewManager(store, nil).Upload(context.Background(), "primary", fixtureImage(), workflow.Actor{Type: "test", ID: "image"})
	if err != nil {
		t.Fatal(err)
	}
	if evidence.Result.URL != "https://ptpimg.me/abc123.png" || evidence.Result.RemoteID != "abc123" {
		t.Fatalf("upload evidence = %#v", evidence)
	}
}

func runtimeStore(adapter, endpoint string, credentials map[string]string) *fakeConfigurationStore {
	return &fakeConfigurationStore{runtime: integrations.RuntimeImageHost{
		ImageHost: integrations.ImageHost{
			ID: "host-id", Name: "primary", Adapter: adapter, Enabled: true,
			Config:    json.RawMessage(`{"endpoint":"` + endpoint + `","timeout_seconds":30}`),
			UpdatedAt: time.Unix(1, 0).UTC(),
		},
		EndpointConfig: integrations.EndpointConfig{Endpoint: endpoint, TimeoutSeconds: 30},
		Credentials:    credentials,
	}}
}

func fixtureImage() Image {
	body := []byte("\x89PNG\r\n\x1a\nfixture")
	digest := sha256.Sum256(body)
	return Image{Filename: "fixture.png", MIMEType: "image/png", Bytes: body, SHA256: hex.EncodeToString(digest[:])}
}
