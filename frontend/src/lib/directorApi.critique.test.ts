/**
 * `critiqueCoverage` produces the failure its caller has to render.
 *
 * `DirectorWorkspace.recheck.test.tsx` mocks this module, so nothing there would
 * notice the message going missing on the way out of `fetch`. These cover that
 * seam — with the shapes `POST /api/director/critique` (backend/main.py:2365)
 * really sends.
 *
 * The endpoint is synchronous, so a failure here IS the outcome: there is no job
 * to fall back on and no later state to correct it. A failure that arrives
 * without its message leaves the caller nothing to show but "it failed", and an
 * unchanged warning list under that reads as "the critic found nothing new".
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import { critiqueCoverage } from "./directorApi";

/** One canned reply from POST /api/director/critique. */
function reply(status: number, body: unknown | undefined) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    headers: new Headers(),
    json: async () => {
      if (body === undefined) throw new SyntaxError("Unexpected token < in JSON");
      return body;
    },
  } as unknown as Response);
}

async function caught(run: () => Promise<unknown>): Promise<Error & { status?: number }> {
  try {
    await run();
  } catch (e) {
    return e as Error & { status?: number };
  }
  throw new Error("expected critiqueCoverage to throw, and it resolved");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a re-check that failed says why", () => {
  test("the catch-all 400 arrives in the backend's own words", async () => {
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error: "planner.critique raised: overloaded_error from Anthropic",
    }));

    const err = await caught(() => critiqueCoverage(["s001", "s002"]));

    expect(err.message).toBe("planner.critique raised: overloaded_error from Anthropic");
    expect(err.status).toBe(400);
  });

  test("`beats[] is required` is an HTTPException, so it lives in `detail`", async () => {
    // Raised, not returned, so FastAPI serialises it under `detail` and there is
    // no `error` key. Reading only `error` replaces the one sentence that says
    // what went wrong with a bare status line.
    vi.stubGlobal("fetch", reply(400, { detail: "beats[] is required" }));

    const err = await caught(() => critiqueCoverage([]));

    expect(err.message).toBe("beats[] is required");
    expect(err.status).toBe(400);
  });

  test("a non-JSON body still reports the status it failed with", async () => {
    // A gateway HTML error page used to throw a SyntaxError straight out of the
    // unguarded res.json(), reaching the caller as "Unexpected token <".
    vi.stubGlobal("fetch", reply(502, undefined));

    const err = await caught(() => critiqueCoverage(["s001"]));

    expect(err.message).toContain("502");
    expect(err.message).toContain("s001");
    expect(err.status).toBe(502);
  });
});

describe("a re-check that ran", () => {
  test("hands back the warning list the server computed", async () => {
    vi.stubGlobal("fetch", reply(200, {
      ok: true,
      warnings: [{ id: "w-1", kind: "repeated_framing", detail: "s001.02 repeats s001.01" }],
      summary: { shots: 9, paid_shots: 2, estimated_cost: 1.85 },
    }));

    const res = await critiqueCoverage(["s001", "s002"]);

    expect(res.warnings).toHaveLength(1);
    expect(res.ok).toBe(true);
  });

  test("an empty list resolves — a clean scene is a result, not a failure", async () => {
    vi.stubGlobal("fetch", reply(200, { ok: true, warnings: [] }));

    const res = await critiqueCoverage(["s001"]);

    expect(res.warnings).toEqual([]);
  });

  test("a 200 with no warning list resolves, and does not invent one", async () => {
    // It re-checked nothing. The caller has to be able to tell that apart from
    // an empty list, so this must not be defaulted to [] on the way through.
    vi.stubGlobal("fetch", reply(200, { ok: true }));

    const res = await critiqueCoverage(["s001"]);

    expect(res.warnings).toBeUndefined();
  });

  test("asks about every beat it was given, in one request", async () => {
    const fetchMock = reply(200, { ok: true, warnings: [] });
    vi.stubGlobal("fetch", fetchMock);

    await critiqueCoverage(["s001", "s002"]);

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/director/critique");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      beats: ["s001", "s002"],
    });
  });
});
