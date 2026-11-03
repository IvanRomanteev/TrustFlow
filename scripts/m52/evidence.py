"""Fail-closed publication and explicit, narrow reproducibility normalization."""

import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .infrastructure import sanitize

NORMALIZATION_RULES = [
    "Compare only the versioned projection in normalized-results.json, never raw artifacts.",
    "Exclude invocation/project/container/image identities, paths, ports, UUIDs and timestamps.",
    "Exclude raw plan hashes (operation identities are random); recompute/bind them per run.",
    "Exclude audit payload hashes and evidence snapshot IDs; validate them independently per run.",
    "Retain scenario IDs/verdicts and each assertion name/outcome, final workflow statuses,",
    "transfer payloads/tx_id order, balances, audit action counts, verification check outcomes,",
    "browser assertion names, screenshot filenames and resource preservation/cleanup verdicts.",
    "Map dynamic intent check suffixes to :<intent>; aggregate counts, preserving multiplicity.",
    "Exclude concurrent winner identity/event order; retain the exact HTTP status multiset.",
]


def write_text(path: Path, value: str) -> None:
    """Flush a private sibling file before atomically publishing its final name."""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            current = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            # Only our exclusively created regular temporary file may be
            # unlinked. A replacement is preserved; no recursive cleanup.
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) \
                    and stat.S_ISREG(current.st_mode) \
                    and not getattr(current, "st_file_attributes", 0) \
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                temporary.unlink()


