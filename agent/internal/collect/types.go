package collect

type Mount struct {
	Point, Source, FSType string
}

type FSStat struct {
	Size, Free, Avail, Files, FilesFree uint64
}

type cpuTimes struct {
	total, idle, iowait, steal uint64
	ok                         bool
}

type diskCounters struct {
	readSectors, writeSectors, ioMs uint64
}

type netCounters struct{ rx, tx uint64 }

const maxCmd = 256
