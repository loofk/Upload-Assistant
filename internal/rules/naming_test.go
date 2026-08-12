package rules

import "testing"

func TestCheckNamingEnforcesReviewedReleaseAndContentPatterns(t *testing.T) {
	policy := Naming{
		ReleaseTitle: NamingConstraint{Required: true, Pattern: `^.+\.1080p\..+-[A-Za-z0-9]+$`, Template: "{title}.1080p.{source}-{group}", MaxLength: 255},
		ContentName:  NamingConstraint{Required: true, Pattern: `^.+-GROUP$`, Template: "{release}-GROUP"},
	}
	if issues := CheckNaming(policy, "Movie.1080p.BluRay-GROUP", "Movie-GROUP"); len(issues) != 0 {
		t.Fatalf("valid naming issues = %#v", issues)
	}
	issues := CheckNaming(policy, "Movie 1080p", "Movie")
	if len(issues) != 2 || issues[0].Code != "target_release_title_mismatch" || issues[1].Code != "target_content_name_mismatch" {
		t.Fatalf("invalid naming issues = %#v", issues)
	}
}

func TestValidateNamingRejectsUnanchoredMandatoryPattern(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(true)))
	if err != nil {
		t.Fatal(err)
	}
	document.Naming.ReleaseTitle = NamingConstraint{Required: true, Pattern: `1080p`}
	if err := document.Validate(); err == nil {
		t.Fatal("Validate() accepted an unanchored mandatory naming pattern")
	}
}

func TestCheckNamingProfileRequiresSelectionAndAppliesSelectedGrammar(t *testing.T) {
	naming := Naming{Profiles: []NamingProfile{
		{ID: "movie", Label: "电影", ReleaseTitle: NamingConstraint{Required: true, Pattern: `^.+ [0-9]{4} (?:UHD )?BluRay (?:1080p|2160p) .+-.+$`, Template: "英文名 年份 来源 分辨率 编码-小组"}},
		{ID: "tv_episode", Label: "电视单集", ReleaseTitle: NamingConstraint{Required: true, Pattern: `^.+ S[0-9]{2}E[0-9]{2}(?:-E[0-9]{2})? (?:1080p|2160p) .+-.+$`, Template: "英文名 季集 分辨率 来源 编码-小组"}},
	}}
	issues := CheckNamingProfile(naming, "", "Movie 2026 BluRay 1080p x265-GROUP", "Movie")
	if len(issues) != 1 || issues[0].Code != "target_naming_profile_required" || len(issues[0].AllowedProfiles) != 2 {
		t.Fatalf("missing profile issues = %#v", issues)
	}
	if issues = CheckNamingProfile(naming, "movie", "Movie 2026 BluRay 1080p x265-GROUP", "Movie"); len(issues) != 0 {
		t.Fatalf("valid movie issues = %#v", issues)
	}
	issues = CheckNamingProfile(naming, "tv_episode", "Movie 2026 BluRay 1080p x265-GROUP", "Movie")
	if len(issues) != 1 || issues[0].Code != "target_release_title_mismatch" || issues[0].Profile != "tv_episode" {
		t.Fatalf("wrong profile issues = %#v", issues)
	}
}

func TestValidateNamingRejectsDuplicateProfiles(t *testing.T) {
	document, err := ParseMarkdown([]byte(testRuleMarkdown(true)))
	if err != nil {
		t.Fatal(err)
	}
	constraint := NamingConstraint{Required: true, Pattern: `^.+$`, Template: "标题"}
	document.Naming.Profiles = []NamingProfile{
		{ID: "movie", Label: "电影", ReleaseTitle: constraint},
		{ID: "movie", Label: "重复电影", ReleaseTitle: constraint},
	}
	if err := document.Validate(); err == nil {
		t.Fatal("Validate() accepted duplicate naming profiles")
	}
}
