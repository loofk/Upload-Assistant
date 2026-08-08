package security

import (
	"bufio"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"
	"strings"
)

type Keyring struct {
	keys   map[int][]byte
	active int
}

type EncryptedValue struct {
	Ciphertext []byte
	Nonce      []byte
	KeyVersion int
}

func LoadKeyring(path string) (*Keyring, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("master key file path is required")
	}
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("stat master key file: %w", err)
	}
	if info.IsDir() || info.Mode().Perm()&0o022 != 0 {
		return nil, errors.New("master key file must be a regular file and not group/world writable")
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open master key file: %w", err)
	}
	defer file.Close()
	return ParseKeyring(file)
}

func ParseKeyring(reader io.Reader) (*Keyring, error) {
	keys := make(map[int][]byte)
	scanner := bufio.NewScanner(io.LimitReader(reader, 64*1024))
	lineNumber := 0
	for scanner.Scan() {
		lineNumber++
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		versionText, encoded, found := strings.Cut(line, ":")
		if !found {
			return nil, fmt.Errorf("master key line %d must use version:base64 format", lineNumber)
		}
		version, err := strconv.Atoi(versionText)
		if err != nil || version <= 0 {
			return nil, fmt.Errorf("master key line %d has invalid version", lineNumber)
		}
		key, err := base64.RawStdEncoding.DecodeString(strings.TrimSpace(encoded))
		if err != nil {
			key, err = base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
		}
		if err != nil || len(key) != 32 {
			return nil, fmt.Errorf("master key line %d must contain a base64-encoded 32-byte key", lineNumber)
		}
		if _, exists := keys[version]; exists {
			return nil, fmt.Errorf("master key version %d is duplicated", version)
		}
		keys[version] = key
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read master key file: %w", err)
	}
	if len(keys) == 0 {
		return nil, errors.New("master key file contains no keys")
	}
	versions := make([]int, 0, len(keys))
	for version := range keys {
		versions = append(versions, version)
	}
	sort.Ints(versions)
	return &Keyring{keys: keys, active: versions[len(versions)-1]}, nil
}

func (k *Keyring) ActiveVersion() int { return k.active }

func (k *Keyring) Encrypt(purpose string, plaintext []byte) (EncryptedValue, error) {
	block, err := aes.NewCipher(k.keys[k.active])
	if err != nil {
		return EncryptedValue{}, fmt.Errorf("initialize master cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return EncryptedValue{}, fmt.Errorf("initialize AES-GCM: %w", err)
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return EncryptedValue{}, fmt.Errorf("generate secret nonce: %w", err)
	}
	ciphertext := aead.Seal(nil, nonce, plaintext, []byte(purpose))
	return EncryptedValue{Ciphertext: ciphertext, Nonce: nonce, KeyVersion: k.active}, nil
}

func (k *Keyring) Decrypt(purpose string, encrypted EncryptedValue) ([]byte, error) {
	key, exists := k.keys[encrypted.KeyVersion]
	if !exists {
		return nil, fmt.Errorf("master key version %d is unavailable", encrypted.KeyVersion)
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("initialize master cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("initialize AES-GCM: %w", err)
	}
	plaintext, err := aead.Open(nil, encrypted.Nonce, encrypted.Ciphertext, []byte(purpose))
	if err != nil {
		return nil, errors.New("decrypt secret: authentication failed")
	}
	return plaintext, nil
}
