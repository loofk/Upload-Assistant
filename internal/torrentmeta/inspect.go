package torrentmeta

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"sort"
	"strconv"
)

const (
	maxDictionaryFields = 4_096
	maxTorrentFiles     = 200_000
	maxTorrentNameBytes = 4_096
	maxTrackerURLBytes  = 4_096
	maxSourceTagBytes   = 256
)

// Inspection is a bounded, non-secret summary of the metainfo fields needed to
// prove that tracker sanitizing did not alter the torrent payload.
type Inspection struct {
	Hashes             InfoHashes `json:"hashes"`
	Announce           string     `json:"announce,omitempty"`
	Name               string     `json:"name"`
	Source             string     `json:"source,omitempty"`
	Private            bool       `json:"private"`
	PrivateSet         bool       `json:"private_set"`
	PieceLength        int64      `json:"piece_length"`
	PieceCount         int        `json:"piece_count"`
	FileCount          int        `json:"file_count"`
	TotalSizeBytes     int64      `json:"total_size_bytes"`
	ContentFingerprint string     `json:"content_fingerprint_sha256"`
	TopLevelKeys       []string   `json:"top_level_keys"`
	InfoKeys           []string   `json:"info_keys"`
	ExtraTopLevelKeys  []string   `json:"extra_top_level_keys"`
	ExtraInfoKeys      []string   `json:"extra_info_keys"`
}

var standardTopLevelKeys = map[string]bool{
	"announce": true, "creation date": true, "created by": true, "encoding": true, "info": true,
}

var standardV1InfoKeys = map[string]bool{
	"files": true, "length": true, "name": true, "piece length": true,
	"pieces": true, "private": true, "source": true,
}

// Inspect validates a v1 torrent and computes a fingerprint over the exact
// bencoded payload fields. Changing announce/source/private does not change the
// fingerprint; changing names, paths, sizes, piece length, or piece hashes does.
func Inspect(metainfo []byte) (Inspection, error) {
	hashes, err := Hashes(metainfo)
	if err != nil {
		return Inspection{}, err
	}
	top, err := dictionaryFields(metainfo)
	if err != nil {
		return Inspection{}, err
	}
	infoRaw, exists := top["info"]
	if !exists || len(infoRaw) == 0 || infoRaw[0] != 'd' {
		return Inspection{}, fmt.Errorf("%w: info dictionary is missing", ErrInvalidTorrent)
	}
	info, err := dictionaryFields(infoRaw)
	if err != nil {
		return Inspection{}, fmt.Errorf("%w: invalid info dictionary", err)
	}
	name, err := requiredByteString(info, "name")
	if err != nil || len(name) > maxTorrentNameBytes {
		return Inspection{}, fmt.Errorf("%w: name is missing or too large", ErrInvalidTorrent)
	}
	pieceLength, err := requiredPositiveInteger(info, "piece length")
	if err != nil {
		return Inspection{}, err
	}
	pieces, err := requiredBytes(info, "pieces")
	if err != nil || len(pieces) == 0 || len(pieces)%20 != 0 {
		return Inspection{}, fmt.Errorf("%w: pieces must contain complete SHA-1 hashes", ErrInvalidTorrent)
	}

	fileCount, totalSize, err := payloadSize(info)
	if err != nil {
		return Inspection{}, err
	}
	expectedPieces := totalSize / pieceLength
	if totalSize%pieceLength != 0 {
		expectedPieces++
	}
	if totalSize <= 0 || expectedPieces != int64(len(pieces)/20) {
		return Inspection{}, fmt.Errorf("%w: piece count does not match payload size and piece length", ErrInvalidTorrent)
	}
	private, privateSet, err := optionalInteger(info, "private")
	if err != nil || (privateSet && private != 0 && private != 1) {
		return Inspection{}, fmt.Errorf("%w: private must be 0 or 1", ErrInvalidTorrent)
	}
	announce, err := optionalByteString(top, "announce")
	if err != nil || len(announce) > maxTrackerURLBytes {
		return Inspection{}, fmt.Errorf("%w: announce URL is invalid or too large", ErrInvalidTorrent)
	}
	source, err := optionalByteString(info, "source")
	if err != nil || len(source) > maxSourceTagBytes {
		return Inspection{}, fmt.Errorf("%w: source tag is invalid or too large", ErrInvalidTorrent)
	}
	fingerprint, err := contentFingerprint(info)
	if err != nil {
		return Inspection{}, err
	}
	topKeys, extraTop := classifiedKeys(top, standardTopLevelKeys)
	infoKeys, extraInfo := classifiedKeys(info, standardV1InfoKeys)
	return Inspection{
		Hashes: hashes, Announce: announce, Name: name, Source: source,
		Private: private == 1, PrivateSet: privateSet,
		PieceLength: pieceLength, PieceCount: len(pieces) / 20,
		FileCount: fileCount, TotalSizeBytes: totalSize,
		ContentFingerprint: fingerprint, TopLevelKeys: topKeys, InfoKeys: infoKeys,
		ExtraTopLevelKeys: extraTop, ExtraInfoKeys: extraInfo,
	}, nil
}

