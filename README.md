<p align="center">
  <h1 align="center">🕷️ Crawler Agent</h1>
  <p align="center">
    <strong>Multi-Agent Intelligent Web Crawler</strong><br/>
    LangGraph Supervisor + Playwright MCP + DeepSeek
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" />
    <img src="https://img.shields.io/badge/Node.js-22.19+-339933?logo=node.js&logoColor=white" />
    <img src="https://img.shields.io/badge/MySQL-8.4+-4479A1?logo=mysql&logoColor=white" />
    <img src="https://img.shields.io/badge/License-MIT-green" />
  </p>
</p>

---

## What is this?

一个基于 **LangGraph 多 Agent 架构**的智能爬虫系统。给定一个 URL 和目标字段，系统自动：

1. 🔍 **探索页面** — Browser Agent 通过 Playwright 分析页面结构、抓包 API
2. 🧠 **制定策略** — Supervisor 根据证据路由决策，复用历史经验
3. ⚙️ **生成爬虫** — Code Agent 自动生成可运行的爬虫脚本
4. 📊 **产出数据** — 输出结构化 CSV / JSON 数据文件

核心特点：**不需要预定义选择器或 API 路径**，Agent 自主探索并适应不同网站。

## Architecture

```
                           ┌─────────────────┐
                           │    User Task     │
                           │  URL + 字段要求   │
                           └────────┬────────┘
                                    │
                           ┌────────▼────────┐
                           │    Supervisor    │
                           │   路由 · 认证     │
                           │   进度 · 决策     │
                           └───┬─────────┬───┘
                               │         │
                  ┌────────────▼──┐  ┌───▼────────────┐
                  │ Browser Agent │  │   Code Agent    │
                  │               │  │                 │
                  │ • 页面快照     │  │ • 脚本生成      │
                  │ • 网络抓包     │  │ • 数据抓取      │
                  │ • DOM 分析     │  │ • 分页处理      │
                  │ • 登录处理     │  │ • 错误修复      │
                  └───────┬───────┘  └───────┬────────┘
                          │                  │
                          └────────┬─────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
             ┌──────▼──────┐ ┌────▼────┐ ┌──────▼──────┐
             │  MySQL RAG  │ │Checkpoint│ │  Artifacts  │
             │  经验记忆    │ │ 断点续传 │ │  数据输出    │
             └─────────────┘ └─────────┘ └─────────────┘
```

## Features

| Feature | Description |
|---------|-------------|
| 🤖 **Multi-Agent** | Supervisor + Browser + Code 三 Agent 协作 |
| 🔐 **Smart Auth** | 自动检测登录墙 · 人工确认 · 登录态持久化 |
| 🧠 **MySQL RAG** | 结构化经验记忆 · 策略复用 · 失败记忆 · fail-open |
| 💾 **Checkpoint** | 断点续传 · 网络证据持久化 · 任务恢复 |
| 📊 **Error Analysis** | 根因分类 · 重试策略 · 进度追踪 |
| 🌐 **Playwright MCP** | 浏览器自动化 · SPA 支持 · 网络拦截 |

## Quick Start

### Prerequisites

- Python 3.10+ · Node.js 22.19+ · MySQL 8.4+

### Installation

```bash
# Clone
git clone https://github.com/Palpitake/crawler.git
cd crawler

# Python dependencies
pip install -r requirements.txt

# Node.js runtime
cd pi-browser-agent && npm ci && cd ..
```

### Database Setup

```bash
# Option A: Docker
cp .env.mysql.example .env
docker compose -f docker-compose.mysql.yml --env-file .env up -d

# Option B: Existing MySQL
cp .env.mysql.example .env
mysql -u root -p < crawler_agent/rag/sql/001_mysql_rag_schema.sql

# Verify connection
python scripts/rag_health.py
```

### Run

```bash
python main.py
```

## Project Structure

```
crawler/
├── main.py                              # Entry point
├── crawler_agent/                       # Core package
│   ├── core/                            #   Logging · utils · error classification
│   ├── pipelines/                       #   Supervisor · Browser · Code agents
│   ├── runtimes/                        #   Pi agent runtime bindings
│   ├── tools/                           #   Playwright MCP · code execution
│   ├── auth/                            #   Auth decision · sessions · evidence
│   └── rag/                             #   MySQL RAG · memory cards · ranking
│       └── sql/001_mysql_rag_schema.sql
├── scripts/                             # Health · migration · maintenance
├── tests/                               # Test suite
├── docs/                                # RAG · Authentication docs
├── pi-browser-agent/                    # Node.js agent runtime
│   └── src/                             #   browser · code · supervisor agents
├── docker-compose.mysql.yml             # Docker MySQL
└── .env.mysql.example                   # Config template
```

## RAG Memory System

MySQL 结构化经验记忆，5 类记忆 + 失败防护：

| Memory Type | Stores |
|-------------|--------|
| `site` | 路由 · 页面类型 · 认证风险 |
| `strategy` | 数据源 · 交互方式 · 字段映射 · 分页策略 |
| `endpoint` | API 端点 · 请求/响应签名 |
| `authentication` | 认证状态 · 验证行为 |
| `failure` | 根因 · 阻断 TTL · 重试策略 |

> **Fail-open**: MySQL 不可用时自动降级为 JSONL，不阻塞爬虫执行。

## Configuration

```env
# .env (from .env.mysql.example)
RAG_BACKEND=mysql
RAG_MYSQL_HOST=127.0.0.1
RAG_MYSQL_PORT=3306
RAG_MYSQL_DATABASE=crawler_rag
RAG_MYSQL_USER=crawler_rag_app
RAG_MYSQL_PASSWORD=your_password
RAG_TOP_K_STRATEGY=8
```

See [.env.mysql.example](.env.mysql.example) for all options.

## Maintenance

```bash
python scripts/rag_health.py          # Health check
python scripts/rag_maintenance.py     # Daily: expire · quarantine · recalculate
python scripts/rag_migrate.py file.jsonl --dry-run  # Legacy migration
```

## Documentation

- [RAG MySQL Schema & Retrieval](docs/RAG_MYSQL.md)
- [Authentication Protocol](docs/AUTHENTICATION.md)

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Orchestration | LangGraph StateGraph |
| LLM | DeepSeek v3/v4 |
| Browser | Playwright MCP |
| Memory | MySQL 8.4 + PyMySQL |
| Runtime | Python 3.10+ · Node.js 22+ |
| Data | Pandas · CSV · JSON |

## License

[MIT](LICENSE)