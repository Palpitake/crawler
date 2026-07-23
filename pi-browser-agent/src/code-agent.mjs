import { createInterface } from "node:readline";
import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import {
  createAgentSession,
  createBashTool,
  DefaultResourceLoader,
  ModelRuntime,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxText,
  fauxToolCall,
  getModel,
} from "@earendil-works/pi-ai/compat";


const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
const startPromise = new Promise((resolveStart) => {
  rl.once("line", (line) => {
    try {
      resolveStart(JSON.parse(line));
    } catch (error) {
      resolveStart({ type: "invalid", error: String(error) });
    }
  });
});

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function textFromMessage(message) {
  if (!message || message.role !== "assistant" || !Array.isArray(message.content)) return "";
  return message.content
    .filter((block) => block.type === "text")
    .map((block) => String(block.text || ""))
    .join("\n");
}

function textFromToolResult(result) {
  if (!result || !Array.isArray(result.content)) return "";
  return result.content
    .filter((block) => block.type === "text")
    .map((block) => String(block.text || ""))
    .join("\n");
}

function isInsideWorkspace(workspace, inputPath) {
  if (!inputPath || typeof inputPath !== "string") return false;
  const resolved = resolve(workspace, inputPath);
  const rel = relative(workspace, resolved);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function safeBashEnv(source) {
  const result = {};
  const denied = /(API[_-]?KEY|TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|TRACE)/i;
  for (const [key, value] of Object.entries(source || {})) {
    if (!denied.test(key) && value !== undefined) result[key] = value;
  }
  result.PYTHONUNBUFFERED = "1";
  result.PYTHONUTF8 = "1";
  result.PYTHONIOENCODING = "utf-8";
  return result;
}

function isDestructiveCommand(command) {
  const text = String(command || "").toLowerCase();
  return [
    /\brm\s+-[^\n]*r[^\n]*f\b/,
    /\bdel\s+\/s\b/,
    /\brmdir\s+\/s\b/,
    /\bformat(?:\.com)?\b/,
    /\bshutdown\b/,
    /\bgit\s+reset\s+--hard\b/,
    /\bgit\s+clean\s+-[^\n]*f/,
  ].some((pattern) => pattern.test(text));
}

function shellMentionsPrimaryMutation(command, primaryCodeFile) {
  const text = String(command || "");
  const file = String(primaryCodeFile || "").trim();
  if (!file) return false;
  const escaped = file.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const patterns = [
    new RegExp(`(?:>|>>|2>)\\s*["']?[^\\n]*${escaped}["']?`, "i"),
    new RegExp(`\\b(?:tee|cp|copy|mv|move|rename|del|erase|rm|truncate)\\b[^\\n]*${escaped}`, "i"),
    new RegExp(`\\b(?:sed|perl)\\b[^\\n]*(?:-i|in-place)[^\\n]*${escaped}`, "i"),
    new RegExp(`\\b(?:set-content|add-content|out-file|clear-content)\\b[^\\n]*${escaped}`, "i"),
    new RegExp(`(?:open|Path)\\s*\\([^\\n]*${escaped}[^\\n]*(?:['"]w|write_text|write_bytes)`, "i"),
  ];
  return patterns.some((pattern) => pattern.test(text));
}

function isDependencyRepairCommand(command) {
  const text = String(command || "").toLowerCase();
  return (
    /(?:^|\s)(?:python(?:\.exe)?\s+-m\s+)?pip\s+install\b/.test(text)
    || /python(?:\.exe)?\s+-m\s+py_compile\b/.test(text)
    || /python(?:\.exe)?\s+-c\s+["'][^"']*(?:import|find_spec|compile)/.test(text)
  );
}

function isDependencyValidationCommand(command) {
  const text = String(command || "").toLowerCase();
  return /python(?:\.exe)?\s+-m\s+py_compile\b/.test(text)
    || /python(?:\.exe)?\s+-c\s+["'][^"']*(?:import|find_spec)/.test(text);
}

function scriptedResponses(script) {
  return script.map((step) => {
    if (step.tool) {
      return fauxAssistantMessage(
        fauxToolCall(String(step.tool), step.args || {}),
        { stopReason: "toolUse" },
      );
    }
    return fauxAssistantMessage(fauxText(String(step.text || "done")));
  });
}

async function configureModel(start, runtime) {
  if (Array.isArray(start.test_script)) {
    const faux = fauxProvider();
    faux.setResponses(scriptedResponses(start.test_script));
    runtime.registerProvider(faux.provider.id, {
      name: faux.provider.name,
      api: faux.api,
      streamSimple: faux.provider.streamSimple,
      baseUrl: "http://localhost:0",
      models: faux.models.map((model) => ({
        id: model.id,
        name: model.name,
        api: model.api,
        baseUrl: model.baseUrl,
        reasoning: Boolean(model.reasoning),
        input: model.input,
        cost: model.cost,
        contextWindow: model.contextWindow,
        maxTokens: model.maxTokens,
      })),
    });
    await runtime.setRuntimeApiKey(faux.provider.id, "offline-test-key");
    return runtime.getModel(faux.provider.id, faux.models[0].id) || faux.models[0];
  }

  const provider = String(start.provider || "deepseek");
  const modelName = String(start.model || "deepseek-v4-flash");
  const baseUrl = String(start.base_url || "");
  if (baseUrl) runtime.registerProvider(provider, { baseUrl });
  if (start.api_key) await runtime.setRuntimeApiKey(provider, String(start.api_key));
  const selected = runtime.getModel(provider, modelName) || getModel(provider, modelName);
  if (!selected) throw new Error(`pi_coding_model_not_found:${provider}:${modelName}`);
  return baseUrl ? { ...selected, baseUrl } : selected;
}

async function main() {
  const start = await startPromise;
  if (start.type !== "start") throw new Error(start.error || "pi_coding_invalid_start");

  const workspace = resolve(String(start.cwd || process.cwd()));
  const agentDir = resolve(workspace, ".pi-crawler-runtime");
  const maxTools = Math.max(4, Math.min(Number(start.max_tools || 24), 60));
  const maxTurns = Math.max(4, Math.min(Number(start.max_turns || 32), 60));
  const bashTimeout = Math.max(30, Math.min(Number(start.bash_timeout_seconds || 1800), 3600));
  const primaryCodeFile = String(start.primary_code_file || "").trim();
  const maxWrites = Math.max(1, Math.min(Number(start.max_writes || 2), 6));
  const resumeExistingFile = Boolean(start.resume_existing_file);
  const initialCodeHash = String(start.initial_code_hash || "");
  const executionMode = String(start.execution_mode || "full");
  const recoveryExecution = start.recovery_checkpoint?.execution || {};
  const recoveryRoot = String(recoveryExecution.root_error_type || recoveryExecution.error_type || "");
  let dependencyRepairMode = resumeExistingFile && ["dependency_error", "import_error"].includes(recoveryRoot);
  let dependencyVerified = !dependencyRepairMode;
  const bashCommandsById = new Map();
  const allowedDomains = new Set(
    (Array.isArray(start.allowed_domains) ? start.allowed_domains : [])
      .map((value) => String(value || "").trim().toLowerCase())
      .filter(Boolean),
  );
  let toolCount = 0;
  let writeCount = 0;
  let turnCount = 0;
  let assistantText = "";
  let bashOutput = "";
  let budgetExhausted = false;
  let primaryFileRead = false;
  const pendingPrimaryReadIds = new Set();
  const toolCalls = [];
  const toolResults = [];
  const acceptedToolCallIds = new Set();
  let activeSession = null;

  const boundaryExtension = {
    name: "crawler-workspace-boundary",
    factory: (pi) => {
      const nativeBash = createBashTool(workspace, {
        spawnHook: (context) => ({
          ...context,
          cwd: workspace,
          env: safeBashEnv(context.env),
        }),
      });
      pi.registerTool({
        ...nativeBash,
        label: "bash (crawler workspace)",
      });

      pi.on("tool_call", (event) => {
        // Check before counting/executing so max_tools=36 can never produce a
        // 37th recorded call. A blocked over-budget call is not a tool run.
        if (toolCount >= maxTools) {
          budgetExhausted = true;
          return { block: true, reason: `Native Pi tool budget exhausted (${maxTools}); finish with the current root cause.` };
        }
        if (["read", "write", "edit"].includes(event.toolName)) {
          if (executionMode === "probe" && ["write", "edit"].includes(event.toolName)) {
            return { block: true, reason: "Bounded access probe mode does not create or modify crawler source files." };
          }
          const path = event.input?.path;
          if (!isInsideWorkspace(workspace, path)) {
            return { block: true, reason: "File access outside the crawler workspace is blocked" };
          }
          const resolvedPath = resolve(workspace, String(path || ""));
          const fileName = resolvedPath.split(/[\\/]/).pop() || "";
          const isPrimaryFile = Boolean(
            primaryCodeFile && fileName.toLowerCase() === primaryCodeFile.toLowerCase()
          );
          if (event.toolName === "read" && isPrimaryFile) {
            pendingPrimaryReadIds.add(event.toolCallId);
          }
          if (resumeExistingFile && isPrimaryFile && existsSync(resolvedPath)) {
            if (event.toolName === "write") {
              return {
                block: true,
                reason: `Recovery checkpoint is active; do not overwrite ${primaryCodeFile}. Read it and use edit to continue from the prior failure.`,
              };
            }
            if (event.toolName === "edit" && !primaryFileRead) {
              return {
                block: true,
                reason: `Read ${primaryCodeFile} before editing the recovered file.`,
              };
            }
          }
          if (
            ["write", "edit"].includes(event.toolName)
            && primaryCodeFile
            && /\.py$/i.test(fileName)
            && fileName.toLowerCase() !== primaryCodeFile.toLowerCase()
          ) {
            return {
              block: true,
              reason: `Only ${primaryCodeFile} may be created or edited; modify the existing crawler instead of creating debug*.py files.`,
            };
          }
          if (event.toolName === "write") {
            if (writeCount >= maxWrites) {
              return {
                block: true,
                reason: `Full-file write budget exhausted (${maxWrites}); use edit on ${primaryCodeFile || "the crawler"}.`,
              };
            }
            writeCount += 1;
          }
        }
        if (event.toolName === "bash") {
          const command = String(event.input?.command || "");
          bashCommandsById.set(event.toolCallId, command);
          if (dependencyRepairMode && !dependencyVerified && !isDependencyRepairCommand(command)) {
            return {
              block: true,
              reason: `Recovered root cause is ${recoveryRoot}. Repair and verify imports/compilation before crawler or network experiments.`,
            };
          }
          if (isDestructiveCommand(command)) {
            return { block: true, reason: "Destructive shell command is blocked" };
          }
          if (resumeExistingFile && !primaryFileRead) {
            return {
              block: true,
              reason: `Recovery checkpoint is active; read ${primaryCodeFile || "the recovered crawler"} before using bash.`,
            };
          }
          if (resumeExistingFile && shellMentionsPrimaryMutation(command, primaryCodeFile)) {
            return {
              block: true,
              reason: `Do not overwrite the recovered ${primaryCodeFile}; use the edit tool for a targeted repair.`,
            };
          }
          if (primaryCodeFile) {
            const pythonFiles = [...command.matchAll(/(?:^|[\s"'])([^\s"']+\.py)(?=$|[\s"'])/gi)]
              .map((match) => String(match[1] || "").split(/[\\/]/).pop().toLowerCase());
            const unexpected = pythonFiles.filter((name) => name && name !== primaryCodeFile.toLowerCase());
            if (unexpected.length) {
              return {
                block: true,
                reason: `Run only ${primaryCodeFile}; temporary debug Python files are not allowed (${unexpected.join(", ")}).`,
              };
            }
            if (pythonFiles.includes(primaryCodeFile.toLowerCase()) && allowedDomains.size) {
              try {
                const source = readFileSync(resolve(workspace, primaryCodeFile), "utf8");
                const literalUrls = [...source.matchAll(/https?:\/\/[^\s'"<>]+/gi)]
                  .map((match) => String(match[0] || ""));
                const rejected = literalUrls.filter((rawUrl) => {
                  try {
                    const host = new URL(rawUrl).hostname.toLowerCase();
                    return host && ![...allowedDomains].some(
                      (domain) => host === domain || host.endsWith(`.${domain}`),
                    );
                  } catch {
                    return false;
                  }
                });
                if (rejected.length) {
                  return {
                    block: true,
                    reason: `Crawler source contains URLs outside allowed domains; edit before running (${rejected.slice(0, 3).join(", ")}).`,
                  };
                }
              } catch (error) {
                return {
                  block: true,
                  reason: `Cannot inspect ${primaryCodeFile} before execution: ${String(error)}`,
                };
              }
            }
          }
          event.input.timeout = Math.max(
            30,
            Math.min(Number(event.input.timeout || bashTimeout), bashTimeout),
          );
        }
        toolCount += 1;
        acceptedToolCallIds.add(event.toolCallId);
        toolCalls.push(event.toolName);
        emit({
          type: "event",
          event: "tool_start",
          tool: event.toolName,
          index: toolCalls.length,
        });
        return undefined;
      });
    },
  };

  const settings = SettingsManager.inMemory({
    compaction: { enabled: true },
    retry: { enabled: true, maxRetries: 2 },
  });
  const modelRuntime = await ModelRuntime.create({
    authPath: resolve(agentDir, "auth.json"),
    modelsPath: null,
    allowModelNetwork: false,
  });
  const model = await configureModel(start, modelRuntime);
  const loader = new DefaultResourceLoader({
    cwd: workspace,
    agentDir,
    settingsManager: settings,
    extensionFactories: [boundaryExtension],
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPromptOverride: () => String(start.system_prompt || ""),
    appendSystemPromptOverride: () => [],
  });
  await loader.reload();

  const { session } = await createAgentSession({
    cwd: workspace,
    agentDir,
    model,
    modelRuntime,
    resourceLoader: loader,
    settingsManager: settings,
    sessionManager: SessionManager.create(workspace),
    tools: ["read", "write", "edit", "bash"],
    thinkingLevel: String(start.thinking_level || "medium"),
  });
  activeSession = session;

  session.subscribe((event) => {
    if (event.type === "turn_end") {
      turnCount += 1;
      if (turnCount >= maxTurns && session.isStreaming) {
        budgetExhausted = true;
        queueMicrotask(() => session.abort().catch(() => {}));
      }
    } else if (event.type === "message_end") {
      const text = textFromMessage(event.message);
      if (text) assistantText = text;
    } else if (event.type === "tool_execution_end") {
      // The SDK emits execution_end even when beforeToolCall blocked a call.
      // Only retain results for calls that passed our pre-execution guard, so
      // max_tools=36 is reported as at most 36 actual tool runs.
      if (!acceptedToolCallIds.has(event.toolCallId)) return;
      acceptedToolCallIds.delete(event.toolCallId);
      if (pendingPrimaryReadIds.has(event.toolCallId)) {
        pendingPrimaryReadIds.delete(event.toolCallId);
        if (!event.isError) primaryFileRead = true;
      }
      const output = textFromToolResult(event.result);
      if (event.toolName === "bash" && output) {
        bashOutput = `${bashOutput}\n${output}`.slice(-250000);
      }
      if (event.toolName === "bash" && dependencyRepairMode && !event.isError) {
        const command = bashCommandsById.get(event.toolCallId) || "";
        if (isDependencyValidationCommand(command) && !/(modulenotfounderror|importerror|syntaxerror|traceback)/i.test(output)) {
          dependencyVerified = true;
          dependencyRepairMode = false;
        }
      }
      bashCommandsById.delete(event.toolCallId);
      toolResults.push({
        tool: event.toolName,
        ok: !event.isError,
        output_tail: output.slice(-4000),
      });
      emit({
        type: "event",
        event: "tool_end",
        tool: event.toolName,
        ok: !event.isError,
        index: toolResults.length,
      });
    }
  });

  try {
    await session.prompt(String(start.user_prompt || "Complete the crawler task."), {
      expandPromptTemplates: false,
      source: "rpc",
    });
    await session.waitForIdle();
    const stats = session.getSessionStats();
    emit({
      type: "final",
      ok: !session.state.errorMessage && !budgetExhausted,
      assistant_text: assistantText,
      tool_calls: toolCalls,
      // At most 60 bounded calls are retained.  Keeping the complete sequence
      // lets the host prove that a successful crawler run happened after the
      // final native write/edit instead of accidentally accepting an older run.
      tool_results: toolResults,
      turns: turnCount,
      bash_output: bashOutput,
      budget_exhausted: budgetExhausted,
      error: session.state.errorMessage || null,
      usage: {
        input: stats.tokens.input,
        output: stats.tokens.output,
        cacheRead: stats.tokens.cacheRead,
        cacheWrite: stats.tokens.cacheWrite,
        totalTokens: stats.tokens.total,
        cost: { total: stats.cost },
      },
      runtime: "pi-coding-agent",
      session_id: session.sessionId,
      session_file: session.sessionFile || null,
      active_tools: session.getActiveToolNames(),
      recovery: {
        resumed_existing_file: resumeExistingFile,
        primary_file_read: primaryFileRead,
        initial_code_hash: initialCodeHash,
        execution_mode: executionMode,
        recovery_root_error_type: recoveryRoot || null,
        dependency_verified: dependencyVerified,
      },
    });
  } finally {
    session.dispose();
    rl.close();
  }
}

main().catch((error) => {
  emit({ type: "fatal", error: error?.stack || String(error) });
  process.exitCode = 1;
  rl.close();
});
