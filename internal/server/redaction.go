package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/url"
	"regexp"
	"strings"

	"github.com/loofk/upload-assistant/v2/internal/workflow"
)

const redactedValue = "[REDACTED]"

var inlineSecretPattern = regexp.MustCompile(`(?i)(passkey|authkey|api[_-]?key|access[_-]?token|token)=([^&\s]+)`)

func redactJob(job workflow.Job) workflow.Job {
	job.Input = redactJSON(job.Input)
	job.Blockers = redactJSON(job.Blockers)
	job.NextActions = redactJSON(job.NextActions)
	job.ResumeState = redactJSON(job.ResumeState)
	job.Summary = redactJSON(job.Summary)
	return job
}

func redactJobs(jobs []workflow.Job) []workflow.Job {
	result := make([]workflow.Job, len(jobs))
	for index, job := range jobs {
		result[index] = redactJob(job)
	}
	return result
}

func redactSteps(steps []workflow.Step) []workflow.Step {
	result := make([]workflow.Step, len(steps))
	for index, step := range steps {
		digest := sha256.Sum256(step.InputSnapshot)
		step.InputSnapshot = mustJSONRaw(map[string]any{
			"redacted": true, "sha256": hex.EncodeToString(digest[:]),
		})
		step.OutputSummary = redactJSON(step.OutputSummary)
		step.Blockers = redactJSON(step.Blockers)
		step.NextActions = redactJSON(step.NextActions)
		step.ResumeState = redactJSON(step.ResumeState)
		result[index] = step
	}
	return result
}

func redactAttempts(attempts []workflow.Attempt) []workflow.Attempt {
	result := make([]workflow.Attempt, len(attempts))
	for index, attempt := range attempts {
		digest := sha256.Sum256(attempt.InputSnapshot)
		attempt.InputSnapshot = mustJSONRaw(map[string]any{
			"redacted": true, "sha256": hex.EncodeToString(digest[:]),
		})
		attempt.OutputSummary = redactJSON(attempt.OutputSummary)
		attempt.ErrorDetails = redactJSON(attempt.ErrorDetails)
		result[index] = attempt
	}
	return result
}

func redactEvents(events []workflow.Event) []workflow.Event {
	result := make([]workflow.Event, len(events))
	for index, event := range events {
		event.Payload = redactJSON(event.Payload)
		result[index] = event
	}
	return result
}

func redactArtifacts(input []workflow.Artifact) []workflow.Artifact {
	result := make([]workflow.Artifact, len(input))
	for index, artifact := range input {
		artifact.Metadata = redactJSON(artifact.Metadata)
		result[index] = artifact
	}
	return result
}

func redactJSON(body json.RawMessage) json.RawMessage {
	if len(body) == 0 {
		return json.RawMessage(`{}`)
	}
	var value any
	if err := json.Unmarshal(body, &value); err != nil {
		return json.RawMessage(`{"redacted":true}`)
	}
	encoded, err := json.Marshal(redactValueTree(value))
	if err != nil {
		return json.RawMessage(`{"redacted":true}`)
	}
	return encoded
}

func redactValueTree(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			if secretField(key) {
				result[key] = redactedValue
			} else {
				result[key] = redactValueTree(item)
			}
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = redactValueTree(item)
		}
		return result
	case string:
		return redactString(typed)
	default:
		return value
	}
}

func secretField(key string) bool {
	normalized := strings.NewReplacer("_", "", "-", "", ".", "").Replace(strings.ToLower(strings.TrimSpace(key)))
	// Digests are evidence about a secret-bearing response, not the response
	// itself, and must remain visible for audit verification.
	if strings.HasSuffix(normalized, "sha256") {
		return false
	}
	for _, marker := range []string{"password", "passwd", "secret", "token", "cookie", "authorization", "apikey", "passkey", "authkey"} {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return false
}

func redactString(value string) string {
	result := inlineSecretPattern.ReplaceAllString(value, "$1="+redactedValue)
	parsed, err := url.Parse(result)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return result
	}
	parsed.User = nil
	query := parsed.Query()
	for key := range query {
		if secretField(key) {
			query.Set(key, redactedValue)
		}
	}
	parsed.RawQuery = query.Encode()
	segments := strings.Split(parsed.EscapedPath(), "/")
	for index, segment := range segments {
		if strings.EqualFold(segment, "announce") && index+1 < len(segments) {
			segments = append(segments[:index+1], url.PathEscape(redactedValue))
			parsed.RawPath = strings.Join(segments, "/")
			parsed.Path, _ = url.PathUnescape(parsed.RawPath)
			break
		}
	}
	return parsed.String()
}

func mustJSONRaw(value any) json.RawMessage {
	body, _ := json.Marshal(value)
	return body
}
