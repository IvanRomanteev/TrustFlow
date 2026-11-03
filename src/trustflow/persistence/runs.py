"""Durable RunRepository (M3): same API as the in-memory store.

The full ``WorkflowRun`` JSON is the record; ``status``/``version``
columns support per-record optimistic concurrency (F2). Every loaded
copy carries its expected version in ``run.persistence_version``;
``save()`` is a compare-and-set against that exact version, so a
stale detached copy can never overwrite newer durable state — in
this process or any other sharing the database file.
"""

import threading

from trustflow.core.errors import ConcurrentUpdateError
from trustflow.domain.models import WorkflowRun
from trustflow.orchestration.engine import RunRepository
from trustflow.persistence.db import connect


class SQLiteRunRepository(RunRepository):
    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)
        self._lock = threading.Lock()

    def save(self, run: WorkflowRun) -> WorkflowRun:
        payload = run.model_dump_json()
        with self._lock:
            if run.persistence_version == 0:
                try:
                    self._conn.execute(
                        "INSERT INTO runs(run_id, status, version, run_json, updated_at)"
                        " VALUES (?, ?, 1, ?, ?)",
                        (run.run_id, str(run.status), payload,
                         run.updated_at.isoformat()),
                    )
                except Exception as exc:
                    raise ConcurrentUpdateError(
                        f"Run {run.run_id} was created concurrently") from exc
                run.persistence_version = 1
                return run
            cursor = self._conn.execute(
                "UPDATE runs SET status = ?, version = version + 1,"
                " run_json = ?, updated_at = ? WHERE run_id = ? AND version = ?",
                (str(run.status), payload, run.updated_at.isoformat(),
                 run.run_id, run.persistence_version),
            )
            if cursor.rowcount == 0:
                raise ConcurrentUpdateError(
                    f"Run {run.run_id} changed since it was loaded; reload and retry")
            run.persistence_version += 1
            return run

    def get(self, run_id: str) -> WorkflowRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, run_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            run = WorkflowRun.model_validate_json(row["run_json"])
            run.persistence_version = int(row["version"])
            return run

    def list(self) -> list[WorkflowRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, run_json FROM runs"
                " ORDER BY updated_at DESC, run_id"
            ).fetchall()
            runs: list[WorkflowRun] = []
            for row in rows:
                run = WorkflowRun.model_validate_json(row["run_json"])
                run.persistence_version = int(row["version"])
                runs.append(run)
            return runs
