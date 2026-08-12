package rules

import (
	"regexp"
	"testing"
)

func TestCompileAndRenderNamingTemplate(t *testing.T) {
	naming := Naming{Profiles: []NamingProfile{{
		ID: "movie", Label: "电影", ResourceClasses: []string{"movie"}, CategoryIDs: []int{419},
		TitleTokens: []NamingToken{
			{Kind: "field", Value: "title", Required: true},
			{Kind: "field", Value: "year", Required: true},
			{Kind: "field", Value: "resolution", Required: true},
			{Kind: "field", Value: "source", Required: true},
			{Kind: "field", Value: "group", Required: true, Separator: "-"},
		},
	}}}
	if err := CompileNamingTemplates(&naming); err != nil {
		t.Fatal(err)
	}
	profile, err := SelectNamingProfile(naming, "", "movie", 0)
	if err != nil {
		t.Fatal(err)
	}
	title, missing := RenderNamingTitle(profile, map[string]string{
		"title": "Fixture", "year": "2026", "resolution": "1080p", "source": "WEB-DL", "group": "GROUP",
	})
	if len(missing) != 0 || title != "Fixture 2026 1080p WEB-DL-GROUP" {
		t.Fatalf("title/missing = %q/%#v", title, missing)
	}
	if matched, err := regexp.MatchString(profile.ReleaseTitle.Pattern, title); err != nil || !matched {
		t.Fatalf("compiled pattern %q did not match %q: %v", profile.ReleaseTitle.Pattern, title, err)
	}
}

func TestSelectNamingProfileFailsClosedOnAmbiguity(t *testing.T) {
	naming := Naming{Profiles: []NamingProfile{
		{ID: "movie-a", ResourceClasses: []string{"movie"}},
		{ID: "movie-b", ResourceClasses: []string{"movie"}},
	}}
	if _, err := SelectNamingProfile(naming, "", "movie", 0); err == nil {
		t.Fatal("expected ambiguous resource class to fail closed")
	}
}
