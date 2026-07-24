// The gateway's wire types, hand-written to mirror `app/api/schemas.py` and the
// SSE frames `app/api/chat.py` emits. Hand-written on purpose: they are small,
// and generating a client from the OpenAPI schema would be a third toolchain to
// justify for four frame shapes and two response bodies.

// --- SSE frames (POST /v1/chat) -------------------------------------------
//
// The stream is `delta* -> (tool_call+ -> usage)* -> delta* -> usage -> done`.
// Every model call reports its own `usage`, so a tool-using request carries
// several — which is the whole point of showing them.

export interface DeltaFrame {
  event: "delta";
  text: string;
}

export interface ToolCallFrame {
  event: "tool_call";
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface UsageFrame {
  event: "usage";
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
}

export interface DoneFrame {
  event: "done";
  // "end_turn" | "tool_use" | "max_tokens" | "refusal" | "error"
  stop_reason: string;
  error: string | null;
}

export type ChatFrame = DeltaFrame | ToolCallFrame | UsageFrame | DoneFrame;

// --- REST bodies ----------------------------------------------------------

export interface Health {
  status: string;
  app: string;
  environment: string;
  provider: string;
  model: string;
  prompt: string;
}

export interface Usage {
  since: string;
  requests: number;
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  cache_hit_rate: number;
  by_model: Record<string, number>;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}
