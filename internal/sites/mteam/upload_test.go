package mteam

import (
	"bytes"
	"context"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/sites"
	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

func TestClientUploadsBoundMTeamMultipartAndAuditsIntentResult(t *testing.T) {
	request := mteamUploadRequest(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/torrent/createOredit" || r.URL.RawQuery != "" || r.Header.Get("x-api-key") != "mteam-secret" {
			t.Fatalf("request path/query/key = %s/%s/%s", r.URL.Path, r.URL.RawQuery, r.Header.Get("x-api-key"))
		}
		if err := r.ParseMultipartForm(maxUploadRequestBytes); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = r.MultipartForm.RemoveAll() })
		expectedMedia, err := decodeMediaEvidence(request.Package.MediaInfo)
		if err != nil {
			t.Fatal(err)
		}
		if r.FormValue("name") != "Fixture 2026 1080p BluRay" || r.FormValue("category") != "419" ||
			r.FormValue("standard") != "1" || r.FormValue("anonymous") != "false" ||
			r.FormValue("descr") != request.Package.Description || r.FormValue("mediainfo") != expectedMedia ||
			r.FormValue("imdb") != "https://www.imdb.com/title/tt1234567/" {
			t.Fatalf("multipart fields = %#v", r.MultipartForm.Value)
		}
		file, header, err := r.FormFile("file")
		if err != nil {
			t.Fatal(err)
		}
		defer file.Close()
		body, _ := io.ReadAll(file)
		if header.Filename != "mteam-upload.torrent" || !bytes.Equal(body, request.Torrent) {
			t.Fatalf("torrent filename/body = %q/%d", header.Filename, len(body))
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"code": "0", "data": map[string]any{"id": 98765}})
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "mteam-secret")
	evidence, err := NewClient(store, nil).Upload(context.Background(), request, workflow.Actor{Type: "worker", ID: "fixture"})
	if err != nil {
		t.Fatal(err)
	}
	if evidence.TorrentID != "98765" || evidence.DetailsURL != "https://kp.m-team.cc/details/98765" ||
		len(evidence.ResponseSHA256) != 64 || len(store.actions) != 2 || store.actions[0] != "target.upload_intent" || store.actions[1] != "target.upload_result" {
		t.Fatalf("upload evidence/actions = %#v/%#v", evidence, store.actions)
	}
	encoded, _ := json.Marshal(map[string]any{"evidence": evidence, "audit": store.details})
	if strings.Contains(string(encoded), "mteam-secret") {
		t.Fatal("upload evidence or audit details exposed API key")
	}
}

func TestClientUploadRequiresConfirmationBeforeAnySiteAction(t *testing.T) {
	request := mteamUploadRequest(t)
	request.Confirmed = false
	store := runtimeSiteStore("https://api.m-team.cc", "key")
	_, err := NewClient(store, nil).Upload(context.Background(), request, workflow.Actor{})
	code, _, _ := sites.ErrorDetails(err)
	if code != "target_upload_request_invalid" || len(store.actions) != 0 {
		t.Fatalf("missing confirmation code/actions/error = %q/%#v/%v", code, store.actions, err)
	}
}

func TestClientUploadTreatsSuccessWithoutIDAsUnknownOutcome(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"code":0,"data":{}}`))
	}))
	defer server.Close()
	store := runtimeSiteStore(server.URL, "key")
	_, err := NewClient(store, nil).Upload(context.Background(), mteamUploadRequest(t), workflow.Actor{})
	code, _, _ := sites.ErrorDetails(err)
	if code != "target_upload_outcome_unknown" || len(store.actions) != 2 || store.actions[1] != "target.upload_outcome" {
		t.Fatalf("unknown outcome code/actions/error = %q/%#v/%v", code, store.actions, err)
	}
}

func TestDecodeMediaEvidenceAcceptsCurrentTextAndLegacyJSONObject(t *testing.T) {
	current, err := decodeMediaEvidence(json.RawMessage(`"DISC INFO:\nVideo: 1080p"`))
	if err != nil || current != "DISC INFO:\nVideo: 1080p" {
		t.Fatalf("current media evidence = %q/%v", current, err)
	}
	legacy, err := decodeMediaEvidence(json.RawMessage(`{"media":{"track":[]}}`))
	if err != nil || legacy != `{"media":{"track":[]}}` {
		t.Fatalf("legacy media evidence = %q/%v", legacy, err)
	}
	for _, invalid := range []json.RawMessage{nil, json.RawMessage(`null`), json.RawMessage(`"\u0000"`)} {
		if _, err := decodeMediaEvidence(invalid); err == nil {
			t.Fatalf("invalid media evidence accepted: %s", invalid)
		}
	}
}

func mteamUploadRequest(t *testing.T) sites.TargetUploadRequest {
	t.Helper()
	torrent := mteamUploadTorrent([]byte("abc"))
	inspection, err := torrentmeta.Inspect(torrent)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(torrent)
	return sites.TargetUploadRequest{
		JobID: "job-id", AttemptID: "attempt-id", Confirmed: true,
		Package: sites.PreparedTargetPackage{
			SchemaVersion: 1, Target: "MTEAM", Adapter: "mteam_api",
			FormFields: map[string]any{
				"name": "Fixture 2026 1080p BluRay", "smallDescr": "Fixture",
				"category": 419, "standard": 1, "anonymous": false,
				"imdb": "https://www.imdb.com/title/tt1234567/",
			},
			Description: "[b]Fixture description[/b]\n", MediaInfo: json.RawMessage(`{"media":{"track":[]}}`),
			Content: sites.TargetContentEvidence{FileCount: 1, TotalSizeBytes: 3}, GeneratedAt: time.Unix(1, 0).UTC(),
		},
		Torrent: torrent, PackageSHA256: strings.Repeat("1", 64), TorrentSHA256: hex.EncodeToString(digest[:]),
		ContentFingerprintSHA256: inspection.ContentFingerprint, RuleFingerprint: strings.Repeat("2", 64),
		DuplicateCheckSHA256: strings.Repeat("3", 64),
	}
}

func mteamUploadTorrent(content []byte) []byte {
	piece := sha1.Sum(content)
	info := uploadBencodeDict(map[string][]byte{
		"length": uploadBencodeInt(int64(len(content))), "name": uploadBencodeBytes([]byte("video.mkv")),
		"piece length": uploadBencodeInt(16384), "pieces": uploadBencodeBytes(piece[:]),
		"private": uploadBencodeInt(1), "source": uploadBencodeBytes([]byte("MTEAM")),
	})
	return uploadBencodeDict(map[string][]byte{
		"announce": uploadBencodeBytes([]byte("https://fake.tracker")), "info": info,
	})
}

func uploadBencodeBytes(value []byte) []byte {
	return append([]byte(strconv.Itoa(len(value))+":"), value...)
}
func uploadBencodeInt(value int64) []byte { return []byte("i" + strconv.FormatInt(value, 10) + "e") }
func uploadBencodeDict(values map[string][]byte) []byte {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := bytes.NewBufferString("d")
	for _, key := range keys {
		result.Write(uploadBencodeBytes([]byte(key)))
		result.Write(values[key])
	}
	result.WriteByte('e')
	return result.Bytes()
}
