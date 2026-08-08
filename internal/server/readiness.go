package server

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/readiness"
)

type LiveReadinessService interface {
	Check(context.Context, readiness.Input) (readiness.Report, error)
}

type liveReadinessAPI struct{ service LiveReadinessService }

func registerLiveReadinessRoutes(mux *http.ServeMux, service LiveReadinessService) {
	api := liveReadinessAPI{service: service}
	mux.HandleFunc("GET /api/v2/readiness/live", api.check)
}

func (api liveReadinessAPI) check(w http.ResponseWriter, r *http.Request) {
	if _, ok := requireScope(w, r, "config:read"); !ok {
		return
	}
	input := readiness.Input{
		Source: strings.TrimSpace(r.URL.Query().Get("source")), Target: strings.TrimSpace(r.URL.Query().Get("target")),
		Downloader: strings.TrimSpace(r.URL.Query().Get("downloader")), TargetDownloader: strings.TrimSpace(r.URL.Query().Get("target_downloader")),
		ImageHost: strings.TrimSpace(r.URL.Query().Get("image_host")), ScreenshotProfile: strings.TrimSpace(r.URL.Query().Get("screenshot_profile")),
		TMDbProvider: strings.TrimSpace(r.URL.Query().Get("tmdb_provider")), PTGenProvider: strings.TrimSpace(r.URL.Query().Get("ptgen_provider")),
	}
	report, err := api.service.Check(r.Context(), input)
	if err != nil {
		if errors.Is(err, readiness.ErrInvalid) {
			writeProblem(w, http.StatusBadRequest, "invalid_live_readiness_input", err.Error())
			return
		}
		writeProblem(w, http.StatusInternalServerError, "internal_error", "live readiness could not be evaluated")
		return
	}
	writeJSON(w, http.StatusOK, report)
}
