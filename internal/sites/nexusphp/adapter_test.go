package nexusphp

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/integrations"
	"github.com/loofk/upload-assistant/v2/internal/sites"
)

type fakeSiteProvider struct{ runtime integrations.RuntimeSite }

func (provider fakeSiteProvider) GetRuntimeSite(_ context.Context, code string) (integrations.RuntimeSite, error) {
	result := provider.runtime
	result.Code = code
	if result.Adapter == "" {
		result.Adapter = "nexusphp"
	}
	return result, nil
}

func TestAdapterInspectsDetailsWithoutExposingCredentials(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/details.php" || r.URL.Query().Get("id") != "60635" {
			t.Fatalf("unexpected request %s?%s", r.URL.Path, r.URL.RawQuery)
		}
		if cookie, err := r.Cookie("session"); err != nil || cookie.Value != "cookie-value" {
			t.Fatalf("cookie = %#v/%v", cookie, err)
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, `<!doctype html><html><head>
			<meta property="og:title" content="Fixture Anime 2026 1080p">
		</head><body><div id="kdescr">fixture <b>description</b></div>
		<a href="https://www.imdb.com/title/tt1234567/">IMDb</a>
		<a href="https://www.themoviedb.org/tv/9876">TMDb</a>
		<a href="https://movie.douban.com/subject/2345678/">豆瓣</a>
		<a href="https://anidb.net/anime/3456">AniDB</a>
		<span class="free">免費</span> Info Hash: 0123456789abcdef0123456789abcdef01234567
		</body></html>`)
	}))
	defer server.Close()

	adapter := newTestAdapter(t, server.URL, map[string]string{"cookie": "session=cookie-value"})
	result, err := adapter.Inspect(context.Background(), sites.SourceReference{Tracker: "U2", TorrentID: "60635"})
	if err != nil {
		t.Fatalf("Inspect() error = %v", err)
	}
	if result.Name != "Fixture Anime 2026 1080p" || result.IMDbID != "tt1234567" ||
		result.TMDbID != "9876" || result.TMDbType != "tv" || result.DoubanID != "2345678" ||
		result.AniDBID != "3456" || !result.Free || result.DescriptionLength != 19 {
		t.Fatalf("Inspect() = %#v", result)
	}
	if strings.Contains(result.DetailsURL+result.Name, "cookie-value") {
		t.Fatal("inspection result exposed credential")
	}
}

func TestAdapterDownloadsAndHashesTorrent(t *testing.T) {
	metainfo := []byte("d8:announce14:https://t.test4:infod4:name7:fixtureee")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/download.php" || r.URL.Query().Get("id") != "7" || r.URL.Query().Get("passkey") != "private-passkey" {
			t.Fatalf("unexpected download request %s?%s", r.URL.Path, r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/x-bittorrent")
		w.Header().Set("Content-Disposition", `attachment; filename="source-7.torrent"`)
		_, _ = w.Write(metainfo)
	}))
	defer server.Close()

	adapter := newTestAdapter(t, server.URL, map[string]string{"passkey": "private-passkey"})
	result, err := adapter.Download(context.Background(), sites.SourceReference{Tracker: "U2", TorrentID: "7"})
	if err != nil {
		t.Fatalf("Download() error = %v", err)
	}
	if result.Filename != "U2-7.torrent" || result.SizeBytes != int64(len(metainfo)) ||
		result.SHA256 == "" || result.Hashes.V1SHA1 == "" || string(result.Bytes) != string(metainfo) {
		t.Fatalf("Download() = %#v", result)
	}
}

func TestAdapterListsCandidateRowsWithPromotionAndDownloadability(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/torrents.php" || r.URL.Query().Get("page") != "0" {
			t.Fatalf("unexpected listing request %s?%s", r.URL.Path, r.URL.RawQuery)
		}
		if cookie, err := r.Cookie("session"); err != nil || cookie.Value != "cookie-value" {
			t.Fatalf("cookie = %#v/%v", cookie, err)
		}
		w.Header().Set("Content-Type", "text/html")
		_, _ = io.WriteString(w, `<!doctype html><table>
			<tr data-size-bytes="5368709120"><td><a href="details.php?id=101&amp;hit=1"><b>Fixture Anime S01</b></a></td><td>FREE</td><td><time datetime="2026-08-08T06:30:00Z"></time></td></tr>
			<tr><td><a href="/details.php?id=102">Fixture Movie 2026</a></td><td>2.5 GiB</td><td>2026-08-08 12:30</td></tr>
		</table>`)
	}))
	defer server.Close()

	adapter := newTestAdapter(t, server.URL, map[string]string{"cookie": "session=cookie-value", "passkey": "private"})
	result, err := adapter.ListCandidates(context.Background(), sites.CandidateScanRequest{Limit: 10, Page: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Items) != 2 || result.Items[0].TorrentID != "101" || result.Items[0].Title != "Fixture Anime S01" ||
		result.Items[0].SizeBytes != 5368709120 || !result.Items[0].Free || !result.Items[0].Downloadable ||
		result.Items[1].SizeBytes != 2684354560 || result.Items[1].PublishedAt == nil {
		t.Fatalf("candidate scan = %#v", result)
	}
	if strings.Contains(result.Items[0].DetailsURL, "private") {
		t.Fatal("candidate details URL exposed passkey")
	}
}

func TestAdapterBlocksLoginAndSanitizesPasskeyRequestFailure(t *testing.T) {
	loginServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = io.WriteString(w, `<html><form action="login.php"><input name="username"><input name="password"></form></html>`)
	}))
	defer loginServer.Close()
	adapter := newTestAdapter(t, loginServer.URL, map[string]string{"cookie": "session=expired"})
	_, err := adapter.Inspect(context.Background(), sites.SourceReference{Tracker: "U2", TorrentID: "1"})
	assertAdapterCode(t, err, "site_authentication_failed")

	closedServer := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	closedURL := closedServer.URL
	closedServer.Close()
	adapter = newTestAdapter(t, closedURL, map[string]string{"passkey": "do-not-leak"})
	_, err = adapter.Download(context.Background(), sites.SourceReference{Tracker: "U2", TorrentID: "1"})
	assertAdapterCode(t, err, "source_request_failed")
	if strings.Contains(err.Error(), "do-not-leak") {
		t.Fatal("request error exposed passkey")
	}
}

func TestParseCookieSecretSupportsNetscapeAndHeaderFormats(t *testing.T) {
	cookies := parseCookieSecret("# Netscape\n#HttpOnly_.example.test\tTRUE\t/\tTRUE\t0\tnexus\tvalue\nsession=abc; uid=42")
	got := map[string]string{}
	for _, cookie := range cookies {
		got[cookie.Name] = cookie.Value
	}
	if got["nexus"] != "value" || got["session"] != "abc" || got["uid"] != "42" {
		t.Fatalf("cookies = %#v", got)
	}
}

func newTestAdapter(t *testing.T, baseURL string, credentials map[string]string) *Adapter {
	t.Helper()
	adapter, err := New(Profile{SiteCode: "U2", BaseURL: baseURL}, fakeSiteProvider{
		runtime: integrations.RuntimeSite{Adapter: "nexusphp", Credentials: credentials},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return adapter
}

func assertAdapterCode(t *testing.T, err error, expected string) {
	t.Helper()
	code, _, _ := sites.ErrorDetails(err)
	if code != expected {
		t.Fatalf("error code = %q (%v), want %q", code, err, expected)
	}
}
