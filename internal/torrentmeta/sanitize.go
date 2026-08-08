package torrentmeta

import (
	"bytes"
	"fmt"
	"sort"
	"strconv"
)

// KeepTopLevelFields removes tracker-specific top-level metadata without
// decoding or re-encoding any value. In particular, the exact info dictionary
// bytes (and therefore both reported info hashes) remain unchanged.
func KeepTopLevelFields(metainfo []byte, required []string) ([]byte, error) {
	before, err := Hashes(metainfo)
	if err != nil {
		return nil, err
	}
	fields, err := dictionaryFields(metainfo)
	if err != nil {
		return nil, err
	}
	keys := append([]string(nil), required...)
	sort.Strings(keys)
	if len(keys) == 0 {
		return nil, fmt.Errorf("%w: required top-level fields are empty", ErrInvalidTorrent)
	}
	result := bytes.NewBuffer(make([]byte, 0, len(metainfo)))
	result.WriteByte('d')
	previous := ""
	for _, key := range keys {
		if key == "" || key == previous {
			return nil, fmt.Errorf("%w: required top-level fields are invalid", ErrInvalidTorrent)
		}
		value, exists := fields[key]
		if !exists {
			return nil, fmt.Errorf("%w: required top-level field %q is missing", ErrInvalidTorrent, key)
		}
		result.WriteString(strconv.Itoa(len(key)))
		result.WriteByte(':')
		result.WriteString(key)
		result.Write(value)
		previous = key
	}
	result.WriteByte('e')
	sanitized := result.Bytes()
	after, err := Hashes(sanitized)
	if err != nil || before != after {
		return nil, fmt.Errorf("%w: top-level sanitizing changed the info dictionary", ErrInvalidTorrent)
	}
	return append([]byte(nil), sanitized...), nil
}
