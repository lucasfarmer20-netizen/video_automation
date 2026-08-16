/**
 * `redirectSceneCoverage` hands its caller enough to tell a refusal from a failure.
 *
 * POST /api/director/plan answers 409 when `start_job` finds a plan for that beat
 * already running. That is not a failure and not the user's mistake — but thrown
 * as a bare Error it is indistinguishable from a 500, which is how "another plan
 * is already running" came to be presented as "your plan is ready".
 *
 * CoverageSurveyPanel's own tests mock this module, so nothing there would notice
 * the status going missing. These cover that seam: the value the consumer branches
 * on is actually produced here.
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import { redirectSceneCoverage } from "./directorApi";

/** One canned reply from POST /api/director/plan. */
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
async function caught(
  run: () => Promise<unknown>
): Promise<Error & { status?: number }> {
  try {
    await run();
  } catch (e) {
    return e as Error & { status?: number };
  }
  throw new Error("expected redirectSceneCoverage to throw, and it resolved");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("POST /api/director/plan, as its callers see it", () => {
  test("a 409 arrives carrying the status, in the server's own words", async () => {
    vi.stubGlobal("fetch", reply(409, {
      ok: false,
      error: "a plan for s003 is already running",
    }));

    const err = await caught(() => redirectSceneCoverage(["s003"], "Initial scene coverage planning"));

    // The branch the panel takes to call this "busy" rather than "failed".
    expect(err.status).toBe(409);
    expect(err.message).toBe("a plan for s003 is already running");
  });

  test("a real failure carries its own status, so the two do not merge", async () => {
    vi.stubGlobal("fetch", reply(500, { ok: false, error: "planner exploded" }));

    const err = await caught(() => redirectSceneCoverage(["s003"], "x"));

    expect(err.status).toBe(500);
    expect(err.message).toBe("planner exploded");
  });

  test("a non-JSON body still reports the status it failed with", async () => {
    // A gateway HTML error page used to throw a SyntaxError out of res.json(),
    // which reached the caller as "Unexpected token <" and no status at all.
    vi.stubGlobal("fetch", reply(502, undefined));

    const err = await caught(() => redirectSceneCoverage(["s003"], "x"));

    expect(err.status).toBe(502);
    expect(err.message).toContain("502");
  });

  test("a started job comes back named, which is what makes waiting possible", async () => {
    vi.stubGlobal("fetch", reply(200, {
      ok: true, started: true, job: "director_plan:s003", beats: ["s003"],
    }));

    const res = await redirectSceneCoverage(["s003"], "Initial scene coverage planning");

    expect(res.job).toBe("director_plan:s003");
    expect(res.started).toBe(true);
  });
});
