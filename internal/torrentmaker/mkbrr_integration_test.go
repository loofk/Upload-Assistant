package torrentmaker

import (
	"bytes"
	"context"
	"crypto/sha1"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"testing"
	"time"

	"github.com/loofk/upload-assistant/v2/internal/torrentmeta"
)

func TestMkbrrRealBinaryContract(t *testing.T) {
	binary := os.Getenv("UA_TEST_MKBRR_BIN")
	if binary == "" {
		t.Skip("UA_TEST_MKBRR_BIN is not set")
	}
	tempRoot := t.TempDir()
	contentPath := filepath.Join(tempRoot, "video.mkv")
	content := []byte("abc")
	if err := os.WriteFile(contentPath, content, 0o600); err != nil {
		t.Fatal(err)
	}
	source := realContractTorrent(content)
	sourceInspection, err := torrentmeta.Inspect(source)
	if err != nil {
		t.Fatal(err)
	}
	result, err := NewMkbrr(binary, tempRoot, time.Minute).SanitizeAndCheck(context.Background(), Request{
		SourceTorrent: source, ContentPath: contentPath,
		AnnounceURL: "https://fake.tracker", SourceTag: "MTEAM",
		TopLevelKeys: []string{"announce", "info"},
	})
	if err != nil {
		t.Fatal(err)
	}
	target, err := torrentmeta.Inspect(result.Torrent)
	if err != nil {
		t.Fatal(err)
	}
	if target.Announce != "https://fake.tracker" || target.Source != "MTEAM" || !target.Private ||
		target.ContentFingerprint != sourceInspection.ContentFingerprint || len(target.ExtraTopLevelKeys) != 0 ||
		len(target.ExtraInfoKeys) != 0 || target.TotalSizeBytes != int64(len(content)) {
		t.Fatalf("real mkbrr target inspection = %#v", target)
	}
}

func realContractTorrent(content []byte) []byte {
	return contractTorrent(content, "https://source.example/announce/fixture", "U2", map[string][]byte{
		"comment": contractBencodeBytes([]byte("must be stripped")),
	})
}

func realContractTargetTorrent(content []byte) []byte {
	announce := "https://fake.tracker"
	return contractTorrent(content, announce, "MTEAM", map[string][]byte{
		"announce-list": contractBencodeList(contractBencodeList(contractBencodeBytes([]byte(announce)))),
	})
}

func contractTorrent(content []byte, announce, source string, extra map[string][]byte) []byte {
	piece := sha1.Sum(content)
	info := contractBencodeDict(map[string][]byte{
		"length": contractBencodeInt(int64(len(content))), "name": contractBencodeBytes([]byte("video.mkv")),
		"piece length": contractBencodeInt(16384), "pieces": contractBencodeBytes(piece[:]),
		"private": contractBencodeInt(1), "source": contractBencodeBytes([]byte(source)),
	})
	top := map[string][]byte{"announce": contractBencodeBytes([]byte(announce)), "info": info}
	for key, value := range extra {
		top[key] = value
	}
	return contractBencodeDict(top)
}

func contractBencodeBytes(value []byte) []byte {
	return append([]byte(strconv.Itoa(len(value))+":"), value...)
}

func contractBencodeInt(value int64) []byte { return []byte("i" + strconv.FormatInt(value, 10) + "e") }

func contractBencodeList(values ...[]byte) []byte {
	result := bytes.NewBufferString("l")
	for _, value := range values {
		result.Write(value)
	}
	result.WriteByte('e')
	return result.Bytes()
}

func contractBencodeDict(values map[string][]byte) []byte {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := bytes.NewBufferString("d")
	for _, key := range keys {
		result.Write(contractBencodeBytes([]byte(key)))
		result.Write(values[key])
	}
	result.WriteByte('e')
	return result.Bytes()
}
