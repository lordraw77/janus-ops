import type { ReactNode } from "react";

import { CommandPalette } from "@/components/CommandPalette";
import { Sidebar } from "@/components/Sidebar";
import { AuthGate } from "@/lib/auth";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return (
    <AuthGate>
      <div className="flex min-h-screen">
        <Sidebar />
        <CommandPalette />
        <main className="flex-1 overflow-x-clip p-6">{children}</main>
      </div>
    </AuthGate>
  );
}
