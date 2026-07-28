"""
智能爬虫 Agent 系统 - 主入口

用户输入自然语言，系统自动生成爬虫代码并执行，最终产出数据文件。

使用方式：
    python main.py
    python main.py "爬取 https://movie.douban.com/top250 全部电影信息，保存为 csv"
"""

import os
import sys
import uuid
import hashlib
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

from crawler_agent.core.logger import (
    get_logger,
    log_event,
    redact_secrets,
    set_log_context,
    setup_logging,
)
from crawler_agent.pipelines.supervisor_pipeline import normalize_task, run_supervisor


load_dotenv(override=True)

logger = get_logger("pipeline")


WORKSPACE = os.getenv("AGENT_WORKSPACE", "./crawler_workspace")
LOG_LEVEL = os.getenv("AGENT_LOG_LEVEL", "INFO")
LOG_FILE = "crawler.log"

OUTPUT_DIR = Path(WORKSPACE) / "output"



def _artifact_metadata(raw_path: Optional[str]) -> dict:
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(WORKSPACE) / path
    try:
        path = path.resolve()
    except Exception:
        pass
    if not path.is_file():
        return {"path": str(path), "exists": False}
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    meta = {
        "path": str(path),
        "exists": True,
        "sha256": h.hexdigest(),
        "size_bytes": path.stat().st_size,
    }
    if path.suffix.lower() == ".py":
        try:
            meta["lines"] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception:
            pass
    return meta

def save_execution_log(result: str, task: str, thread_id: str) -> Optional[str]:
    """保存执行日志到文件"""
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"log_{timestamp}_{thread_id[:8]}.md"
        filepath = OUTPUT_DIR / filename

        content = f"""# 爬虫任务执行日志

**执行时间**: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
**任务ID**: {thread_id}
**用户需求**: {task}

---

{result}
"""

        filepath.write_text(content, encoding="utf-8")
        log_event(logger, "artifact.save", status="saved", artifact_type="execution_log", path=filepath)

        return str(filepath)

    except Exception as e:
        log_event(logger, "artifact.save", level="ERROR", status="failed", artifact_type="execution_log", error_type="artifact_write_failed", reason=str(e), exc_info=True)
        return None


def display_welcome():
    """显示欢迎信息"""
    print("\n" + "=" * 70)
    print("🤖 智能爬虫 Agent 系统")
    print("=" * 70)
    print("\n功能：")
    print("  • 理解自然语言描述的爬取需求")
    print("  • 自动分析网页结构")
    print("  • 生成爬虫代码并执行")
    print("  • 产出数据文件（CSV/JSON/Excel）\n")
    print("使用示例：")
    print('  "爬取 https://movie.douban.com/top250 电影信息，保存为 csv"')
    print('  "获取 https://news.ycombinator.com 的前10条新闻标题和链接"')
    print('  "抓取 https://quotes.toscrape.com 的所有名言和作者"')
    print('  "https://example.com"  （直接输入URL也可）\n')
    print("输入 exit / quit / q 退出")
    print("=" * 70)


def read_user_input() -> Optional[str]:
    """读取用户输入"""
    print("\n📝 请输入您的爬取需求：")
    print("-" * 70)

    try:
        user_input = input(">>> ").strip()
    except (KeyboardInterrupt, EOFError):
        return None

    if user_input.lower() in {"exit", "quit", "q"}:
        return None

    return user_input


