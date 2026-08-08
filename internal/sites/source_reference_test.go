package sites

import "testing"

func TestParseSourceReference(t *testing.T) {
	tests := []struct {
		value     string
		tracker   string
		torrentID string
	}{
		{value: "https://u2.dmhy.org/details.php?id=60635", tracker: "U2", torrentID: "60635"},
		{value: "https://ptchdbits.co/details.php?id=12345", tracker: "CHD", torrentID: "12345"},
		{value: "https://kp.m-team.cc/detail/999", tracker: "MTEAM", torrentID: "999"},
		{value: "www.tjupt.org/details.php?torrentid=88", tracker: "TJUPT", torrentID: "88"},
	}
	for _, test := range tests {
		t.Run(test.tracker, func(t *testing.T) {
			got, err := ParseSourceReference(test.value)
			if err != nil {
				t.Fatalf("ParseSourceReference() error = %v", err)
			}
			if got.Tracker != test.tracker || got.TorrentID != test.torrentID {
				t.Fatalf("reference = %s/%s, want %s/%s", got.Tracker, got.TorrentID, test.tracker, test.torrentID)
			}
		})
	}
}

func TestParseSourceReferenceRejectsUnknownHost(t *testing.T) {
	if _, err := ParseSourceReference("https://example.com/details.php?id=1"); err == nil {
		t.Fatal("ParseSourceReference() error = nil, want unsupported host error")
	}
}
