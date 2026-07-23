import { createInterface } from "node:readline";
import { Agent } from "@earendil-works/pi-agent-core";
import {
  Type,
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { getModel } from "@earendil-works/pi-ai/compat";


const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Map();
let startMessage = null;
let resolveStart;
const startPromise = new Promise((resolve) => { resolveStart = resolve; });

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }
  if (message.type === "start" && !startMessage) {
    startMessage = message;
    resolveStart(message);
    return;
  }
  if (message.type === "tool_result") {
    const waiter = pending.get(String(message.id || ""));
    if (!waiter) return;
    pending.delete(String(message.id || ""));
    if (message.ok) waiter.resolve(message.result);
    else waiter.reject(new Error(String(message.error || "python_tool_error")));
  }
});

function bridgeCall(name, args, signal) {
  const id = `pi-tool-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  emit({ type: "tool_call", id, name, args });
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      pending.delete(id);
      reject(new Error("tool_call_aborted"));
    };
    if (signal?.aborted) return onAbort();
    signal?.addEventListener("abort", onAbort, { once: true });
    pending.set(id, {
      resolve: (value) => {
        signal?.removeEventListener("abort", onAbort);
        resolve(value);
      },
      reject: (error) => {
        signal?.removeEventListener("abort", onAbort);
        reject(error);
      },
    });
  });
}

const empty = () => Type.Object({}, { additionalProperties: false });
const optionalInt = (options = {}) => Type.Optional(Type.Integer(options));
const optionalString = (options = {}) => Type.Optional(Type.String(options));
const optionalBoolean = (options = {}) => Type.Optional(Type.Boolean(options));

const toolSpecs = [
  {
    name: "browser_open",
    description: "Open the task URL. In full-browser mode Python binds this to the user-authorized target URL.",
    parameters: Type.Object({
      url: Type.String(),
      wait_until: optionalString(),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_snapshot",
    description: "Read the current accessibility snapshot and stable @refs. Use this after every action that changes visible UI.",
    parameters: Type.Object({
      max_text_chars: optionalInt({ minimum: 500, maximum: 12000 }),
      max_links: optionalInt({ minimum: 1, maximum: 100 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_dom_probe",
    description: "Find repeated DOM collections and pagination candidates across open shadow roots.",
    parameters: Type.Object({
      max_candidates: optionalInt({ minimum: 1, maximum: 20 }),
      max_samples: optionalInt({ minimum: 1, maximum: 3 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_network_log",
    description: "Inspect captured requests. Filter for likely target request families and optionally read a few response bodies.",
    parameters: Type.Object({
      resource_type: optionalString(),
      url_pattern: optionalString(),
      include_body: optionalBoolean(),
      max_items: optionalInt({ minimum: 1, maximum: 100 }),
      max_body_items: optionalInt({ minimum: 0, maximum: 50 }),
      max_body_chars: optionalInt({ minimum: 500, maximum: 40000 }),
      after_index: optionalInt({ minimum: 0 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_network_response_body",
    description: "Read one captured response by request index without exposing request headers or credentials.",
    parameters: Type.Object({
      index: Type.Integer({ minimum: 1 }),
      max_chars: optionalInt({ minimum: 500, maximum: 60000 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_checkpoint_evidence",
    description: "Read stable network evidence preserved from a previous Browser Agent attempt. Use list first, then read_response with an evidence_id; this remains valid even when the page had to be reopened and MCP request indexes changed.",
    parameters: Type.Object({
      action: Type.Union([Type.Literal("list"), Type.Literal("read_response")]),
      evidence_id: optionalString(),
      partition: optionalString(),
      max_chars: optionalInt({ minimum: 500, maximum: 60000 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_explore_collection_action",
    description: "Perform exactly one AI-chosen collection action and atomically return snapshot plus network delta. Prefer this for comments, feeds, tables, and lazy collections.",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("click"), Type.Literal("scroll"),
        Type.Literal("infinite_scroll"), Type.Literal("wait"),
        Type.Literal("reload"),
      ]),
      target: optionalString({ description: "CSS selector or current @ref for click" }),
      direction: Type.Optional(Type.Union([Type.Literal("up"), Type.Literal("down")])),
      amount: optionalInt({ minimum: 1, maximum: 8 }),
      max_scrolls: optionalInt({ minimum: 1, maximum: 10 }),
      wait_ms: optionalInt({ minimum: 250, maximum: 5000 }),
      url_pattern: optionalString(),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_action_feedback",
    description: "Perform one generic click/scroll/wait/reload action, then return the new accessibility snapshot and raw network delta. It does not judge or validate the result; you must interpret the evidence.",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("click"), Type.Literal("scroll"),
        Type.Literal("infinite_scroll"), Type.Literal("wait"),
        Type.Literal("reload"),
      ]),
      target: optionalString(),
      target_descriptor: Type.Optional(Type.Object({
        role: optionalString(),
        name: optionalString(),
        selector: optionalString(),
        text: optionalString(),
      }, { additionalProperties: true })),
      direction: Type.Optional(Type.Union([Type.Literal("up"), Type.Literal("down")])),
      amount: optionalInt({ minimum: 1, maximum: 8 }),
      max_scrolls: optionalInt({ minimum: 1, maximum: 20 }),
      wait_ms: optionalInt({ minimum: 100, maximum: 10000 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_manual_login",
    description: "Request a visible manual-login session only when page observations show that target data is actually blocked by authentication.",
    parameters: Type.Object({
      timeout_seconds: optionalInt({ minimum: 30, maximum: 1800 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_auth_probe",
    description: "Read raw post-login page facts. This tool never declares login success; use the observations to set auth.authentication_state and auth.verification_state in submit_parser.",
    parameters: Type.Object({
      target_url: optionalString(),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_activate_comments",
    description: "Reveal or activate a lazy comment component, including components under open shadow roots.",
    parameters: Type.Object({
      max_scrolls: optionalInt({ minimum: 0, maximum: 8 }),
      pause_ms: optionalInt({ minimum: 200, maximum: 3000 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_verify_selectors",
    description: "Batch-check selector counts and sample values before submitting a DOM parser.",
    parameters: Type.Object({
      selectors: Type.Array(Type.Object({
        name: Type.String(),
        selector: Type.String(),
        attribute: optionalString(),
      }, { additionalProperties: false }), { minItems: 1, maxItems: 30 }),
      max_samples: optionalInt({ minimum: 1, maximum: 20 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_click",
    description: "Click a CSS selector or current @ref. Prefer the atomic collection action for paginated collections.",
    parameters: Type.Object({
      selector: Type.String(),
      button: optionalString(),
      double_click: optionalBoolean(),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_scroll",
    description: "Scroll the page a small number of times.",
    parameters: Type.Object({
      direction: Type.Optional(Type.Union([Type.Literal("up"), Type.Literal("down")])),
      amount: optionalInt({ minimum: 1, maximum: 8 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_infinite_scroll",
    description: "Scroll until page height stabilizes, with a bounded maximum.",
    parameters: Type.Object({
      max_scrolls: optionalInt({ minimum: 1, maximum: 20 }),
      pause_ms: optionalInt({ minimum: 200, maximum: 5000 }),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_wait_dynamic",
    description: "Wait for a duration or visible text after a page action.",
    parameters: Type.Object({
      timeout_ms: optionalInt({ minimum: 100, maximum: 30000 }),
      text: optionalString(),
    }, { additionalProperties: false }),
  },
  {
    name: "browser_reload",
    description: "Reload the current target page.",
    parameters: empty(),
  },
  {
    name: "browser_text",
    description: "Read visible body text, bounded by character count.",
    parameters: Type.Object({ max_chars: optionalInt({ minimum: 500, maximum: 30000 }) }, { additionalProperties: false }),
  },
  {
    name: "browser_html",
    description: "Read a bounded HTML excerpt. Use only when snapshot and DOM probe are insufficient.",
    parameters: Type.Object({ max_chars: optionalInt({ minimum: 500, maximum: 30000 }) }, { additionalProperties: false }),
  },
  {
    name: "browser_links",
    description: "Read visible links and destinations.",
    parameters: Type.Object({ max_links: optionalInt({ minimum: 1, maximum: 100 }) }, { additionalProperties: false }),
  },
  {
    name: "browser_frames",
    description: "List frames when the target collection may be inside an iframe.",
    parameters: Type.Object({ include_iframe_elements: optionalBoolean() }, { additionalProperties: false }),
  },
  {
    name: "browser_use_frame",
    description: "Select an observed frame by index for subsequent inspection.",
    parameters: Type.Object({ frame_index: optionalInt({ minimum: 0 }) }, { additionalProperties: false }),
  },
  {
    name: "browser_evaluate",
    description: "Run bounded read-only JavaScript to inspect runtime page state. Never access cookies, storage, credentials, or mutate the page.",
    parameters: Type.Object({ javascript: Type.String() }, { additionalProperties: false }),
  },
];

const parserResultSchema = Type.Object({
  page_type: Type.Union([
    Type.Literal("static"), Type.Literal("dynamic"), Type.Literal("api"),
    Type.Literal("unknown"), Type.Literal("iframe"), Type.Literal("auth_required"),
  ]),
  data_source: Type.Union([
    Type.Literal("dom"), Type.Literal("api"), Type.Literal("embedded_json"),
    Type.Literal("iframe"), Type.Literal("mixed"), Type.Literal("unknown"),
  ]),
  fields: Type.Optional(Type.Array(Type.Object({
    name: Type.String(),
    selector: optionalString(),
    attribute: optionalString(),
  }, { additionalProperties: true }))),
  list_container: Type.Optional(Type.Object({ selector: optionalString() }, { additionalProperties: true })),
  selectors: Type.Optional(Type.Record(Type.String(), Type.String())),
  api_endpoints: Type.Optional(Type.Array(Type.Object({
    url: Type.String(),
    method: optionalString(),
    data_path: optionalString(),
    field_mapping: Type.Optional(Type.Record(Type.String(), Type.String())),
    source: Type.Optional(Type.Union([Type.Literal("observed"), Type.Literal("historical"), Type.Literal("hypothesized")])),
    verified: optionalBoolean(),
    evidence_id: optionalString(),
    response_index: optionalInt({ minimum: 0 }),
  }, { additionalProperties: true }))),
  pagination: Type.Optional(Type.Object({
    type: Type.String(),
    next_selector: optionalString(),
    page_param: optionalString(),
    cursor_param: optionalString(),
    next_cursor_path: optionalString(),
    has_more_path: optionalString(),
    total_path: optionalString(),
  }, { additionalProperties: true })),
  pagination_contract: Type.Optional(Type.Object({
    execution_mode: Type.Union([Type.Literal("direct_http"), Type.Literal("browser_replay")]),
    terminal: Type.Optional(Type.Object({
      path: Type.String(),
      value_means_end: Type.Unknown(),
    }, { additionalProperties: true })),
    observed_transitions: Type.Optional(Type.Array(Type.Object({
      request_cursor: Type.Unknown(),
      next_cursor: Type.Unknown(),
      terminal_raw: Type.Unknown(),
      terminal_observed: optionalBoolean(),
      new_unique_items: optionalInt({ minimum: 0 }),
    }, { additionalProperties: true }))),
    terminal_observation: Type.Optional(Type.Object({
      request_cursor: Type.Unknown(),
      terminal_raw: Type.Unknown(),
      observed_items: optionalInt({ minimum: 0 }),
      total: Type.Optional(Type.Unknown()),
    }, { additionalProperties: true })),
  }, { additionalProperties: true })),
  analysis_status: Type.Optional(Type.Union([
    Type.Literal("complete"), Type.Literal("incomplete"),
  ])),
  interaction_plan: Type.Optional(Type.Array(Type.Record(Type.String(), Type.Unknown()))),
  confidence: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
  page_metadata: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
  auth: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
  analysis_summary: optionalString(),
}, { additionalProperties: true });


function textFromAssistant(message) {
  if (!message || !Array.isArray(message.content)) return "";
  return message.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n");
}

function addUsage(total, current) {
  if (!current || typeof current !== "object") return total;
  const next = { ...total };
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens"]) {
    next[key] = Number(next[key] || 0) + Number(current[key] || 0);
  }
  const currentCost = current.cost && typeof current.cost === "object" ? current.cost : {};
  const totalCost = next.cost && typeof next.cost === "object" ? next.cost : {};
  next.cost = {};
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "total"]) {
    next.cost[key] = Number(totalCost[key] || 0) + Number(currentCost[key] || 0);
  }
  return next;
}

function scriptedRuntime(script) {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses(script.map((step) => {
    if (Array.isArray(step.tools)) {
      return fauxAssistantMessage(
        step.tools.map((item) => fauxToolCall(item.tool, item.args || {})),
        { stopReason: "toolUse" },
      );
    }
    if (step.tool) {
      return fauxAssistantMessage(
        fauxToolCall(step.tool, step.args || {}),
        { stopReason: "toolUse" },
      );
    }
    return fauxAssistantMessage(fauxText(String(step.text || "done")));
  }));
  return {
    model: faux.getModel(),
    streamFn: (model, context, options) => models.streamSimple(model, context, options),
  };
}


function userMessage(text) {
  return {
    role: "user",
    content: [{ type: "text", text: String(text || "") }],
    timestamp: Date.now(),
  };
}

function safeClone(value, fallback = null) {
  try { return JSON.parse(JSON.stringify(value)); }
  catch { return fallback; }
}

function isToolResultMessage(message) {
  return ["tool", "toolResult", "tool_result"].includes(String(message?.role || ""));
}

function assistantToolCallIds(message) {
  const ids = new Set();
  for (const call of (Array.isArray(message?.tool_calls) ? message.tool_calls : [])) {
    if (call?.id != null) ids.add(String(call.id));
  }
  for (const block of (Array.isArray(message?.content) ? message.content : [])) {
    if (!["toolCall", "tool_call"].includes(String(block?.type || ""))) continue;
    const id = block?.id ?? block?.toolCallId ?? block?.tool_call_id;
    if (id != null) ids.add(String(id));
  }
  return ids;
}

function assistantHasToolCalls(message) {
  if (Array.isArray(message?.tool_calls) && message.tool_calls.length) return true;
  return Array.isArray(message?.content)
    && message.content.some((block) => ["toolCall", "tool_call"].includes(String(block?.type || "")));
}

function toolResultId(message) {
  return String(message?.toolCallId ?? message?.tool_call_id ?? message?.tool_use_id ?? message?.id ?? "");
}

function sanitizeTranscript(messages, maxMessages = 160) {
  if (!Array.isArray(messages)) return [];
  const source = safeClone(messages, []).filter((message) => message && typeof message === "object");
  const groups = [];
  for (let i = 0; i < source.length;) {
    const message = source[i];
    if (isToolResultMessage(message)) {
      i += 1;
      continue;
    }
    if (message?.role === "assistant" && assistantHasToolCalls(message)) {
      const expected = assistantToolCallIds(message);
      const observed = new Set();
      const group = [message];
      let j = i + 1;
      while (j < source.length && isToolResultMessage(source[j])) {
        const resultId = toolResultId(source[j]);
        if (expected.size && resultId && !expected.has(resultId)) break;
        group.push(source[j]);
        if (resultId) observed.add(resultId);
        j += 1;
      }
      const complete = expected.size ? [...expected].every((id) => observed.has(id)) : group.length > 1;
      if (complete) groups.push(group);
      i = j;
      continue;
    }
    groups.push([message]);
    i += 1;
  }
  const selected = [];
  let count = 0;
  for (let i = groups.length - 1; i >= 0; i -= 1) {
    const group = groups[i];
    if (selected.length && count + group.length > maxMessages) break;
    if (!selected.length && group.length > maxMessages) continue;
    selected.push(group);
    count += group.length;
  }
  selected.reverse();
  return selected.flat();
}

async function compactBrowserContext(messages) {
  messages = sanitizeTranscript(messages, 160);
  if (!Array.isArray(messages) || messages.length <= 18) return messages;
  const keepFrom = Math.max(0, messages.length - 12);
  return messages.map((message, index) => {
    if (index >= keepFrom) return message;
    if (message?.role === "toolResult") {
      const details = message.details && typeof message.details === "object" ? message.details : {};
      const evidence = details._agent_evidence || {};
      const compact = {
        ok: !message.isError,
        tool: message.toolName,
        error: details.error || null,
        evidence,
        url: details.url || null,
        index: details.index || null,
        evidence_id: details.evidence_id || null,
      };
      return {
        ...message,
        content: [{ type: "text", text: JSON.stringify(compact) }],
        details: compact,
      };
    }
    if (message?.role === "assistant" && Array.isArray(message.content)) {
      return {
        ...message,
        content: message.content.map((block) => (
          block.type === "text" && String(block.text || "").length > 1200
            ? { ...block, text: `${String(block.text).slice(0, 1200)}\n[older browser reasoning compacted]` }
            : block
        )),
      };
    }
    return message;
  });
}

async function main() {
  const start = await startPromise;
  const maxTurns = Math.max(4, Math.min(Number(start.max_turns || 24), 80));
  const maxTools = Math.max(4, Math.min(Number(start.max_tools || 40), 120));
  const operationMode = String(start.operation_mode || "explore");
  const requiredAction = String(start.required_action || "");
  const authResolutionMode = operationMode === "resolve_authentication";
  let candidate = null;
  let assistantText = "";
  let turns = 0;
  let executedTools = 0;
  let scheduledTools = 0;
  let stopReason = "completed";
  let usage = {};
  let toolBudgetExhausted = false;
  let latestEvidence = {};
  let convergence = false;
  let followUpCount = 0;
  let convergenceSteeringSent = false;
  let authVerificationPending = false;
  let manualLoginAttempted = false;
  let manualLoginSucceeded = false;
  let authProbeAttempted = false;
  let agent;

  let model;
  let streamFn;
  if (Array.isArray(start.test_script)) {
    ({ model, streamFn } = scriptedRuntime(start.test_script));
  } else {
    const provider = String(start.provider || "deepseek");
    const modelName = String(start.model || "deepseek-v4-flash");
    const fallback = getModel("deepseek", "deepseek-v4-flash");
    const selected = getModel(provider, modelName) || fallback;
    if (!selected) throw new Error(`pi_model_not_found:${provider}:${modelName}`);
    const requestedMaxTokens = Math.max(
      1024,
      Math.min(
        Number(start.max_completion_tokens || process.env.PI_MAX_COMPLETION_TOKENS || 32768),
        131072,
      ),
    );
    model = {
      ...selected,
      id: modelName,
      name: modelName,
      provider,
      baseUrl: String(start.base_url || selected.baseUrl),
      maxTokens: Math.min(Number(selected.maxTokens || requestedMaxTokens), requestedMaxTokens),
    };
  }

  const fullBrowserToolNames = new Set([
    "browser_open", "browser_snapshot", "browser_dom_probe",
    "browser_network_log", "browser_network_response_body", "browser_checkpoint_evidence", "browser_action_feedback",
    "browser_manual_login", "browser_auth_probe", "browser_activate_comments", "browser_verify_selectors",
    "browser_click", "browser_scroll", "browser_infinite_scroll", "browser_wait_dynamic",
    "browser_reload", "browser_text", "browser_html", "browser_links", "browser_frames",
    "browser_use_frame", "browser_evaluate",
  ]);
  if (String(start.tool_profile || "") !== "full_browser") {
    throw new Error(`unsupported_pi_agent_core_browser_profile:${start.tool_profile}`);
  }
  let activeToolSpecs = toolSpecs.filter((spec) => fullBrowserToolNames.has(spec.name));
  if (Array.isArray(start.allowed_tools)) {
    const allowedNames = new Set(start.allowed_tools.map((name) => String(name)));
    activeToolSpecs = activeToolSpecs.filter((spec) => allowedNames.has(spec.name));
  }
  const readOnlyTools = new Set([
    "browser_snapshot", "browser_dom_probe", "browser_network_log",
    "browser_network_response_body", "browser_checkpoint_evidence",
    "browser_verify_selectors", "browser_auth_probe", "browser_text", "browser_html",
    "browser_links", "browser_frames", "browser_evaluate",
  ]);
  const convergenceTools = new Set([
    "browser_network_log", "browser_network_response_body",
    "browser_checkpoint_evidence", "browser_action_feedback",
  ]);
  const browserTools = activeToolSpecs.map((spec) => ({
    ...spec,
    label: spec.name,
    executionMode: readOnlyTools.has(spec.name) ? "parallel" : "sequential",
    execute: async (_toolCallId, args, signal) => {
      const result = await bridgeCall(spec.name, args || {}, signal);
      executedTools += 1;
      if (spec.name === "browser_manual_login") {
        manualLoginAttempted = true;
        manualLoginSucceeded = result?.ok !== false;
        authVerificationPending = manualLoginSucceeded;
        agent?.steer(userMessage(
          manualLoginSucceeded
            ? "Manual login confirmation is provisional. Call browser_auth_probe next, inspect the page/auth facts, then submit the authentication result. Do not perform generic exploration or change browser fingerprint settings."
            : `Manual login could not be completed: ${JSON.stringify(result)}. Submit the authentication failure facts now; do not continue generic exploration.`,
        ));
      }
      if (spec.name === "browser_auth_probe") {
        authProbeAttempted = true;
        authVerificationPending = false;
        agent?.steer(userMessage(
          `Authentication probe completed: ${JSON.stringify(result)}. Decide verified, required, challenge, rejected, or uncertain from these facts and call submit_parser now.`,
        ));
      }
      if (result?._agent_evidence && typeof result._agent_evidence === "object") {
        latestEvidence = result._agent_evidence;
        convergence = result._agent_evidence.phase === "convergence";
        if (convergence && !convergenceSteeringSent) {
          convergenceSteeringSent = true;
          agent?.steer(userMessage(
            `Target item evidence is now sufficient for convergence: ${JSON.stringify(latestEvidence)}. `
            + "Stop generic exploration. Batch-read the strongest response/checkpoint evidence, then call submit_parser. "
            + "If only pagination proof is missing, submit runtime_validation_required instead of continuing DOM exploration.",
          ));
        }
      }
      return {
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: result,
      };
    },
  }));

  const submitTool = {
    name: "submit_parser",
    label: "Submit final parser result",
    description: "Submit the best evidence-grounded parser result. Call exactly once when evidence is sufficient or only runtime pagination verification remains.",
    parameters: Type.Object({
      parser_result: parserResultSchema,
      reason: optionalString(),
    }, { additionalProperties: false }),
    executionMode: "sequential",
    execute: async (_toolCallId, args) => {
      executedTools += 1;
      candidate = args.parser_result;
      return {
        content: [{ type: "text", text: "Parser result submitted." }],
        details: { submitted: true, reason: args.reason || null },
        terminate: true,
      };
    },
  };
  const allTools = [...browserTools, submitTool];

  function dynamicTools() {
    if (candidate) return [];
    if (authResolutionMode) {
      if (!manualLoginAttempted) {
        return allTools.filter((tool) => tool.name === "browser_manual_login");
      }
      if (manualLoginSucceeded && !authProbeAttempted) {
        return allTools.filter((tool) => tool.name === "browser_auth_probe");
      }
      return allTools.filter((tool) => tool.name === "submit_parser");
    }
    if (authVerificationPending) {
      return allTools.filter((tool) => tool.name === "browser_auth_probe");
    }
    if (!convergence) return allTools;
    return allTools.filter((tool) => convergenceTools.has(tool.name) || tool.name === "submit_parser");
  }

  const initialMessages = authResolutionMode
    ? []
    : sanitizeTranscript(Array.isArray(start.initial_messages) ? start.initial_messages : [], 160);
  agent = new Agent({
    initialState: {
      systemPrompt: String(start.system_prompt || ""),
      model,
      thinkingLevel: String(start.thinking_level || "low"),
      tools: dynamicTools(),
      messages: initialMessages,
    },
    ...(streamFn ? { streamFn } : {}),
    getApiKey: async (providerName) => {
      if (providerName === "deepseek") return process.env.DEEPSEEK_API_KEY;
      return process.env.PI_MODEL_API_KEY || process.env.OPENAI_API_KEY;
    },
    toolExecution: "parallel",
    transformContext: compactBrowserContext,
    prepareNextTurnWithContext: async ({ context }) => ({
      context: { ...context, tools: dynamicTools() },
      thinkingLevel: convergence ? "medium" : "low",
    }),
    beforeToolCall: async ({ toolCall }) => {
      if (authResolutionMode) {
        const expected = !manualLoginAttempted
          ? "browser_manual_login"
          : (manualLoginSucceeded && !authProbeAttempted ? "browser_auth_probe" : "submit_parser");
        if (toolCall.name !== expected) {
          return { block: true, reason: `Authentication protocol requires ${expected} next.` };
        }
      }
      if (authVerificationPending && toolCall.name !== "browser_auth_probe") {
        return { block: true, reason: "Manual login is provisional. Run browser_auth_probe before any other Browser action." };
      }
      if (turns >= maxTurns - 2 && toolCall.name !== "submit_parser") {
        return {
          block: true,
          reason: "Final synthesis reserve reached. Submit the evidence-grounded parser now; runtime pagination validation is allowed when item evidence is complete.",
        };
      }
      if (scheduledTools >= maxTools && toolCall.name !== "submit_parser") {
        toolBudgetExhausted = true;
        return { block: true, reason: "Browser tool budget exhausted; call submit_parser now." };
      }
      if (convergence && !convergenceTools.has(toolCall.name) && toolCall.name !== "submit_parser") {
        return {
          block: true,
          reason: "Non-empty target item evidence already exists. Generic DOM exploration is disabled; inspect response/checkpoint evidence or submit_parser.",
        };
      }
      if (toolCall.name !== "submit_parser") scheduledTools += 1;
      return undefined;
    },
    afterToolCall: async ({ toolCall }) => {
      if (toolCall.name === "submit_parser" && candidate) return { terminate: true };
      return undefined;
    },
  });

  agent.subscribe((event) => {
    if (event.type === "message_end" && event.message?.role === "assistant") {
      const text = textFromAssistant(event.message);
      if (text) assistantText = text;
      usage = addUsage(usage, event.message.usage);
    }
    if (event.type === "turn_end") {
      turns += 1;
      const toolCalls = Array.isArray(event.message?.content)
        ? event.message.content.filter((block) => block.type === "toolCall").map((block) => block.name)
        : [];
      emit({ type: "event", event: "turn_end", turn: turns, tool_calls: toolCalls });
      emit({
        type: "event",
        event: "transcript_checkpoint",
        turn: turns,
        messages: safeClone(agent.state.messages, []),
        state_summary: { convergence, auth_verification_pending: authVerificationPending, evidence: latestEvidence },
        recommended_actions: convergence
          ? ["browser_network_response_body", "browser_checkpoint_evidence", "submit_parser"]
          : [],
      });
      if (!candidate && toolCalls.length === 0 && followUpCount < 2 && turns < maxTurns) {
        followUpCount += 1;
        if (authResolutionMode) {
          const expected = !manualLoginAttempted
            ? "browser_manual_login"
            : (manualLoginSucceeded && !authProbeAttempted ? "browser_auth_probe" : "submit_parser");
          agent.followUp(userMessage(
            `Authentication protocol is incomplete. Call ${expected} now. Required action: ${requiredAction || "manual_login_and_verify"}.`,
          ));
        } else {
          agent.followUp(userMessage(
            `No parser has been submitted. Current evidence: ${JSON.stringify(latestEvidence)}. `
            + "Use the remaining native tools to read the strongest response evidence and call submit_parser; do not only explain.",
          ));
        }
      }
      if (turns >= maxTurns && !candidate) {
        stopReason = "turn_budget_exhausted";
        queueMicrotask(() => agent.abort());
      }
    }
  });

  const prompt = String(start.user_prompt || "Analyze the current page.");
  if (initialMessages.length) {
    agent.messages = [
      ...initialMessages,
      userMessage(`Resume Browser analysis from the preserved transcript and checkpoint. ${prompt}`),
    ];
    await agent.continue();
  } else {
    await agent.prompt(prompt);
  }

  const stateError = agent.state.errorMessage;
  if (stateError && stopReason === "completed") stopReason = "agent_error";
  emit({
    type: "final",
    candidate: candidate || {},
    assistant_text: assistantText,
    turns,
    tool_calls: executedTools,
    usage,
    stop_reason: stopReason,
    abort_source: stopReason === "turn_budget_exhausted" ? "graceful_turn_limit" : null,
    tool_budget_exhausted: toolBudgetExhausted,
    error: stopReason === "turn_budget_exhausted" ? "pi_turn_budget_exhausted" : (stateError || null),
    transcript: safeClone(agent.state.messages, []),
    state_summary: {
      convergence, evidence: latestEvidence, operation_mode: operationMode,
      manual_login_attempted: manualLoginAttempted, manual_login_succeeded: manualLoginSucceeded,
      auth_probe_attempted: authProbeAttempted,
    },
  });
}

if (process.argv.includes("--self-test")) {
  emit({ type: "fatal", error: "Use test_pi_runtime.py so the real Python/Node tool bridge is exercised." });
  process.exit(2);
}

main().catch((error) => {
  emit({ type: "fatal", error: error?.stack || String(error) });
  process.exitCode = 1;
}).finally(() => {
  rl.close();
});
