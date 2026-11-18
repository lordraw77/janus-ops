"use client";

import { Suspense } from "react";

import { CopilotConsole } from "@/components/Copilot";
import { RequirePermission } from "@/lib/auth";

export default function CopilotPage() {
  return (
    <RequirePermission permission="ai.use">
      <Suspense fallback={null}>
        <CopilotConsole />
      </Suspense>
    </RequirePermission>
  );
}
