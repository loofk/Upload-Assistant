package rules

import (
	"strings"
	"testing"
)

func TestProjectReviewSectionsProvidesHumanReadableFacts(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(false)))
	if err != nil {
		t.Fatal(err)
	}
	document.Limits.SeedboxUpload = "20MiB/s"
	document.Naming.ReleaseTitle = NamingConstraint{Required: true, Pattern: `^.+-[A-Za-z0-9]+$`, Template: "{release}-{group}", MaxLength: 255}
	policy, err := document.PolicyJSON()
	if err != nil {
		t.Fatal(err)
	}
	sections, err := projectReviewSections(Revision{Policy: policy, MarkdownSHA256: "markdown-sha", SourceURL: document.Source.URL})
	if err != nil {
		t.Fatal(err)
	}
	limits := findReviewSection(t, sections, "upload_limit")
	if got := findReviewFact(t, limits.Facts, "盒子上传上限").Value; got != "20 MiB/s" {
		t.Fatalf("seedbox upload limit fact = %q", got)
	}
	naming := findReviewSection(t, sections, "naming")
	if got := findReviewFact(t, naming.Facts, "发布标题").Value; got != "{release}-{group}" {
		t.Fatalf("release title naming fact = %q", got)
	}
}

func TestProjectReviewSectionsShowsEveryNamingProfile(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(false)))
	if err != nil {
		t.Fatal(err)
	}
	document.Naming.Profiles = []NamingProfile{
		{ID: "movie", Label: "电影", ReleaseTitle: NamingConstraint{Required: true, Pattern: `^.+ [0-9]{4} .+-.+$`, Template: "英文名 年份 参数-小组"}},
		{ID: "tv_episode", Label: "电视单集", ReleaseTitle: NamingConstraint{Required: true, Pattern: `^.+ S[0-9]{2}E[0-9]{2} .+-.+$`, Template: "英文名 季集 参数-小组"}},
	}
	policy, err := document.PolicyJSON()
	if err != nil {
		t.Fatal(err)
	}
	sections, err := projectReviewSections(Revision{Policy: policy, MarkdownSHA256: "markdown-sha", SourceURL: document.Source.URL})
	if err != nil {
		t.Fatal(err)
	}
	naming := findReviewSection(t, sections, "naming")
	if got := findReviewFact(t, naming.Facts, "发布标题 · 电影").Value; got != "英文名 年份 参数-小组" {
		t.Fatalf("movie naming fact = %q", got)
	}
	if got := findReviewFact(t, naming.Facts, "发布标题 · 电视单集").Value; got != "英文名 季集 参数-小组" {
		t.Fatalf("TV naming fact = %q", got)
	}
}

func TestNamingFactDistinguishesUnenforceableExtractionFromMissingRule(t *testing.T) {
	fact := namingFact("发布标题", NamingConstraint{Pattern: "^.*$", Template: "按资源分类使用标题格式"})
	if !strings.Contains(fact.Value, "已提取但当前不可执行") {
		t.Fatalf("naming fact = %#v", fact)
	}
}

func findReviewSection(t *testing.T, sections []ReviewSection, key string) ReviewSection {
	t.Helper()
	for _, section := range sections {
		if section.Key == key {
			return section
		}
	}
	t.Fatalf("section %s not found", key)
	return ReviewSection{}
}

func findReviewFact(t *testing.T, facts []ReviewFact, label string) ReviewFact {
	t.Helper()
	for _, fact := range facts {
		if fact.Label == label {
			return fact
		}
	}
	t.Fatalf("fact %s not found", label)
	return ReviewFact{}
}
