"use client";

import {
  Background,
  BackgroundVariant,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  type Edge,
  type EdgeProps,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
  type ReactFlowInstance,
  getSmoothStepPath,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Bell, GitBranch, ShieldCheck, Terminal, Trash2, Wand2, X, Zap } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { StepFields } from "@/components/WorkflowStepFields";
import { Button, Field, Select, TextInput } from "@/components/ui";
import {
  type FlowEdge,
  type FlowNode,
  STATUS_TONE,
  TRIGGER_ID,
  addStep,
  connect,
  describeStep,
  dropStep,
  relayout,
  removeBranch,
  removeStep,
  renameStep,
  setOutcome,
  setWhen,
  toGraph,
} from "@/lib/workflowGraph";
import {
  type Action,
  type ChannelOption,
  type Definition,
  type NodeOption,
  type Outcome,
  type Step,
  problemSteps,
} from "@/lib/workflows";

const META: Record<Action, { label: string; icon: ReactNode; tone: string }> = {
  exec: { label: "Command", icon: <Terminal className="h-3.5 w-3.5" aria-hidden />, tone: "text-primary" },
  notify: { label: "Notification", icon: <Bell className="h-3.5 w-3.5" aria-hidden />, tone: "text-ok" },
  approval: { label: "Approval", icon: <ShieldCheck className="h-3.5 w-3.5" aria-hidden />, tone: "text-warn" },
};
const TRIGGER_LABEL: Record<string, string> = {
  manual: "By hand",
  interval: "On a schedule",
  alert: "When a check raises a problem",
  webhook: "When a signed webhook arrives",
};
const OUTCOME_LABEL: Record<Outcome, string> = { ok: "succeeded", failed: "failed", skipped: "was skipped" };

type NodeData = FlowNode["data"];
type RFNode = Node<NodeData, "trigger" | "step">;

// ---------------------------------------------------------------- nodes
function TriggerNode({ data }: NodeProps<RFNode>) {
  const t = data.trigger;
  return (
    <div data-testid="node-trigger" className="w-[260px] rounded border border-primary/60 bg-elevated p-2 text-xs">
      <div className="flex items-center gap-1.5 font-semibold text-primary">
        <Zap className="h-3.5 w-3.5" aria-hidden /> When this starts
      </div>
      <div className="mt-1 text-fg-secondary">
        {TRIGGER_LABEL[t?.type ?? "manual"]}
        {t?.type === "interval" && t.interval_s ? ` · every ${t.interval_s} s` : ""}
      </div>
      <Handle type="source" position={Position.Bottom} id="out" isConnectable={false} className="!bg-primary" />
    </div>
  );
}

function StepNode({ data, selected }: NodeProps<RFNode>) {
  const s = data.step as Step;
  const meta = META[s.action];
  const waits = s.action === "approval" || (s.action === "exec" && !s.params.read_only);
  const tone = data.status ? (STATUS_TONE[data.status] ?? "border-line") : data.issue ? "border-crit" : selected ? "border-primary" : "border-line";
  return (
    <div
      data-testid={`node-${s.id}`}
      data-status={data.status ?? ""}
      className={`w-[260px] rounded border-2 bg-canvas p-2 text-xs ${tone} ${selected ? "shadow-[0_0_0_2px_rgba(16,185,129,0.25)]" : ""}`}
    >
      <Handle type="target" position={Position.Top} id="in" isConnectable={false} className="!bg-line" />
      <Handle type="target" position={Position.Left} id="cond" title="Drop a branch here" className="!h-3 !w-3 !bg-warn" />
      <div className="flex items-center gap-1.5">
        <span className={`flex items-center gap-1 font-semibold ${meta.tone}`}>
          {meta.icon} {meta.label}
        </span>
        <span className="ml-auto font-mono text-[10px] text-fg-muted">{s.id}</span>
      </div>
      <div className="mt-1 truncate text-fg-secondary" title={describeStep(s)}>
        {describeStep(s)}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {data.status && <span className="rounded border border-current px-1 text-[10px]">{data.status.replace("_", " ")}</span>}
        {s.when && (
          <span className="flex items-center gap-1 rounded border border-warn/50 px-1 text-[10px] text-warn">
            <GitBranch className="h-3 w-3" aria-hidden /> if {s.when.step} {OUTCOME_LABEL[s.when.outcome]}
          </span>
        )}
        {waits && <span className="rounded border border-warn/50 px-1 text-[10px] text-warn">waits for a person</span>}
        {s.on_failure === "continue" && <span className="rounded border border-line px-1 text-[10px] text-fg-muted">carries on if it fails</span>}
        {data.issue && <span className="rounded border border-crit/60 px-1 text-[10px] text-crit">needs attention</span>}
      </div>
      <Handle type="source" position={Position.Bottom} id="out" isConnectable={false} className="!bg-line" />
      <Handle type="source" position={Position.Right} id="branch" title="Drag to another step to make a branch" className="!h-3 !w-3 !bg-warn" />
    </div>
  );
}

