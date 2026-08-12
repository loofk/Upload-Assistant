package server

import (
	"bytes"
	"context"
	"fmt"
	"image/png"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/security"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type fakeImageHostProbeService struct {
	calls int
	image imagehosts.Image
}

func TestImageHostProbePreservesSafeFailureStage(t *testing.T) {
	response := httptest.NewRecorder()
	writeImageHostProbeError(response, fmt.Errorf(
		"imgbox upload request failed: %w: image host returned HTTP 500",
		imagehosts.ErrUploadOutcomeUnknown,
	))
	if response.Code != http.StatusConflict ||
		!strings.Contains(response.Body.String(), "imgbox upload request failed") ||
		!strings.Contains(response.Body.String(), "HTTP 500") {
		t.Fatalf("probe failure = %d body=%s", response.Code, response.Body.String())
	}
}

func (service *fakeImageHostProbeService) Upload(_ context.Context, _ string, image imagehosts.Image, _ workflow.Actor) (imagehosts.UploadEvidence, error) {
	service.calls++
	service.image = image
	return imagehosts.UploadEvidence{
		ImageHostName: "images", Adapter: "imgbb", ConfigurationTime: time.Now(),
		SourceSHA256: image.SHA256, Result: imagehosts.UploadResult{URL: "https://i.ibb.co/probe/test.png"},
	}, nil
}

func TestImageHostProbeRequiresConfirmationBeforeUpload(t *testing.T) {
	service := &fakeImageHostProbeService{}
	handler := New(Dependencies{
		Database: fakeDatabase{}, DataDir: t.TempDir(), ImageHosts: service,
		Auth: fakeAuthenticator{principal: security.Principal{UserID: "2cbfe1ba-d85c-4ab8-b529-50cdacb87a03", Role: "admin", TokenScopes: []string{"*"}}},
	})

	request := httptest.NewRequest(http.MethodPost, "/api/v2/image-hosts/images/probe", bytes.NewBufferString(`{"confirm_upload":false}`))
	request.Header.Set("Authorization", "Bearer token")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest || service.calls != 0 {
		t.Fatalf("unconfirmed probe = %d calls=%d body=%s", response.Code, service.calls, response.Body.String())
	}

	request = httptest.NewRequest(http.MethodPost, "/api/v2/image-hosts/images/probe", bytes.NewBufferString(`{"confirm_upload":true}`))
	request.Header.Set("Authorization", "Bearer token")
	request.Header.Set("Content-Type", "application/json")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || service.calls != 1 || service.image.MIMEType != "image/png" || service.image.SHA256 == "" {
		t.Fatalf("confirmed probe = %d calls=%d image=%#v body=%s", response.Code, service.calls, service.image, response.Body.String())
	}
	imageConfig, err := png.DecodeConfig(bytes.NewReader(service.image.Bytes))
	if err != nil || imageConfig.Width != 100 || imageConfig.Height != 100 {
		t.Fatalf("probe image config = %#v, %v", imageConfig, err)
	}
}
