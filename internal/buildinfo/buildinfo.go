package buildinfo

import "fmt"

var (
	version = "dev"
	commit  = "unknown"
	builtAt = "unknown"
)

type Info struct {
	Version string `json:"version"`
	Commit  string `json:"commit"`
	BuiltAt string `json:"built_at"`
}

func Current() Info {
	return Info{Version: version, Commit: commit, BuiltAt: builtAt}
}

func (i Info) String() string {
	return fmt.Sprintf("upload-assistant %s (commit=%s built_at=%s)", i.Version, i.Commit, i.BuiltAt)
}
