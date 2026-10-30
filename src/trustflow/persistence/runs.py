"""Durable RunRepository (M3): same API as the in-memory store.

The full ``WorkflowRun`` JSON is the record; ``status``/``version``
columns support optimistic concurrency. The version seen at ``get()``
must still match at ``save()`` — this keeps hash-bound approvals
single-use across processes, not only under an in-process lock.
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
        self._versions: dict[str, int] = {}

    def save(self, run: WorkflowRun) -> WorkflowRun:
        payload = run.model_dump_json()
        with self._lock:
            expected = self._versions.get(run.run_id)
            if expected is None:
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
                self._versions[run.run_id] = 1
                return run
            cursor = self._conn.execute(
                "UPDATE runs SET status = ?, version = version + 1,"
                " run_json = ?, updated_at = ? WHERE run_id = ? AND version = ?",
                (str(run.status), payload, run.updated_at.isoformat(),
                 run.run_id, expected),
            )
            if cursor.rowcount == 0:
                raise ConcurrentUpdateError(
                    f"Run {run.run_id} changed since it was loaded; reload and retry")
            self._versions[run.run_id] = expected + 1
            return run

    def get(self, run_id: str) -> WorkflowRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, run_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            self._versions[run_id] = int(row["version"])
            return WorkflowRun.model_validate_json(row["run_json"])

    def list(self) -> list[WorkflowRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, version, run_json FROM runs"
                " ORDER BY updated_at DESC, run_id"
            ).fetchall()
            runs: list[WorkflowRun] = []
            for row in rows:
                self._versions[str(row["run_id"])] = int(row["version"])
                runs.append(WorkflowRun.model_validate_json(row["run_json"]))
            return runs
