"""Tool registry: allowlist, schema validation, flat JSON schemas."""

from typing import Any, Protocol

from pydantic import BaseModel

from trustflow.core.errors import ToolNotFoundError, ToolValidationError
from trustflow.tools.ledger import (
    FxQuoteArgs,
    GetBalanceArgs,
    ListTransactionsArgs,
    SimulateTransferArgs,
    SyntheticLedger,
)


class ToolResult(BaseModel):
    ok: bool
    data: dict = {}
    error: str | None = None


class Tool(Protocol):
    name: str
    args_model: type[BaseModel]

    def invoke(self, args: BaseModel, idempotency_key: str = "") -> ToolResult: ...


class _BalanceTool:
    name = "get_balance"
    args_model = GetBalanceArgs

    def __init__(self, ledger: SyntheticLedger) -> None:
        self._ledger = ledger

    def invoke(self, args: BaseModel, idempotency_key: str = "") -> ToolResult:
        a = GetBalanceArgs.model_validate(args)
        acct = self._ledger.get_balance(a.account_id)
        return ToolResult(ok=True, data=acct.model_dump())


class _ListTxTool:
    name = "list_transactions"
    args_model = ListTransactionsArgs

    def __init__(self, ledger: SyntheticLedger) -> None:
        self._ledger = ledger

    def invoke(self, args: BaseModel, idempotency_key: str = "") -> ToolResult:
        a = ListTransactionsArgs.model_validate(args)
        rows = self._ledger.list_transactions(a.account_id, a.limit)
        return ToolResult(ok=True, data={"transactions": [r.model_dump() for r in rows]})


class _TransferTool:
    name = "simulate_transfer"
    args_model = SimulateTransferArgs

    def __init__(self, ledger: SyntheticLedger) -> None:
        self._ledger = ledger

    def invoke(self, args: BaseModel, idempotency_key: str = "") -> ToolResult:
        a = SimulateTransferArgs.model_validate(args)
        key = idempotency_key or a.idempotency_key
        if not key:
            raise ToolValidationError("simulate_transfer requires an idempotency_key")
        tx = self._ledger.transfer(a.from_account, a.to_account, a.amount, key, a.currency)
        return ToolResult(ok=True, data=tx.model_dump())


class _FxTool:
    name = "fx_quote"
    args_model = FxQuoteArgs
    _RATES = {("USD", "EUR"): 0.92, ("EUR", "USD"): 1.09, ("USD", "USD"): 1.0}

    def invoke(self, args: BaseModel, idempotency_key: str = "") -> ToolResult:
        a = FxQuoteArgs.model_validate(args)
        rate = self._RATES.get((a.from_currency, a.to_currency), 1.0)
        return ToolResult(
            ok=True,
            data={"rate": rate, "converted": round(a.amount * rate, 2), "synthetic": True},
        )


class ToolRegistry:
    """Only registered tools can run; args are always schema-validated."""

    def __init__(self, ledger: SyntheticLedger | None = None) -> None:
        self.ledger = ledger or SyntheticLedger()
        self._tools: dict[str, Tool] = {}
        for tool in (
            _BalanceTool(self.ledger),
            _ListTxTool(self.ledger),
            _TransferTool(self.ledger),
            _FxTool(),
        ):
            self._tools[tool.name] = tool  # type: ignore[assignment]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool not allowed: {name}")
        return self._tools[name]

    def invoke(self, name: str, raw_args: dict[str, Any], idempotency_key: str = "") -> ToolResult:
        tool = self.get(name)
        try:
            args = tool.args_model.model_validate(raw_args)
        except Exception as exc:
            raise ToolValidationError(f"Invalid args for {name}: {exc}") from exc
        return tool.invoke(args, idempotency_key=idempotency_key)

    def committed(self, idempotency_key: str):
        return self.ledger.committed(idempotency_key)

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Flat, non-recursive JSON Schemas for the Meta Model API."""
        defs: list[dict[str, Any]] = []
        for name in self.names():
            schema = self.get(name).args_model.model_json_schema()
            defs.append({"name": name, "parameters": schema})
        return defs
