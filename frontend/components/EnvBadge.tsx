"use client";

import { envClass, useEnvironments } from "@/lib/environments";

export function EnvBadge({ env, className = "" }: { env: string; className?: string }) {
  const { list } = useEnvironments();
  const found = list.find((e) => e.name === env);
  return (
    <span
      title={found?.label}
      className={`inline-block shrink-0 rounded border px-1.5 font-mono text-[10px] uppercase ${envClass(found?.color)} ${className}`}
    >
      {env}
    </span>
  );
}
