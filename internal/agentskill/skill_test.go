package agentskill

import (
	"bytes"
	"os"
	"testing"
)

func TestEmbeddedSkillMatchesRepositorySkill(t *testing.T) {
	want, err := os.ReadFile("../../.agents/skills/upload-assistant/SKILL.md")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(Markdown(), want) {
		t.Fatal("embedded skill differs from .agents/skills/upload-assistant/SKILL.md")
	}
}
