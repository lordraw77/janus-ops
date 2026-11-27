"use client";

import { Network } from "lucide-react";

import { LanWatch } from "@/components/LanWatch";
import { RequirePermission } from "@/lib/auth";

export default function LanPage() {
  return (
    <RequirePermission permission="inventory.manage">
      <div className="space-y-4">
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <Network className="h-5 w-5 text-telemetry" aria-hidden /> Network watch
        </h1>
        <LanWatch />
      </div>
    </RequirePermission>
  );
}
