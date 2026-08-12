package imagehosts

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeConfigurationStore struct {
	runtime     integrations.RuntimeImageHost
	health      string
	actions     []string
	failAuditAt int
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
	if len(store.actions) == store.failAuditAt {
		return errors.New("fixture audit unavailable")
	}
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
	if evidence.Result.URL != "https://i.ibb.co/path/image.png" || evidence.Result.ViewerURL != "https://ibb.co/image-id" || store.health != "ready" ||
		len(store.actions) != 2 || store.actions[0] != "image.upload_intent" || store.actions[1] != "image.upload" {
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

func TestManagerUploadsPixhostWithoutCredentials(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/images" || r.URL.RawQuery != "" {
			t.Fatalf("pixhost request path/query = %q/%q", r.URL.Path, r.URL.RawQuery)
		}
		if err := r.ParseMultipartForm(12 << 20); err != nil {
			t.Fatal(err)
		}
		if r.FormValue("content_type") != "0" || r.FormValue("max_th_size") != "350" {
			t.Fatalf("pixhost fields = %#v", r.MultipartForm.Value)
		}
		file, header, err := r.FormFile("img")
		if err != nil {
			t.Fatal(err)
		}
		_ = file.Close()
		if header.Filename != "fixture.png" {
			t.Fatalf("pixhost filename = %q", header.Filename)
		}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"name": "fixture.png", "show_url": "https://pixhost.to/show/8582/563_fixture.png",
			"th_url": "https://t1.pixhost.to/thumbs/8582/563_fixture.png",
		})
	}))
	defer server.Close()
	store := runtimeStore("pixhost", server.URL+"/images", nil)
	evidence, err := NewManager(store, nil).Upload(context.Background(), "primary", fixtureImage(), workflow.Actor{Type: "test", ID: "image"})
	if err != nil {
		t.Fatal(err)
	}
	if evidence.Result.URL != "https://img1.pixhost.to/images/8582/563_fixture.png" ||
		evidence.Result.ViewerURL != "https://pixhost.to/show/8582/563_fixture.png" ||
		evidence.Result.ThumbnailURL != "https://t1.pixhost.to/thumbs/8582/563_fixture.png" || evidence.Result.RemoteID != "563_fixture.png" {
		t.Fatalf("pixhost upload evidence = %#v", evidence)
	}
	tampered := evidence
	tampered.Result.URL = "https://img2.pixhost.to/images/8582/563_fixture.png"
	if err := ValidateUploadEvidence(tampered, fixtureImage()); err == nil {
		t.Fatal("tampered Pixhost direct URL was accepted")
	}
}

