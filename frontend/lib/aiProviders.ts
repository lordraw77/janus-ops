export type Preset = {
  type: string;
  label: string;
  base_url: string;
  key: boolean;
  notes: string;
  config_fields: string[];
  paid: boolean;
  headers: Record<string, string>;
};

export type ProviderKey = { id: string; label: string; last4: string; status: "ok" | "invalid" | "cooling_down"; reason: string };

export type ProviderModel = {
  model_id: string;
  kind: "chat" | "embedding";
  context_window: number | null;
  tools: boolean | null;
  price_in: number | null;
  price_out: number | null;
  manual: boolean;
  available: boolean;
  test_ok: boolean | null;
  test_at: string | null;
  test_error: string | null;
};

export type AiProvider = {
  id: string;
  slug: string;
  type: string;
  base_url: string;
  enabled: boolean;
  tls_unverified: boolean;
  budget_tokens_daily: number | null;
  budget_tokens_monthly: number | null;
  keys: ProviderKey[];
  models: ProviderModel[];
};

export type RouteEntry = { provider: string; model: string; key_id: string | null };
export type AiRoute = { name: string; protected: boolean; entries: RouteEntry[] };

export type HealthRow = {
  provider: string;
  enabled: boolean;
  entries: { route: string; model: string; state: "ok" | "cooling_down" | "disabled"; reason: string; until: string | null }[];
};

export type UsageRow = {
  provider: string;
  model: string;
  calls: number;
  ok: number;
  fallbacks: number;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
};

export type LlmChoices = {
  configured: boolean;
  routes: AiRoute[];
  models: { provider: string; model: string; tools: boolean | null; spec: string }[];
};
