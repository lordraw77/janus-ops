package collect

import (
	"sort"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

func sortProcesses(list []proto.Process, less func(a, b proto.Process) bool) {
	sort.SliceStable(list, func(i, j int) bool { return less(list[i], list[j]) })
}
