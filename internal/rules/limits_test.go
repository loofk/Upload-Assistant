package rules

import "testing"

func TestParseByteRate(t *testing.T) {
	tests := map[string]int64{
		"": 0, "100M/S": 100_000_000, "20MiB/s": 20 * 1024 * 1024,
		"1.5 GB/s": 1_500_000_000, "512KiB/s": 512 * 1024,
	}
	for input, expected := range tests {
		actual, err := ParseByteRate(input)
		if err != nil || actual != expected {
			t.Fatalf("ParseByteRate(%q) = %d/%v, want %d", input, actual, err, expected)
		}
	}
	for _, input := range []string{"100Mbps", "unlimited", "-1MiB/s", "0B/s"} {
		if _, err := ParseByteRate(input); err == nil {
			t.Fatalf("ParseByteRate(%q) error = nil", input)
		}
	}
}
