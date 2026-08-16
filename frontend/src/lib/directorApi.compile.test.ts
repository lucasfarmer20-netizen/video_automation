/**
 * `compileCoverage` hands its caller everything the server used to refuse it.
 *
 * `POST /api/director/compile/{beat}` answers five distinct refusals. Each one
 * carries a sentence written to tell the human what to do next — lock the plan,
 * review a drifted approval, re-approve after a script edit, decide the critic
 * findings, or wait for the compile already running. Thrown as a bare Error they
 * collapse into one "compile failed", which is the single most useful thing the
 * backend produces, discarded.
 *
 * DirectorWorkspace's own tests mock this module, so nothing there would notice
 * the message or the discriminator going missing. These cover that seam: the
 * values the component branches on are actually produced here, from the shapes
 * `backend/main.py:3079` really sends.
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import { compileCoverage } from "./directorApi";
import type { CompileRefusal } from "./directorApi";

/** One canned reply from POST /api/director/compile/{beat}. */
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

/** Throw-and-catch, so the caught value can be inspected rather than matched. */
async function caught(run: () => Promise<unknown>): Promise<CompileRefusal> {
  try {
    await run();
  } catch (e) {
    return e as CompileRefusal;
  }
  throw new Error("expected compileCoverage to throw, and it resolved");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the refusals arrive distinguishable, in the server's own words", () => {
  test("a draft plan says lock it first, and says it was never approved", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error:
        "plan for s001 is a draft; lock it first - approval is what allocates " +
        "the render budget.",
      approval_drifted: false,
      plan_signature: "ab12cd34ef56",
    }));

    const err = await caught(() => compileCoverage("s001"));

    // Verbatim. "Compile failed" would be true and useless.
    expect(err.message).toBe(
      "plan for s001 is a draft; lock it first - approval is what allocates " +
        "the render budget."
    );
    // `false` is a value, not an absence — it is what tells this apart from
    // drift, and `if (data.approval_drifted)` would have dropped it silently.
    expect(err.approvalDrifted).toBe(false);
    expect(err.status).toBe(409);
  });

  test("approval drift is not the same refusal as a plain draft", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error:
        "plan for s001 changed after it was approved, so the approval no longer " +
        "covers it; review and lock it again.",
      approval_drifted: true,
      plan_signature: "ab12cd34ef56",
    }));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toBe(
      "plan for s001 changed after it was approved, so the approval no longer " +
        "covers it; review and lock it again."
    );
    expect(err.approvalDrifted).toBe(true);
    // "Lock it first" is misleading advice for a plan that WAS locked.
    expect(err.message).not.toContain("lock it first");
  });

  test("a script line that moved under the plan carries its staleness", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error:
        "the narration for s001 was rewritten after this plan was approved. " +
        "Review and lock it again.",
      stale: { reason: "line_rewritten", beat_id: "s001" },
    }));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toContain("the narration for s001 was rewritten");
    expect(err.stale).toEqual({ reason: "line_rewritten", beat_id: "s001" });
    expect(err.approvalDrifted).toBeUndefined();
  });

  test("undecided critic findings come back with the findings", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error:
        "s001 has 2 critic warning(s) with no recorded decision; resolve or " +
        "accept them first.",
      warnings: [
        { kind: "identity_risk", detail: "s001.03 shows the face at 3.1s" },
        { kind: "repeated_framing", detail: "s001.05 repeats s001.02" },
      ],
    }));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toBe(
      "s001 has 2 critic warning(s) with no recorded decision; resolve or " +
        "accept them first."
    );
    expect(err.warnings).toHaveLength(2);
    expect(err.status).toBe(409);
  });

  test("a compile already running is a 409 with no discriminator at all", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error: "a compile for s001 is already running",
    }));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toBe("a compile for s001 is already running");
    // Nothing to key on but the status — which is why the status must survive.
    expect(err.status).toBe(409);
    expect(err.approvalDrifted).toBeUndefined();
    expect(err.stale).toBeUndefined();
    expect(err.warnings).toBeUndefined();
  });

  test("the 404 is raised as an HTTPException, so its message lives in `detail`", async () => {
    // The route re-raises HTTPException, so FastAPI serialises it under
    // `detail` and there is no `error` key. Reading only `error` would replace
    // the one sentence that says WHY with a generic status line.
    vi.stubGlobal("fetch", reply(404, { detail: "no director plan for s001" }));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toBe("no director plan for s001");
    expect(err.status).toBe(404);
  });

  test("the catch-all 400 still reports what the backend said broke", async () => {
    vi.stubGlobal("fetch", reply(400, {
      ok: false,
      error: "[Errno 2] No such file or directory: 'render/s001'",
    }));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toBe("[Errno 2] No such file or directory: 'render/s001'");
    expect(err.status).toBe(400);
  });

  test("a non-JSON body still reports the status it failed with", async () => {
    vi.stubGlobal("fetch", reply(502, undefined));

    const err = await caught(() => compileCoverage("s001"));

    expect(err.message).toContain("502");
    expect(err.status).toBe(502);
  });
});

describe("a started compile", () => {
  test("comes back named, which is what makes waiting possible", async () => {
    vi.stubGlobal("fetch", reply(200, {
      ok: true, started: true, beat_id: "s001", job: "director:s001", shots: 9,
    }));

    const res = await compileCoverage("s001");

    expect(res.job).toBe("director:s001");
    expect(res.started).toBe(true);
    expect(res.shots).toBe(9);
  });

  test("is a POST to the compile route for that beat, with no force flag", async () => {
    // There is no `force` parameter and there must not be one: the backend
    // removed it because force=true skipped the draft check and could send an
    // unapproved plan into paid generation.
    const fetchMock = reply(200, { ok: true, started: true, job: "director:s001" });
    vi.stubGlobal("fetch", fetchMock);

    await compileCoverage("s001");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/director/compile/s001");
    expect(String(url)).not.toContain("force");
    expect((init as RequestInit).method).toBe("POST");
  });
});
