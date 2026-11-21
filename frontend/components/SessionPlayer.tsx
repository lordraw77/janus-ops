"use client";

import "@xterm/xterm/css/xterm.css";

import { RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { monoFont, TERMINAL_THEME } from "@/components/TerminalView";
import { Alert, Button, Modal } from "@/components/ui";
import { message } from "@/lib/api";

type CastEvent = [number, string, string];

export function Player({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [speed, setSpeed] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState(0);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let dispose = () => {};
    void (async () => {
      const res = await fetch(`/api/v1/terminal/sessions/${sessionId}/recording`, { cache: "no-store" });
      if (!res.ok) {
        if (!stopped) setError(message(res.status === 404 ? "not_found" : `http_${res.status}`));
        return;
      }
      const lines = (await res.text()).split("\n").filter(Boolean);
      const header = JSON.parse(lines[0]) as { width: number; height: number };
      const events = lines.slice(1).map((l) => JSON.parse(l) as CastEvent);
      const { Terminal } = await import("@xterm/xterm");
      if (stopped || !box.current) return;
      const term = new Terminal({
        theme: TERMINAL_THEME,
        fontFamily: monoFont(),
        fontSize: 12,
        cols: header.width,
        rows: header.height,
        disableStdin: true,
      });
      term.open(box.current);
      dispose = () => term.dispose();
      let i = 0;
      const step = () => {
        if (stopped || i >= events.length) return;
        const [t, kind, data] = events[i++];
        if (kind === "o") term.write(data);
        else if (kind === "r") {
          const [c, r] = data.split("x").map(Number);
          term.resize(c, r);
        }
        const next = events[i];
        if (next) {
          // long pauses are capped at 2 s so idle time does not stall the replay
          const gap = Math.min(next[0] - t, 2) / speed;
          timer = setTimeout(step, gap * 1000);
        }
      };
      step();
    })();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      dispose();
    };
  }, [sessionId, speed, run]);

  return (
    <Modal title="Session replay" onClose={onClose}>
      <div className="space-y-2">
        {error && <Alert>{error}</Alert>}
        <div className="flex items-center gap-1 text-xs">
          {[1, 2, 4, 8].map((s) => (
            <Button key={s} variant={s === speed ? "primary" : "secondary"} onClick={() => setSpeed(s)}>
              {s}×
            </Button>
          ))}
          <Button onClick={() => setRun((r) => r + 1)}>
            <RotateCcw className="h-3 w-3" aria-hidden /> Restart
          </Button>
        </div>
        <div ref={box} className="max-h-[60vh] overflow-auto rounded border border-line bg-canvas p-1" />
      </div>
    </Modal>
  );
}
