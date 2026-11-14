// Package buffer stores samples on disk until the server acknowledges them (FR-23).
package buffer

import (
	"encoding/binary"
	"encoding/json"
	"time"

	bolt "go.etcd.io/bbolt"
)

var bucket = []byte("samples")

// Buffer is an ordered queue of samples keyed by a monotonic sequence number.
type Buffer struct {
	db     *bolt.DB
	maxAge time.Duration
}

// Record is one buffered sample: Seq is its key, Data the JSON sent to the server.
type Record struct {
	Seq  uint64
	TS   int64 // Unix milliseconds, used for expiry
	Data json.RawMessage
}

func Open(path string, maxAge time.Duration) (*Buffer, error) {
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	err = db.Update(func(tx *bolt.Tx) error {
		_, err := tx.CreateBucketIfNotExists(bucket)
		return err
	})
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Buffer{db: db, maxAge: maxAge}, nil
}

func (b *Buffer) Close() error { return b.db.Close() }

// value layout: 8-byte big-endian timestamp, then the sample JSON
func encode(ts int64, data []byte) []byte {
	v := make([]byte, 8+len(data))
	binary.BigEndian.PutUint64(v, uint64(ts))
	copy(v[8:], data)
	return v
}

func key(seq uint64) []byte {
	k := make([]byte, 8)
	binary.BigEndian.PutUint64(k, seq)
	return k
}

// Append stores a sample and drops the ones older than maxAge.
func (b *Buffer) Append(ts int64, data []byte) (uint64, error) {
	var seq uint64
	err := b.db.Update(func(tx *bolt.Tx) error {
		bk := tx.Bucket(bucket)
		var err error
		if seq, err = bk.NextSequence(); err != nil {
			return err
		}
		if err := bk.Put(key(seq), encode(ts, data)); err != nil {
			return err
		}
		cutoff := ts - b.maxAge.Milliseconds()
		var expired [][]byte
		c := bk.Cursor()
		for k, v := c.First(); k != nil && int64(binary.BigEndian.Uint64(v[:8])) < cutoff; k, v = c.Next() {
			expired = append(expired, k)
		}
		return deleteKeys(bk, expired)
	})
	return seq, err
}

// After returns up to n records with a sequence greater than seq, oldest first.
func (b *Buffer) After(seq uint64, n int) ([]Record, error) {
	var out []Record
	err := b.db.View(func(tx *bolt.Tx) error {
		c := tx.Bucket(bucket).Cursor()
		for k, v := c.Seek(key(seq + 1)); k != nil && len(out) < n; k, v = c.Next() {
			data := make([]byte, len(v)-8)
			copy(data, v[8:])
			out = append(out, Record{
				Seq:  binary.BigEndian.Uint64(k),
				TS:   int64(binary.BigEndian.Uint64(v[:8])),
				Data: data,
			})
		}
		return nil
	})
	return out, err
}

// Ack deletes every record up to and including seq.
func (b *Buffer) Ack(seq uint64) error {
	return b.db.Update(func(tx *bolt.Tx) error {
		bk := tx.Bucket(bucket)
		var done [][]byte
		c := bk.Cursor()
		for k, _ := c.First(); k != nil && binary.BigEndian.Uint64(k) <= seq; k, _ = c.Next() {
			done = append(done, k)
		}
		return deleteKeys(bk, done)
	})
}

// deleteKeys runs after the scan: deleting under a moving cursor skips entries in bbolt.
func deleteKeys(bk *bolt.Bucket, keys [][]byte) error {
	for _, k := range keys {
		if err := bk.Delete(k); err != nil {
			return err
		}
	}
	return nil
}

// Len is the number of buffered records.
func (b *Buffer) Len() int {
	n := 0
	_ = b.db.View(func(tx *bolt.Tx) error {
		n = tx.Bucket(bucket).Stats().KeyN
		return nil
	})
	return n
}
