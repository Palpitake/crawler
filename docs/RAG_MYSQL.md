# MySQL RAG architecture

## 1. Data flow

```text
Task state
  │
  ├─ URL normalisation and sensitive-query removal
  ├─ route template generation
  ├─ task/entity/collection classification
  ├─ canonical field mapping
  └─ authentication/access-environment facts
          │
          ▼
Structured candidate filter
  domain / route_hash / task_type / collection_type / status
          │
          ▼
InnoDB ngram FULLTEXT recall
          │
          ▼
Application reranker
  structural + lexical + reliability + freshness + completion + environment
          │
          ▼
Diverse Memory Cards
  Supervisor / Browser / Code / Failure
          │
          ▼
Current-task Browser or Code Probe validation
          │
          ▼
Final execution feedback and MySQL transaction
```

## 2. Tables

### `rag_memory`

Holds Site, Strategy, Endpoint and Authentication Memory. Frequently filtered
facts are ordinary indexed columns; evolving facts are JSON.

The unique `memory_key` implements upsert instead of append-only duplication.

### `rag_strategy_endpoint`

Stores endpoint templates and request/response/pagination facts separately from
the main memory row. An endpoint always belongs to a memory record.

### `rag_failure_memory`

Stores failures by domain, route, task, endpoint family, root error,
authentication state and access-environment fingerprint. `block_until` prevents
identical full attempts; `expires_at` removes temporary failures from retrieval.

### `rag_execution`

One audit row per crawler task. It stores final Runtime Facts, result quality,
Agent builds and selected strategy ID.

### `rag_memory_usage`

Records whether a Memory Card was merely provided or matched actual runtime
facts. Reliability is updated only when current execution facts prove use.

### `rag_memory_event`

Low-volume retrieval and administration events.

### `rag_field_alias`

Domain-specific and global field aliases. Domain rows take precedence over
global rows.

## 3. Memory keys

Examples:

```text
Site:
sha256(site + domain + route_template)

Strategy:
sha256(domain + route_template + task_type + data_source
       + pagination_type + canonical_field_set)

Endpoint:
sha256(endpoint_family + method + pagination_type)

Failure:
sha256(domain + route_template + task_type + endpoint_family
       + root_error_type + auth_state + environment_fingerprint)
```

Entity IDs and sensitive query values are excluded.

## 4. Retrieval score

Without embeddings:

```text
0.36 structural match
0.24 ngram FULLTEXT score
0.18 reliability
0.10 freshness
0.08 completion
0.04 environment match
minus penalties
```

Cross-domain strategy matching uses task, collection, data source and reusable
pagination patterns. It does not require an impossible same-domain bonus.

Penalties include:

- stale or quarantined memory;
- hypothesized source;
- recent validation failures;
- authentication/environment mismatch;
- a known failure identical to the current root error.

## 5. Quality classes

### `verified_success`

Requires a non-empty accepted output, complete pagination or reached limit,
consistent authentication and current observed endpoint/DOM facts.

### `partial_success`

A real non-empty result without enough completeness/current verification for
high-trust reuse.

### `environment_failure`

Authentication, challenge, access denial, rate limit, service or network
failure. It creates Failure/Auth Memory without degrading parser strategy as if
it were a structural failure.

### `strategy_failure`

Parser, endpoint contract, pagination or generated-code strategy failure.

## 6. Failure block semantics

A Failure Memory block is active only when:

```text
block_until > now
AND environment_fingerprint matches
```

The recommended action can still be `resolve_authentication`. A full retry is
allowed after an auth epoch/access context change or block expiry.

## 7. Feedback semantics

Retrieval increments `retrieval_count`.

At finalisation, cards are compared with actual runtime facts:

```text
provided   Memory Card was available but use was not proven
selected   current data source/endpoint/auth/site facts matched the card
validated  selected memory contributed to a successful run
rejected   selected memory matched a failed run
contributed selected memory matched a successful final result
```

The system does not mark every retrieved card as successful.

## 8. Transaction boundary

One finalisation transaction contains:

```text
upsert memory rows
upsert endpoint rows
upsert failure row
upsert execution row
insert usage feedback
update memory counters/reliability
COMMIT
```

Model or browser calls never occur inside a database transaction.

## 9. Rollback

```text
RAG_BACKEND=mysql    normal mode
RAG_BACKEND=jsonl    compatibility rollback
RAG_BACKEND=disabled no retrieval or persistence
```

With `RAG_FAIL_OPEN=true`, a MySQL error automatically falls back to JSONL for
that operation and leaves the crawler pipeline running.
