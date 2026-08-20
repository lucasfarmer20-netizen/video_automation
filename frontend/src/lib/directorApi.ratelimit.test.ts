/**
 * Every director fetch helper, against the reply that started this.
 *
 * The defect reported was one call site — `decideDirectorWarning` did a bare
 * `res.json()` over a `429 Rate exceeded.` body and the SyntaxError reached the
 * screen as "404 / Unplanned Beat Coverage". But the assumption behind it was
 * shared by the whole module: parse first, look at the status afterwards. Every
 * helper here made some version of it, so fixing the one that was reported
 * would have left the class open — the same shape as the seven handlers that
 * each swallowed a refusal one at a time.
 *
 * This file is therefore a table, not a case. Adding a helper to `directorApi`
 * without adding it here leaves a hole; adding it here without handling the
 * status leaves a failing test.
 *
 * Two things are asserted of every one of them, and the FIRST is the one that
 * matters: a shed request must make no claim about the human's work.
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  compileCoverage,
  critiqueCoverage,
  decideDirectorWarning,
  fetchBeatCoverageStates,
  fetchCoveragePlan,
  fetchCoverageSurvey,
  fetchDirectorProfiles,
  fetchRunningPlanJobs,
  performShotAction,
  redirectSceneCoverage,
  setCoverageStatus,
  updateShot,
  waitForJob,
} from "./directorApi";

/** Phrases that would tell a human their coverage is gone. */
const CLAIMS_THE_WORK_IS_MISSING = [/no plan/i, /not found/i, /unplanned/i, /\b404\b/];
/** The parser's complaint, which is never a fact about a beat. */
const PARSER_COMPLAINTS = [/unexpected token/i, /is not valid json/i];

/** Exactly what Google's front end sends when it sheds a request. */
const shed = () =>
  new Response("Rate exceeded.", {
    status: 429,
    headers: { "content-type": "text/html; charset=UTF-8" },
  });

/** THE DEFECT-PROVING ASSERTION, and it goes first. */
function saysNothingAboutTheWork(message: string, who: string) {
  CLAIMS_THE_WORK_IS_MISSING.forEach((claim) =>
    expect(message, `${who} claimed ${claim} because a request was shed`).not.toMatch(claim)
  );
  PARSER_COMPLAINTS.forEach((complaint) =>
    expect(message, `${who} let the JSON parser speak for the server`).not.toMatch(complaint)
  );
}

type Shed = Error & { status?: number; rateLimited?: boolean; transport?: boolean };

async function caught(run: () => Promise<unknown>, who: string): Promise<Shed> {
  try {
    await run();
  } catch (e) {
    return e as Shed;
  }
  throw new Error(`${who} resolved over a 429 — a shed request is not a result`);
}

/**
 * Reads are re-issued; writes are not.
 *
 * A 429 from Google's front end almost certainly means the request never
 * reached the app. "Almost certainly" is enough to re-issue a read and nowhere
 * near enough to re-issue `POST /api/director/compile`, which buys the beat's
 * paid shots. The split is the point of this column.
 */
interface Case {
  who: string;
  run: () => Promise<unknown>;
  /** How many times `fetch` must be called for one shed call. */
  calls: number;
}

const READ_CALLS = 3; // one attempt plus DEFAULT_RETRY's two re-issues
const WRITE_CALLS = 1;

const READS: Case[] = [
  { who: "fetchDirectorProfiles", run: () => fetchDirectorProfiles(), calls: READ_CALLS },
  { who: "fetchCoveragePlan", run: () => fetchCoveragePlan("s006"), calls: READ_CALLS },
  { who: "fetchBeatCoverageStates", run: () => fetchBeatCoverageStates(["s006"]), calls: READ_CALLS },
  { who: "fetchCoverageSurvey", run: () => fetchCoverageSurvey(), calls: READ_CALLS },
];

