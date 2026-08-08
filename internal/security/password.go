package security

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"golang.org/x/crypto/argon2"
)

const (
	argonMemory      = 64 * 1024
	argonIterations  = 3
	argonParallelism = 2
	argonSaltLength  = 16
	argonKeyLength   = 32
)

func HashPassword(password string) (string, error) {
	if len(password) < 12 {
		return "", errors.New("password must contain at least 12 characters")
	}
	salt := make([]byte, argonSaltLength)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("generate password salt: %w", err)
	}
	hash := argon2.IDKey([]byte(password), salt, argonIterations, argonMemory, argonParallelism, argonKeyLength)
	return fmt.Sprintf(
		"$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version, argonMemory, argonIterations, argonParallelism,
		base64.RawStdEncoding.EncodeToString(salt), base64.RawStdEncoding.EncodeToString(hash),
	), nil
}

func VerifyPassword(encoded, password string) (bool, error) {
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[1] != "argon2id" {
		return false, errors.New("invalid Argon2id password hash")
	}
	version, err := parseVersion(parts[2])
	if err != nil || version != argon2.Version {
		return false, errors.New("unsupported Argon2id version")
	}
	memory, iterations, parallelism, err := parseArgonParameters(parts[3])
	if err != nil {
		return false, err
	}
	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil {
		return false, errors.New("invalid Argon2id salt")
	}
	expected, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil || len(expected) == 0 {
		return false, errors.New("invalid Argon2id hash")
	}
	actual := argon2.IDKey([]byte(password), salt, iterations, memory, parallelism, uint32(len(expected)))
	return subtle.ConstantTimeCompare(actual, expected) == 1, nil
}

func parseVersion(value string) (int, error) {
	if !strings.HasPrefix(value, "v=") {
		return 0, errors.New("missing Argon2id version")
	}
	return strconv.Atoi(strings.TrimPrefix(value, "v="))
}

func parseArgonParameters(value string) (uint32, uint32, uint8, error) {
	var memory, iterations uint32
	var parallelism uint8
	if _, err := fmt.Sscanf(value, "m=%d,t=%d,p=%d", &memory, &iterations, &parallelism); err != nil {
		return 0, 0, 0, errors.New("invalid Argon2id parameters")
	}
	if memory == 0 || iterations == 0 || parallelism == 0 {
		return 0, 0, 0, errors.New("invalid Argon2id parameters")
	}
	return memory, iterations, parallelism, nil
}