func TestManagerUploadsImgboxWithEphemeralTokensAndNoAPIKey(t *testing.T) {
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestCount.Add(1)
		if r.UserAgent() != "Upload-Assistant/2" || r.URL.RawQuery != "" {
			t.Fatalf("imgbox request exposed unexpected user agent/query = %q/%q", r.UserAgent(), r.URL.RawQuery)
		}
		switch r.URL.Path {
		case "/":
			if r.Method != http.MethodGet {
				t.Fatalf("imgbox entry method = %s", r.Method)
			}
			http.SetCookie(w, &http.Cookie{Name: "imgbox-session", Value: "fixture-session", Path: "/", HttpOnly: true})
			_, _ = w.Write([]byte(`<html><head><meta content="fixture-csrf" name="csrf-token"></head></html>`))
		case "/ajax/token/generate":
			if r.Method != http.MethodPost || r.Header.Get("X-CSRF-Token") != "fixture-csrf" {
				t.Fatalf("imgbox token method/csrf = %s/%q", r.Method, r.Header.Get("X-CSRF-Token"))
			}
			cookie, err := r.Cookie("imgbox-session")
			if err != nil || cookie.Value != "fixture-session" {
				t.Fatalf("imgbox token cookie = %#v/%v", cookie, err)
			}
			if err := r.ParseForm(); err != nil {
				t.Fatal(err)
			}
			if r.FormValue("gallery") != "true" || r.FormValue("comments_enabled") != "0" {
				t.Fatalf("imgbox token fields = %#v", r.Form)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"token_id": 17, "token_secret": "token-secret", "gallery_id": nil, "gallery_secret": nil,
			})
		case "/upload/process":
			if r.Method != http.MethodPost || r.Header.Get("X-CSRF-Token") != "fixture-csrf" {
				t.Fatalf("imgbox upload method/csrf = %s/%q", r.Method, r.Header.Get("X-CSRF-Token"))
			}
			if err := r.ParseMultipartForm(12 << 20); err != nil {
				t.Fatal(err)
			}
			if r.FormValue("token_id") != "17" || r.FormValue("token_secret") != "token-secret" ||
				r.FormValue("gallery_id") != "null" || r.FormValue("gallery_secret") != "null" ||
				r.FormValue("content_type") != "1" || r.FormValue("thumbnail_size") != "100r" {
				t.Fatalf("imgbox upload fields = %#v", r.MultipartForm.Value)
			}
			file, _, err := r.FormFile("files[]")
			if err != nil {
				t.Fatal(err)
			}
			_ = file.Close()
			_ = json.NewEncoder(w).Encode(map[string]any{"files": []map[string]string{{
				"original_url": "https://images2.imgbox.com/aa/bb/original.png",
			}}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	store := runtimeStore("imgbox", server.URL, nil)
	evidence, err := NewManager(store, nil).Upload(context.Background(), "primary", fixtureImage(), workflow.Actor{Type: "test", ID: "image"})
	if err != nil {
		t.Fatal(err)
	}
	if requestCount.Load() != 3 || evidence.Result.URL != "https://images2.imgbox.com/aa/bb/original.png" ||
		evidence.Result.ViewerURL != "" || evidence.Result.ThumbnailURL != "" || evidence.Result.RemoteID != "original.png" {
		t.Fatalf("imgbox request count/evidence = %d/%#v", requestCount.Load(), evidence)
	}
	tampered := evidence
	tampered.Result.ViewerURL = "https://viewer.imgbox.com/AbCd1234"
	if err := ValidateUploadEvidence(tampered, fixtureImage()); err == nil {
		t.Fatal("tampered Imgbox viewer URL was accepted")
	}
}

func TestAnonymousImageHostsRejectOversizeImageBeforeIntentOrNetwork(t *testing.T) {
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { requestCount.Add(1) }))
	defer server.Close()
	image := fixtureImage()
	image.Bytes = make([]byte, maxAnonymousImageBytes+1)
	copy(image.Bytes, []byte("\x89PNG\r\n\x1a\n"))
	digest := sha256.Sum256(image.Bytes)
	image.SHA256 = hex.EncodeToString(digest[:])
	for _, adapter := range []string{"imgbox", "pixhost"} {
		store := runtimeStore(adapter, server.URL, nil)
		_, err := NewManager(store, nil).Upload(context.Background(), "primary", image, workflow.Actor{Type: "test", ID: "image"})
		if err == nil || len(store.actions) != 0 {
			t.Fatalf("%s oversize error/actions = %v/%#v", adapter, err, store.actions)
		}
	}
	if requestCount.Load() != 0 {
		t.Fatalf("oversize images performed %d external requests", requestCount.Load())
	}
}

func TestImgboxRejectsWebPBeforeIntentOrNetwork(t *testing.T) {
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { requestCount.Add(1) }))
	defer server.Close()
	body := []byte("RIFF\x04\x00\x00\x00WEBP")
	digest := sha256.Sum256(body)
	image := Image{Filename: "fixture.webp", MIMEType: "image/webp", Bytes: body, SHA256: hex.EncodeToString(digest[:])}
	store := runtimeStore("imgbox", server.URL, nil)
	_, err := NewManager(store, nil).Upload(context.Background(), "primary", image, workflow.Actor{Type: "test", ID: "image"})
	if err == nil || len(store.actions) != 0 || requestCount.Load() != 0 {
		t.Fatalf("imgbox WebP error/actions/requests = %v/%#v/%d", err, store.actions, requestCount.Load())
	}
}

func TestManagerReturnsKnownEvidenceWhenResultAuditFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"success": true, "status": 200,
			"data": map[string]any{
				"id": "image-id", "url": "https://i.ibb.co/path/image.png",
				"image": map[string]any{"url": "https://i.ibb.co/path/image.png"},
			},
		})
	}))
	defer server.Close()
	store := runtimeStore("imgbb", server.URL, map[string]string{"api_key": "imgbb-secret"})
	store.failAuditAt = 2
	evidence, err := NewManager(store, nil).Upload(context.Background(), "primary", fixtureImage(), workflow.Actor{Type: "test", ID: "image"})
	if !errors.Is(err, ErrUploadOutcomeUnknown) || evidence.Result.URL != "https://i.ibb.co/path/image.png" || len(store.actions) != 2 {
		t.Fatalf("partial image evidence/error/actions = %#v/%v/%#v", evidence, err, store.actions)
	}
}

func TestManagerClassifiesInvalidSuccessResponseAsUnknown(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true,"status":200}`))
	}))
	defer server.Close()
	store := runtimeStore("imgbb", server.URL, map[string]string{"api_key": "imgbb-secret"})
	_, err := NewManager(store, nil).Upload(context.Background(), "primary", fixtureImage(), workflow.Actor{Type: "test", ID: "image"})
	if !errors.Is(err, ErrUploadOutcomeUnknown) || len(store.actions) != 1 {
		t.Fatalf("invalid success response error/actions = %v/%#v", err, store.actions)
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
