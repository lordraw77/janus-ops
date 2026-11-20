export type ActionRisk = "low" | "medium" | "high";
export type ActionStatus = "proposed" | "approved" | "rejected" | "executing" | "executed" | "failed";

export type DiagnosisAction = {
  id: string;
  title: string;
  description: string;
  command: string | null;
  risk: ActionRisk;
  status: ActionStatus;
  decided_by: string | null;
  decided_at: string | null;
  edited?: boolean;
  executed_by?: string | null;
  result?: ActionResult;
};

export type ActionResult = {
  exit_code: number | null;
  reason: string;
  output: string;
  truncated: boolean;
  argv: string[];
  at: string;
  by: string;
};

export type Diagnosis = {
  id: string;
  problem_id: string | null;
  node_name: string;
  check_name: string;
  status: "pending" | "done" | "error";
  model: string;
  requested_by: string;
  requested_at: string;
  finished_at: string | null;
  summary: string | null;
  actions: DiagnosisAction[];
  error: string | null;
  second_status: "pending" | "done" | "error" | null;
};

export type Comparison = {
  agreement?: "agree" | "partial" | "disagree";
  common?: string[];
  differences?: string[];
  preferred?: "first" | "second" | "both" | "neither";
  verdict?: string;
  judge?: string;
  error?: string;
};

export type DiagnosisDetail = Diagnosis & {
  prompt: string;
  response: string | null;
  second_model: string | null;
  second_summary: string | null;
  second_actions: DiagnosisAction[];
  second_error: string | null;
  comparison: Comparison | null;
};

export type AiStatus = {
  enabled: boolean;
  model: string | null;
  review_enabled: boolean;
  review_model: string | null;
};

export type IacKind = "ansible" | "compose" | "shell";

export type IacResult = {
  id: string;
  node_id: string | null;
  node_name: string;
  kind: IacKind;
  request: string;
  status: "pending" | "done" | "error";
  model: string;
  requested_by: string;
  requested_at: string;
  finished_at: string | null;
  content: string | null;
  notes: string[];
  warnings: string[];
  error: string | null;
};

export type OpenProblem = {
  id: string;
  check_name: string;
  node_name: string;
  state: string;
  opened_at: string;
  last_output: string | null;
};

export type AiSlot = {
  slot: "main" | "review";
  base_url: string | null;
  model: string | null;
  timeout_s: number | null;
  key_set: boolean;
  key_source: "database" | "environment" | "none";
  in_database: boolean;
};

export type AiConfig = { slots: AiSlot[]; vault_available: boolean };
