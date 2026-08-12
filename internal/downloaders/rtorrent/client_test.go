package rtorrent

import (
	"bytes"
	"context"
	"encoding/xml"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/downloaders/qbittorrent"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

type methodCall struct {
	XMLName xml.Name   `xml:"methodCall"`
	Method  string     `xml:"methodName"`
	Params  []xmlParam `xml:"params>param"`
}

type rtorrentFixture struct {
	testing  *testing.T
	mu       sync.Mutex
	hash     string
	name     string
	dir      string
	label    string
	throttle string
	started  bool
	methods  []string
	raw      []byte
	downMax  int64
	upMax    int64
}

func (fixture *rtorrentFixture) handler(w http.ResponseWriter, r *http.Request) {
	username, password, ok := r.BasicAuth()
	if !ok || username != "operator" || password != "secret" {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	var request methodCall
	if err := xml.NewDecoder(r.Body).Decode(&request); err != nil {
		http.Error(w, "invalid XML", http.StatusBadRequest)
		return
	}
	params := make([]any, len(request.Params))
	for index, item := range request.Params {
		decoded, err := decodeValue(item.Value)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		params[index] = decoded
	}
	fixture.mu.Lock()
	defer fixture.mu.Unlock()
	fixture.methods = append(fixture.methods, request.Method)
	var response any = int64(0)
	switch request.Method {
	case "system.client_version":
		response = "0.15.3"
	case "system.library_version":
		response = "0.15.3"
	case "system.methodExist":
		response = true
	case "load.raw":
		if len(params) != 2 || stringValue(params[0]) != "" {
			http.Error(w, "load.raw requires an empty target before metainfo", http.StatusBadRequest)
			return
		}
		fixture.raw, _ = params[1].([]byte)
	case "d.directory.set":
		fixture.dir = stringValue(params[1])
	case "d.custom1.set":
		fixture.label = stringValue(params[1])
	case "d.start":
		fixture.started = true
	case "throttle.down":
		fixture.downMax = parseFixtureThrottle(stringValue(params[1]))
	case "throttle.up":
		fixture.upMax = parseFixtureThrottle(stringValue(params[1]))
	case "d.throttle_name.set":
		fixture.throttle = stringValue(params[1])
	case "d.throttle_name":
		response = fixture.throttle
	case "throttle.down.max":
		response = fixture.downMax
	case "throttle.up.max":
		response = fixture.upMax
	case "system.multicall":
		response = fixture.multicall(params)
	case "d.multicall2":
		response = []any{fixture.snapshotRow()}
	case "f.multicall":
		response = []any{[]any{"release/video.mkv", int64(1024), int64(4), int64(4), int64(1)}}
	default:
		http.Error(w, "unexpected method", http.StatusBadRequest)
		return
	}
	writeRPCResponse(fixture.testing, w, response)
}

func (fixture *rtorrentFixture) multicall(params []any) []any {
	calls, _ := params[0].([]any)
	values := fixture.snapshotValues()
	result := make([]any, 0, len(calls))
	for _, rawCall := range calls {
		call, _ := rawCall.(map[string]any)
		result = append(result, []any{values[stringValue(call["methodName"])]})
	}
	return result
}

func (fixture *rtorrentFixture) snapshotValues() map[string]any {
	return map[string]any{
		"d.hash": fixture.hash, "d.name": fixture.name, "d.directory": fixture.dir,
		"d.custom1": fixture.label, "d.throttle_name": fixture.throttle, "d.message": "",
		"d.complete": int64(1), "d.state": int64(1), "d.is_active": int64(1),
		"d.is_multi_file": int64(0), "d.is_hash_checking": int64(0), "d.size_bytes": int64(1024),
		"d.completed_bytes": int64(1024), "d.left_bytes": int64(0), "d.down.rate": int64(0),
		"d.down.total": int64(1024), "d.up.rate": int64(128), "d.up.total": int64(2048),
		"d.ratio": int64(2000), "d.load_date": int64(100), "d.timestamp.started": int64(105), "d.timestamp.finished": int64(110),
	}

}

func (fixture *rtorrentFixture) snapshotRow() []any {
	values := fixture.snapshotValues()
	row := make([]any, 0, len(snapshotCalls))
	for _, method := range snapshotCalls {
		row = append(row, values[method])
	}
	return row
}

func TestClientProbeAddInspectFilesLimitsAndWait(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1024e4:name7:releaseee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &rtorrentFixture{testing: t, hash: strings.ToUpper(hashes.V1SHA1), name: "release"}
	server := httptest.NewServer(http.HandlerFunc(fixture.handler))
	t.Cleanup(server.Close)
	client, err := New(Config{
		Endpoint: server.URL, Credentials: map[string]string{"username": "operator", "password": "secret"},
		HTTPClient: &http.Client{Timeout: 5 * time.Second},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	probe, err := client.Probe(ctx)
	if err != nil || probe.ApplicationVersion != "0.15.3" || probe.WebAPIVersion != "XML-RPC (libtorrent 0.15.3)" || probe.Authentication != "basic" {
		t.Fatalf("Probe() result/error = %#v/%v", probe, err)
	}
	result, err := client.Add(ctx, metainfo, qbittorrent.AddOptions{
		SavePath: "/downloads/A&B", Category: "U2", Tags: []string{"retorrent", "U2"},
		DownloadLimit: 4096, UploadLimit: 8192,
	})
	if err != nil || result.Observed == nil {
		t.Fatalf("Add() result/error = %#v/%v", result, err)
	}
	if result.Observed.Hash != hashes.V1SHA1 || result.Observed.ContentPath != "/downloads/A&B/release" || result.Observed.Category != "U2" || result.Observed.Tags != "retorrent" {
		t.Fatalf("Add() observed = %#v", result.Observed)
	}
	if result.Observed.DownloadLimit != 4096 || result.Observed.UploadLimit != 8192 || result.Observed.State != "seeding" {
		t.Fatalf("Add() observed limits/state = %#v", result.Observed)
	}
	torrents, err := client.List(ctx)
	if err != nil || len(torrents) != 1 || torrents[0].Name != "release" {
		t.Fatalf("List() torrents/error = %#v/%v", torrents, err)
	}
	files, err := client.Files(ctx, hashes.V1SHA1)
	if err != nil || len(files) != 1 || files[0].Name != "release/video.mkv" || files[0].Size != 1024 || !files[0].Seed {
		t.Fatalf("Files() result/error = %#v/%v", files, err)
	}
	completed, err := client.WaitComplete(ctx, hashes.V1SHA1, time.Millisecond)
	if err != nil || completed.Progress != 1 || completed.Ratio != 2 {
		t.Fatalf("WaitComplete() result/error = %#v/%v", completed, err)
	}
	fixture.mu.Lock()
	defer fixture.mu.Unlock()
	if !bytes.Equal(fixture.raw, metainfo) || fixture.dir != "/downloads/A&B" || fixture.label != "U2,retorrent" || !fixture.started {
		t.Fatalf("remote fixture state raw=%t dir=%q label=%q started=%t", bytes.Equal(fixture.raw, metainfo), fixture.dir, fixture.label, fixture.started)
	}
	if !containsMethod(fixture.methods, "load.raw") || containsMethod(fixture.methods, "load.raw_start") {
		t.Fatalf("RPC methods = %#v", fixture.methods)
	}
}

func TestClientReturnsPartialAddWhenNamedThrottleIsIneffective(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request methodCall
		if err := xml.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "invalid", http.StatusBadRequest)
			return
		}
		response := any(int64(0))
		if request.Method == "throttle.down.max" || request.Method == "throttle.up.max" {
			response = int64(0)
		}
		writeRPCResponse(t, w, response)
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: 5 * time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{UploadLimit: 4096})
	var partial *PartialAddError
	if !errors.As(err, &partial) || partial.Hash != hashes.V1SHA1 || result.Hashes.V1SHA1 != hashes.V1SHA1 || !strings.Contains(err.Error(), "ineffective") {
		t.Fatalf("Add() result/error = %#v/%#v", result, err)
	}
}

func TestClientRejectsUnsafeConfigurationAndInputs(t *testing.T) {
	if _, err := New(Config{Endpoint: "http://operator:secret@localhost/RPC2"}); err == nil {
		t.Fatal("New() accepted endpoint credentials")
	}
	if _, err := New(Config{Endpoint: "http://localhost/RPC2", Credentials: map[string]string{"username": "only"}}); !errors.Is(err, qbittorrent.ErrUnauthorized) {
		t.Fatalf("New() partial credentials error = %v", err)
	}
	client, err := New(Config{Endpoint: "http://localhost/RPC2"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Get(context.Background(), "not-a-hash"); err == nil {
		t.Fatal("Get() accepted an invalid hash")
	}
	if err := client.SetLimits(context.Background(), strings.Repeat("a", 40), 1, 0); err == nil || !strings.Contains(err.Error(), "1024") {
		t.Fatalf("SetLimits() granularity error = %v", err)
	}
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	if _, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{SkipChecking: true}); err == nil || !strings.Contains(err.Error(), "skip_checking") {
		t.Fatalf("Add() skip_checking error = %v", err)
	}
}

func TestClientRejectsAmbiguousLabelsBeforeRemoteMutation(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		http.Error(w, "unexpected", http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	if _, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{Tags: []string{"ambiguous,label"}}); err == nil || !strings.Contains(err.Error(), "commas") {
		t.Fatalf("Add() ambiguous label error = %v", err)
	}
	if calls.Load() != 0 {
		t.Fatalf("Add() made %d remote calls before validating labels", calls.Load())
	}
}

func TestClientTreatsLoadFaultAsDuplicateOnlyAfterExactHashObservation(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod6:lengthi1e4:name4:testee")
	hashes, err := torrentmeta.Hashes(metainfo)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &rtorrentFixture{testing: t, hash: strings.ToUpper(hashes.V1SHA1), name: "test", dir: "/downloads"}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request methodCall
		if err := xml.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "invalid", http.StatusBadRequest)
			return
		}
		if request.Method == "load.raw" {
			writeRPCFault(t, w, -503, "Could not add torrent")
			return
		}
		params := make([]any, len(request.Params))
		for index, param := range request.Params {
			params[index], _ = decodeValue(param.Value)
		}
		fixture.mu.Lock()
		defer fixture.mu.Unlock()
		if request.Method == "system.multicall" {
			writeRPCResponse(t, w, fixture.multicall(params))
			return
		}
		if request.Method == "d.start" {
			fixture.started = true
			writeRPCResponse(t, w, int64(0))
			return
		}
		http.Error(w, "unexpected", http.StatusBadRequest)
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Add(context.Background(), metainfo, qbittorrent.AddOptions{})
	if err != nil || result.Observed == nil || result.Observed.Hash != hashes.V1SHA1 {
		t.Fatalf("Add() duplicate result/error = %#v/%v", result, err)
	}
}

