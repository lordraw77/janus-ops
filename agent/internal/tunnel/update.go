package tunnel

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"hash"
	"os"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// Self-update (F8, FR-84, C8). The server pushes a binary in an `update` stream; the agent replaces
// itself only if the binary carries an ed25519 signature, made offline with the release key, that
// verifies against update_public_key from its local configuration. The server only distributes: it
// cannot forge a signature. The signature covers version, architecture and SHA-256, so a binary
// cannot be passed off as another version or architecture, and the version must be newer than the
// running one (no downgrade to an old signed binary). The agent exits after installing; systemd
// (Restart=always) starts the new binary.
const (
	updateMaxSize = 64 << 20
	updateContext = "janus-agent-update-v1"
)

var (
	updateVersionRe = regexp.MustCompile(`^[0-9]+(\.[0-9]+){0,3}$`)
	sha256HexRe     = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// UpdateMessage is what the release key signs.
func UpdateMessage(version, arch, sha string) []byte {
	return []byte(updateContext + "\n" + version + "\n" + arch + "\n" + sha)
}

// versionNewer reports whether candidate is newer than current. A current version that is not
// numeric (a development build) accepts any signed candidate.
func versionNewer(candidate, current string) bool {
	cur := strings.TrimPrefix(current, "v")
	if !updateVersionRe.MatchString(cur) {
		return true
	}
	a, b := strings.Split(candidate, "."), strings.Split(cur, ".")
	for i := 0; i < 4; i++ {
		x, y := 0, 0
		if i < len(a) {
			x, _ = strconv.Atoi(a[i])
		}
		if i < len(b) {
			y, _ = strconv.Atoi(b[i])
		}
		if x != y {
			return x > y
		}
	}
	return false
}

type updateSink struct {
	m       *mux
	ctx     context.Context
	id      uint32
	version string
	sha     string
	size    int64
	tmp     string
	dst     string
	f       *os.File
	h       hash.Hash
	total   int64
	done    atomic.Bool // set before the stream is dropped: the stop hook then does nothing
}

func (u *updateSink) Write(p []byte) (int, error) {
	if u.done.Load() {
		return 0, errors.New("update finished")
	}
	if u.total+int64(len(p)) > u.size {
		u.finish("failed:size")
		return 0, errors.New("too many bytes")
	}
	if _, err := u.f.Write(p); err != nil {
		u.finish("failed:write")
		return 0, err
	}
	u.h.Write(p)
	u.total += int64(len(p))
	if u.total == u.size {
		u.install()
	}
	return len(p), nil
}

func (u *updateSink) install() {
	if hex.EncodeToString(u.h.Sum(nil)) != u.sha {
		u.finish("failed:checksum")
		return
	}
	if err := u.f.Close(); err != nil {
		u.finish("failed:write")
		return
	}
	if err := os.Chmod(u.tmp, 0o755); err != nil {
		u.finish("failed:install")
		return
	}
	if err := installBinary(u.tmp, u.dst, u.m.stateDir, u.version); err != nil {
		u.finish("failed:install")
		return
	}
	u.done.Store(true)
	u.m.log.Info("agent updated, restarting", "version", u.version)
	u.m.drop(u.ctx, u.id, "updated:"+u.version)
	time.AfterFunc(500*time.Millisecond, func() { u.m.exit(0) })
}

// finish reports a failure and removes the partial file.
func (u *updateSink) finish(reason string) {
	u.done.Store(true)
	u.f.Close()
	os.Remove(u.tmp)
	u.m.drop(u.ctx, u.id, reason)
}

// abort is the stream's stop hook: the server closed the stream before the end.
func (u *updateSink) abort() {
	if u.done.Swap(true) {
		return
	}
	u.f.Close()
	os.Remove(u.tmp)
}

func (m *mux) startUpdate(ctx context.Context, id uint32, req openRequest) error {
	if len(m.updateKey) != ed25519.PublicKeySize {
		return m.refuse(ctx, id, "update_not_configured")
	}
	sig, err := base64.StdEncoding.DecodeString(req.Signature)
	if err != nil || len(sig) != ed25519.SignatureSize || !sha256HexRe.MatchString(req.Sha256) ||
		!updateVersionRe.MatchString(req.Version) || req.Size <= 0 || req.Size > updateMaxSize {
		return m.refuse(ctx, id, "bad_request")
	}
	if !ed25519.Verify(m.updateKey, UpdateMessage(req.Version, runtime.GOARCH, req.Sha256), sig) {
		return m.refuse(ctx, id, "bad_signature")
	}
	if !versionNewer(req.Version, m.version) {
		return m.refuse(ctx, id, "not_newer")
	}
	if !m.updating.CompareAndSwap(false, true) {
		return m.refuse(ctx, id, "too_many_streams")
	}
	// not under m.mu: the stop hook runs while drop holds it
	release := func() { m.updating.Store(false) }
	dst := m.exePath
	tmp := dst + ".new"
	_ = os.Remove(tmp)
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o700)
	if err != nil {
		release()
		return m.refuse(ctx, id, "update_unavailable")
	}
	u := &updateSink{m: m, ctx: ctx, id: id, version: req.Version, sha: req.Sha256, size: req.Size,
		tmp: tmp, dst: dst, f: f, h: sha256.New()}
	s := &stream{w: u, in: make(chan []byte, streamBacklog), stop: func() { u.abort(); release() }}
	m.mu.Lock()
	m.open[id] = s
	m.mu.Unlock()
	if err := m.send(ctx, frameOpenOK, id, nil); err != nil {
		s.close()
		return err
	}
	go m.writer(s)
	return nil
}

// configureUpdate enables self-update with the release key from the configuration.
// testUpdateHook lets tests redirect the executable path and the exit.
var testUpdateHook func(*mux)

func (m *mux) configureUpdate(publicKeyB64, version, stateDir string) {
	m.version = version
	m.stateDir = stateDir
	m.exit = os.Exit
	if exe, err := os.Executable(); err == nil {
		m.exePath = exe
	}
	if k, err := base64.StdEncoding.DecodeString(publicKeyB64); err == nil && len(k) == ed25519.PublicKeySize {
		m.updateKey = k
	}
	if testUpdateHook != nil {
		testUpdateHook(m)
	}
}
