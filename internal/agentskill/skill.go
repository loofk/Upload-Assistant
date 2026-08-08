package agentskill

import _ "embed"

//go:embed SKILL.md
var markdown []byte

// Markdown returns a copy of the embedded AgentSkills-compatible skill.
func Markdown() []byte {
	return append([]byte(nil), markdown...)
}