func TestDecodeValueRejectsExcessiveNesting(t *testing.T) {
	value := xmlValue{String: &xmlText{Text: "leaf"}}
	for range 66 {
		value = xmlValue{Array: &xmlArray{Values: []xmlValue{value}}}
	}
	if _, err := decodeValue(value); err == nil || !strings.Contains(err.Error(), "64") {
		t.Fatalf("decodeValue() nesting error = %v", err)
	}
}

func TestNormalizeTorrentUsesDirectoryAsMultiFileContentRoot(t *testing.T) {
	now := time.Now().Unix()
	item := normalizeTorrent(torrentSnapshot{
		Hash: strings.Repeat("A", 40), Name: "release", Directory: "/downloads/release",
		Complete: 1, State: 1, Active: 1, MultiFile: 1, Size: 1024, Completed: 1024,
		StartedOn: now - 120, CompletionOn: now - 100,
	}, 0, 0)
	if item.SavePath != "/downloads" || item.ContentPath != "/downloads/release" || item.State != "seeding" || item.SeedingTime < 99 || item.SeedingTime > 101 {
		t.Fatalf("multi-file torrent = %#v", item)
	}
	paused := normalizeTorrent(torrentSnapshot{
		Hash: strings.Repeat("B", 40), Name: "release.mkv", Directory: "/downloads",
		State: 1, Active: 0, Size: 1024, Completed: 512, Left: 512,
	}, 0, 0)
	if paused.ContentPath != "/downloads/release.mkv" || paused.State != "paused" {
		t.Fatalf("paused single-file torrent = %#v", paused)
	}
}

