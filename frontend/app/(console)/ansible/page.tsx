"use client";

import { AnsibleConsole } from "@/components/Ansible";
import { RequirePermission } from "@/lib/auth";

export default function AnsiblePage() {
  return (
    <RequirePermission permission="ansible.read">
      <AnsibleConsole />
    </RequirePermission>
  );
}