// ---------------------------------------------------------------- edges
type BranchData = { outcome: Outcome; readOnly: boolean; onOutcome: (target: string, outcome: Outcome) => void; onRemove: (target: string) => void };

function BranchEdge({ id, source, target, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, markerEnd }: EdgeProps) {
  const d = data as unknown as BranchData;
  const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 12 });
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={{ stroke: "var(--color-warn, #d97706)", strokeDasharray: "6 4", strokeWidth: 1.6 }} />
      <EdgeLabelRenderer>
        <div
          className="nodrag nopan absolute flex items-center gap-1 rounded border border-warn/60 bg-canvas px-1 py-0.5 text-[10px] text-warn"
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`, pointerEvents: "all" }}
          data-testid={`branch-${source}-${target}`}
        >
          {d.readOnly ? (
            <span>{OUTCOME_LABEL[d.outcome]}</span>
          ) : (
            <>
              <select aria-label={`Outcome of the branch to ${target}`} value={d.outcome} onChange={(e) => d.onOutcome(target, e.target.value as Outcome)} className="bg-canvas text-[10px] outline-none">
                <option value="ok">succeeded</option>
                <option value="failed">failed</option>
                <option value="skipped">was skipped</option>
              </select>
              <button type="button" title="Remove the branch" aria-label={`Remove the branch to ${target}`} onClick={() => d.onRemove(target)} className="text-fg-muted hover:text-crit">
                <X className="h-3 w-3" aria-hidden />
              </button>
            </>
          )}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes = { trigger: TriggerNode, step: StepNode };
const edgeTypes = { branch: BranchEdge };

// ---------------------------------------------------------------- the canvas
export function WorkflowGraph({
  value,
  onChange,
  nodes: nodeOptions = [],
  channels = [],
  channelsReadable = true,
  readOnly = false,
  statuses,
  height = 520,
}: {
  value: Definition;
  onChange?: (next: Definition) => void;
  nodes?: NodeOption[];
  channels?: ChannelOption[];
  channelsReadable?: boolean;
  /** draw only (the run view): no dragging, no panel */
  readOnly?: boolean;
  /** step id -> status of the run, to colour the nodes */
  statuses?: Record<string, string>;
  height?: number;
}) {
  const change = useCallback((next: Definition) => onChange?.(next), [onChange]);
  const issues = useMemo(() => problemSteps(value), [value]);
  const graph = useMemo(() => toGraph(value, statuses ?? {}, issues), [value, statuses, issues]);
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const instance = useRef<ReactFlowInstance<RFNode, Edge> | null>(null);

  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<RFNode>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);

  const onOutcome = useCallback((target: string, outcome: Outcome) => change(setOutcome(value, target, outcome)), [change, value]);
  const onRemove = useCallback((target: string) => change(removeBranch(value, target)), [change, value]);

  // the definition is the single source of truth: whenever it changes the canvas is redrawn from it
  useEffect(() => {
    setRfNodes((previous) =>
      graph.nodes.map((n) => ({
        ...n,
        deletable: n.type !== "trigger" && !readOnly,
        selected: previous.find((p) => p.id === n.id)?.selected ?? false,
      })) as RFNode[],
    );
  }, [graph.nodes, readOnly, setRfNodes]);
  useEffect(() => {
    setRfEdges(graph.edges.map((e) => asEdge(e, readOnly, onOutcome, onRemove)));
  }, [graph.edges, readOnly, onOutcome, onRemove, setRfEdges]);

  const fit = useCallback(() => {
    window.setTimeout(() => void instance.current?.fitView({ padding: 0.2, duration: 200 }), 60);
  }, []);

  const step = value.steps.find((s) => s.id === selected) ?? null;
  const index = step ? value.steps.indexOf(step) : -1;

  function add(action: Action) {
    const next = addStep(value, action);
    change(next);
    setSelected(next.steps[next.steps.length - 1].id);
    setNote(null);
    fit();
  }

  return (
    <div className="space-y-2">
      {!readOnly && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-fg-muted">Add a step:</span>
          {(Object.keys(META) as Action[]).map((a) => (
            <Button key={a} onClick={() => add(a)}>
              {META[a].icon} {META[a].label}
            </Button>
          ))}
          <Button onClick={() => { change(relayout(value)); setNote(null); fit(); }} title="Put the steps back in a tidy column">
            <Wand2 className="h-3.5 w-3.5" aria-hidden /> Tidy up
          </Button>
          <span className="ml-auto text-[11px] text-fg-muted">
            Runs top to bottom. Drag the amber dot of a step onto another step to run it only after that outcome.
          </span>
        </div>
      )}
      {note && (
        <div role="status" className="rounded border border-warn/40 bg-warn/10 px-2 py-1 text-[11px] text-warn">
          {note}
        </div>
      )}
      <div className={`grid gap-3 ${readOnly ? "" : "lg:grid-cols-[1fr_22rem]"}`}>
        <div style={{ height }} className="dark overflow-hidden rounded border border-line bg-canvas" data-testid="workflow-canvas">
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.3}
            maxZoom={1.6}
            nodesDraggable={!readOnly}
            nodesConnectable={!readOnly}
            elementsSelectable={!readOnly}
            deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
            onInit={(i) => {
              instance.current = i;
            }}
            onNodeClick={(_, n) => !readOnly && setSelected(n.id === TRIGGER_ID ? null : n.id)}
            onPaneClick={() => setSelected(null)}
            onNodeDragStop={(_, n) => {
              if (readOnly || n.id === TRIGGER_ID) return;
              change(dropStep(value, n.id, n.position));
            }}
            isValidConnection={(c) => Boolean(c.source && c.target && c.source !== c.target && c.source !== TRIGGER_ID && c.target !== TRIGGER_ID)}
            onConnect={(c) => {
              if (c.sourceHandle !== "branch" || c.targetHandle !== "cond") return;
              const r = connect(value, c.source, c.target, "failed");
              change(r.def);
              setNote(r.note ?? null);
              if (r.note) fit();
            }}
            onNodesDelete={(gone) => {
              let next = value;
              for (const g of gone) if (g.id !== TRIGGER_ID) next = removeStep(next, g.id);
              change(next);
              setSelected(null);
            }}
            onEdgesDelete={(gone) => {
              let next = value;
              for (const g of gone) if (g.type === "branch") next = removeBranch(next, g.target);
              if (next !== value) change(next);
            }}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        {!readOnly && (
          <aside className="max-h-[calc(100%)] overflow-y-auto rounded border border-line bg-elevated p-3 text-xs" style={{ maxHeight: height }} aria-label="Selected step">
            {!step ? (
              <p className="text-fg-muted">
                Select a step to edit it. Drag steps to reorder them (the run goes top to bottom), and drag the amber dot of one step onto another to
                make the second run only after a given outcome of the first.
              </p>
            ) : (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <span className={`flex items-center gap-1 font-semibold ${META[step.action].tone}`}>
                    {META[step.action].icon} {META[step.action].label}
                  </span>
                  <button
                    type="button"
                    title="Remove this step"
                    aria-label="Remove this step"
                    onClick={() => { change(removeStep(value, step.id)); setSelected(null); }}
                    className="ml-auto rounded p-1 text-fg-muted hover:bg-canvas hover:text-crit"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
                <StepFields
                  compact
                  step={step}
                  nodes={nodeOptions}
                  channels={channels}
                  channelsReadable={channelsReadable}
                  onParams={(patch) => change({ ...value, steps: value.steps.map((s) => (s.id === step.id ? { ...s, params: { ...s.params, ...patch } } : s)) })}
                  onRename={(id) => { change(renameStep(value, step.id, id)); setSelected(id); }}
                />
                <div className="space-y-2 border-t border-line pt-2">
                  {index > 0 ? (
                    <>
                      <label className="flex items-center gap-2 text-fg-secondary">
                        <input
                          type="checkbox"
                          checked={Boolean(step.when)}
                          onChange={(e) => change(setWhen(value, step.id, e.target.checked ? { step: value.steps[index - 1].id, outcome: "ok" } : null))}
                        />
                        Run only if an earlier step…
                      </label>
                      {step.when && (
                        <div className="grid gap-2">
                          <Select value={step.when.step} aria-label="Earlier step" onChange={(e) => change(setWhen(value, step.id, { ...step.when!, step: e.target.value }))}>
                            {value.steps.slice(0, index).map((p, k) => (
                              <option key={p.id} value={p.id}>
                                {k + 1}. {p.id}
                              </option>
                            ))}
                          </Select>
                          <Select value={step.when.outcome} aria-label="Outcome" onChange={(e) => change(setWhen(value, step.id, { ...step.when!, outcome: e.target.value as Outcome }))}>
                            <option value="ok">succeeded</option>
                            <option value="failed">failed</option>
                            <option value="skipped">was skipped</option>
                          </Select>
                          <Field label="…and its output contains (optional)">
                            <TextInput value={step.when.contains ?? ""} maxLength={200} onChange={(e) => change(setWhen(value, step.id, { ...step.when!, contains: e.target.value }))} />
                          </Field>
                        </div>
                      )}
                    </>
                  ) : (
                    <p className="text-fg-muted">The first step always runs.</p>
                  )}
                  {step.action !== "approval" && (
                    <label className="flex items-center gap-2 text-fg-secondary">
                      <input
                        type="checkbox"
                        checked={step.on_failure === "continue"}
                        onChange={(e) => change({ ...value, steps: value.steps.map((s) => (s.id === step.id ? { ...s, on_failure: e.target.checked ? "continue" : "fail" } : s)) })}
                      />
                      If this step fails, carry on
                    </label>
                  )}
                </div>
              </div>
            )}
          </aside>
        )}
      </div>
    </div>
  );
}

function asEdge(e: FlowEdge, readOnly: boolean, onOutcome: BranchData["onOutcome"], onRemove: BranchData["onRemove"]): Edge {
  const arrow = { type: MarkerType.ArrowClosed, width: 16, height: 16 };
  if (e.type === "branch") {
    return {
      id: e.id,
      type: "branch" as const,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle,
      targetHandle: e.targetHandle,
      markerEnd: { ...arrow, color: "var(--color-warn, #d97706)" },
      deletable: !readOnly,
      data: { outcome: e.data?.outcome ?? "failed", readOnly, onOutcome, onRemove } as unknown as Record<string, unknown>,
    };
  }
  return {
    id: e.id,
    type: "smoothstep" as const,
    source: e.source,
    target: e.target,
    markerEnd: { ...arrow, color: "var(--color-line-strong, #475569)" },
    style: { stroke: "var(--color-line-strong, #475569)", strokeWidth: 1.6 },
    deletable: false,
    selectable: false,
    focusable: false,
    data: {} as Record<string, unknown>,
  };
}
