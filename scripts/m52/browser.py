"""Real Chromium interactions for the local, synthetic M5.2 demonstration.

Playwright is deliberately imported at runtime: default offline tests do not
need a browser. No route interception, mocked responses or direct storage
updates are used. Every displayed run is checked against an authoritative GET.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _amount_pattern(amount: float, currency: str) -> re.Pattern[str]:
    # JavaScript String(5000.0) renders "5000". Match the complete amount
    # and currency, so 50000 USD cannot satisfy a 5000 USD inspection.
    displayed = format(amount, ".15g")
    return re.compile(rf"(?:^|\s){re.escape(displayed)}\s+{re.escape(currency)}(?:\s|$)")


class DemoBrowser:
    """A browser session whose screenshots and diagnostic log contain no token."""

    def __init__(self, base_url: str, api_url: str, out_dir: Path, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.out_dir = Path(out_dir)
        self.token = token
        self.events: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.screenshots: list[str] = []
        self._expected_unavailability = 0
        self._document_navigation = 0
        self._closing = False
        self._manager = None
        self._browser = None
        self.context = None
        self.page = None
        self.version = "not-started"

    def _sanitize(self, value: object) -> str:
        text = str(value)
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        return text[:2000]

    @staticmethod
    def _path(url: str) -> str:
        parsed = urlsplit(url)
        # Queries/fragments may contain secrets. They are never recorded.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _record(self, kind: str, **values: Any) -> None:
        self.events.append({"kind": kind, **values})

    def _check(self, name: str) -> None:
        self.assertions.append({"name": name, "passed": True})

    def __enter__(self) -> DemoBrowser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "M5.2 requires Playwright: pip install -e '.[dev,demo]'; "
                "python -m playwright install chromium"
            ) from exc

        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "screenshots").mkdir(exist_ok=True)
        self._manager = sync_playwright().start()
        try:
            self._browser = self._manager.chromium.launch(headless=True)
            self.version = self._browser.version
            self.context = self._browser.new_context(
                viewport={"width": 1600, "height": 1080},
                locale="en-US",
                timezone_id="UTC",
                reduced_motion="reduce",
            )
            self.page = self.context.new_page()
            self.page.set_default_timeout(30000)
            self.page.on(
                "pageerror",
                lambda error: self._record("pageerror", error=self._sanitize(error)),
            )
            self.page.on("requestfailed", self._request_failed)
            self.page.on("response", self._response)
            self.page.on(
                "console",
                lambda message: self._record(
                    "console", level=message.type, text=self._sanitize(message.text)
                ) if message.type in {"error", "warning"} else None,
            )
            self.page.goto(self.base_url, wait_until="domcontentloaded")
            self._expect(
                self.page.get_by_text("SANDBOX / NO REAL FUNDS", exact=False)
            ).to_be_visible()
            self._expect(self.page.locator("header.page-head")).to_contain_text(
                re.compile(r"backend ok.*persistence=sqlite.*llm=fake")
            )
            self._check("real_chromium_frontend_connected_to_fake_sqlite_backend")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def _expect(locator: Any) -> Any:
        from playwright.sync_api import expect

        return expect(locator)

    def _request_failed(self, request: Any) -> None:
        failure = self._sanitize(request.failure or "unknown request failure")
        # A browser navigation can cancel the previous document's fetch. Record
        # it, but do not classify intentional document cancellation as an outage.
        cancelled_navigation = "ERR_ABORTED" in failure and (
            request.is_navigation_request() or self._document_navigation or self._closing
        )
        self._record(
            "requestfailed",
            method=request.method,
            url=self._path(request.url),
            failure=failure,
            expected=bool(self._expected_unavailability or cancelled_navigation),
        )

    def _response(self, response: Any) -> None:
        self._record(
            "response", method=response.request.method,
            url=self._path(response.url), status=response.status,
            expected=bool(self._expected_unavailability),
        )

    @contextmanager
    def expect_backend_unavailable(self):
        """Bracket an actual, owned backend interruption, including its recovery.

        Errors remain visible in the log. This does not excuse JavaScript page
        errors, or failed requests outside the explicit lifecycle experiment.
        """
        self._expected_unavailability += 1
        self._record("backend_interruption_begin")
        try:
            yield
        finally:
            self._expected_unavailability -= 1
            self._record("backend_interruption_end")

    def _get(self, path: str) -> dict[str, Any]:
        response = self.context.request.get(f"{self.api_url}{path}")
        self._record("authoritative_get", path=path, status=response.status)
        if response.status != 200:
            raise AssertionError(f"authoritative GET {path}: HTTP {response.status}")
        return response.json()

    def _read(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/workflows/{run_id}")

    def create(
        self, title: str, description: str, require_evidence: bool = False
    ) -> dict[str, Any]:
        """Create through the real task form and then inspect backend GET state."""
        self.page.get_by_label("Title", exact=True).fill(title)
        self.page.get_by_label("Description", exact=True).fill(description)
        self.page.get_by_role(
            "checkbox", name="Require mandatory evidence (M2 opt-in)"
        ).set_checked(require_evidence)
        with self.page.expect_response(
            lambda response: response.url == f"{self.api_url}/workflows"
            and response.request.method == "POST"
        ) as pending:
            self.page.get_by_role("button", name="Create workflow", exact=True).click()
        response = pending.value
        if response.status != 201:
            raise AssertionError(f"UI workflow creation: HTTP {response.status}")
        acknowledgement = response.json()
        run_id = acknowledgement["run_id"]
        self._expect(self.page.get_by_label("Runs (durable, newest first)")).to_have_value(
            run_id
        )
        run = self._read(run_id)
        self.assert_run(run)
        self._check(f"ui_create:{title}")
        return run

    def select(self, run_id: str) -> dict[str, Any]:
        """Use the actual run selector; no localStorage manipulation."""
        selector = self.page.get_by_label("Runs (durable, newest first)")
        self._expect(selector.locator(f'option[value="{run_id}"]')).to_have_count(1)
        selector.select_option(run_id)
        self._expect(selector).to_have_value(run_id)
        run = self._read(run_id)
        self.assert_run(run)
        self._check("ui_run_selection")
        return run

    @staticmethod
    def _run_id(run: dict[str, Any] | str) -> str:
        return run if isinstance(run, str) else run["run_id"]

    def refresh(self, run: dict[str, Any] | str) -> dict[str, Any]:
        """Reload the document and prove that the persisted selection survives."""
        run_id = self._run_id(run)
        self._expect(self.page.get_by_label("Runs (durable, newest first)")).to_have_value(
            run_id
        )
        self._document_navigation += 1
        try:
            self.page.reload(wait_until="domcontentloaded")
        finally:
            self._document_navigation -= 1
        self._expect(self.page.get_by_label("Runs (durable, newest first)")).to_have_value(
            run_id
        )
        current = self._read(run_id)
        if isinstance(run, dict):
            assert current["status"] == run["status"], "reload changed expected persisted status"
            assert (current.get("plan") or {}).get("plan_hash") == (
                run.get("plan") or {}).get("plan_hash"), "reload changed exact plan hash"
            assert current.get("approved_hash") == run.get("approved_hash"), (
                "reload changed approved hash")
            assert current.get("approvals") == run.get("approvals"), (
                "reload changed persisted decisions")
        self.assert_run(current)
        self._check("ui_reload_authoritative_persisted_state")
        return current

    def inspect_approval(self, run: dict[str, Any]) -> None:
        if run["status"] != "AWAITING_APPROVAL":
            raise AssertionError("approval inspection requires AWAITING_APPROVAL")
        self.assert_run(run)
        timeline = self.page.get_by_role("region", name="Run timeline", exact=True)
        self._expect(timeline.get_by_role("cell", name="HIGH", exact=True).first).to_be_visible()
        approval = self.page.get_by_role(
            "region", name="Action approval and evidence", exact=True
        )
        self._expect(approval.get_by_text(run["plan"]["plan_hash"], exact=True)).to_be_visible()
        self._expect(approval).to_contain_text("simulate_transfer")
        for step in run["plan"]["steps"]:
            if step["tool_name"] == "simulate_transfer":
                operation = step.get("operation_id") or step["step_id"]
                row = approval.get_by_role("row").filter(
                    has=self.page.get_by_role("cell", name=operation, exact=True)
                )
                self._expect(row.get_by_role("cell").nth(1)).to_contain_text(
                    _amount_pattern(step["args"]["amount"], step["args"].get("currency", "USD"))
                )
        self._check("ui_high_risk_exact_plan_hash_inspection")

    def _decide(self, run: dict[str, Any], action: str, button: str) -> dict[str, Any]:
        self.select(run["run_id"])
        self.page.get_by_label("Approver", exact=True).fill("demo-manager")
        self.page.get_by_label("Approval token (demo secret, session only)", exact=True).fill(
            self.token
        )
        with self.page.expect_response(
            lambda response: response.url == f"{self.api_url}/workflows/{run['run_id']}/{action}"
            and response.request.method == "POST"
        ) as pending:
            self.page.get_by_role("button", name=button, exact=True).click()
        response = pending.value
        self.page.get_by_label("Approval token (demo secret, session only)", exact=True).fill("")
        if response.status != 200:
            raise AssertionError(f"UI {action}: HTTP {response.status}")
        current = self._read(run["run_id"])
        self.assert_run(current)
        self._check(f"ui_{action}")
        return current

    def approve(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._decide(run, "approve", "Approve exact plan")

    def reject(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._decide(run, "reject", "Reject")

    def recovery(self, run: dict[str, Any], action: str = "reconcile") -> dict[str, Any]:
        buttons = {
            "reconcile": "Reconcile from ledger",
            "verify-retry": "Retry verification only",
        }
        if action not in buttons:
            raise ValueError(f"unsupported recovery action {action!r}")
        return self._decide(run, action, buttons[action])

    def assert_run(self, run: dict[str, Any]) -> None:
        """Compare status, hash, deterministic evidence and audit with fresh GETs."""
        timeline = self.page.get_by_role("region", name="Run timeline", exact=True)
        self._expect(timeline.get_by_text(f"run_id: {run['run_id']}", exact=True)).to_be_visible()
        self._expect(timeline.get_by_text(run["goal"]["title"], exact=True)).to_be_visible()
        self._expect(timeline.get_by_text(run["status"], exact=True)).to_be_visible()
        self._expect(timeline.get_by_role("heading", name="Verification evidence")).to_be_visible()
        self._expect(
            timeline.get_by_role("heading", name="Audit trail (append-only, hashed)")
        ).to_be_visible()
        audit = self._get(f"/workflows/{run['run_id']}/audit")
        if not audit["events"]:
            raise AssertionError("created workflow has no audit evidence")
        for action in {event["action"] for event in audit["events"]}:
            self._expect(timeline.locator("ul.audit").get_by_text(action, exact=True).first
                         ).to_be_visible()
        verification = run.get("verification")
        if verification:
            checks = verification["checks"]
            deterministic = "PASS" if checks and all(c["passed"] for c in checks) else "FAIL"
            if checks:
                self._expect(timeline).to_contain_text(f"Deterministic: {deterministic}")
            self._expect(timeline).to_contain_text(
                f"Overall: {'PASS' if verification['passed'] else 'FAIL'}"
            )
            for check in checks:
                cell = timeline.get_by_role("cell", name=check["name"], exact=True)
                self._expect(cell).to_be_visible()
                self._expect(cell.locator("..")).to_contain_text(
                    "pass" if check["passed"] else "FAIL"
                )
        if run.get("plan"):
            approval = self.page.get_by_role("region", name="Action approval and evidence")
            self._expect(approval.get_by_text(run["plan"]["plan_hash"], exact=True).first
                         ).to_be_visible()
        self._check(f"ui_authoritative_status_audit_verification:{run['status']}")

    def capture(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("screenshot name must be a simple identifier")
        # Clear the password even though Chromium masks it. Trace/HAR/video are
        # intentionally disabled because they can retain request headers.
        self.page.get_by_label("Approval token (demo secret, session only)", exact=True).fill("")
        destination = self.out_dir / "screenshots" / f"{name}.png"
        self.page.screenshot(path=str(destination), full_page=True, animations="disabled")
        self.screenshots.append(destination.relative_to(self.out_dir).as_posix())
        return destination

    def assert_clean(self) -> None:
        failures = [
            event for event in self.events
            if event["kind"] == "pageerror"
            or (event["kind"] == "requestfailed" and not event.get("expected"))
            or (event["kind"] == "response" and event["status"] >= 400
                and not event.get("expected")
                and urlsplit(event["url"]).path != "/favicon.ico")
        ]
        if failures:
            raise AssertionError(f"unexpected browser errors: {self._sanitize(failures)}")
        self._check("no_unexpected_browser_page_or_network_errors")

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._closing = True
        try:
            if self.context is not None:
                self.context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._manager is not None:
                self._manager.stop()
            if self.out_dir.is_dir():
                (self.out_dir / "browser-log.json").write_text(
                    json.dumps(
                        {"chromium_version": self.version, "events": self.events,
                         "assertions": self.assertions, "screenshots": self.screenshots},
                        indent=2, sort_keys=True,
                    ) + "\n", encoding="utf-8",
                )
        return False
