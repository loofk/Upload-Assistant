package security

import "testing"

func TestPasswordHashRoundTrip(t *testing.T) {
	encoded, err := HashPassword("correct horse battery staple")
	if err != nil {
		t.Fatalf("HashPassword() error = %v", err)
	}
	matched, err := VerifyPassword(encoded, "correct horse battery staple")
	if err != nil || !matched {
		t.Fatalf("VerifyPassword() match/error = %t/%v", matched, err)
	}
	matched, err = VerifyPassword(encoded, "wrong password")
	if err != nil || matched {
		t.Fatalf("VerifyPassword() wrong match/error = %t/%v", matched, err)
	}
}

func TestPasswordMinimumLength(t *testing.T) {
	if _, err := HashPassword("too-short"); err == nil {
		t.Fatal("HashPassword() error = nil for short password")
	}
}
