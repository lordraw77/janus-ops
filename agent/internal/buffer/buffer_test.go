package buffer

import (
	"path/filepath"
	"testing"
	"time"
)

func TestAppendAfterAck(t *testing.T) {
	path := filepath.Join(t.TempDir(), "b.db")
	b, err := Open(path, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	for i := int64(0); i < 5; i++ {
		if _, err := b.Append(1000+i, []byte(`{"i":`+string(rune('0'+i))+`}`)); err != nil {
			t.Fatal(err)
		}
	}
	recs, _ := b.After(0, 3)
	if len(recs) != 3 || recs[0].Seq != 1 || string(recs[2].Data) != `{"i":2}` {
		t.Fatalf("after(0,3) = %+v", recs)
	}
	recs, _ = b.After(3, 10)
	if len(recs) != 2 || recs[0].Seq != 4 {
		t.Fatalf("after(3) = %+v", recs)
	}
	if err := b.Ack(3); err != nil {
		t.Fatal(err)
	}
	if b.Len() != 2 {
		t.Fatalf("len after ack = %d", b.Len())
	}
	// survives a restart, sequence keeps growing
	b.Close()
	b, err = Open(path, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	seq, _ := b.Append(2000, []byte(`{}`))
	if seq != 6 || b.Len() != 3 {
		t.Fatalf("seq=%d len=%d", seq, b.Len())
	}
	recs, _ = b.After(0, 10)
	if recs[0].Seq != 4 {
		t.Fatalf("oldest after restart = %d", recs[0].Seq)
	}
}

func TestOldSamplesExpire(t *testing.T) {
	b, err := Open(filepath.Join(t.TempDir(), "b.db"), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	for _, ts := range []int64{0, 30_000, 59_000, 61_000, 120_000} {
		if _, err := b.Append(ts, []byte(`{}`)); err != nil {
			t.Fatal(err)
		}
	}
	// at 120 s, everything older than 60 s is gone
	recs, _ := b.After(0, 10)
	if len(recs) != 2 || recs[0].TS != 61_000 {
		t.Fatalf("kept = %+v", recs)
	}
}
