package webui

import (
	"bytes"
	"embed"
	"io/fs"
	"mime"
	"net/http"
	"path/filepath"
	"strings"
	"time"
)

//go:embed dist
var embedded embed.FS

func Register(mux *http.ServeMux) {
	handler := Handler()
	mux.Handle("GET /{$}", handler)
	mux.Handle("GET /app", handler)
	mux.Handle("GET /app/", handler)
	mux.Handle("GET /assets/", handler)
	mux.Handle("GET /favicon.svg", handler)
}

func Handler() http.Handler {
	dist, err := fs.Sub(embedded, "dist")
	if err != nil {
		panic("embedded Web UI is unavailable: " + err.Error())
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asset := strings.TrimPrefix(r.URL.Path, "/")
		cacheControl := "public, max-age=31536000, immutable"
		if r.URL.Path == "/" || r.URL.Path == "/app" || strings.HasPrefix(r.URL.Path, "/app/") {
			asset = "index.html"
			cacheControl = "no-store"
		}
		if asset == "" || strings.HasSuffix(asset, "/") || strings.Contains(asset, "..") {
			http.NotFound(w, r)
			return
		}
		body, err := fs.ReadFile(dist, asset)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		contentType := mime.TypeByExtension(filepath.Ext(asset))
		if contentType == "" {
			contentType = "application/octet-stream"
		}
		w.Header().Set("Content-Type", contentType)
		w.Header().Set("Cache-Control", cacheControl)
		http.ServeContent(w, r, asset, time.Time{}, bytes.NewReader(body))
	})
}
