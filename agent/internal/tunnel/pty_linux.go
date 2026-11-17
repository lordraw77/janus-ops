//go:build linux

package tunnel

import (
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// openPTY allocates a pseudo-terminal pair of the given size.
func openPTY(cols, rows uint16) (master, slave *os.File, err error) {
	fd, err := unix.Open("/dev/ptmx", unix.O_RDWR|unix.O_NOCTTY|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, nil, err
	}
	master = os.NewFile(uintptr(fd), "/dev/ptmx")
	fail := func(e error) (*os.File, *os.File, error) {
		master.Close()
		return nil, nil, e
	}
	if err := unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
		return fail(err)
	}
	n, err := unix.IoctlGetInt(fd, unix.TIOCGPTN)
	if err != nil {
		return fail(err)
	}
	slave, err = os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		return fail(err)
	}
	if err := setPTYSize(master, cols, rows); err != nil {
		slave.Close()
		return fail(err)
	}
	return master, slave, nil
}

func setPTYSize(master *os.File, cols, rows uint16) error {
	return unix.IoctlSetWinsize(int(master.Fd()), unix.TIOCSWINSZ, &unix.Winsize{Row: rows, Col: cols})
}