def execute_crawler_task(task: str, thread_id: Optional[str] = None) -> dict:
    """
    执行爬虫任务

    参数:
        task: 用户自然语言任务描述
        thread_id: 可选的任务ID

    返回:
        包含结果的字典
    """
    if thread_id is None:
        thread_id = f"task-{uuid.uuid4().hex[:8]}"

    task = normalize_task(task)
    task_started = time.monotonic()
    set_log_context(task_id=thread_id)

    log_event(logger, "pipeline.start", status="started", mode="native_agent_loop", request=redact_secrets(task))

    print(f"\n🚀 任务已提交 (ID: {thread_id})")
    print("   模式：pi-agent-core 自主选择 RAG、网页分析、代码执行、复验与结束能力\n")

    try:
        state = run_supervisor(
            user_request=task,
            thread_id=thread_id,
        )

        final = state.get("final_output", {})

        parts = ["# 爬虫任务执行结果\n"]
        parts.append(f"**状态**: {final.get('status', 'unknown')}")
        parts.append(f"**摘要**: {final.get('summary', 'N/A')}")
        counts = final.get("capability_counts") if isinstance(final.get("capability_counts"), dict) else {}
        parts.append(
            "**实际运行**: "
            f"Browser运行={int(final.get('browser_runs', 0) or 0)}, "
            f"Code运行={int(final.get('code_runs', 0) or 0)} "
            f"(探针={int(final.get('code_probe_runs', 0) or 0)}, 完整={int(final.get('code_full_runs', 0) or 0)})"
        )
        parts.append(
            "**能力请求**: "
            f"Code复验={int(counts.get('recheck_code', 0) or 0)}, "
            f"Supervisor检查={int(counts.get('inspect_task', 0) or 0)}, "
            f"总计={sum(int(v or 0) for v in counts.values())}"
        )
        if any(int(final.get(key, 0) or 0) > 0 for key in ("browser_retries", "execution_retries", "code_repairs")):
            parts.append(
                "**重试**: "
                f"Browser={int(final.get('browser_retries', 0) or 0)}, "
                f"Code={int(final.get('execution_retries', 0) or 0)}, "
                f"Code修复/复验={int(final.get('code_repairs', 0) or 0)}"
            )
        if final.get("duration_ms") is not None:
            parts.append(f"**总耗时**: {float(final.get('duration_ms', 0) or 0) / 1000:.1f} 秒")
        if final.get("authentication_state"):
            parts.append(
                f"**认证状态**: {final.get('authentication_state')} "
                f"(verified={final.get('auth_verification_state')}, authenticated={final.get('authenticated')})"
            )
        if final.get("data_file"):
            parts.append(f"\n**数据文件**: {final['data_file']}")
            parts.append(f"**数据条数**: {final.get('total_items', 0)}")
            parts.append(f"**字段**: {', '.join(final.get('fields', []))}")
        if final.get("code_file"):
            code_meta = _artifact_metadata(final.get("code_file"))
            parts.append(f"**代码文件**: {code_meta.get('path') or final['code_file']}")
            if code_meta.get("sha256"):
                parts.append(f"**代码 SHA-256**: {code_meta['sha256']}")
            if code_meta.get("lines") is not None:
                parts.append(f"**代码行数**: {code_meta['lines']}")
        if final.get("selected_run"):
            parts.append(f"**选中运行版本**: {final['selected_run']}")
        if final.get("latest_candidate_run") and final.get("latest_candidate_run") != final.get("selected_run"):
            parts.append(
                f"**未选中候选**: {final.get('latest_candidate_run')} "
                f"({int(final.get('latest_candidate_items', 0) or 0)} 条)"
            )
        if final.get("replacement_reason"):
            parts.append(f"**版本选择原因**: {final.get('replacement_reason')}")
        warning_codes = final.get("warning_codes") or []
        if warning_codes:
            parts.append(f"**警告代码**: {', '.join(map(str, warning_codes))}")
        if final.get("debug_file"):
            parts.append(f"**Python Debug文件**: {final['debug_file']}")
        if final.get("log_file"):
            parts.append(f"**日志文件**: {final['log_file']}")
        error = final.get("error_info") or {}
        if error.get("error_type"):
            parts.append(f"\n**根因类型**: {error.get('root_error_type') or error['error_type']}")
            if error.get("terminal_error_type"):
                parts.append(f"**终止症状**: {error['terminal_error_type']}")
            if error.get("error_category"):
                parts.append(f"**错误分类**: {error['error_category']}")
            if error.get("retry_strategy"):
                parts.append(f"**建议策略**: {error['retry_strategy']}")
            if error.get("internal_error_type") and error.get("internal_error_type") != error.get("error_type"):
                parts.append(f"**内部错误类型**: {error['internal_error_type']}")
            parts.append(f"**错误信息**: {error.get('error_message', 'N/A')}")
        if final.get("next_action"):
            parts.append(f"\n**下一步**: {final['next_action']}")
        result = "\n".join(parts)

        log_file = save_execution_log(result, task, thread_id)

        data_file = final.get("data_file")
        data_files = [data_file] if data_file else []
        data_meta = _artifact_metadata(data_file)
        code_meta = _artifact_metadata(final.get("code_file"))
        if data_meta.get("exists"):
            log_event(logger, "artifact.save", status="saved", artifact_type="data_file", rows=final.get("total_items", 0), **data_meta)
        if code_meta.get("exists"):
            log_event(logger, "artifact.save", status="saved", artifact_type="source_code", **code_meta)
        succeeded = final.get("status") == "success"
        log_event(
            logger, "pipeline.finish",
            level="INFO" if succeeded else "WARNING",
            status="success" if succeeded else "failed",
            items=final.get("total_items", 0),
            duration_ms=final.get("duration_ms") or int((time.monotonic() - task_started) * 1000),
            browser_runs=final.get("browser_runs", 0),
            code_runs=final.get("code_runs", 0),
            browser_retries=final.get("browser_retries", 0),
            execution_retries=final.get("execution_retries", 0),
            code_repairs=final.get("code_repairs", 0),
            capability_counts=final.get("capability_counts", {}),
            selected_run=final.get("selected_run"),
            previous_best_items=final.get("previous_best_items", 0),
            latest_candidate_items=final.get("latest_candidate_items", 0),
            latest_candidate_run=final.get("latest_candidate_run"),
            replacement_reason=final.get("replacement_reason"),
            warning_codes=final.get("warning_codes", []),
            root_error_type=(error or {}).get("root_error_type") if not succeeded else None,
            terminal_error_type=(error or {}).get("terminal_error_type") if not succeeded else None,
            error_category=(error or {}).get("error_category") if not succeeded else None,
            retry_strategy=(error or {}).get("retry_strategy") if not succeeded else None,
            authentication_state=final.get("authentication_state"),
            code_probe_runs=final.get("code_probe_runs", 0),
            code_full_runs=final.get("code_full_runs", 0),
            artifact_path=data_meta.get("path") or data_file,
        )

        return {
            "success": succeeded,
            "thread_id": thread_id,
            "result": result,
            "log_file": log_file,
            "data_files": data_files,
            "generated_code": final.get("generated_code"),
        }

    except Exception as e:
        log_event(logger, "pipeline.finish", level="ERROR", status="failed", error_type=type(e).__name__, reason=str(e), exc_info=True)
        return {
            "success": False,
            "thread_id": thread_id,
            "error": str(e),
        }


