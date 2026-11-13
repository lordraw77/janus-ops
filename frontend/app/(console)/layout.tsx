import type { ReactNode } from "react";

import { Sidebar } from "@/components/Sidebar";
import { AuthGate } from "@/lib/auth";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return (
    <AuthGate>
      <div className="flex min-h-screen">
        <Sidebar />
        <main className="flex-1 overflow-x-hidden p-6">{children}</main>
      </div>
    </AuthGate>
  );
}
