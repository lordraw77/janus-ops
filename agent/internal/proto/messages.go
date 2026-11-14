// Package proto holds the agent ⇄ server messages (contract: proto/agent-protocol.md).
package proto

import "encoding/json"

// Version of the protocol spoken by this agent.
const Version = 1

// AuthContext prefixes the signed challenge so the signature cannot be reused elsewhere.
const AuthContext = "janus-agent-auth-v1"

// MaxMessage is the largest frame either side accepts.
const MaxMessage = 1 << 20

// Envelope reads the type of any incoming message.
type Envelope struct {
	T string `json:"t"`
}

type Challenge struct {
	T     string `json:"t"`
	V     int    `json:"v"`
	Nonce string `json:"nonce"`
}

type Policy struct {
	AllowExec   bool `json:"allow_exec" yaml:"allow_exec"`
	AllowPTY    bool `json:"allow_pty" yaml:"allow_pty"`
	AllowDocker bool `json:"allow_docker" yaml:"allow_docker"`
}

type Hello struct {
	T           string `json:"t"`
	V           int    `json:"v"`
	NodeID      string `json:"node_id"`
	PublicKey   string `json:"public_key"`
	Signature   string `json:"signature"`
	EnrollToken string `json:"enroll_token,omitempty"`
	Version     string `json:"version"`
	Arch        string `json:"arch"`
	OS          string `json:"os"`
	Kernel      string `json:"kernel"`
	Hostname    string `json:"hostname"`
	IntervalS   int    `json:"interval_s"`
	Policy      Policy `json:"policy"`
	// Features this agent supports; the server uses nothing an agent did not announce.
	Features []string `json:"features"`
}

// Features of this agent version.
var Features = []string{"streams"}

type Welcome struct {
	T         string `json:"t"`
	V         int    `json:"v"`
	IntervalS int    `json:"interval_s"`
	Enrolled  bool   `json:"enrolled"`
}

type Error struct {
	T    string `json:"t"`
	Code string `json:"code"`
}

// Point is one value of one series at the sample's timestamp.
type Point struct {
	N string            `json:"n"`
	L map[string]string `json:"l,omitempty"`
	V float64           `json:"v"`
}

// Sample is everything collected in one cycle.
type Sample struct {
	TS     int64   `json:"ts"` // Unix milliseconds
	Points []Point `json:"points"`
}

type Metrics struct {
	T   string `json:"t"`
	Seq uint64 `json:"seq"`
	// Samples are sent as stored in the buffer: no decode/encode round trip.
	Samples []json.RawMessage `json:"samples"`
}

type Ack struct {
	T   string `json:"t"`
	Seq uint64 `json:"seq"`
}

type Process struct {
	PID      int     `json:"pid"`
	Name     string  `json:"name"`
	Cmd      string  `json:"cmd,omitempty"`
	User     string  `json:"user,omitempty"`
	CPUPct   float64 `json:"cpu_pct"`
	RSSBytes uint64  `json:"rss_bytes"`
}

type Listener struct {
	Proto   string `json:"proto"`
	Addr    string `json:"addr"`
	Port    int    `json:"port"`
	Process string `json:"process,omitempty"`
}

type FactsData struct {
	UptimeS     float64    `json:"uptime_s"`
	FailedUnits []string   `json:"failed_units"`
	Listening   []Listener `json:"listening"`
	TopCPU      []Process  `json:"top_cpu"`
	TopMem      []Process  `json:"top_mem"`
}

type Facts struct {
	T    string    `json:"t"`
	TS   int64     `json:"ts"`
	Data FactsData `json:"data"`
}
