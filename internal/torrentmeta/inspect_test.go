package torrentmeta

import (
	"bytes"
	"sort"
	"strconv"
	"testing"
)

func TestInspectBindsPayloadButAllowsTrackerMetadataChanges(t *testing.T) {
	pieces := bytes.Repeat([]byte{0x42}, 20)
	baseInfo := map[string][]byte{
		"length":       testBencodeInt(3),
		"name":         testBencodeBytes([]byte("video.mkv")),
		"piece length": testBencodeInt(16384),
		"pieces":       testBencodeBytes(pieces),
		"private":      testBencodeInt(1),
	}
	sourceInfo := cloneRawMap(baseInfo)
	sourceInfo["source"] = testBencodeBytes([]byte("U2"))
	targetInfo := cloneRawMap(baseInfo)
	targetInfo["source"] = testBencodeBytes([]byte("MTEAM"))
	source := testBencodeDict(map[string][]byte{
		"announce": testBencodeBytes([]byte("https://source.example/announce/passkey")),
		"comment":  testBencodeBytes([]byte("source-only")),
		"info":     testBencodeDict(sourceInfo),
	})
	target := testBencodeDict(map[string][]byte{
		"announce": testBencodeBytes([]byte("https://fake.tracker")),
		"info":     testBencodeDict(targetInfo),
	})

	sourceInspection, err := Inspect(source)
	if err != nil {
		t.Fatal(err)
	}
	targetInspection, err := Inspect(target)
	if err != nil {
		t.Fatal(err)
	}
	if sourceInspection.ContentFingerprint != targetInspection.ContentFingerprint ||
		targetInspection.Announce != "https://fake.tracker" || targetInspection.Source != "MTEAM" ||
		!targetInspection.Private || targetInspection.FileCount != 1 || targetInspection.TotalSizeBytes != 3 {
		t.Fatalf("source/target inspection = %#v/%#v", sourceInspection, targetInspection)
	}
	if len(sourceInspection.ExtraTopLevelKeys) != 1 || sourceInspection.ExtraTopLevelKeys[0] != "comment" || len(targetInspection.ExtraTopLevelKeys) != 0 {
		t.Fatalf("extra top-level keys = %#v/%#v", sourceInspection.ExtraTopLevelKeys, targetInspection.ExtraTopLevelKeys)
	}

	mutatedInfo := cloneRawMap(targetInfo)
	mutatedInfo["length"] = testBencodeInt(4)
	mutated, err := Inspect(testBencodeDict(map[string][]byte{
		"announce": testBencodeBytes([]byte("https://fake.tracker")), "info": testBencodeDict(mutatedInfo),
	}))
	if err != nil {
		t.Fatal(err)
	}
	if mutated.ContentFingerprint == targetInspection.ContentFingerprint {
		t.Fatal("payload mutation did not change content fingerprint")
	}
}

func TestInspectCountsMultiFilePayload(t *testing.T) {
	files := testBencodeList(
		testBencodeDict(map[string][]byte{"length": testBencodeInt(2), "path": testBencodeList(testBencodeBytes([]byte("a.mkv")))}),
		testBencodeDict(map[string][]byte{"length": testBencodeInt(5), "path": testBencodeList(testBencodeBytes([]byte("b.mkv")))}),
	)
	info := testBencodeDict(map[string][]byte{
		"files": files, "name": testBencodeBytes([]byte("release")),
		"piece length": testBencodeInt(16384), "pieces": testBencodeBytes(bytes.Repeat([]byte{1}, 20)),
	})
	inspection, err := Inspect(testBencodeDict(map[string][]byte{"info": info}))
	if err != nil || inspection.FileCount != 2 || inspection.TotalSizeBytes != 7 {
		t.Fatalf("Inspect() inspection/error = %#v/%v", inspection, err)
	}
}

func TestInspectRejectsMalformedPayloadMetadata(t *testing.T) {
	invalid := testBencodeDict(map[string][]byte{
		"info": testBencodeDict(map[string][]byte{
			"length": testBencodeInt(1), "files": testBencodeList(),
			"name": testBencodeBytes([]byte("x")), "piece length": testBencodeInt(16384),
			"pieces": testBencodeBytes(bytes.Repeat([]byte{1}, 19)),
		}),
	})
	if _, err := Inspect(invalid); err == nil {
		t.Fatal("Inspect() malformed payload error = nil")
	}
}

func testBencodeBytes(value []byte) []byte {
	result := []byte(strconv.Itoa(len(value)) + ":")
	return append(result, value...)
}

func testBencodeInt(value int64) []byte { return []byte("i" + strconv.FormatInt(value, 10) + "e") }

func testBencodeList(values ...[]byte) []byte {
	result := []byte{'l'}
	for _, value := range values {
		result = append(result, value...)
	}
	return append(result, 'e')
}

func testBencodeDict(values map[string][]byte) []byte {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := []byte{'d'}
	for _, key := range keys {
		result = append(result, testBencodeBytes([]byte(key))...)
		result = append(result, values[key]...)
	}
	return append(result, 'e')
}

func cloneRawMap(source map[string][]byte) map[string][]byte {
	result := make(map[string][]byte, len(source))
	for key, value := range source {
		result[key] = append([]byte(nil), value...)
	}
	return result
}
