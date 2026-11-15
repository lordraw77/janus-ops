export type ActionRisk = "low" | "medium" | "high";
export type ActionStatus = "proposed" | "approved" | "rejected";

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
};

export type DiagnosisDetail = Diagnosis & { prompt: string; response: string | null };

export type AiStatus = { enabled: boolean; model: string | null };

export type OpenProblem = {
  id: string;
  check_name: string;
  node_name: string;
  state: string;
  opened_at: string;
  last_output: string | null;
};