func dictionaryFields(data []byte) (map[string][]byte, error) {
	if len(data) < 2 || data[0] != 'd' {
		return nil, fmt.Errorf("%w: expected dictionary", ErrInvalidTorrent)
	}
	result := make(map[string][]byte)
	position := 1
	for position < len(data) && data[position] != 'e' {
		keyBytes, next, err := scanBytes(data, position)
		if err != nil {
			return nil, err
		}
		if len(keyBytes) == 0 || len(keyBytes) > 1_024 || len(result) >= maxDictionaryFields {
			return nil, fmt.Errorf("%w: dictionary key or field count exceeds limits", ErrInvalidTorrent)
		}
		key := string(keyBytes)
		if _, duplicate := result[key]; duplicate {
			return nil, fmt.Errorf("%w: duplicate dictionary key %q", ErrInvalidTorrent, key)
		}
		valueStart := next
		valueEnd, err := scanValue(data, valueStart, 1)
		if err != nil {
			return nil, err
		}
		result[key] = data[valueStart:valueEnd]
		position = valueEnd
	}
	if position != len(data)-1 || data[position] != 'e' {
		return nil, fmt.Errorf("%w: dictionary has trailing or missing data", ErrInvalidTorrent)
	}
	return result, nil
}

func payloadSize(info map[string][]byte) (int, int64, error) {
	lengthRaw, single := info["length"]
	filesRaw, multiple := info["files"]
	if single == multiple {
		return 0, 0, fmt.Errorf("%w: info must contain exactly one of length or files", ErrInvalidTorrent)
	}
	if single {
		length, err := decodeInteger(lengthRaw)
		if err != nil || length < 0 {
			return 0, 0, fmt.Errorf("%w: invalid single-file length", ErrInvalidTorrent)
		}
		return 1, length, nil
	}
	if len(filesRaw) < 2 || filesRaw[0] != 'l' {
		return 0, 0, fmt.Errorf("%w: files must be a list", ErrInvalidTorrent)
	}
	count := 0
	var total int64
	position := 1
	for position < len(filesRaw) && filesRaw[position] != 'e' {
		end, err := scanValue(filesRaw, position, 1)
		if err != nil {
			return 0, 0, err
		}
		fields, err := dictionaryFields(filesRaw[position:end])
		if err != nil {
			return 0, 0, fmt.Errorf("%w: invalid file entry", err)
		}
		length, err := requiredNonNegativeInteger(fields, "length")
		if err != nil || total > math.MaxInt64-length {
			return 0, 0, fmt.Errorf("%w: invalid or overflowing file length", ErrInvalidTorrent)
		}
		if err := validatePathList(fields["path"]); err != nil {
			return 0, 0, fmt.Errorf("%w: file path list is missing", ErrInvalidTorrent)
		}
		total += length
		count++
		if count > maxTorrentFiles {
			return 0, 0, fmt.Errorf("%w: torrent file count exceeds limit", ErrInvalidTorrent)
		}
		position = end
	}
	if position != len(filesRaw)-1 || count == 0 {
		return 0, 0, fmt.Errorf("%w: files list is empty or malformed", ErrInvalidTorrent)
	}
	return count, total, nil
}

