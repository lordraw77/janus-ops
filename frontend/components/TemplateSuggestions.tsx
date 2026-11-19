"use client";

import { useState } from "react";

import { type MacroValue, type Template, TemplateApplyDialog } from "@/components/TemplateApply";
import { Alert, Button, Card } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type Suggestion = {
  template_id: string;
  name: string;
  category: string;
  description: string;
  requires: string | null;
  reasons: string[];
  applied: boolean;
  applicable: boolean;
  macros: Record<string, { value: MacroValue; origin: "manual" | "auto" | "default" }>;
};

type Suggestions = { supported: boolean; suggestions: Suggestion[] };

/** Templates worth applying to a node, from the services its agent found running there. */
export function TemplateSuggestions({
  nodeId,
  hasFacts,
  onApplied,
}: {
  nodeId: string;
  /** the agent has reported at least once */
  hasFacts: boolean;
  onApplied: () => void;
}) {
  const { can } = useAuth();
  const found = useResource<Suggestions>(`/templates/suggestions?node_id=${nodeId}`);
  const [applying, setApplying] = useState<Template | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function open(id: string) {
    setError(null);
    const r = await api<Template>(`/templates/${id}`);
    if (!r.ok) return setError(message(r.code));
    setApplying(r.data);
  }

  if (found.error) return <Alert>{found.error}</Alert>;
  const data = found.data;
  if (!data) return null;
  if (!data.supported) {
    // an agent that predates the discovery, or that has not reported yet
    return hasFacts ? (
      <Alert kind="info">
        This agent is too old to detect services. Update it to get template suggestions for this node.
      </Alert>
    ) : null;
  }
  if (data.suggestions.length === 0) return null;

  return (
    <Card title="Detected services">
      <div className="space-y-3">
        {error && <Alert>{error}</Alert>}
        {data.suggestions.map((s) => {
          const auto = Object.entries(s.macros).filter(([, v]) => v.origin === "auto");
          return (
            <div key={s.template_id} className="flex items-start justify-between gap-4 border-t border-line pt-3 first:border-t-0 first:pt-0">
              <div className="min-w-0 space-y-1">
                <div className="text-sm font-medium">
                  {s.name}
                  {s.applied && (
                    <span className="ml-2 rounded bg-ok/15 px-1.5 py-0.5 text-[10px] font-semibold text-ok">applied</span>
                  )}
                </div>
                <div className="text-xs text-fg-muted">Found: {s.reasons.join(", ")}</div>
                {auto.length > 0 && (
                  <div className="font-mono text-[11px] text-fg-muted">
                    {auto.map(([k, v]) => `${k} = ${String(v.value)}`).join(" · ")}
                  </div>
                )}
                {!s.applicable && s.requires && (
                  <div className="text-xs text-warn">The node needs the {s.requires} {["postgres", "fritzbox"].includes(s.requires) ? "service" : "capability"} first (Inventory).</div>
                )}
              </div>
              {can("monitoring.manage") && (
                <Button type="button" disabled={!s.applicable} onClick={() => void open(s.template_id)}>
                  {s.applied ? "Review" : "Apply…"}
                </Button>
              )}
            </div>
          );
        })}
      </div>
      {applying && (
        <TemplateApplyDialog
          template={applying}
          nodeId={nodeId}
          onClose={() => setApplying(null)}
          onApplied={() => {
            found.reload();
            onApplied();
          }}
        />
      )}
    </Card>
  );
}
