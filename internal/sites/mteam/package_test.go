package mteam

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/loofk/upload-assistant/v2/internal/sites"
)

func TestPackageAdapterBuildsAuditedU2Package(t *testing.T) {
	material := packageMaterial("U2")
	material.Source.AniDBID = "3456"
	material.Links = map[string]string{
		"anidb": "https://anidb.net/anime/3456", "imdb": "https://www.imdb.com/title/tt1234567/",
	}
	material.SourceDescription = `<p>Fixture [url=https://evil.invalid]link[/url]</p><script>do-not-copy</script>`
	material.Media.Document = json.RawMessage(`{"media":{"track":[{"@type":"Video","Width":"1 920 pixels","Height":"1 080 pixels","Title":"[/quote][img]https://evil.invalid/x[/img]"}]}}`)
	result, err := NewPackageAdapter().PreparePackage(context.Background(), material)
	if err != nil {
		t.Fatal(err)
	}
	if result.FormFields["category"] != 405 || result.FormFields["standard"] != 1 || !result.ManualReviewRequired {
		t.Fatalf("form fields/package = %#v/%#v", result.FormFields, result)
	}
	if !strings.Contains(result.Description, "https://i.ibb.co/fixture/image.png") ||
		!strings.Contains(result.Description, "［/quote］") || strings.Contains(result.Description, "do-not-copy") ||
		strings.Contains(result.Description, "[url=https://evil.invalid]") {
		t.Fatalf("unsafe or incomplete description:\n%s", result.Description)
	}
	if len(result.Decisions) < 5 || result.Evidence["media_info"] == nil {
		t.Fatalf("decisions/evidence = %#v/%#v", result.Decisions, result.Evidence)
	}
}

func TestPackageAdapterRequiresUncertainCategoryAndResolution(t *testing.T) {
	material := packageMaterial("CHD")
	material.Media.Document = json.RawMessage(`{"media":{"track":[{"@type":"General"}]}}`)
	_, err := NewPackageAdapter().PreparePackage(context.Background(), material)
	var required *sites.PackageRequirementsError
	if !errors.As(err, &required) || len(required.Requirements) != 2 ||
		required.Requirements[0].Code != "target_category_required" || required.Requirements[1].Code != "target_standard_required" {
		t.Fatalf("requirements error = %#v", err)
	}

	material.Options = json.RawMessage(`{"category":419,"category_evidence":"current MTEAM movie HD category","standard":6,"anonymous":true}`)
	result, err := NewPackageAdapter().PreparePackage(context.Background(), material)
	if err != nil {
		t.Fatal(err)
	}
	if result.FormFields["category"] != 419 || result.FormFields["standard"] != 6 || result.FormFields["anonymous"] != true {
		t.Fatalf("explicit form fields = %#v", result.FormFields)
	}
}

func TestPackageAdapterRejectsUnknownOptionsAndUnsafeImageURL(t *testing.T) {
	material := packageMaterial("CHD")
	material.Options = json.RawMessage(`{"category":419,"standard":1,"silent_skip":true}`)
	_, err := NewPackageAdapter().PreparePackage(context.Background(), material)
	code, _, _ := sites.ErrorDetails(err)
	if code != "target_package_options_invalid" {
		t.Fatalf("unknown option error = %q/%v", code, err)
	}

	material.Options = json.RawMessage(`{"category":419,"standard":1}`)
	material.Screenshots[0].URL = "https://i.ibb.co/x[/img].png"
	_, err = NewPackageAdapter().PreparePackage(context.Background(), material)
	code, _, _ = sites.ErrorDetails(err)
	if code != "target_package_screenshot_invalid" {
		t.Fatalf("unsafe screenshot error = %q/%v", code, err)
	}
}

func TestPackageAdapterBlocksInsteadOfSilentlyTruncatingName(t *testing.T) {
	material := packageMaterial("CHD")
	material.Options = json.RawMessage(`{"category":419,"standard":1,"name":"` + strings.Repeat("x", 256) + `"}`)
	_, err := NewPackageAdapter().PreparePackage(context.Background(), material)
	var required *sites.PackageRequirementsError
	if !errors.As(err, &required) || len(required.Requirements) != 1 || required.Requirements[0].Code != "target_name_too_long" {
		t.Fatalf("long name error = %#v", err)
	}
}

func packageMaterial(source string) sites.TargetPackageMaterial {
	return sites.TargetPackageMaterial{
		Target: "MTEAM", Source: sites.SourceInfo{
			Tracker: source, TorrentID: "60635", Name: "Fixture.Release.2026.1080p",
			DetailsURL: "https://source.invalid/details.php?id=60635",
		},
		Title: "Fixture.Release.2026.1080p",
		Content: sites.TargetContentEvidence{
			LocalRoot: "/downloads/release", FileCount: 1, TotalSizeBytes: 13,
			ManifestID: "manifest-id", ManifestSHA256: strings.Repeat("a", 64),
		},
		Media: sites.TargetMediaEvidence{
			Kind: "mediainfo", Tool: "mediainfo", Version: "fixture",
			Document: json.RawMessage(`{"media":{"track":[{"@type":"Video","Width":"1920","Height":"1080"}]}}`),
		},
		Screenshots: []sites.TargetScreenshotEvidence{{
			Index: 1, SourceSHA256: strings.Repeat("b", 64), ReceiptArtifactID: "receipt-id",
			ReceiptSHA256: strings.Repeat("c", 64), URL: "https://i.ibb.co/fixture/image.png", ViewerURL: "https://ibb.co/fixture",
		}},
		Evidence: map[string]any{"media_info": map[string]any{"artifact_id": "media-id"}},
		Options:  json.RawMessage(`{}`),
	}
}
