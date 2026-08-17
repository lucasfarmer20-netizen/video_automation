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
import {
  redirectSceneCoverage,
  fetchRunningPlanJobs,
  fetchBeatCoverageStates,
} from "./directorApi";

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

/**
 * Which beats already have coverage, and whether the planner will refuse them.
 *
 * The panel decides what to OFFER from this. If it under-reports "locked", the
 * survey goes back to inviting the user into a refusal it cannot satisfy —
 * which is the defect. Mocked away in the panel's own tests, so it is proved
 * here.
 */
describe("reading existing coverage for the survey's beats", () => {
  const sceneReply = (beats: unknown[]) => reply(200, { ok: true, beats });

  test("locked coverage is reported as locked, with its shot count and the server's cost", async () => {
    vi.stubGlobal("fetch", reply(200, {
      ok: true,
      beats: [{
        beat_id: "s001", beat_duration: 17.7,
        plan: { status: "locked", coverage: [1, 2, 3, 4, 5, 6, 7, 8, 9], warnings: [{}, {}] },
      }],
      summary: { beats: [{ beat_id: "s001", estimated_cost: 1.85 }] },
    }));

    expect(await fetchBeatCoverageStates(["s001"])).toEqual({
      s001: {
        status: "locked", shots: 9, locked: true,
        estimatedCost: 1.85, warnings: 2, durationSeconds: 17.7,
      },
    });
  });

  test("a cost comes from the summary and from nowhere else", () => {
    // The figure a human acts on. It may only ever be the server's own number
    // for this beat of this project — not a total, not a default, not a fixture.
    // A beat the summary does not price is reported unpriced, and the panel
    // renders that as "not priced" rather than $0.00.
    return (async () => {
      vi.stubGlobal("fetch", reply(200, {
        ok: true,
        beats: [
          { beat_id: "s001", plan: { status: "draft", coverage: [1] } },
          { beat_id: "s002", plan: { status: "draft", coverage: [1, 2] } },
        ],
        summary: { estimated_cost: 99.99, beats: [{ beat_id: "s002", estimated_cost: 0.42 }] },
      }));

      const states = await fetchBeatCoverageStates(["s001", "s002"]);
      // s001 is absent from the summary: unpriced, NOT given the total.
      expect(states.s001.estimatedCost).toBeNull();
      expect(states.s002.estimatedCost).toBe(0.42);
    })();
  });

  test("a genuine zero cost is kept, not treated as missing", () => {
    return (async () => {
      vi.stubGlobal("fetch", reply(200, {
        ok: true,
        beats: [{ beat_id: "s001", plan: { status: "draft", coverage: [1] } }],
        summary: { beats: [{ beat_id: "s001", estimated_cost: 0 }] },
      }));

      expect((await fetchBeatCoverageStates(["s001"])).s001.estimatedCost).toBe(0);
    })();
  });

  test("compiled coverage is locked too — the planner refuses both", async () => {
    // `director.plan_scene` declines "locked coverage" for either status. A
    // client that only knew about "locked" would offer PLAN SCENE on a compiled
    // beat and earn the same refusal by a different name.
    vi.stubGlobal("fetch", sceneReply([
      { beat_id: "s002", plan: { status: "compiled", coverage: [1, 2] } },
    ]));

    const states = await fetchBeatCoverageStates(["s002"]);
    expect(states.s002.locked).toBe(true);
    expect(states.s002.status).toBe("compiled");
  });

  test("draft coverage exists but is not locked", async () => {
    vi.stubGlobal("fetch", sceneReply([
      { beat_id: "s003", plan: { status: "draft", coverage: [1, 2, 3] } },
    ]));

    expect(await fetchBeatCoverageStates(["s003"])).toEqual({
      s003: {
        status: "draft", shots: 3, locked: false,
        estimatedCost: null, warnings: 0, durationSeconds: null,
      },
    });
  });

  test("a beat with no plan is absent, not present-and-empty", async () => {
    // Absent is what the panel reads as "offer PLAN SCENE". An entry with
    // shots: 0 would suppress the only action that beat has.
    vi.stubGlobal("fetch", sceneReply([
      { beat_id: "s004", plan: null },
      { beat_id: "s005", plan: { status: "locked", coverage: [1] } },
    ]));

    const states = await fetchBeatCoverageStates(["s004", "s005"]);
    expect(states.s004).toBeUndefined();
    expect(states.s005.shots).toBe(1);
  });

  test("no beats means no request at all", async () => {
    const fetchMock = sceneReply([]);
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchBeatCoverageStates([])).toEqual({});
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("every beat is asked for in one request", async () => {
    const fetchMock = sceneReply([]);
    vi.stubGlobal("fetch", fetchMock);

    await fetchBeatCoverageStates(["s001", "s002", "s003"]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("beats=s001%2Cs002%2Cs003");
  });

  test("a refused read throws rather than reporting everything unplanned", async () => {
    // Silently returning {} would put PLAN SCENE back on every locked beat.
    vi.stubGlobal("fetch", reply(500, { ok: false, error: "scene read failed" }));

    await expect(fetchBeatCoverageStates(["s001"])).rejects.toThrow("scene read failed");
  });
});

/**
 * What actually goes on the wire.
 *
 * The panel's tests assert the ARGUMENT handed to a mocked
 * `redirectSceneCoverage`. That says nothing about the body this function then
 * builds — a hardcoded `replan: true` in the request passes every one of them
 * while overriding, on every ordinary plan, the guard that protects locked
 * coverage. Asserting the argument is not asserting the request.
 */
describe("the replan flag, as the server receives it", () => {
  /** The parsed JSON body of the single fetch that was made. */
  const sentBody = (fetchMock: ReturnType<typeof vi.fn>) =>
    JSON.parse(fetchMock.mock.calls[0][1].body as string);

  test("an ordinary plan asks the server NOT to overwrite existing coverage", async () => {
    const fetchMock = reply(200, { ok: true, started: true, job: "director_plan:s001" });
    vi.stubGlobal("fetch", fetchMock);

    await redirectSceneCoverage(["s001"], "Initial scene coverage planning");

    // The whole point of the guard. `replan` must be false unless a user was
    // shown what it discards and said yes.
    expect(sentBody(fetchMock).replan).toBe(false);
  });

  test("a confirmed re-plan asks for the override, explicitly", async () => {
    const fetchMock = reply(200, { ok: true, started: true, job: "director_plan:s001" });
    vi.stubGlobal("fetch", fetchMock);

    await redirectSceneCoverage(["s001"], "Re-planning", [], undefined, undefined, true);

    expect(sentBody(fetchMock).replan).toBe(true);
  });

  test("the beats and critique flag still travel with it", async () => {
    const fetchMock = reply(200, { ok: true, started: true, job: "director_plan:s001" });
    vi.stubGlobal("fetch", fetchMock);

    await redirectSceneCoverage(["s001", "s002"], "notes");

    const body = sentBody(fetchMock);
    expect(body.beats).toEqual(["s001", "s002"]);
    expect(body.critique).toBe(true);
  });
});

/**
 * The seam CoverageSurveyPanel's tests mock away. Re-attaching after a remount
 * is only as good as this reading the registry correctly, and a panel test that
 * stubs it would not notice if it stopped.
 */
describe("which plan jobs the server has running", () => {
  test("a running plan is reported, keyed by its beat", async () => {
    vi.stubGlobal("fetch", reply(200, {
      ok: true,
      jobs: { "director_plan:s001": { status: "running", log: "Planning coverage..." } },
    }));

    expect(await fetchRunningPlanJobs()).toEqual({ s001: "director_plan:s001" });
  });

  test("a finished plan is not reported as running", async () => {
    // Re-attaching to a job that already ended would hang the panel on a
    // "still planning" line forever — the one direction of error the fix must
    // not introduce while removing the other.
    vi.stubGlobal("fetch", reply(200, {
      ok: true,
      jobs: {
        "director_plan:s001": { status: "done", log: "Process completed successfully." },
        "director_plan:s002": { status: "error", log: "planner raised" },
      },
    }));

    expect(await fetchRunningPlanJobs()).toEqual({});
  });

  test("other running jobs are not mistaken for plans", async () => {
    vi.stubGlobal("fetch", reply(200, {
      ok: true,
      jobs: {
        narration: { status: "running", log: "" },
        script_draft: { status: "running", log: "" },
        "director_compile:s001": { status: "running", log: "" },
      },
    }));

    expect(await fetchRunningPlanJobs()).toEqual({});
  });

  test("an unreadable registry is empty, not a crash", async () => {
    vi.stubGlobal("fetch", reply(500, undefined));

    expect(await fetchRunningPlanJobs()).toEqual({});
  });
});