def display_result(result_data: dict):
    """显示执行结果"""
    print("\n" + "=" * 70)
    print("📊 执行结果")
    print("=" * 70)

    if result_data.get("success"):
        result = result_data.get("result", "")
        print(result)

        data_files = result_data.get("data_files", [])
        if data_files:
            print("\n" + "-" * 40)
            print("📁 生成的数据文件：")
            for path in data_files:
                print(f"   ✅ {path}")
    else:
        result = result_data.get("result", "")
        if result:
            print(result)
        else:
            print(f"\n❌ 任务执行失败")
            print(f"   错误信息：{result_data.get('error', '未知错误')}")

    log_file = result_data.get("log_file")
    if log_file:
        print(f"\n📄 执行日志：{log_file}")

    print("=" * 70)


def main():
    """
    智能爬虫 Agent 系统主入口

    支持两种使用方式：
    1. 命令行参数：python main.py "爬取 https://example.com 的数据，保存为 csv"
    2. 交互模式：python main.py 后输入需求
    """
    setup_logging(
        workspace=WORKSPACE,
        level=LOG_LEVEL,
        log_file=LOG_FILE,
    )

    log_event(logger, "system.ready", status="ready", system="crawler-agent")

    display_welcome()

    initial_task = None
    if len(sys.argv) > 1:
        initial_task = " ".join(sys.argv[1:])
        log_event(logger, "input.received", status="received", source="argv", request=redact_secrets(initial_task))

    while True:
        try:
            if initial_task:
                task = initial_task
                initial_task = None
            else:
                task = read_user_input()

        except (KeyboardInterrupt, EOFError):
            print("\n\n👋 再见！")
            break

        if task is None:
            print("\n👋 再见！")
            break

        if not task:
            print("⚠️  没有检测到有效输入，请重新输入。")
            continue

        result_data = execute_crawler_task(task)
        display_result(result_data)

        print("\n是否继续执行其他爬取任务？")
        try:
            continue_input = input("输入 y 继续，其他退出：").strip().lower()
            if continue_input not in {"y", "yes", "是"}:
                print("\n👋 再见！")
                break
        except (KeyboardInterrupt, EOFError):
            print("\n\n👋 再见！")
            break


if __name__ == "__main__":
    main()