const WRITES: Case[] = [
  { who: "redirectSceneCoverage", run: () => redirectSceneCoverage(["s006"], "x"), calls: WRITE_CALLS },
  { who: "compileCoverage", run: () => compileCoverage("s006", "sig"), calls: WRITE_CALLS },
  { who: "critiqueCoverage", run: () => critiqueCoverage(["s006"]), calls: WRITE_CALLS },
  { who: "updateShot", run: () => updateShot("s006.01", { angle: "low" }), calls: WRITE_CALLS },
  {
    who: "performShotAction",
    run: () => performShotAction("s006.01", "alternate_angle", undefined),
    calls: WRITE_CALLS,
  },
  {
    who: "decideDirectorWarning",
    run: () => decideDirectorWarning("s006", "w_3f2a", "resolved"),
    calls: WRITE_CALLS,
  },
  { who: "setCoverageStatus (lock scene)", run: () => setCoverageStatus(["s006"], true), calls: WRITE_CALLS },
  { who: "setCoverageStatus (unlock scene)", run: () => setCoverageStatus(["s006"], false), calls: WRITE_CALLS },
  { who: "setCoverageStatus (one beat)", run: () => setCoverageStatus("s006", true), calls: WRITE_CALLS },
];

afterEach(() => {
  vi.unstubAllGlobals();
});

describe.each([...READS, ...WRITES])("$who, when the server sheds the request", ({ who, run }) => {
  test("says the request was refused, never that the work is missing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(shed()));

    const err = await caught(run, who);

    saysNothingAboutTheWork(err.message, who);

    expect(err.message, `${who} did not say it was rate-limited`).toMatch(/rate-limited/i);
    expect(err.status, `${who} lost the status`).toBe(429);
    expect(err.rateLimited, `${who} lost the fact a UI branches on`).toBe(true);
  });
});

describe.each(READS)("$who is idempotent, so a shed read is re-issued", ({ who, run, calls }) => {
  test(`issues ${READ_CALLS} requests before giving up`, async () => {
    const fetchMock = vi.fn().mockResolvedValue(shed());
    vi.stubGlobal("fetch", fetchMock);

    await caught(run, who);

    expect(fetchMock).toHaveBeenCalledTimes(calls);
  }, 15000);
});

describe.each(WRITES)("$who changes state, so a shed write is NOT re-issued", ({ who, run, calls }) => {
  test("issues exactly one request", async () => {
    const fetchMock = vi.fn().mockResolvedValue(shed());
    vi.stubGlobal("fetch", fetchMock);

    await caught(run, who);

    expect(
      fetchMock,
      `${who} re-issued a shed write; a compile that runs twice buys twice`
    ).toHaveBeenCalledTimes(calls);
  });
});

describe("the two helpers that answer rather than throw", () => {
  test("fetchRunningPlanJobs re-reads, and reports no plans rather than inventing one", async () => {
    // Deliberately the exception: its caller re-attaches to a running job on
    // mount and treats a failure as "nothing to re-attach to", which is a
    // truthful thing to conclude from a read that did not happen. What it must
    // not do is skip the retry, because a merely SHED read is the one case
    // where re-asking would have found the job.
    const fetchMock = vi.fn().mockResolvedValue(shed());
    vi.stubGlobal("fetch", fetchMock);

    expect(await fetchRunningPlanJobs()).toEqual({});
    expect(fetchMock).toHaveBeenCalledTimes(READ_CALLS);
  }, 15000);

  test("waitForJob keeps waiting rather than calling a running job finished", async () => {
    // The dangerous wrong answer for this one is `{ok: true}`: the caller opens
    // the workspace on a plan that has not landed. A shed poll must teach it
    // nothing at all.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(shed()));

    const out = await waitForJob("director_plan:s006", { timeoutMs: 30, intervalMs: 5 });

    expect(out.ok, "a shed poll must never read as a finished job").toBe(false);
    expect(out.status).toBe("timeout");
  }, 15000);
});
