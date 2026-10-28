"""In-memory synthetic ledger. No real money ever moves here."""

import math
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field


def strict_json_int(value: object) -> int:
    """JSON Schema ``integer``: accepts ints and integer-valued numbers,
    rejects bool (an int subclass in Python), strings and fractions."""
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError("expected a JSON integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError("expected a JSON integer")


def strict_json_number(value: object) -> float:
    """JSON Schema ``number``: accepts ints/floats, rejects bool/strings."""
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError("expected a JSON number")
    if isinstance(value, (int, float)):
        result = float(value)
        if math.isfinite(result):
            return result
    raise ValueError("expected a JSON number")


StrictJsonInt = Annotated[int, BeforeValidator(strict_json_int)]
StrictJsonNumber = Annotated[float, BeforeValidator(strict_json_number)]


class Account(BaseModel):
    account_id: str
    owner: str
    balance: float
    currency: str = "USD"


class LedgerTransaction(BaseModel):
    tx_id: str
    from_account: str
    to_account: str
    amount: float
    currency: str = "USD"
    idempotency_key: str


class SyntheticLedger:
    """Deterministic fake ledger with idempotent transfers."""

    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {
            "acc_alice": Account(account_id="acc_alice", owner="Alice (synthetic)", balance=5000.0),
            "acc_bob": Account(account_id="acc_bob", owner="Bob (synthetic)", balance=1200.0),
            "acc_ops": Account(account_id="acc_ops", owner="Ops (synthetic)", balance=10000.0),
        }
        self.transactions: list[LedgerTransaction] = []
        self._by_idempotency: dict[str, LedgerTransaction] = {}
        self._counter = 0
        # Test hook: when True the next transfer fails (failure injection).
        self.fail_next_transfer = False

    def get_balance(self, account_id: str) -> Account:
        if account_id not in self.accounts:
            raise KeyError(f"Unknown synthetic account: {account_id}")
        return self.accounts[account_id]

    def list_transactions(self, account_id: str, limit: int = 20) -> list[LedgerTransaction]:
        rows = [
            t
            for t in self.transactions
            if t.from_account == account_id or t.to_account == account_id
        ]
        return rows[-limit:]

    def total_balance(self) -> float:
        return sum(a.balance for a in self.accounts.values())

    def committed(self, idempotency_key: str) -> LedgerTransaction | None:
        """Return an already-committed tx for a key (timeout reconciliation)."""
        return self._by_idempotency.get(idempotency_key)

    def find_by_key(self, idempotency_key: str) -> LedgerTransaction | None:
        return self._by_idempotency.get(idempotency_key)

    def transfer(
        self, from_account: str, to_account: str, amount: float, idempotency_key: str,
        currency: str = "USD",
    ) -> LedgerTransaction:
        """Idempotent: same key + same payload returns the original tx.
        Same key with conflicting payload fails explicitly (no silent reuse)."""
        if idempotency_key in self._by_idempotency:
            existing = self._by_idempotency[idempotency_key]
            if (
                existing.from_account != from_account
                or existing.to_account != to_account
                or existing.amount != amount
                or existing.currency != currency
            ):
                from trustflow.core.errors import ConflictingOperationError

                raise ConflictingOperationError(
                    f"Operation {idempotency_key} was already committed with "
                    "different accounts/amount/currency"
                )
            return existing
        if self.fail_next_transfer:
            self.fail_next_transfer = False
            raise RuntimeError("Simulated transfer failure (injected)")
        src = self.get_balance(from_account)
        dst = self.get_balance(to_account)
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if src.balance < amount:
            raise ValueError("Insufficient synthetic funds")
        src.balance -= amount
        dst.balance += amount
        self._counter += 1
        tx = LedgerTransaction(
            tx_id=f"tx_{self._counter:04d}",
            from_account=from_account,
            to_account=to_account,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
        )
        self.transactions.append(tx)
        self._by_idempotency[idempotency_key] = tx
        return tx


# Flat arg/result models only (no recursive schemas, Meta API compatible).
class GetBalanceArgs(BaseModel):
    account_id: str


class ListTransactionsArgs(BaseModel):
    account_id: str
    limit: Annotated[StrictJsonInt, Field(ge=1, le=100)] = 20


class SimulateTransferArgs(BaseModel):
    from_account: str
    to_account: str
    amount: Annotated[StrictJsonNumber, Field(gt=0)]
    currency: str = "USD"
    idempotency_key: str = ""


class FxQuoteArgs(BaseModel):
    from_currency: str
    to_currency: str
    amount: Annotated[StrictJsonNumber, Field(gt=0)]
