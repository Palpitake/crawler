# Crawler Agent v13.5 — Authentication Command Protocol

v13.5 keeps the shared runtime-facts architecture and replaces advisory login prompts with a dedicated authentication command protocol. Supervisor authentication decisions now become an executable Browser mode rather than text embedded in generic Browser feedback.


## v13.5 authentication command protocol

Authentication is now a separate Supervisor capability:

```text
resolve_authentication(reason=...)
```

It is intentionally different from generic `run_browser(focus=...)`. The host passes:

```text
operation_mode=resolve_authentication
required_action=manual_login_and_verify
```

The Browser runtime starts a fresh authentication session and exposes only this sequence:

```text
browser_manual_login
→ browser_auth_probe
→ submit_parser
```

Generic network, DOM, click, scroll, snapshot and evaluate tools are not advertised during this mode. The Browser Agent still owns the semantic judgment of the probe facts, but it cannot silently return to generic exploration.

### Authentication precedence

When `authentication_state` is `required`, `challenge`, or `provisional`:

1. `resolve_authentication` is recommended before a Code probe.
2. Generic Browser no-progress counters do not block the dedicated authentication protocol.
3. Authentication has its own bounded attempt counter.
4. A verified endpoint never enables full Code while authentication remains unresolved.
5. Existing Parser facts are retained; authentication resolution updates the auth facts without replacing a previously useful Parser strategy.

### Transcript integrity

Pi transcripts are persisted and restored by atomic message groups. An assistant tool-call message and all matching tool results are retained or dropped together. The runtime removes orphan tool results and incomplete tool-call groups before `agent.continue()`. This prevents provider errors such as:

