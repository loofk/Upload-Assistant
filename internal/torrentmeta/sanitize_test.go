package torrentmeta

import "testing"

func TestKeepTopLevelFieldsRemovesAnnounceListWithoutChangingInfohash(t *testing.T) {
	info := testBencodeDict(map[string][]byte{
		"length": testBencodeInt(1), "name": testBencodeBytes([]byte("x")),
		"piece length": testBencodeInt(16384), "pieces": testBencodeBytes(make([]byte, 20)),
	})
	metainfo := testBencodeDict(map[string][]byte{
		"announce":      testBencodeBytes([]byte("https://fake.tracker")),
		"announce-list": testBencodeList(testBencodeList(testBencodeBytes([]byte("https://fake.tracker")))),
		"comment":       testBencodeBytes([]byte("remove me")), "info": info,
	})
	before, _ := Hashes(metainfo)
	sanitized, err := KeepTopLevelFields(metainfo, []string{"info", "announce"})
	if err != nil {
		t.Fatal(err)
	}
	after, _ := Hashes(sanitized)
	inspection, err := Inspect(sanitized)
	if err != nil || before != after || len(inspection.ExtraTopLevelKeys) != 0 || len(inspection.TopLevelKeys) != 2 {
		t.Fatalf("sanitized inspection/hashes/error = %#v/%#v/%#v/%v", inspection, before, after, err)
	}
}
