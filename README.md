# Crawler Agent v13.6 — Multi-Agent Intelligent Crawler

基于 LangGraph 的多 Agent 智能爬虫系统，通过 Supervisor + Browser Agent + Code Agent 协作完成网页数据抓取。

## Architecture

```text
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Supervisor  │────▶│  Browser Agent   │     │  Code Agent  │
│  (pi-agent)  │     │  (Playwright MCP)│     │  (subprocess)│
│              │────▶│                  │     │              │
│  路由决策     │     │  页面探索         │     │  爬虫生成     │
│  认证管理     │     │  网络抓包         │     │  数据抓取     │
│  进度评估     │     │  登录处理         │     │  分页处理     │
└──────┬───────┘     └──────────────────┘     └──────────────┘
       │
       ▼
┌─────────────┐     ┌──────────────────┐
│  MySQL RAG  │     │  Runtime Facts   │
│  经验记忆     │     │  运行时事实       │
│  策略复用     │     │  错误分类         │
│  失败记忆     │     │  进度追踪         │
└─────────────┘     └──────────────────┘
```

### Key components

- **Supervisor** (`crawler_agent/pipelines/supervisor_pipeline.py`): 路由决策、认证管理、进度评估
- **Browser Agent** (`crawler_agent/pipelines/browser_agent_pipeline.py`): 页面探索、网络抓包、登录处理
- **Code Agent** (`crawler_agent/pipelines/code_pipeline.py`): 爬虫脚本生成、数据抓取、分页处理
- **RAG** (`crawler_agent/rag/`): MySQL 结构化经验记忆、策略复用、失败记忆
- **Auth** (`crawler_agent/auth/`): 认证决策、会话管理、证据收集
- **Tools** (`crawler_agent/tools/`): Playwright MCP 浏览器工具、代码执行工具
- **Runtimes** (`crawler_agent/runtimes/`): Browser / Code 运行时
- **Core** (`crawler_agent/core/`): 日志、工具函数、运行时事实、错误分类

## Features

- **多 Agent 协作**: Supervisor 路由 + Browser 探索 + Code 实现
- **智能登录**: 自动检测登录墙、人工登录确认、登录态持久化
- **MySQL RAG**: 结构化经验记忆，避免重复探索
- **断点续传**: Checkpoint 机制支持任务恢复
- **错误分类**: 根因分析 + 重试策略
- **进度追踪**: 基于证据的收敛判断

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 22.19.0+
- MySQL 8.4+ (or MariaDB 11.4+)

### 1. Install dependencies

```bash
pip install -r requirements.txt
cd pi-browser-agent && npm ci && cd ..
```

### 2. Configure MySQL

**Option A: Docker (recommended)**

```bash
cp .env.mysql.example .env
# Edit .env to change passwords
docker compose -f docker-compose.mysql.yml --env-file .env up -d
```

**Option B: Existing MySQL**

```bash
cp .env.mysql.example .env
# Edit .env with your MySQL connection details
mysql -u root -p < crawler_agent/rag/sql/001_mysql_rag_schema.sql
```

### 3. Verify

```bash
python scripts/rag_health.py
```

### 4. Run

```bash
python main.py
```

## MySQL RAG

The RAG system stores structured experience memories for strategy reuse and failure prevention.

### Schema

7 tables: `rag_memory`, `rag_strategy_endpoint`, `rag_failure_memory`, `rag_execution`, `rag_memory_usage`, `rag_memory_event`, `rag_field_alias`

### Memory types

- **site**: route, page type, authentication risks
- **strategy**: data source, interaction, field and pagination strategy
- **endpoint**: endpoint provenance, request/response signatures
- **authentication**: observed auth state and verification behaviour
- **failure**: root cause, terminal symptom, block TTL

### Fail-open behavior

When MySQL is unavailable, RAG falls back to JSONL and never blocks crawler execution:

```text
rag.query status=degraded → JSONL fallback → task continues
```

Set `RAG_FAIL_OPEN=false` only for dedicated RAG testing.

## Configuration

See `.env.mysql.example` for all settings:

```text
RAG_BACKEND=mysql|jsonl|disabled
RAG_FAIL_OPEN=true|false
RAG_DUAL_WRITE_JSONL=true|false
RAG_MYSQL_HOST / PORT / DATABASE / USER / PASSWORD
RAG_MYSQL_POOL_SIZE=6
RAG_ENABLE_FULLTEXT=true
RAG_TOP_K_SITE=5 / STRATEGY=8 / FAILURE=5
```

## Project structure

```text
├── main.py                         # Entry point
├── crawler_agent/                  # Core package
│   ├── __init__.py / version.py / cli.py
│   ├── core/                       # Shared utilities
│   │   ├── common.py               #   LLM helpers, JSON utils
│   │   ├── logger.py               #   Structured logging
│   │   ├── api_logger.py           #   API cost tracking
│   │   ├── runtime_facts.py        #   Error classification, progress
│   │   ├── collection_evidence.py  #   Evidence collection
│   │   ├── tooling.py              #   Tool utilities
│   │   └── transcript_utils.py     #   Transcript sanitization
│   ├── pipelines/                  # Agent pipelines
│   │   ├── supervisor_pipeline.py  #   Supervisor (routing, auth, progress)
│   │   ├── browser_agent_pipeline.py # Browser (Playwright MCP exploration)
│   │   ├── browser_pipeline.py     #   Browser pipeline wrapper
│   │   └── code_pipeline.py        #   Code (crawler generation)
│   ├── runtimes/                   # Pi agent runtimes
│   │   ├── pi_browser_runtime.py   #   Browser agent runtime
│   │   └── pi_code_runtime.py      #   Code agent runtime
│   ├── tools/                      # Tool implementations
│   │   ├── mcp_browser_tools.py    #   Playwright MCP browser tools
│   │   └── code_tools.py           #   Code execution tools
│   ├── auth/                       # Authentication module
│   │   ├── contracts.py / decision.py / evidence.py
│   │   ├── models.py / service.py / sessions.py
│   └── rag/                        # MySQL RAG module
│       ├── config.py / models.py / normalizer.py
│       ├── ranker.py / memory_cards.py / pool.py
│       ├── repository.py / writer.py / service.py
│       ├── feedback.py / migration.py / maintenance.py
│       └── sql/001_mysql_rag_schema.sql
├── scripts/                        # Utility scripts
│   ├── rag_health.py / rag_init_mysql.py
│   ├── rag_migrate.py / rag_maintenance.py
│   └── _bootstrap.py
├── tests/                          # Test suite
├── docs/                           # Documentation
│   ├── RAG_MYSQL.md
│   └── AUTHENTICATION.md
├── pi-browser-agent/               # Node.js agent runtime
│   └── src/ (browser-agent.mjs, code-agent.mjs, supervisor-agent.mjs, agent-utils.mjs)
├── docker-compose.mysql.yml        # Docker MySQL setup
└── .env.mysql.example              # Environment template
```

## Maintenance

```bash
# Health check
python scripts/rag_health.py

# Daily maintenance (expire stale, quarantine failures, recalculate reliability)
python scripts/rag_maintenance.py

# Legacy JSONL migration
python scripts/rag_migrate.py crawler_workspace/runtime/rag/crawler_rag.jsonl --dry-run
python scripts/rag_migrate.py crawler_workspace/runtime/rag/crawler_rag.jsonl
```

## Privacy

RAG persistence removes or masks: cookies, auth tokens, passwords, secrets, storage-state paths, sensitive URL parameters (`xsec_token`, `pcdk`, `spmTag`, etc.).

Response bodies and browser storage state are never written to MySQL.

## License

[MIT](LICENSE)