def write_json(path: Path, value) -> None:
    # Exclusive attempt directories eliminate overwrite of previous successes.
    write_text(path, json.dumps(sanitize(value), indent=2, sort_keys=True, ensure_ascii=False)
               + "\n")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def new_attempt(base: Path, invocation: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    out = base / invocation
    out.mkdir(exist_ok=False)
    return out


def normalized(scenarios: list[dict], final: dict, browser: dict,
               cleanup: dict, preservation: dict) -> dict:
    from collections import Counter

    runs = []
    for item in final.get("runs", {}).values():
        run = item["run"]
        checks = []
        for check in (run.get("verification") or {}).get("checks", []):
            name = check["name"]
            if name.startswith(("intent_coverage:", "intent_integrity:", "intent_conflict:",
                                "intent_cancellation:")):
                name = name.split(":", 1)[0] + ":<intent>"
            checks.append({"name": name, "passed": check["passed"]})
        actions = Counter(e["action"] for e in final["audit"] if e["run_id"] == run["run_id"])
        runs.append({"title": run["goal"]["title"], "status": run["status"],
                     "checks": checks, "audit_actions": dict(sorted(actions.items())),
                     "approval_count": len(run["approvals"]),
                     "semantic_attempts": run["verification_attempts"]})
    return {"schema_version": 1, "scenarios": [
        {**{k: s[k] for k in ("id", "verdict", "assertions")},
         "concurrent_statuses": s.get("concurrent_statuses")} for s in scenarios],
        "balances": final.get("balances"), "total": final.get("total"),
        "transfers": [{k: t[k] for k in (
            "tx_id", "from_account", "to_account", "amount", "currency")}
            for t in final.get("transactions", [])],
        "runs": sorted(runs, key=lambda r: r["title"]),
        "browser_assertions": [{"name": name, "passed": passed} for name, passed in sorted(
            {(a["name"], bool(a["passed"])) for a in browser.get("assertions", [])})],
        "screenshots": sorted(browser.get("screenshots", [])),
        "cleanup_passed": cleanup.get("passed", False),
        "preservation_passed": preservation.get("unchanged", False)}


def compare(previous: Path, current: dict) -> dict:
    complete = json.loads((previous / "COMPLETE.json").read_text(encoding="utf-8"))
    assert complete.get("verdict") == "PASS", "comparison requires successful prior run"
    old = json.loads((previous / "normalized-results.json").read_text(encoding="utf-8"))
    expected = complete["normalized_digest"]
    assert digest(old) == expected, "prior normalization digest is invalid"
    return {"previous": str(previous.resolve()), "previous_digest": expected,
            "current_digest": digest(current), "equivalent": old == current}


def _publish(out: Path, manifest: dict, failures: list[str], projection: dict) -> bool:
    """COMPLETE is the final write, after every mandatory evidence file is durable."""
    write_json(out / "normalized-results.json", projection)
    write_json(out / "normalization-rules.json", NORMALIZATION_RULES)
    required = ["scenario-results.json", "ledger-before.json", "ledger-after.json",
                "audit-summary.json", "browser-log.json", "commands.jsonl", "environment.json",
                "runtime-logs.json", "runtime-bindings.json", "README.md",
                "preservation-before.json", "preservation-after.json"]
    missing = [name for name in required if not (out / name).is_file()]
    failures.extend(f"mandatory artifact missing: {name}" for name in missing)
    if not list((out / "screenshots").glob("*.png")):
        failures.append("mandatory browser screenshots missing")
    scenarios = manifest["scenarios"]
    if len(scenarios) != 6 or {s["id"] for s in scenarios} != {f"S{i}" for i in range(1, 7)} \
            or any(s["verdict"] != "PASS" or not s.get("assertions")
                   or any(a.get("passed") is not True for a in s["assertions"]) for s in scenarios):
        failures.append("six mandatory scenarios did not all pass")
    if (out / "browser-log.json").is_file():
        browser = json.loads((out / "browser-log.json").read_text(encoding="utf-8"))
        if not browser.get("chromium_version") or browser["chromium_version"] == "not-started" \
                or not browser.get("assertions") \
                or any(a.get("passed") is not True for a in browser["assertions"]):
            failures.append("mandatory real browser assertions did not pass")
        screenshot_names = browser.get("screenshots", [])
        if not screenshot_names or any(
                Path(name).is_absolute() or ".." in Path(name).parts
                or not (out / name).is_file() for name in screenshot_names):
            failures.append("mandatory referenced browser screenshots missing")
    if not manifest.get("cleanup", {}).get("passed"):
        failures.append("mandatory resource cleanup did not pass")
    if not manifest.get("preservation", {}).get("unchanged"):
        failures.append("original Docker/user SQLite preservation did not pass")
    excluded = {"manifest.json", "COMPLETE.json", "FAILED_ATTEMPT.json"}
    manifest.update({"verdict": "FAIL" if failures else "PASS", "failures": failures,
                     "normalized_digest": digest(projection),
                     "artifact_hashes": {p.relative_to(out).as_posix():
                         hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted(out.rglob("*")) if p.is_file()
                         and p.name not in excluded}})
    write_json(out / "manifest.json", manifest)
    marker = {"verdict": manifest["verdict"], "normalized_digest": digest(projection),
              "manifest_sha256": hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest(),
              "finished_at": datetime.now(UTC).isoformat(), "failures": failures}
    write_json(out / ("FAILED_ATTEMPT.json" if failures else "COMPLETE.json"), marker)
    return not failures


def failed_attempt(out: Path, manifest: dict, failures: list[str], error: Exception) -> bool:
    """Retain a sanitized failure marker when the remaining storage permits it."""
    message = sanitize(f"final evidence publication: {type(error).__name__}: {error}")
    failures.append(message)
    # A completed invocation belongs to its earlier successful publication.
    # Never turn a caller's accidentally reused successful directory into a
    # failed attempt or overwrite its manifest.
    if (out / "COMPLETE.json").exists():
        return False
    manifest.update({"verdict": "FAIL", "failures": list(failures)})
    try:
        write_json(out / "manifest.json", manifest)
    except Exception as manifest_error:
        failures.append(sanitize(
            f"failure manifest unavailable: {type(manifest_error).__name__}: {manifest_error}"))
    marker = {"verdict": "FAIL", "failures": list(failures),
              "finished_at": datetime.now(UTC).isoformat()}
    try:
        write_json(out / "FAILED_ATTEMPT.json", marker)
    except Exception as marker_error:
        failures.append(sanitize(
            f"failure marker unavailable: {type(marker_error).__name__}: {marker_error}"))
    return False


def publish(out: Path, manifest: dict, failures: list[str], projection: dict) -> bool:
    """Publish once; late read/write failures cannot produce a COMPLETE marker."""
    try:
        if (out / "COMPLETE.json").exists():
            raise FileExistsError("refusing to overwrite a completed invocation")
        return _publish(out, manifest, failures, projection)
    except Exception as error:
        return failed_attempt(out, manifest, failures, error)


def finalize(out: Path, manifest: dict, failures: list[str], final: dict,
             readme: str, previous: Path | None = None) -> bool:
    """Finalize without an unhandled late browser-log, comparison or I/O error."""
    try:
        if (out / "COMPLETE.json").exists():
            raise FileExistsError("refusing to overwrite a completed invocation")
        browser_path = out / "browser-log.json"
        browser = json.loads(browser_path.read_text(encoding="utf-8")) \
            if browser_path.is_file() else {}
        projection = normalized(manifest["scenarios"], final, browser,
                                manifest.get("cleanup", {}), manifest.get("preservation", {}))
        if previous:
            try:
                comparison = compare(previous, projection)
                write_json(out / "comparison.json", comparison)
                if not comparison["equivalent"]:
                    failures.append(
                        "normalized results differ from the prior successful invocation")
            except Exception as error:
                failures.append(sanitize(f"comparison: {type(error).__name__}: {error}"))
        write_text(out / "README.md", readme)
        return publish(out, manifest, failures, projection)
    except Exception as error:
        return failed_attempt(out, manifest, failures, error)
