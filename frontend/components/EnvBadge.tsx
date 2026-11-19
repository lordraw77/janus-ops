"use client";

import { envStyle } from "@/lib/envColor";
import { useEnvironments } from "@/lib/environments";

export function EnvBadge({ env, className = "" }: { env: string; className?: string }) {
  const { list } = useEnvironments();
  const found = list.find((e) => e.name === env);
  const style = envStyle(found?.color);
  return (
    <span
      title={found?.label}
      style={style}
      className={`inline-block shrink-0 rounded border px-1.5 font-mono text-[10px] uppercase ${style ? "" : "border-line text-fg-muted"} ${className}`}
    >
      {env}
    </span>
  );
}
