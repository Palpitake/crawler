import {
  createModels,
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { getModel } from "@earendil-works/pi-ai/compat";


export function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

export function textFromAssistant(message) {
  if (!message || !Array.isArray(message.content)) return "";
  return message.content
    .filter((block) => block.type === "text")
    .map((block) => String(block.text || ""))
    .join("\n");
}

export function addUsage(total, current) {
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

export function userMessage(text) {
  return {
    role: "user",
    content: [{ type: "text", text: String(text || "") }],
    timestamp: Date.now(),
  };
}

export function safeClone(value, fallback = null) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return fallback;
  }
}

export function createBridgeCall({ pending, idPrefix, abortError }) {
  return function bridgeCall(name, args, signal) {
    const id = `${idPrefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    emit({ type: "tool_call", id, name, args });
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        pending.delete(id);
        reject(new Error(abortError));
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
  };
}

export function scriptedRuntime(
  script,
  {
    allowBatchTools = false,
    stringifyToolName = false,
  } = {},
) {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses(script.map((step) => {
    if (allowBatchTools && Array.isArray(step.tools)) {
      return fauxAssistantMessage(
        step.tools.map((item) => fauxToolCall(item.tool, item.args || {})),
        { stopReason: "toolUse" },
      );
    }
    if (step.tool) {
      const toolName = stringifyToolName ? String(step.tool) : step.tool;
      return fauxAssistantMessage(
        fauxToolCall(toolName, step.args || {}),
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

export async function getApiKey(providerName) {
  if (providerName === "deepseek") return process.env.DEEPSEEK_API_KEY;
  return process.env.PI_MODEL_API_KEY || process.env.OPENAI_API_KEY;
}

export function resolveAgentModel(
  start,
  {
    defaultMaxCompletionTokens,
    maxCompletionTokens,
  },
) {
  const provider = String(start.provider || "deepseek");
  const modelName = String(start.model || "deepseek-v4-flash");
  const fallback = getModel("deepseek", "deepseek-v4-flash");
  const selected = getModel(provider, modelName) || fallback;
  if (!selected) throw new Error(`pi_model_not_found:${provider}:${modelName}`);
  const requestedMaxTokens = Math.max(
    1024,
    Math.min(
      Number(
        start.max_completion_tokens
          || process.env.PI_MAX_COMPLETION_TOKENS
          || defaultMaxCompletionTokens,
      ),
      maxCompletionTokens,
    ),
  );
  return {
    ...selected,
    id: modelName,
    name: modelName,
    provider,
    baseUrl: String(start.base_url || selected.baseUrl),
    maxTokens: Math.min(Number(selected.maxTokens || requestedMaxTokens), requestedMaxTokens),
  };
}
