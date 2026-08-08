package torrentmeta

import (
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"testing"
)

func TestHashesUsesExactInfoDictionaryBytes(t *testing.T) {
	info := []byte("d6:lengthi1e4:name4:teste")
	metainfo := append([]byte("d8:announce14:https://t.test4:info"), info...)
	metainfo = append(metainfo, 'e')
	hashes, err := Hashes(metainfo)
	if err != nil {
		t.Fatalf("Hashes() error = %v", err)
	}
	v1 := sha1.Sum(info)
	v2 := sha256.Sum256(info)
	if hashes.V1SHA1 != hex.EncodeToString(v1[:]) || hashes.V2SHA256 != hex.EncodeToString(v2[:]) {
		t.Fatalf("hashes = %#v", hashes)
	}
}

func TestHashesRejectsMalformedOrAmbiguousMetainfo(t *testing.T) {
	for _, metainfo := range [][]byte{
		nil,
		[]byte("l4:infoe"),
		[]byte("d4:info4:nopee"),
		[]byte("d4:infodejunk"),
		[]byte("d4:infode4:infodee"),
	} {
		if _, err := Hashes(metainfo); !errors.Is(err, ErrInvalidTorrent) {
			t.Fatalf("Hashes(%q) error = %v, want ErrInvalidTorrent", metainfo, err)
		}
	}
}
