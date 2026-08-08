package torrentmeta

import (
	"crypto/sha1"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
)

const (
	maxTorrentBytes = 32 << 20
	maxBencodeDepth = 128
)

var ErrInvalidTorrent = errors.New("invalid torrent metainfo")

type InfoHashes struct {
	V1SHA1   string `json:"v1_sha1"`
	V2SHA256 string `json:"v2_sha256"`
}

func Hashes(metainfo []byte) (InfoHashes, error) {
	if len(metainfo) == 0 || len(metainfo) > maxTorrentBytes || metainfo[0] != 'd' {
		return InfoHashes{}, ErrInvalidTorrent
	}
	position := 1
	infoStart, infoEnd := -1, -1
	for {
		if position >= len(metainfo) {
			return InfoHashes{}, ErrInvalidTorrent
		}
		if metainfo[position] == 'e' {
			position++
			break
		}
		key, next, err := scanBytes(metainfo, position)
		if err != nil {
			return InfoHashes{}, err
		}
		position = next
		valueStart := position
		position, err = scanValue(metainfo, position, 1)
		if err != nil {
			return InfoHashes{}, err
		}
		if string(key) == "info" {
			if infoStart >= 0 || metainfo[valueStart] != 'd' {
				return InfoHashes{}, fmt.Errorf("%w: info dictionary is missing or duplicated", ErrInvalidTorrent)
			}
			infoStart, infoEnd = valueStart, position
		}
	}
	if position != len(metainfo) || infoStart < 0 {
		return InfoHashes{}, fmt.Errorf("%w: top-level dictionary or info value is invalid", ErrInvalidTorrent)
	}
	info := metainfo[infoStart:infoEnd]
	v1 := sha1.Sum(info)
	v2 := sha256.Sum256(info)
	return InfoHashes{V1SHA1: hex.EncodeToString(v1[:]), V2SHA256: hex.EncodeToString(v2[:])}, nil
}

func scanValue(data []byte, position, depth int) (int, error) {
	if depth > maxBencodeDepth || position >= len(data) {
		return 0, ErrInvalidTorrent
	}
	switch data[position] {
	case 'i':
		position++
		start := position
		if position < len(data) && data[position] == '-' {
			position++
		}
		if position >= len(data) || data[position] < '0' || data[position] > '9' {
			return 0, ErrInvalidTorrent
		}
		for position < len(data) && data[position] >= '0' && data[position] <= '9' {
			position++
		}
		if position >= len(data) || data[position] != 'e' || position == start {
			return 0, ErrInvalidTorrent
		}
		return position + 1, nil
	case 'l':
		position++
		for position < len(data) && data[position] != 'e' {
			next, err := scanValue(data, position, depth+1)
			if err != nil {
				return 0, err
			}
			position = next
		}
		if position >= len(data) {
			return 0, ErrInvalidTorrent
		}
		return position + 1, nil
	case 'd':
		position++
		for position < len(data) && data[position] != 'e' {
			_, next, err := scanBytes(data, position)
			if err != nil {
				return 0, err
			}
			position, err = scanValue(data, next, depth+1)
			if err != nil {
				return 0, err
			}
		}
		if position >= len(data) {
			return 0, ErrInvalidTorrent
		}
		return position + 1, nil
	default:
		_, next, err := scanBytes(data, position)
		return next, err
	}
}

func scanBytes(data []byte, position int) ([]byte, int, error) {
	if position >= len(data) || data[position] < '0' || data[position] > '9' {
		return nil, 0, ErrInvalidTorrent
	}
	length := 0
	for position < len(data) && data[position] >= '0' && data[position] <= '9' {
		if length > len(data)/10 {
			return nil, 0, ErrInvalidTorrent
		}
		length = length*10 + int(data[position]-'0')
		position++
	}
	if position >= len(data) || data[position] != ':' {
		return nil, 0, ErrInvalidTorrent
	}
	position++
	end := position + length
	if end < position || end > len(data) {
		return nil, 0, ErrInvalidTorrent
	}
	return data[position:end], end, nil
}