func TestRPCRejectsDocumentDeclarationsAndMapsAuthentication(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") == "" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(`<?xml version="1.0"?><!DOCTYPE x [<!ENTITY secret "x">]><methodResponse><params><param><value><string>&secret;</string></value></param></params></methodResponse>`))
	}))
	t.Cleanup(server.Close)
	unauthenticated, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := unauthenticated.Probe(context.Background()); !errors.Is(err, qbittorrent.ErrUnauthorized) {
		t.Fatalf("Probe() unauthenticated error = %v", err)
	}
	authenticated, err := New(Config{
		Endpoint: server.URL, Credentials: map[string]string{"username": "operator", "password": "secret"},
		HTTPClient: &http.Client{Timeout: time.Second},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := authenticated.Probe(context.Background()); err == nil || !strings.Contains(err.Error(), "prohibited") {
		t.Fatalf("Probe() declaration error = %v", err)
	}
}

func TestRPCFaultMapsMissingTorrent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeRPCFault(t, w, -501, "Could not find info-hash")
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Get(context.Background(), strings.Repeat("a", 40)); !errors.Is(err, qbittorrent.ErrNotFound) {
		t.Fatalf("Get() missing error = %v", err)
	}
}

func TestProbeRejectsIncompleteXMLRPCEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request methodCall
		if err := xml.NewDecoder(r.Body).Decode(&request); err != nil {
			http.Error(w, "invalid", http.StatusBadRequest)
			return
		}
		response := any("0.15.3")
		if request.Method == "system.methodExist" {
			method, _ := decodeValue(request.Params[0].Value)
			response = method != "load.raw"
		}
		writeRPCResponse(t, w, response)
	}))
	t.Cleanup(server.Close)
	client, err := New(Config{Endpoint: server.URL, HTTPClient: &http.Client{Timeout: time.Second}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Probe(context.Background()); err == nil || !strings.Contains(err.Error(), "load.raw") {
		t.Fatalf("Probe() incomplete endpoint error = %v", err)
	}
}

func writeRPCResponse(t *testing.T, w http.ResponseWriter, value any) {
	t.Helper()
	var body bytes.Buffer
	body.WriteString(`<?xml version="1.0"?><methodResponse><params><param>`)
	if err := encodeValue(&body, value); err != nil {
		t.Fatal(err)
	}
	body.WriteString(`</param></params></methodResponse>`)
	w.Header().Set("Content-Type", "text/xml")
	_, _ = w.Write(body.Bytes())
}

func writeRPCFault(t *testing.T, w http.ResponseWriter, code int64, message string) {
	t.Helper()
	var body bytes.Buffer
	body.WriteString(`<?xml version="1.0"?><methodResponse><fault>`)
	if err := encodeValue(&body, map[string]any{"faultCode": code, "faultString": message}); err != nil {
		t.Fatal(err)
	}
	body.WriteString(`</fault></methodResponse>`)
	_, _ = w.Write(body.Bytes())
}

func parseFixtureThrottle(value string) int64 {
	value = strings.TrimSuffix(value, "K")
	parsed, _ := strconv.ParseInt(value, 10, 64)
	return parsed * 1024
}

func containsMethod(methods []string, target string) bool {
	for _, method := range methods {
		if method == target {
			return true
		}
	}
	return false
}
