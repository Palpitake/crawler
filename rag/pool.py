from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .config import RagConfig


@dataclass
class _PooledConnection:
    connection: Any
    created_at: float


class MySQLPool:
    """Small dependency-light PyMySQL pool with lazy imports.

    PyMySQL is imported only when MySQL is actually enabled, so the crawler can
    still start with RAG disabled or in JSONL fallback mode.
    """

    def __init__(self, config: RagConfig):
        self.config = config
        self._queue: queue.LifoQueue[_PooledConnection] = queue.LifoQueue(maxsize=config.mysql_pool_size)
        self._created = 0
        self._lock = threading.Lock()

    def _driver(self):
        try:
            import pymysql  # type: ignore
            return pymysql
        except Exception as exc:
            raise RuntimeError("PyMySQL is required when RAG_BACKEND=mysql; install requirements.txt") from exc

    def _create(self) -> _PooledConnection:
        pymysql = self._driver()
        connection = pymysql.connect(
            host=self.config.mysql_host,
            port=self.config.mysql_port,
            user=self.config.mysql_user,
            password=self.config.mysql_password,
            database=self.config.mysql_database,
            charset=self.config.mysql_charset,
            connect_timeout=self.config.mysql_connect_timeout,
            read_timeout=self.config.mysql_read_timeout,
            write_timeout=self.config.mysql_write_timeout,
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
        return _PooledConnection(connection=connection, created_at=time.time())

    def acquire(self) -> _PooledConnection:
        try:
            pooled = self._queue.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self.config.mysql_pool_size:
                    self._created += 1
                    try:
                        return self._create()
                    except Exception:
                        self._created -= 1
                        raise
            pooled = self._queue.get(timeout=max(1, self.config.mysql_connect_timeout))
        if time.time() - pooled.created_at > self.config.mysql_pool_recycle_seconds:
            self.discard(pooled)
            with self._lock:
                self._created += 1
            try:
                return self._create()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        try:
            pooled.connection.ping(reconnect=True)
        except Exception:
            self.discard(pooled)
            with self._lock:
                self._created += 1
            try:
                return self._create()
            except Exception:
                with self._lock:
                    self._created -= 1
                raise
        return pooled

    def release(self, pooled: _PooledConnection) -> None:
        try:
            self._queue.put_nowait(pooled)
        except queue.Full:
            self.discard(pooled)

    def discard(self, pooled: _PooledConnection) -> None:
        try:
            pooled.connection.close()
        except Exception:
            pass
        with self._lock:
            self._created = max(0, self._created - 1)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        pooled = self.acquire()
        try:
            yield pooled.connection
        finally:
            self.release(pooled)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        pooled = self.acquire()
        connection = pooled.connection
        try:
            connection.begin()
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            self.release(pooled)
