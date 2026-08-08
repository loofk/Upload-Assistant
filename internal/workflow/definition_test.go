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
