"use client";

import { ArchiveConsole } from "@/components/Archive";
import { RequirePermission } from "@/lib/auth";

export default function ArchivePage() {
  return (
    <RequirePermission permission="archive.read">
      <ArchiveConsole />
    </RequirePermission>
  );
}
