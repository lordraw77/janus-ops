"use client";

import { CopilotConsole } from "@/components/Copilot";
import { RequirePermission } from "@/lib/auth";

export default function CopilotPage() {
  return (
    <RequirePermission permission="ai.use">
      <CopilotConsole />
    </RequirePermission>
  );
}
