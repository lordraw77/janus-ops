//go:build !windows

package collect

import "syscall"

func statfs(path string) (FSStat, error) {
	var s syscall.Statfs_t
	if err := syscall.Statfs(path, &s); err != nil {
		return FSStat{}, err
	}
	bs := uint64(s.Bsize)
	return FSStat{
		Size: s.Blocks * bs, Free: s.Bfree * bs, Avail: s.Bavail * bs,
		Files: s.Files, FilesFree: s.Ffree,
	}, nil
}
