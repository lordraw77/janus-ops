"use client";

import { OperateConsole } from "@/components/Operate";
import { RequirePermission } from "@/lib/auth";

export default function OperatePage() {
  return (
    <RequirePermission permission="ai.operate">
      <OperateConsole />
    </RequirePermission>
  );
}
