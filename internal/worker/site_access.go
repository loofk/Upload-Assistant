package worker

import (
	"errors"

	"github.com/loofk/upload-assistant/v2/internal/siteaccess"
)

func deferredSiteAccess(err error) *siteaccess.DeferredError {
	var deferred *siteaccess.DeferredError
	if errors.As(err, &deferred) {
		return deferred
	}
	return nil
}
