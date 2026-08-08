package downloaders

import "errors"

var ErrAddOutcomeUnknown = errors.New("downloader add outcome is unknown")

// partialAddError is implemented by downloader-specific errors when the
// remote mutation succeeded but a mandatory post-add configuration did not.
// Keeping the interface here lets the API and workflow expose one stable
// reconciliation contract without importing every adapter package.
type partialAddError interface {
	error
	PartialHash() string
}

// PartialAddHash returns the exact remotely observed hash when a torrent was
// added but still needs reconciliation. The boolean is false for ordinary
// request failures where no remote mutation is known to have happened.
func PartialAddHash(err error) (string, bool) {
	var partial partialAddError
	if !errors.As(err, &partial) {
		return "", false
	}
	return partial.PartialHash(), true
}
