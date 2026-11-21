package collect

import (
	"bufio"
	"strings"
)

// ParseFailedUnits reads `systemctl list-units --state=failed --no-legend --plain`.
func ParseFailedUnits(out string) []string {
	units := []string{}
	s := bufio.NewScanner(strings.NewReader(out))
	for s.Scan() {
		f := strings.Fields(s.Text())
		if len(f) > 0 && strings.Contains(f[0], ".") {
			units = append(units, f[0])
		}
	}
	return units
}

// Info describes the host for the hello message.
type Info struct {
	OS, Kernel, Hostname string
}
