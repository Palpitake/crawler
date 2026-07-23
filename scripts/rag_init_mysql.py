from __future__ import annotations

from pathlib import Path

from rag.config import RagConfig
from rag.pool import MySQLPool


def main() -> None:
    config = RagConfig.from_env()
    sql_path = Path(__file__).resolve().parents[1] / "rag" / "sql" / "001_mysql_rag_schema.sql"
    sql = sql_path.read_text(encoding="utf-8")
    pool = MySQLPool(config)
    # The schema file intentionally contains no stored programs, so semicolon
    # splitting is sufficient and avoids enabling multi-statements globally.
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    with pool.transaction() as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    print(f"initialized {len(statements)} statements in {config.mysql_database}")


if __name__ == "__main__":
    main()
