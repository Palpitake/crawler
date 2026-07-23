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
let resolveStart;
const startPromise = new Promise((resolve) => { resolveStart = resolve; });

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

rl.on("line", (line) => {
  let message;
  try { message = JSON.parse(line); }
  catch { return; }
  if (message.type === "start") {
    resolveStart(message);
    return;
  }
  if (message.type === "tool_result") {
    const waiter = pending.get(String(message.id || ""));
    if (!waiter) return;
    pending.delete(String(message.id || ""));
    if (message.ok) waiter.resolve(message.result);
    else waiter.reject(new Error(String(message.error || "python_capability_tool_error")));
  }
});

function bridgeCall(name, args, signal) {
  const id = `pi-supervisor-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  emit({ type: "tool_call", id, name, args });
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      pending.delete(id);
      reject(new Error("capability_tool_call_aborted"));
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

function textFromAssistant(message) {
  if (!message || !Array.isArray(message.content)) return "";
  return message.content
    .filter((block) => block.type === "text")
    .map((block) => String(block.text || ""))
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
    if (step.tool) {
      return fauxAssistantMessage(
        fauxToolCall(String(step.tool), step.args || {}),
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

function compactToolResult(message) {
  const details = message?.details && typeof message.details === "object" ? message.details : {};
  const summary = {
    ok: details.ok,
    capability: details.capability || details.completed_capability,
    error: details.error || null,
    state: details.state || null,
    validation: details.validation || null,
    recommended_actions: details.recommended_actions || [],
    final_output: details.final_output || null,
  };
  return {
    ...message,
    content: [{ type: "text", text: JSON.stringify(summary) }],
    details: summary,
  };
}

async function compactContext(messages) {
  if (!Array.isArray(messages) || messages.length <= 14) return messages;
  const keepFrom = Math.max(0, messages.length - 10);
  return messages.map((message, index) => {
    if (index >= keepFrom) return message;
    if (message?.role === "toolResult") return compactToolResult(message);
    if (message?.role === "assistant" && Array.isArray(message.content)) {
      return {
        ...message,
        content: message.content.map((block) => (
          block.type === "text" && String(block.text || "").length > 1600
            ? { ...block, text: `${String(block.text).slice(0, 1600)}\n[older reasoning text compacted]` }
            : block
        )),
      };
    }
    return message;
  });
}

const empty = () => Type.Object({}, { additionalProperties: false });
const requestSchema = Type.Object({
  target_url: Type.Union([Type.String(), Type.Null()]),
  target_fields: Type.Array(Type.String(), { maxItems: 30 }),
  max_items: Type.Union([Type.Integer({ minimum: 1 }), Type.Null()]),
  output_format: Type.Union([
    Type.Literal("csv"), Type.Literal("json"), Type.Literal("xlsx"),
  ]),
  need_login: Type.Union([
    Type.Literal("yes"), Type.Literal("no"), Type.Literal("unknown"),
  ]),
  need_code_return: Type.Boolean(),
}, { additionalProperties: false });

const capabilitySpecs = [
  {
    name: "set_task_spec",
    description: "Normalize or correct the crawl task specification. Call this when the target URL, fields, limit, or output format is not yet recorded or is wrong.",
    parameters: Type.Object({ request: requestSchema }, { additionalProperties: false }),
    mutates: true,
  },
  {
    name: "search_strategy",
    description: "Query the MySQL structured crawler memory (site, strategy, endpoint, failure, authentication). Returned Memory Cards are advisory hypotheses and must be validated in the current task. Database failure is fail-open.",
    parameters: empty(),
    mutates: true,
  },
  {
    name: "run_browser",
    description: "Run or resume the pi-agent-core Browser Agent. The Browser Agent judges its own parser evidence; the host returns facts and advisory warnings. Call again only when the AI decides more evidence is useful.",
    parameters: Type.Object({
      focus: Type.Optional(Type.String({ maxLength: 2000 })),
      force_refresh: Type.Optional(Type.Boolean()),
    }, { additionalProperties: false }),
    mutates: true,
  },
  {
    name: "resolve_authentication",
    description: "Run the dedicated authentication protocol. This is not generic Browser exploration: the Browser runtime is restricted to manual_login -> auth_probe -> submit_parser. Use when authentication_state is required, challenge, or provisional and authentication must be resolved.",
    parameters: Type.Object({
      reason: Type.String({ minLength: 8, maxLength: 2000 }),
    }, { additionalProperties: false }),
    mutates: true,
  },
  {
    name: "run_code",
    description: "Run pi-coding-agent in the host-selected mode. If authentication/API evidence is unresolved, this is a bounded access probe that creates no data artifact; only verified observed evidence enables full crawler implementation. Once a successful artifact exists, this capability is hidden.",
    parameters: Type.Object({
      repair_focus: Type.Optional(Type.String({ maxLength: 2000 })),
    }, { additionalProperties: false }),
    mutates: true,
  },
  {
    name: "recheck_code",
    description: "Explicitly re-run Code after a successful artifact only when a concrete defect is identified. A reason is mandatory. A smaller result is retained only when accept_smaller_result is true and replacement_reason explains why it is more trustworthy.",
    parameters: Type.Object({
      recheck_reason: Type.String({ minLength: 8, maxLength: 2000 }),
      repair_focus: Type.Optional(Type.String({ maxLength: 2000 })),
      accept_smaller_result: Type.Optional(Type.Boolean()),
      replacement_reason: Type.Optional(Type.String({ maxLength: 2000 })),
    }, { additionalProperties: false }),
    mutates: true,
  },
  {
    name: "inspect_task",
    description: "Read the current compact task state, AI review, advisory warnings, retry budgets, and recommended capabilities without mutating the task.",
    parameters: Type.Object({
      include_evidence: Type.Optional(Type.Boolean()),
    }, { additionalProperties: false }),
    mutates: false,
  },
  {
    name: "finalize_task",
    description: "Build the final user-facing result from the current AI-reviewed state. Use when a fresh non-empty result exists or when no safe productive capability remains.",
    parameters: Type.Object({
      reason: Type.Optional(Type.String({ maxLength: 2000 })),
    }, { additionalProperties: false }),
    mutates: true,
  },
];

async function main() {
  const start = await startPromise;
  if (start.type !== "start") throw new Error(start.error || "pi_supervisor_invalid_start");
  if (String(start.tool_profile || "") !== "supervisor_native") {
    throw new Error(`unsupported_supervisor_profile:${start.tool_profile}`);
  }

  const maxTurns = Math.max(6, Math.min(Number(start.max_turns || 36), 80));
  const maxTools = Math.max(8, Math.min(Number(start.max_tools || 32), 80));
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
      Math.min(Number(start.max_completion_tokens || process.env.PI_MAX_COMPLETION_TOKENS || 16384), 65536),
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

  let finalCandidate = null;
  let assistantText = "";
  let turns = 0;
  let executedTools = 0;
  let scheduledTools = 0;
  let usage = {};
  let stopReason = "completed";
  let toolBudgetExhausted = false;
  let latestState = safeClone(start.state_summary, {}) || {};
  let recommendedActions = Array.isArray(start.recommended_actions)
    ? start.recommended_actions.map(String)
    : [];
  let followUpCount = 0;
  let agent;

  const allTools = capabilitySpecs.map((spec) => ({
    name: spec.name,
    label: spec.name,
    description: spec.description,
    parameters: spec.parameters,
    executionMode: spec.mutates ? "sequential" : "parallel",
    execute: async (_toolCallId, args, signal) => {
      const result = await bridgeCall(spec.name, args || {}, signal);
      executedTools += 1;
      if (result?.state && typeof result.state === "object") latestState = result.state;
      if (Array.isArray(result?.recommended_actions)) {
        recommendedActions = result.recommended_actions.map(String);
      }
      if (spec.name === "finalize_task" && result?.final_output) {
        finalCandidate = result.final_output;
      }
      if (!result?.ok || result?.state?.error_type) {
        agent?.steer(userMessage(
          `Capability ${spec.name} returned new facts: ${JSON.stringify({
            error: result?.error || result?.state?.root_error_type || result?.state?.error_type || null,
            terminal_error_type: result?.state?.terminal_error_type || null,
            error_category: result?.state?.error_category || null,
            retry_strategy: result?.state?.retry_strategy || null,
            authentication_state: result?.state?.authentication_state || null,
            no_progress_streaks: result?.state?.no_progress_streaks || {},
            recommended_actions: result?.recommended_actions || [],
          })}. Re-plan from the evidence; do not repeat the same failed call without a focused change.`,
        ));
      }
      return {
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: result,
        ...(spec.name === "finalize_task" ? { terminate: true } : {}),
      };
    },
  }));
  const toolsByName = new Map(allTools.map((tool) => [tool.name, tool]));

  function dynamicTools() {
    if (finalCandidate) return [];
    const names = new Set(["inspect_task", "finalize_task"]);
    if (!latestState?.spec_ready) {
      names.add("set_task_spec");
      return [...names].map((name) => toolsByName.get(name)).filter(Boolean);
    }
    names.add("set_task_spec");
    if (!latestState?.rag_checked) names.add("search_strategy");
    for (const action of recommendedActions) {
      if (latestState?.execution_success && action === "run_code") continue;
      if (!latestState?.execution_success && action === "recheck_code") continue;
      if (toolsByName.has(action)) names.add(action);
    }
    if (latestState?.execution_success) names.add("recheck_code");
    return [...names].map((name) => toolsByName.get(name)).filter(Boolean);
  }

  const initialMessages = Array.isArray(start.initial_messages)
    ? safeClone(start.initial_messages, [])
    : [];

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
    transformContext: compactContext,
    prepareNextTurnWithContext: async ({ context }) => {
      let thinkingLevel = "low";
      if (latestState?.root_error_type || Number(latestState?.retry_count || 0) > 0) thinkingLevel = "medium";
      if (["authentication", "access", "service", "parser"].includes(latestState?.error_category)) thinkingLevel = "high";
      return {
        context: {
          ...context,
          tools: dynamicTools(),
        },
        thinkingLevel,
      };
    },
    beforeToolCall: async ({ toolCall }) => {
      if (turns >= maxTurns - 2 && toolCall.name !== "finalize_task") {
        return {
          block: true,
          reason: "Final synthesis reserve reached. Use finalize_task with the verified state; do not start another long capability.",
        };
      }
      if (scheduledTools >= maxTools && toolCall.name !== "finalize_task") {
        toolBudgetExhausted = true;
        return {
          block: true,
          reason: "Supervisor capability budget exhausted. Inspect the current state if possible, then finalize_task with the verified result or failure.",
        };
      }
      if (toolCall.name !== "finalize_task") scheduledTools += 1;
      return undefined;
    },
    afterToolCall: async ({ toolCall }) => {
      if (toolCall.name === "finalize_task" && finalCandidate) return { terminate: true };
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
        state_summary: latestState,
        recommended_actions: recommendedActions,
      });
      if (!finalCandidate && toolCalls.length === 0 && followUpCount < 2 && turns < maxTurns) {
        followUpCount += 1;
        agent.followUp(userMessage(
          `The task is not finalized. Current state: ${JSON.stringify(latestState)}. `
          + `Recommended capabilities: ${JSON.stringify(recommendedActions)}. If a successful artifact already exists, finalize it unless you can state a concrete defect and use recheck_code with an explicit reason. `
          + "Choose and call the most useful available capability now; do not merely describe it.",
        ));
      }
      if (turns >= maxTurns && !finalCandidate) {
        stopReason = "turn_budget_exhausted";
        queueMicrotask(() => agent.abort());
      }
    }
  });

  const prompt = String(start.user_prompt || "Complete the crawl task using the available capabilities.");
  if (initialMessages.length) {
    agent.messages = [
      ...initialMessages,
      userMessage(
        `Resume the interrupted task using the preserved transcript and current state. ${prompt}`,
      ),
    ];
    await agent.continue();
  } else {
    await agent.prompt(prompt);
  }

  const stateError = agent.state.errorMessage;
  if (stateError && stopReason === "completed") stopReason = "agent_error";
  emit({
    type: "final",
    candidate: finalCandidate || {},
    assistant_text: assistantText,
    turns,
    tool_calls: executedTools,
    usage,
    stop_reason: stopReason,
    abort_source: stopReason === "turn_budget_exhausted" ? "graceful_turn_limit" : null,
    tool_budget_exhausted: toolBudgetExhausted,
    error: stopReason === "turn_budget_exhausted" ? "pi_turn_budget_exhausted" : (stateError || null),
    transcript: safeClone(agent.state.messages, []),
    state_summary: latestState,
    recommended_actions: recommendedActions,
  });
}

main().catch((error) => {
  emit({ type: "fatal", error: error?.stack || String(error) });
  process.exitCode = 1;
}).finally(() => rl.close());
