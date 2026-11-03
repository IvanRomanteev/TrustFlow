"""Durable synthetic ledger (M3): same API and contract as in-memory.

Each transfer is one ``BEGIN IMMEDIATE`` transaction: idempotency
lookup, balance updates and the transaction insert commit atomically.
``UNIQUE(idempotency_key)`` is the cross-process backstop — a racing
duplicate returns the committed tx for an identical payload and
raises ``ConflictingOperationError`` for a different one, exactly
like ``SyntheticLedger``. Balances/transactions are always read from
SQLite, so a new process sees every committed payment.
"""

import threading

from trustflow.core.errors import ConflictingOperationError
from trustflow.persistence.db import connect
from trustflow.tools.ledger import Account, LedgerTransaction, SyntheticLedger

_DEFAULT_ACCOUNTS = (
    ("acc_alice", "Alice (synthetic)", 5000.0),
    ("acc_bob", "Bob (synthetic)", 1200.0),
    ("acc_ops", "Ops (synthetic)", 10000.0),
)


class PersistentLedger(SyntheticLedger):
    def __init__(self, db_path: str) -> None:
        self._persistent_ready = False
        super().__init__()
        self._db_lock = threading.RLock()
        self._conn = connect(db_path)
        with self._db_lock:
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM ledger_accounts").fetchone()["n"]
            if int(count) == 0:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.executemany(
                        "INSERT INTO ledger_accounts(account_id, owner, balance)"
                        " VALUES (?, ?, ?)", _DEFAULT_ACCOUNTS)
                    self._conn.execute(
                        "INSERT OR IGNORE INTO ledger_meta(key, value)"
                        " VALUES ('tx_counter', 0)")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            self._persistent_ready = True

    # -- durable views (replace the in-memory mirrors) ----------------
    @property
    def accounts(self) -> dict[str, Account]:  # type: ignore[override]
        if not self._persistent_ready:
            fallback: dict[str, Account] = self.__dict__.get("_mem_accounts") or {}
            return fallback
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT account_id, owner, balance, currency"
                " FROM ledger_accounts").fetchall()
        return {
            row["account_id"]: Account(
                account_id=row["account_id"], owner=row["owner"],
                balance=float(row["balance"]), currency=row["currency"])
            for row in rows
        }

    @accounts.setter
    def accounts(self, value: dict[str, Account]) -> None:
        self.__dict__["_mem_accounts"] = value

    @property
    def transactions(self) -> list[LedgerTransaction]:  # type: ignore[override]
        if not self._persistent_ready:
            fallback: list[LedgerTransaction] = (
                self.__dict__.get("_mem_transactions") or [])
            return fallback
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT tx_id, from_account, to_account, amount, currency,"
                " idempotency_key FROM ledger_transactions ORDER BY seq").fetchall()
        return [self._to_tx(row) for row in rows]

    @transactions.setter
    def transactions(self, value: list[LedgerTransaction]) -> None:
        self.__dict__["_mem_transactions"] = value

    @staticmethod
    def _to_tx(row) -> LedgerTransaction:
        return LedgerTransaction(
            tx_id=row["tx_id"], from_account=row["from_account"],
            to_account=row["to_account"], amount=float(row["amount"]),
            currency=row["currency"], idempotency_key=row["idempotency_key"])

    # -- SyntheticLedger API ------------------------------------------
    def get_balance(self, account_id: str) -> Account:
        account = self.accounts.get(account_id)
        if account is None:
            raise KeyError(f"Unknown synthetic account: {account_id}")
        return account

    def list_transactions(self, account_id: str,
                          limit: int = 20) -> list[LedgerTransaction]:
        rows = [t for t in self.transactions
                if t.from_account == account_id or t.to_account == account_id]
        return rows[-limit:] if limit > 0 else []

    def total_balance(self) -> float:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(balance), 0) AS total"
                " FROM ledger_accounts").fetchone()
        return float(row["total"])

    def committed(self, idempotency_key: str) -> LedgerTransaction | None:
        return self.find_by_key(idempotency_key)

    def find_by_key(self, idempotency_key: str) -> LedgerTransaction | None:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT tx_id, from_account, to_account, amount, currency,"
                " idempotency_key FROM ledger_transactions"
                " WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return self._to_tx(row) if row is not None else None

    def transfer(self, from_account: str, to_account: str, amount: float,
                 idempotency_key: str, currency: str = "USD") -> LedgerTransaction:
        with self._db_lock:
            existing = self.find_by_key(idempotency_key)
            if existing is not None:
                return self._return_or_conflict(
                    existing, from_account, to_account, amount, currency)
            if self.fail_next_transfer:
                self.fail_next_transfer = False
                raise RuntimeError("Simulated transfer failure (injected)")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-check under the write lock: another process may
                # have committed this key since the unlocked read.
                row = self._conn.execute(
                    "SELECT tx_id, from_account, to_account, amount, currency,"
                    " idempotency_key FROM ledger_transactions"
                    " WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                if row is not None:
                    self._conn.execute("COMMIT")
                    return self._return_or_conflict(
                        self._to_tx(row), from_account, to_account,
                        amount, currency)
                src = self._account_row(from_account)
                self._account_row(to_account)  # existence check, as in-memory
                if amount <= 0:
                    raise ValueError("Amount must be positive")
                if float(src["balance"]) < amount:
                    raise ValueError("Insufficient synthetic funds")
                self._conn.execute(
                    "UPDATE ledger_accounts SET balance = balance - ?"
                    " WHERE account_id = ?", (amount, from_account))
                self._conn.execute(
                    "UPDATE ledger_accounts SET balance = balance + ?"
                    " WHERE account_id = ?", (amount, to_account))
                self._conn.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES ('tx_counter', 1)"
                    " ON CONFLICT(key) DO UPDATE SET value = value + 1")
                counter = int(self._conn.execute(
                    "SELECT value FROM ledger_meta WHERE key = 'tx_counter'"
                ).fetchone()["value"])
                tx_id = f"tx_{counter:04d}"
                self._conn.execute(
                    "INSERT INTO ledger_transactions"
                    "(tx_id, idempotency_key, from_account, to_account, amount, currency)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (tx_id, idempotency_key, from_account, to_account,
                     amount, currency))
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            committed = self.find_by_key(idempotency_key)
            assert committed is not None
            return committed

    def _account_row(self, account_id: str):
        row = self._conn.execute(
            "SELECT account_id, balance FROM ledger_accounts"
            " WHERE account_id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown synthetic account: {account_id}")
        return row

    @staticmethod
    def _return_or_conflict(existing: LedgerTransaction, from_account: str,
                            to_account: str, amount: float,
                            currency: str) -> LedgerTransaction:
        if (existing.from_account != from_account
                or existing.to_account != to_account
                or existing.amount != amount
                or existing.currency != currency):
            raise ConflictingOperationError(
                f"Operation {existing.idempotency_key} was already committed with "
                "different accounts/amount/currency")
        return existing