func validatePathList(raw []byte) error {
	if len(raw) < 2 || raw[0] != 'l' {
		return ErrInvalidTorrent
	}
	position := 1
	segments := 0
	for position < len(raw) && raw[position] != 'e' {
		segment, end, err := scanBytes(raw, position)
		if err != nil || len(segment) == 0 {
			return ErrInvalidTorrent
		}
		segments++
		position = end
	}
	if position != len(raw)-1 || segments == 0 {
		return ErrInvalidTorrent
	}
	return nil
}

func contentFingerprint(info map[string][]byte) (string, error) {
	keys := []string{"name", "piece length", "pieces"}
	if _, exists := info["length"]; exists {
		keys = append(keys, "length")
	} else {
		keys = append(keys, "files")
	}
	hasher := sha256.New()
	for _, key := range keys {
		value, exists := info[key]
		if !exists {
			return "", fmt.Errorf("%w: payload field %q is missing", ErrInvalidTorrent, key)
		}
		_, _ = hasher.Write([]byte(key))
		_, _ = hasher.Write([]byte{0})
		_, _ = hasher.Write(value)
		_, _ = hasher.Write([]byte{0})
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}

func classifiedKeys(values map[string][]byte, allowed map[string]bool) ([]string, []string) {
	all := make([]string, 0, len(values))
	extra := make([]string, 0)
	for key := range values {
		all = append(all, key)
		if !allowed[key] {
			extra = append(extra, key)
		}
	}
	sort.Strings(all)
	sort.Strings(extra)
	return all, extra
}

func requiredByteString(values map[string][]byte, key string) (string, error) {
	value, err := requiredBytes(values, key)
	if err != nil || len(value) == 0 {
		return "", fmt.Errorf("%w: %s must be a non-empty byte string", ErrInvalidTorrent, key)
	}
	return string(value), nil
}

func optionalByteString(values map[string][]byte, key string) (string, error) {
	raw, exists := values[key]
	if !exists {
		return "", nil
	}
	value, err := decodeBytes(raw)
	if err != nil {
		return "", fmt.Errorf("%w: %s must be a byte string", ErrInvalidTorrent, key)
	}
	return string(value), nil
}

func requiredBytes(values map[string][]byte, key string) ([]byte, error) {
	raw, exists := values[key]
	if !exists {
		return nil, fmt.Errorf("%w: %s is missing", ErrInvalidTorrent, key)
	}
	value, err := decodeBytes(raw)
	if err != nil {
		return nil, fmt.Errorf("%w: %s must be a byte string", ErrInvalidTorrent, key)
	}
	return value, nil
}

func decodeBytes(raw []byte) ([]byte, error) {
	value, end, err := scanBytes(raw, 0)
	if err != nil || end != len(raw) {
		return nil, ErrInvalidTorrent
	}
	return value, nil
}

func requiredPositiveInteger(values map[string][]byte, key string) (int64, error) {
	value, err := requiredNonNegativeInteger(values, key)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%w: %s must be positive", ErrInvalidTorrent, key)
	}
	return value, nil
}

func requiredNonNegativeInteger(values map[string][]byte, key string) (int64, error) {
	raw, exists := values[key]
	if !exists {
		return 0, fmt.Errorf("%w: %s is missing", ErrInvalidTorrent, key)
	}
	value, err := decodeInteger(raw)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("%w: %s must be a non-negative integer", ErrInvalidTorrent, key)
	}
	return value, nil
}

func optionalInteger(values map[string][]byte, key string) (int64, bool, error) {
	raw, exists := values[key]
	if !exists {
		return 0, false, nil
	}
	value, err := decodeInteger(raw)
	return value, true, err
}

func decodeInteger(raw []byte) (int64, error) {
	if len(raw) < 3 || raw[0] != 'i' || raw[len(raw)-1] != 'e' {
		return 0, ErrInvalidTorrent
	}
	digits := string(raw[1 : len(raw)-1])
	if digits == "" || digits == "-0" || (len(digits) > 1 && digits[0] == '0') || (len(digits) > 2 && digits[0] == '-' && digits[1] == '0') {
		return 0, ErrInvalidTorrent
	}
	value, err := strconv.ParseInt(digits, 10, 64)
	if err != nil {
		return 0, ErrInvalidTorrent
	}
	return value, nil
}
