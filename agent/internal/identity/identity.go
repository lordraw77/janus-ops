// Package identity holds the agent's ed25519 key: generated on the node, never sent anywhere.
package identity

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"github.com/lordraw77/janus-ops/agent/internal/proto"
)

type Identity struct {
	key ed25519.PrivateKey
}

// LoadOrCreate reads <dir>/agent.key, creating the key (mode 0600) on first start.
func LoadOrCreate(dir string) (*Identity, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, "agent.key")
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return create(path)
	}
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(raw)
	if block == nil || block.Type != "PRIVATE KEY" {
		return nil, fmt.Errorf("%s: not a PEM private key", path)
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	key, ok := parsed.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("%s: not an ed25519 key", path)
	}
	return &Identity{key: key}, nil
}

func create(path string) (*Identity, error) {
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, err
	}
	// O_EXCL: two agents starting at once must not overwrite each other's key
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, err
	}
	if err := pem.Encode(f, &pem.Block{Type: "PRIVATE KEY", Bytes: der}); err != nil {
		f.Close()
		os.Remove(path)
		return nil, err
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	return &Identity{key: key}, nil
}

// PublicKey is the raw 32-byte public key, base64-encoded.
func (i *Identity) PublicKey() string {
	return base64.StdEncoding.EncodeToString(i.key.Public().(ed25519.PublicKey))
}

// SignChallenge signs the server nonce bound to this node and server host.
func (i *Identity) SignChallenge(nonce, nodeID, host string) string {
	msg := ChallengeMessage(nonce, nodeID, host)
	return base64.StdEncoding.EncodeToString(ed25519.Sign(i.key, msg))
}

// ChallengeMessage is the exact byte string that is signed (proto/agent-protocol.md).
func ChallengeMessage(nonce, nodeID, host string) []byte {
	return []byte(proto.AuthContext + "\n" + nonce + "\n" + nodeID + "\n" + host)
}
