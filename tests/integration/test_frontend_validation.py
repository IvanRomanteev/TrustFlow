"""Opt-in production Chromium validation regression; isolated Docker resources."""

import os
import uuid
from pathlib import Path

import pytest
from scripts.m52.infrastructure import DemoStack

pytestmark = pytest.mark.skipif(
    os.environ.get("TRUSTFLOW_FRONTEND_TESTS") != "1",
    reason="opt-in production UI: set TRUSTFLOW_FRONTEND_TESTS=1",
)


def test_empty_title_and_fastapi_validation_array(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    stack = DemoStack(Path(__file__).resolve().parents[2], tmp_path, uuid.uuid4().hex)
    before = stack.snapshot_preservation()
    try:
        stack.up()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            posts = []
            page.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
            page.goto(stack.frontend_url)
            expect(page.locator("header")).to_contain_text("backend ok")
            create = page.get_by_role("button", name="Create workflow", exact=True)
            notice = page.locator("header .notice")
            for empty in ("", "   "):
                page.locator("#title").fill(empty)
                create.click()
                expect(notice).to_have_text("Create workflow failed: Title is required.")
                assert posts == []

            page.locator("#title").fill("T" * 201)
            page.locator("#desc").fill("D" * 2001)
            with page.expect_response(lambda r: r.request.method == "POST") as response:
                create.click()
            assert response.value.status == 422
            details = response.value.json()["detail"]
            assert {item["loc"][-1] for item in details} == {"title", "description"}
            for item in details:
                expect(notice).to_contain_text(f"{item['loc'][-1]}: {item['msg']}")
            expect(notice).not_to_contain_text("[object Object]")
            page.screenshot(path=str(tmp_path / "validation-array.png"))

            page.locator("#title").fill("Check balance")
            page.locator("#desc").fill("")
            create.click()
            expect(page.locator('section[aria-label="Run timeline"]')).to_contain_text("COMPLETED")
            page.reload()
            expect(page.locator('section[aria-label="Run timeline"]')).to_contain_text("COMPLETED")
            browser.close()
    finally:
        failures = stack.teardown()
        stack.assert_preserved(before)
        assert not failures, failures