```text
Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

Authentication mode never restores the previous generic Browser reasoning transcript; it starts from the objective checkpoint facts and the dedicated authentication instruction.

## Runtime architecture

- **Supervisor Agent:** `pi-agent-core`, chooses capabilities from current facts.
- **Browser Agent:** `pi-agent-core`, explores the page, handles authentication, records network evidence, and submits a parser.
- **Code Agent:** `pi-coding-agent`, runs either a bounded access probe or a full crawler implementation.
- **Python runtime:** permissions, evidence persistence, authentication facts, root-cause taxonomy, progress scoring, checkpoints, and artifact integrity.

The shared implementation is in `runtime_facts.py`.

## 1. Authentication facts

Authentication is no longer represented by a vague `login=success` field. The normalized states are:

```text
unknown
anonymous
not_required
required
challenge
provisional
verified
rejected
stale
```

Loading a storage-state file only produces:

```text
authentication_state=provisional
verification_state=unverified
```

Manual login now follows this sequence:

```text
open visible browser
→ user confirms completion
→ save storage state
→ restore the same headless context and fingerprint
→ reopen target page
→ browser_auth_probe
→ Browser AI submits verified/required/challenge facts
```

User confirmation is never treated as proof of successful authentication. After login, the Agent is explicitly forbidden from changing User-Agent, viewport, locale, timezone, browser engine, proxy, or other fingerprint attributes.

## 2. Recoverable network evidence

Network evidence is no longer tied to short-lived MCP response handles.

Each response record includes:

```text
evidence_id
normalized URL
captured page URL
page partition
auth epoch
capture time
response summary
body SHA-256
stable local body file
```

Available response bodies are persisted under:

```text
crawler_workspace/checkpoints/browser_bodies/<task_id>/<sha256>.body.txt
```

A resumed Browser Agent reads the local body file directly. It does not rely on an ID from a previous MCP process.

Evidence is partitioned into:

```text
target_pre_login
target_post_login
same_site_pre_login
same_site_post_login
login
challenge
external_or_redirect
unknown
```

When `page_target_mismatch` occurs, aggregate evidence from the old page is quarantined. Historical evidence remains inspectable by partition but is not merged into the current target-page ledger.

## 3. Endpoint provenance

Every API candidate carries explicit provenance:

```text
observed       captured by Browser or bounded runtime probe
historical     retrieved from local strategy memory
hypothesized   proposed by an Agent but not observed
```

It also carries `verified=true/false`. A hypothesized endpoint cannot directly authorize full crawler generation.

## 4. Bounded Code access probe

Code execution now has two modes:

### `probe`

Used when authentication, access, or API evidence is unresolved.

- Maximum default tool budget: 10.
- No source-code or data-file creation.
- Tests the target page and at most three endpoint candidates.
- Records status codes, content types, response excerpts, target-data presence, and observed endpoints.
- Does not rotate User-Agent, proxy, fingerprint, or guessed signatures.
- Emits `PROBE_REPORT_JSON` with the true root cause and recommended next action.

### `full`

Enabled only after Browser or the bounded probe observes a usable target-data source, or after a usable DOM strategy is established.

A successful probe can promote an endpoint to:

```text
source=observed
verified=true
evidence_source=bounded_access_probe
```

Only then does Supervisor expose full Code execution as the productive next action.

## 5. Root cause and terminal symptom

Failures preserve two separate fields:

```text
root_error_type
terminal_error_type
```

Example:

```text
root_error_type=access_denied
terminal_error_type=empty_data
error_category=access
retry_strategy=stop_or_change_access_context
```

Root-cause priority includes:

```text
dependency/import
challenge/authentication
rate limit
access denied
service unavailable
timeout/network
syntax
API contract/parser
tool budget
empty data
```

`empty_data` is a terminal symptom and no longer automatically becomes a parser error.

## 6. Dependency-first recovery

When a recovered Code checkpoint reports an import or dependency root cause:

```text
read recovered source
→ install/fix dependency
→ run import or py_compile validation
→ resume crawler/network tests
```

Until dependency validation succeeds, arbitrary crawler and network Bash experiments are blocked. This prevents dozens of requests from being attempted while the source still cannot import.

## 7. Progress-based convergence

Each Browser and Code capability is measured using objective changes:

```text
new item IDs
new persisted response bodies
new observed endpoints
new verified endpoints
authentication-state changes
new output rows
root-cause changes
```

The runtime emits `capability.progress` with a score and change set. Two consecutive no-progress executions hide or reject the same capability unless a materially different refresh is explicitly requested.

This replaces “retry until max_retries” as the primary convergence mechanism.

## 8. Browser no-progress controls

Equivalent actions are normalized across tools. Examples:

- reopening the same URL repeatedly;
- clicking the same target repeatedly;
- reading the same failed response body;
- repeating identical evaluate/snapshot/HTML calls;
- repeating an interaction without new evidence.

After two equivalent no-progress attempts, the action is blocked and the Agent must change dimension, inspect persisted evidence, authenticate, submit a parser, or stop.

## 9. Accurate status semantics

Browser completion reports separate facts:

```text
runtime_status
analysis_status
artifact_status
authentication_state
verification_state
```

A normally completed model session with low confidence, unresolved authentication, or incomplete evidence is logged as `incomplete`, not ordinary business success.

Code completion separately reports:

```text
runtime_status
artifact_status
review_status
terminal_reason
```

## 10. Final reporting and privacy

Failure reports prioritize authentication, access, rate-limit, service, dependency, network, and parser root causes before confidence heuristics.

When no accepted artifact exists:

- `data_file` is null;
- `code_file` is null;
- the console does not claim that a target path was generated.

Sensitive and long URL query parameters, including `xsec_token`, `pcdk`, and `spmTag`, are redacted in text and JSONL logs. The interactive manual-login prompt also displays a sanitized URL.

## Runtime files

```text
crawler_workspace/crawler.log
crawler_workspace/crawler.jsonl
crawler_workspace/runtime/logs/<task_id>.jsonl
crawler_workspace/runtime/states/<task_id>_checkpoint.json
crawler_workspace/checkpoints/browser_<task_id>.json
crawler_workspace/checkpoints/browser_bodies/<task_id>/
crawler_workspace/checkpoints/code_<task_id>.json
crawler_workspace/artifacts/<task_id>/run_<n>/
```

## Requirements

- Python 3.10+
- Node.js 22.19.0+
- Pi packages pinned to 0.80.10

Install:

```bash
pip install -r requirements.txt
cd pi-browser-agent
npm ci
```

Run:

```bash
python main.py
```

## Validation scope

The release is validated with Python compilation/import checks, Node syntax checks, authentication-state regressions, access-probe regressions, root-cause preservation, progress convergence, stable response-body recovery, evidence partitioning, URL redaction, artifact integrity, and final-report tests.

A live JD/Bilibili/Xiaohongshu crawl still depends on the local network, account state, site policy, and compatible Node/Pi runtime.
