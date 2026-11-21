//go:build !linux && !windows

package tunnel

import (
	"errors"
	"os"
)

func openPTY(cols, rows uint16) (master, slave *os.File, err error) {
	return nil, nil, errors.New("pty is only supported on linux")
}

func setPTYSize(master *os.File, cols, rows uint16) error { return errors.New("unsupported") }
