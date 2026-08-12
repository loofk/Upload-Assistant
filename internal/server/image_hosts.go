package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"net/http"
	"net/url"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/imagehosts"
	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

type ImageHostProbeService interface {
	Upload(context.Context, string, imagehosts.Image, workflow.Actor) (imagehosts.UploadEvidence, error)
}

type imageHostAPI struct{ service ImageHostProbeService }

type imageHostProbeRequest struct {
	ConfirmUpload bool `json:"confirm_upload"`
}

func registerImageHostRoutes(mux *http.ServeMux, service ImageHostProbeService) {
	api := imageHostAPI{service: service}
	mux.HandleFunc("POST /api/v2/image-hosts/{name}/probe", api.probe)
}

func (api imageHostAPI) probe(w http.ResponseWriter, r *http.Request) {
	principal, ok := requireScope(w, r, "config:manage")
	if !ok {
		return
	}
	var request imageHostProbeRequest
	if err := decodeJSON(w, r, &request); err != nil {
		writeProblem(w, http.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if !request.ConfirmUpload {
		writeProblem(w, http.StatusBadRequest, "image_host_probe_confirmation_required", "confirm_upload=true is required because this test creates a remote image")
		return
	}
	probeImage, err := buildImageHostProbeImage()
	if err != nil {
		writeProblem(w, http.StatusInternalServerError, "image_host_probe_unavailable", "the built-in probe image is unavailable")
		return
	}
	evidence, err := api.service.Upload(r.Context(), strings.TrimSpace(r.PathValue("name")), probeImage, workflow.Actor{Type: "user", ID: principal.UserID})
	if err != nil {
		writeImageHostProbeError(w, err)
		return
	}
	host := ""
	if parsed, parseErr := url.Parse(evidence.Result.URL); parseErr == nil {
		host = parsed.Hostname()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "status": "ready", "image_host": evidence.ImageHostName,
		"probe": map[string]any{
			"adapter": evidence.Adapter, "created_remote_content": true,
			"remote_host": host, "source_sha256": evidence.SourceSHA256,
		},
		"blockers": []any{}, "next_actions": []any{},
	})
}

// buildImageHostProbeImage uses a small but normally processable image instead
// of a 1x1 PNG. Anonymous image hosts commonly run the upload through thumbnail
// pipelines, and Imgbox currently rejects the otherwise valid 1x1 fixture while
// accepting ordinary images. Keep the generated content deterministic and free
// of user data so the remote write remains safe to identify and audit.
func buildImageHostProbeImage() (imagehosts.Image, error) {
	canvas := image.NewRGBA(image.Rect(0, 0, 100, 100))
	draw.Draw(canvas, canvas.Bounds(), &image.Uniform{C: color.RGBA{R: 42, G: 111, B: 151, A: 255}}, image.Point{}, draw.Src)
	var body bytes.Buffer
	if err := png.Encode(&body, canvas); err != nil {
		return imagehosts.Image{}, err
	}
	digest := sha256.Sum256(body.Bytes())
	return imagehosts.Image{
		Filename: "upload-assistant-connection-test.png",
		MIMEType: "image/png",
		Bytes:    body.Bytes(),
		SHA256:   hex.EncodeToString(digest[:]),
	}, nil
}

func writeImageHostProbeError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, integrations.ErrNotFound):
		writeProblem(w, http.StatusNotFound, "image_host_not_found", "the requested image host was not found")
	case errors.Is(err, integrations.ErrValidation), errors.Is(err, imagehosts.ErrAdapterUnavailable):
		writeProblem(w, http.StatusBadRequest, "invalid_image_host_probe", err.Error())
	case errors.Is(err, imagehosts.ErrUploadOutcomeUnknown):
		writeProblem(w, http.StatusConflict, "image_host_probe_outcome_unknown", imagehosts.SafeErrorDetail(err))
	default:
		writeProblem(w, http.StatusBadGateway, "image_host_probe_failed", imagehosts.SafeErrorDetail(err))
	}
}
