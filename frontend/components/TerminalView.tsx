"use client";

import "@xterm/xterm/css/xterm.css";

import { useEffect, useRef } from "react";

export type TerminalStatus =
  | { state: "connecting" }
  | { state: "connected"; user: string; recorded: boolean }
  | { state: "error"; code: string }
  | { state: "closed"; reason: string };

/** What the page can do with a live terminal (broadcast, snippets). */
export type TerminalHandle = {
  /** Send keystrokes as if typed in this terminal. */
  input: (data: string) => void;
  /** Ask the server to type a snippet (audited server side). */
  snippet: (id: string, broadcast: boolean) => void;
};

const NOTICES: Record<string, string> = {
  snippet_not_found: "Snippet not found (deleted, or not visible to you).",
};

export const TERMINAL_THEME = {
  background: "#090A0F",
  foreground: "#F1F5F9",
  cursor: "#10B981",
  cursorAccent: "#090A0F",
  selectionBackground: "#334155",
  black: "#1B1E2B",
  brightBlack: "#64748B",
  red: "#EF4444",
  green: "#10B981",
  yellow: "#F59E0B",
  blue: "#818CF8",
  magenta: "#8B5CF6",
  cyan: "#22D3EE",
  white: "#CBD5E1",
  brightWhite: "#F1F5F9",
};

export function monoFont(): string {
  const loaded = getComputedStyle(document.documentElement).getPropertyValue("--font-jetbrains-mono");
  return `${loaded ? `${loaded}, ` : ""}ui-monospace, monospace`;
}

/**
 * One live SSH session: xterm.js <-> WebSocket <-> Janus-Ops <-> SSH.
 * The browser only ever sees terminal bytes; credentials stay on the server.
 * `routeInput` lets the page take over keystrokes (broadcast): when it returns true the
 * terminal does not send them itself.
 */
export function TerminalView({
  nodeId,
  active,
  onStatus,
  onHandle,
  routeInput,
}: {
  nodeId: string;
  active: boolean;
  onStatus: (status: TerminalStatus) => void;
  onHandle?: (handle: TerminalHandle | null) => void;
  routeInput?: (data: string) => boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const fitRef = useRef<(() => void) | null>(null);
  const statusRef = useRef(onStatus);
  const handleRef = useRef(onHandle);
  const routeRef = useRef(routeInput);

  useEffect(() => {
    statusRef.current = onStatus;
    handleRef.current = onHandle;
    routeRef.current = routeInput;
  }, [onStatus, onHandle, routeInput]);

  useEffect(() => {
    let disposed = false;
    let cleanup = () => {};

    void (async () => {
      // xterm.js touches `window` at import time: load it in the browser only
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed || !container.current) return;
      const term = new Terminal({
        theme: TERMINAL_THEME,
        fontFamily: monoFont(),
        fontSize: 13,
        cursorBlink: true,
        scrollback: 5000,
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(container.current);
      const safeFit = () => {
        if (container.current && container.current.offsetWidth > 0) fit.fit();
      };
      safeFit();
      fitRef.current = safeFit;

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const params = new URLSearchParams({ node_id: nodeId, cols: String(term.cols), rows: String(term.rows) });
      const ws = new WebSocket(`${proto}//${window.location.host}/api/v1/terminal/ws?${params}`);
      ws.binaryType = "arraybuffer";
      let ended = false;

      ws.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
        if (typeof event.data !== "string") {
          term.write(new Uint8Array(event.data));
          return;
        }
        const msg = JSON.parse(event.data) as { type: string; user?: string; recorded?: boolean; code?: string; reason?: string };
        if (msg.type === "notice") {
          term.write(`\r\n\x1b[33m[Janus-Ops] ${NOTICES[msg.code ?? ""] ?? msg.code}\x1b[0m\r\n`);
          return;
        }
        if (msg.type === "connected") {
          statusRef.current({ state: "connected", user: msg.user ?? "", recorded: Boolean(msg.recorded) });
          term.focus();
        } else if (msg.type === "error") {
          ended = true;
          statusRef.current({ state: "error", code: msg.code ?? "error" });
        } else if (msg.type === "closed") {
          ended = true;
          statusRef.current({ state: "closed", reason: msg.reason ?? "closed" });
        }
      };
      ws.onclose = () => {
        if (!ended) statusRef.current({ state: "closed", reason: "connection_lost" });
        term.options.cursorBlink = false;
      };

      const send = (payload: object) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
      };
      const onData = term.onData((data) => {
        if (!routeRef.current?.(data)) send({ type: "input", data });
      });
      handleRef.current?.({
        input: (data) => send({ type: "input", data }),
        snippet: (id, broadcast) => send({ type: "snippet", id, broadcast }),
      });
      const onResize = term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
      const observer = new ResizeObserver(() => safeFit());
      observer.observe(container.current);

      cleanup = () => {
        handleRef.current?.(null);
        observer.disconnect();
        onData.dispose();
        onResize.dispose();
        ws.close();
        term.dispose();
      };
    })();

    return () => {
      disposed = true;
      cleanup();
    };
  }, [nodeId]);

  // Re-fit when the tab becomes visible again
  useEffect(() => {
    if (active) fitRef.current?.();
  }, [active]);

  return <div ref={container} className="h-full w-full overflow-hidden rounded border border-line bg-canvas p-1" />;
}
