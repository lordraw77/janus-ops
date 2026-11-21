//go:build windows

package tunnel

import "context"

// The interactive terminal needs ConPTY on Windows, which is not implemented yet: the server is
// told the agent cannot open one, and the rest of the agent works as usual.
const maxPTYs = 4

func (m *mux) startPTY(ctx context.Context, id uint32, req openRequest) error {
	return m.refuse(ctx, id, "pty_unavailable")
}
