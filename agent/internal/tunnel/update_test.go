package tunnel

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

func TestVersionNewer(t *testing.T) {
	for _, c := range []struct {
		cand, cur string
		want      bool
	}{
		{"0.3.0", "0.2.0", true}, {"0.2.0", "0.2.0", false}, {"0.1.9", "0.2.0", false},
		{"1.0", "0.9.9", true}, {"0.2.1", "v0.2.0", true}, {"0.1.0", "dev", true},
	} {
		if got := versionNewer(c.cand, c.cur); got != c.want {
			t.Errorf("versionNewer(%q,%q) = %v", c.cand, c.cur, got)
		}
	}
}

// runUpdate drives an update stream. mutate may alter the request or the payload.
func runUpdate(t *testing.T, version, current string, mutate func(req *openRequest, payload []byte) []byte) (got []frameMsg, exe string, exited chan int) {
	t.Helper()
	return runUpdateWith(t, []byte("#!/bin/sh\necho new agent\n"), version, current, mutate)
}

func runUpdateWith(t *testing.T, payload []byte, version, current string, mutate func(req *openRequest, payload []byte) []byte) (got []frameMsg, exe string, exited chan int) {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	sum := sha256.Sum256(payload)
	sha := hex.EncodeToString(sum[:])
	sig := ed25519.Sign(priv, UpdateMessage(version, runtime.GOARCH, sha))
	req := openRequest{Kind: "update", Version: version, Sha256: sha, Size: int64(len(payload)),
		Signature: base64.StdEncoding.EncodeToString(sig)}
	if mutate != nil {
		payload = mutate(&req, payload)
	}
	dir := t.TempDir()
	exe = filepath.Join(dir, "janus-agent")
	if err := os.WriteFile(exe, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	f := newFakeServer(t)
	result := make(chan []frameMsg, 1)
	f.session = func(ctx context.Context, c *websocket.Conn) {
		body, _ := json.Marshal(req)
		_ = c.Write(ctx, websocket.MessageBinary, frame(frameOpen, 4, body))
		var frames []frameMsg
		dl, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		for {
			kind, data, err := c.Read(dl)
			if err != nil {
				break
			}
			if kind != websocket.MessageBinary {
				continue
			}
			m := frameMsg{data[0], 0, string(data[5:])}
			frames = append(frames, m)
			switch m.kind {
			case frameOpenOK:
				for off := 0; off < len(payload); off += chunkSize { // frames of at most 32 KiB, like the server
					end := min(off+chunkSize, len(payload))
					_ = c.Write(ctx, websocket.MessageBinary, frame(frameData, 4, payload[off:end]))
				}
			case frameClose, frameOpenFail:
				result <- frames
				return
			}
		}
		result <- frames
	}
	a, _ := newAgent(t, f, "update_public_key: "+base64.StdEncoding.EncodeToString(pub)+"\n")
	a.Version = current
	exited = make(chan int, 1)
	testUpdateHook = func(m *mux) { m.exePath = exe; m.exit = func(c int) { exited <- c } }
	defer func() { testUpdateHook = nil }()
	_, done := runSession(t, a)
	got = <-result
	<-done
	return got, exe, exited
}

func TestUpdateInstallsASignedNewerBinary(t *testing.T) {
	got, exe, exited := runUpdate(t, "0.3.0", "0.2.0", nil)
	last := got[len(got)-1]
	if last.kind != frameClose || last.payload != "updated:0.3.0" {
		t.Fatalf("frames = %+v", got)
	}
	if b, _ := os.ReadFile(exe); string(b) != "#!/bin/sh\necho new agent\n" {
		t.Fatalf("binary not replaced: %q", b)
	}
	if b, _ := os.ReadFile(exe + ".old"); string(b) != "old" {
		t.Fatalf("previous binary not kept: %q", b)
	}
	select {
	case <-exited:
	case <-time.After(3 * time.Second):
		t.Fatal("agent did not exit for the restart")
	}
}

func TestUpdateRefusals(t *testing.T) {
	for name, c := range map[string]struct {
		version, current, reason string
		mutate                   func(*openRequest, []byte) []byte
	}{
		"downgrade": {"0.1.0", "0.2.0", "not_newer", nil},
		"same":      {"0.2.0", "0.2.0", "not_newer", nil},
		"bad signature": {"0.3.0", "0.2.0", "bad_signature", func(r *openRequest, p []byte) []byte {
			r.Version = "0.4.0" // signed for 0.3.0
			return p
		}},
		"wrong arch": {"0.3.0", "0.2.0", "bad_signature", func(r *openRequest, p []byte) []byte {
			r.Sha256 = hex.EncodeToString(make([]byte, 32))
			return p
		}},
	} {
		t.Run(name, func(t *testing.T) {
			got, exe, _ := runUpdate(t, c.version, c.current, c.mutate)
			if len(got) != 1 || got[0].kind != frameOpenFail || got[0].payload != c.reason {
				t.Fatalf("frames = %+v", got)
			}
			if b, _ := os.ReadFile(exe); string(b) != "old" {
				t.Fatal("binary was replaced")
			}
		})
	}
}

func TestUpdateWithTamperedBytesIsRejected(t *testing.T) {
	got, exe, _ := runUpdate(t, "0.3.0", "0.2.0", func(r *openRequest, p []byte) []byte {
		q := append([]byte(nil), p...)
		q[0] ^= 0xff // signed checksum no longer matches
		return q
	})
	last := got[len(got)-1]
	if last.kind != frameClose || last.payload != "failed:checksum" {
		t.Fatalf("frames = %+v", got)
	}
	if b, _ := os.ReadFile(exe); string(b) != "old" {
		t.Fatal("binary was replaced")
	}
	if _, err := os.Stat(exe + ".new"); err == nil {
		t.Fatal("partial file left behind")
	}
}

func TestUpdateNeedsAConfiguredKey(t *testing.T) {
	got := runExecCase(t, "", `{"kind":"update","version":"9.9.9"}`)
	if len(got) != 1 || got[0].payload != "update_not_configured" {
		t.Fatalf("frames = %+v", got)
	}
}

// Vector made by the Python signer (janus_ops.agents.update): both sides must agree on the message.
func TestUpdateMessageMatchesThePythonSigner(t *testing.T) {
	pub, _ := base64.StdEncoding.DecodeString("A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=")
	sig, _ := base64.StdEncoding.DecodeString("pf5kQLYLk/WDUlLs2AQsAetfUvICZqeuK29Vvf9HDXlwD5yUT3NgxHopSFACl8405Q+Olo1Z381masly9BV6DQ==")
	if !ed25519.Verify(pub, UpdateMessage("1.2.3", "arm64", "b0d51c58c8b9c1f458fadf16c7d375630ef51da4df81915893b05c0fa4ed8bc6"), sig) {
		t.Fatal("signature from the server-side tooling does not verify")
	}
}

// The agent confirms what it has written, every updateAckEvery bytes, with the total so far: that is
// what lets the server keep a small window in flight on a slow link.
func TestUpdateConfirmsWhatItHasWritten(t *testing.T) {
	payload := make([]byte, 400<<10) // 400 KiB: several acks
	for i := range payload {
		payload[i] = byte(i * 7)
	}
	got, exe, _ := runUpdateWith(t, payload, "0.3.0", "0.2.0", nil)
	if last := got[len(got)-1]; last.kind != frameClose || last.payload != "updated:0.3.0" {
		t.Fatalf("last frame = %+v", last)
	}
	var acks []uint64
	for _, f := range got {
		if f.kind == frameData && len(f.payload) == 8 {
			acks = append(acks, binary.BigEndian.Uint64([]byte(f.payload)))
		}
	}
	if len(acks) < 4 {
		t.Fatalf("expected at least 4 acks for 400 KiB, got %v", acks)
	}
	for i, a := range acks {
		if a > uint64(len(payload)) || (i > 0 && a <= acks[i-1]) {
			t.Fatalf("acks must grow and stay within the size: %v", acks)
		}
	}
	if b, _ := os.ReadFile(exe); len(b) != len(payload) {
		t.Fatalf("installed %d bytes, want %d", len(b), len(payload))
	}
}

func TestAgentAnnouncesUpdateAck(t *testing.T) {
	found := false
	for _, f := range proto.Features {
		found = found || f == "update_ack"
	}
	if !found {
		t.Fatalf("features = %v", proto.Features)
	}
}
