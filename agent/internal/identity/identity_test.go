package identity

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"
)

func TestKeyIsCreatedOncePrivateAndSigns(t *testing.T) {
	dir := t.TempDir()
	a, err := LoadOrCreate(dir)
	if err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(filepath.Join(dir, "agent.key"))
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("key file: %v %v", err, st)
	}
	b, err := LoadOrCreate(dir)
	if err != nil || b.PublicKey() != a.PublicKey() {
		t.Fatal("the key must survive restarts")
	}
	pub, _ := base64.StdEncoding.DecodeString(a.PublicKey())
	sig, _ := base64.StdEncoding.DecodeString(a.SignChallenge("bm9uY2U=", "node", "host:443"))
	if !ed25519.Verify(pub, []byte("janus-agent-auth-v1\nbm9uY2U=\nnode\nhost:443"), sig) {
		t.Fatal("signature does not verify")
	}
	if ed25519.Verify(pub, ChallengeMessage("bm9uY2U=", "node", "other:443"), sig) {
		t.Fatal("signature must be bound to the server host")
	}
}
