package main

import (
	"strings"
	"testing"
)

func TestAdminLLMProbeRequiresExplicitExternalConfirmation(t *testing.T) {
	err := admin([]string{"llm", "probe", "--provider-id", "11111111-1111-4111-8111-111111111111"})
	if err == nil || !strings.Contains(err.Error(), "--confirm-external") {
		t.Fatalf("admin llm probe error = %v", err)
	}
}

func TestAdminRuleAnalysisRequiresExplicitExternalConfirmation(t *testing.T) {
	err := admin([]string{"llm", "analyze-rule", "--provider-id", "11111111-1111-4111-8111-111111111111", "--revision-id", "22222222-2222-4222-8222-222222222222", "--stream"})
	if err == nil || !strings.Contains(err.Error(), "--confirm-external") {
		t.Fatalf("admin llm analyze-rule error = %v", err)
	}
}

func TestAdminRuleImportRequiresExplicitConfirmation(t *testing.T) {
	err := admin([]string{"rules", "import", "--file", "fixture.md"})
	if err == nil || !strings.Contains(err.Error(), "--confirm") {
		t.Fatalf("admin rules import error = %v", err)
	}
}
