/**
 * The storage-gate screen, reported rather than asserted.
 *
 * This is the probe for `scratch/mutate_slice8_storage_gate_ui.py`. It exists
 * as a REAL, tracked spec on purpose. The harness used to write a spec into
 * `src/app/` for the duration of a run and unlink it in a `finally` — which a
 * Ctrl-C or a dying subprocess does not reach, leaving an untracked test file
 * in the source tree where the next `git add -A` sweeps it into a commit. That
 * is exactly how `prompt_ledger.jsonl` got committed, and it was the third
 * instance of one class: tooling writing into the workspace where real files
 * live.
 *
 * Relocating the write to a temp directory does not work — a vitest config
 * outside the project cannot resolve `vitest/config` or `@vitejs/plugin-react`,
 * because Node resolves from the config's own location (measured:
 * ERR_MODULE_NOT_FOUND). So the write is removed instead of moved. Nothing is
 * generated, nothing needs cleaning up, and this file is reviewed like any
 * other test.
 *
 * Two modes, one file:
 *
 *   plain `npm test`      — asserts, like any other spec.
 *   STORAGE_PROBE=1       — additionally prints PROBE_UI_* lines for the
 *                           harness to read. It still asserts; a probe that
 *                           stopped checking would go green under the very
 *                           mutations it is meant to describe.
 */
import React from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import Page from "./page";

const GATE_MESSAGE = "PROBE-GATE-SENTENCE";
const PROBING = process.env.STORAGE_PROBE === "1";

const reply = (status: number, body: unknown) => ({
  ok: status < 400,
  status,
  headers: new Headers({ "X-Project-Id": "leshy" }),
  json: async () => body,
  text: async () => JSON.stringify(body),
}) as unknown as Response;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo) => {
    const url = String(input).replace("http://localhost:5000", "");
    if (url.startsWith("/api/project/active")) {
      return reply(503, {
        detail: { error: GATE_MESSAGE, storage_gate: "unavailable", project_id: "leshy" },
      });
    }
    if (url.startsWith("/api/projects")) return reply(200, { ok: true, projects: [] });
    if (url.startsWith("/api/assemble/status")) return reply(200, { ok: true, jobs: {} });
    return reply(200, { ok: true });
  }));
  vi.stubGlobal("alert", vi.fn());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Emit only under STORAGE_PROBE=1, so an ordinary run stays quiet. */
const say = (line: string) => {
  if (PROBING) console.log(line);
};

test("what the studio shows when the durable store cannot answer", async () => {
  render(<Page />);
  await waitFor(() => {
    if (screen.queryByText(/Loading Workspace/i)) throw new Error("still loading");
  });

  const create = screen.queryByRole("button", { name: /initialize project workspace/i });
  say("PROBE_UI_CREATE_BUTTON=" + (create ? "shown" : "absent"));

  const gate = screen.queryByTestId("storage-gate-block");
  const noproj = screen.queryByText(/No Active Project Loaded/i);
  say("PROBE_UI_SCREEN=" + (gate ? "storage-gate" : noproj ? "no-active-project" : "other"));

  const body = document.body.textContent || "";
  say("PROBE_UI_SERVER_SENTENCE=" + (body.includes(GATE_MESSAGE) ? "shown" : "absent"));
  say("PROBE_UI_INTACT_LINE="
    + (/Nothing has been lost and nothing has been changed/i.test(body) ? "shown" : "absent"));
  say("PROBE_UI_RETRY="
    + (screen.queryByRole("button", { name: /try again/i }) ? "shown" : "absent"));

  // Asserted in both modes. The create button is the one that matters: under
  // the regression this screen is "No Active Project Loaded", whose call to
  // action is to seed a fresh manifest over a film that is merely unreachable.
  expect(create).toBeNull();
  expect(gate).toBeTruthy();
});
