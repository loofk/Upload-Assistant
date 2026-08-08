package workflow

import "testing"

func TestRetorrentDefinitionHasStableHardGates(t *testing.T) {
	definition := RetorrentDefinition()
	if len(definition.Steps) != 20 {
		t.Fatalf("step count = %d, want 20", len(definition.Steps))
	}
	wantGates := map[string]string{
		"source_rules":           "accept_rules",
		"target_duplicate_check": "duplicate_check",
		"target_rules":           "accept_rules",
		"target_upload":          "confirm_upload",
	}
	for _, step := range definition.Steps {
		if want, exists := wantGates[step.Key]; exists && step.GateKind != want {
			t.Fatalf("step %s gate = %q, want %q", step.Key, step.GateKind, want)
		}
		delete(wantGates, step.Key)
	}
	if len(wantGates) != 0 {
		t.Fatalf("missing hard gates: %v", wantGates)
	}
}

func TestDefinitionHashIsStable(t *testing.T) {
	definition := RetorrentDefinition()
	bodyA, hashA, err := definition.MarshalAndHash()
	if err != nil {
		t.Fatalf("MarshalAndHash() error = %v", err)
	}
	bodyB, hashB, err := definition.MarshalAndHash()
	if err != nil {
		t.Fatalf("MarshalAndHash() second error = %v", err)
	}
	if string(bodyA) != string(bodyB) || hashA != hashB {
		t.Fatal("workflow definition serialization is not stable")
	}
}

func TestDailyCandidatesDefinitionHasAuditableBoundaries(t *testing.T) {
	definition := DailyCandidatesDefinition()
	want := []string{"candidate_rules", "candidate_scan", "candidate_evaluate", "candidate_rank", "candidate_summary"}
	if definition.Name != "daily_candidates" || len(definition.Steps) != len(want) {
		t.Fatalf("daily candidate definition = %s/%d", definition.Name, len(definition.Steps))
	}
	for index, key := range want {
		if definition.Steps[index].Key != key || !definition.Steps[index].Required {
			t.Fatalf("step %d = %#v, want required %s", index, definition.Steps[index], key)
		}
	}
	if definition.Steps[0].GateKind != "active_rules" || definition.Steps[2].GateKind != "duplicate_check" {
		t.Fatalf("candidate gates = %q/%q", definition.Steps[0].GateKind, definition.Steps[2].GateKind)
	}
}
