package qbittorrent

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

func TestClientCookieAuthenticationAddInspectAndLimits(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	added := false
	limits := map[string]int64{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Origin") == "" || r.Header.Get("Referer") == "" {
			http.Error(w, "origin required", http.StatusBadRequest)
			return
		}
		switch r.URL.Path {
		case "/api/v2/auth/login":
			_ = r.ParseForm()
			if r.Form.Get("username") != "admin" || r.Form.Get("password") != "secret" {
				http.Error(w, "Fails.", http.StatusForbidden)
				return
			}
			http.SetCookie(w, &http.Cookie{Name: "SID", Value: "test-session", Path: "/"})
			_, _ = io.WriteString(w, "Ok.")
			return
		}
		cookie, cookieErr := r.Cookie("SID")
		if cookieErr != nil || cookie.Value != "test-session" {
			http.Error(w, "Forbidden", http.StatusForbidden)
			return
		}
		switch r.URL.Path {
		case "/api/v2/app/version":
			_, _ = io.WriteString(w, "v5.2.0")
		case "/api/v2/app/webapiVersion":
			_, _ = io.WriteString(w, "2.14.1")
		case "/api/v2/torrents/add":
			if err := r.ParseMultipartForm(1 << 20); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			file, _, err := r.FormFile("torrents")
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			body, _ := io.ReadAll(file)
			_ = file.Close()
			if string(body) != string(metainfo) || r.FormValue("category") != "U2" || r.FormValue("tags") != "retorrent,source" {
				http.Error(w, "unexpected add form", http.StatusBadRequest)
				return
			}
			mu.Lock()
			added = true
			mu.Unlock()
			_, _ = io.WriteString(w, "Ok.")
		case "/api/v2/torrents/info":
			mu.Lock()
			isAdded := added
			mu.Unlock()
			if !isAdded {
				_, _ = io.WriteString(w, "[]")
				return
			}
			_ = json.NewEncoder(w).Encode([]Torrent{{
				Hash: hashes.V1SHA1, Name: "test", State: "uploading", Progress: 1,
				TotalSize: 1, Completed: 1, AmountLeft: 0, SavePath: "/downloads", ContentPath: "/downloads/test",
			}})
		case "/api/v2/torrents/files":
			_ = json.NewEncoder(w).Encode([]TorrentFile{{
				Index: 0, Name: "test", Size: 1, Progress: 1, Priority: 1, Seed: true, Availability: 1,
			}})
		case "/api/v2/torrents/setDownloadLimit", "/api/v2/torrents/setUploadLimit":
			_ = r.ParseForm()
			limit, _ := strconv.ParseInt(r.Form.Get("limit"), 10, 64)
			mu.Lock()
			limits[r.URL.Path] = limit
			mu.Unlock()
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	jar, _ := cookiejar.New(nil)
	httpClient := &http.Client{Timeout: 5 * time.Second, Jar: jar}
	client, err := New(Config{
		Endpoint: server.URL, Credentials: map[string]string{"username": "admin", "password": "secret"}, HTTPClient: httpClient,
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	probe, err := client.Probe(ctx)
	if err != nil || probe.ApplicationVersion != "v5.2.0" || probe.WebAPIVersion != "2.14.1" || probe.Authentication != "cookie" {
		t.Fatalf("Probe() result/error = %#v/%v", probe, err)
	}
	result, err := client.Add(ctx, metainfo, AddOptions{
		SavePath: "/downloads", Category: "U2", Tags: []string{"retorrent", "source"}, DownloadLimit: 1024, UploadLimit: 2048,
	})
	if err != nil || result.Observed == nil || result.Observed.Hash != hashes.V1SHA1 {
		t.Fatalf("Add() result/error = %#v/%v", result, err)
	}
	if err := client.SetLimits(ctx, hashes.V1SHA1, 4096, 8192); err != nil {
		t.Fatal(err)
	}
	files, err := client.Files(ctx, hashes.V1SHA1)
	if err != nil || len(files) != 1 || files[0].Name != "test" {
		t.Fatalf("Files() files/error = %#v/%v", files, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if limits["/api/v2/torrents/setDownloadLimit"] != 4096 || limits["/api/v2/torrents/setUploadLimit"] != 8192 {
		t.Fatalf("limits = %#v", limits)
	}
}

func TestClientAPIKeyAuthenticationDoesNotCallLogin(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v2/auth/login" {
			t.Error("API key client called login")
			http.Error(w, "unexpected", http.StatusInternalServerError)
			return
		}
		if r.Header.Get("Authorization") != "Bearer qbt_test-api-key" {
			http.Error(w, "missing API key", http.StatusForbidden)
			return
		}
		switch r.URL.Path {
		case "/api/v2/app/version":
			_, _ = io.WriteString(w, "v5.2.0")
		case "/api/v2/app/webapiVersion":
			_, _ = io.WriteString(w, "2.14.1")
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, Credentials: map[string]string{"api_key": "qbt_test-api-key"}})
	if err != nil {
		t.Fatal(err)
	}
	probe, err := client.Probe(context.Background())
	if err != nil || probe.Authentication != "api_key" {
		t.Fatalf("Probe() result/error = %#v/%v", probe, err)
	}
}

func TestClientRejectsInvalidHashesAndRedirects(t *testing.T) {
	client, err := New(Config{Endpoint: "http://localhost:8080", Credentials: map[string]string{"api_key": "qbt_test"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Get(context.Background(), "not-a-hash"); err == nil || !strings.Contains(err.Error(), "invalid") {
		t.Fatalf("Get() invalid hash error = %v", err)
	}
	if _, err := New(Config{Endpoint: "http://user:pass@localhost:8080"}); err == nil {
		t.Fatal("New() accepted endpoint credentials")
	}
}
