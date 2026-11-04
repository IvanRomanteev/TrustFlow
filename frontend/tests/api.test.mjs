import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

// Execute the actual TypeScript client without an additional test framework.
const source = await readFile(new URL("../lib/api.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
});
const { api } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("FastAPI errors remain useful without exposing input or context", async (t) => {
  const cases = [
    ["empty title", { detail: [{ loc: ["body", "title"], msg: "String should have at least 1 character", input: "private-value" }] }, "title: String should have at least 1 character"],
    ["multiple fields", { detail: [{ loc: ["body", "items", 0, "title"], msg: "Field required", ctx: { secret: "private-value" } }, { loc: ["body", "description"], msg: "Too long" }] }, "items.0.title: Field required; description: Too long"],
    ["string", { detail: "Invalid approval token" }, "Invalid approval token"],
    ["mixed array", { detail: [null, 4, { msg: {} }, { msg: "Invalid request" }] }, "Invalid request"],
    ["invalid location", { detail: [{ loc: ["body", {}, "title"], msg: "Invalid" }] }, "title: Invalid"],
    ...[null, [], {}, { detail: {} }, { detail: [] }, { detail: "" }, { detail: [{ msg: 3 }] }]
      .map((body, i) => [`fallback ${i}`, body, "HTTP 422"]),
  ];
  for (const [name, body, expected] of cases) {
    await t.test(name, async (t) => {
      t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify(body), { status: 422 }));
      await assert.rejects(api.create("", "", false), { message: expected });
    });
  }
  await t.test("non-JSON server error", async (t) => {
    t.mock.method(globalThis, "fetch", async () => new Response("<html>error</html>", { status: 502 }));
    await assert.rejects(api.health(), { message: "HTTP 502" });
  });
  await t.test("success is unchanged", async (t) => {
    t.mock.method(globalThis, "fetch", async () => new Response('{"run_id":"synthetic"}', { status: 201 }));
    assert.deepEqual(await api.create("Check balance", "", false), { run_id: "synthetic" });
  });
});
