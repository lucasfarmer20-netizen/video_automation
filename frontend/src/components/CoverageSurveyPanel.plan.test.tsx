/**
 * "Plan Scene" tells the truth about the job it started (§11.4 — no false success).
 *
 * POST /api/director/plan answers the instant `start_job` spawns a thread. The
 * plan itself is a plan -> critic -> re-plan cycle, measured at ~90s:
 *
 *   21:19:35  POST /api/director/plan 200 OK -> [DIRECTOR_PLAN:S001] Planning coverage...
 *   21:21:02  [DIRECTOR_PLAN:S001] Process completed successfully.
 *
 * The panel used to call `onSelectBeats` about one second into that, from BOTH
 * the success and the failure branch. Three outcomes — the plan finished, the
 * plan failed, the server refused with 409 because another plan was already
 * running — collapsed into one: the workspace opens and shows "404 / Unplanned".
 *
 * So each test below covers exactly one of those outcomes, and in each one the
 * FIRST assertion is the one the original defect fails: `onSelectBeats` was not
 * called. A cheaper assertion placed ahead of it (the message text, say) would
 * fail first under a regression and hide which of the three had collapsed.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/directorApi", () => ({
  fetchCoverageSurvey: vi.fn(),
  redirectSceneCoverage: vi.fn(),
  waitForJob: vi.fn(),
  fetchRunningPlanJobs: vi.fn(),
}));

import CoverageSurveyPanel from "./CoverageSurveyPanel";
import {
  fetchCoverageSurvey,
  redirectSceneCoverage,
  waitForJob,
  fetchRunningPlanJobs,
} from "../lib/directorApi";
import type { CoverageSurvey } from "../types/director";

const mockSurvey = vi.mocked(fetchCoverageSurvey);
const mockPlan = vi.mocked(redirectSceneCoverage);
const mockWait = vi.mocked(waitForJob);
const mockRunning = vi.mocked(fetchRunningPlanJobs);

const SURVEY: CoverageSurvey = {
  episode_seconds: 600,
  frozen_if_nothing_covered: 480,
  frozen_pct: 80,
  recommended: ["s003"],
  scenes: [["s003"]],
  beats: [
    {
      beat_id: "s003",
      seconds: 31.4,
      motion_type: "parallax",
      frozen_if_left: 31.4,
      recommend: 3,
      reason: "31.4s on one plate",
    },
  ],
};

/** A promise whose settling this test controls — i.e. a job still running. */
function deferred<T>() {
  let settle!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    settle = res;
  });
  return { promise, settle };
}

/** An error shaped the way `redirectSceneCoverage` throws one. */
function httpError(status: number, message: string): Error & { status: number } {
  const err = new Error(message) as Error & { status: number };
  err.status = status;
  return err;
}

/** Render, wait for the survey, and hand back the click target. */
async function mountPanel() {
  const onSelectBeats = vi.fn();
  render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
  const button = await screen.findByTestId("plan-beat-s003");
  return { onSelectBeats, button };
}

/** Let queued microtasks (the awaits inside the click handler) run. */
const flush = () => act(async () => {});

beforeEach(() => {
  vi.clearAllMocks();
  mockSurvey.mockResolvedValue(SURVEY);
  // Nothing already running: these tests are about starting one. The re-attach
  // path has its own file.
  mockRunning.mockResolvedValue({});
});

afterEach(cleanup);

// --- path 1: the job succeeds ------------------------------------------------

describe("the job succeeds", () => {
  test("the workspace opens when the plan is written, not when the POST returns", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockReturnValue(job.promise);

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    // THE DEFECT. The POST has returned {started: true} and the job is still
    // running. Opening the workspace here is what showed the user "404 /
    // Unplanned" for a plan that arrived ninety seconds later.
    expect(onSelectBeats).not.toHaveBeenCalled();

    // …and the wait is on the job the server named, not a guess at a duration.
    expect(mockWait).toHaveBeenCalledTimes(1);
    expect(mockWait.mock.calls[0][0]).toBe("director_plan:s003");

    // While it runs, the user is told it is running.
    expect(screen.getByTestId("plan-running-s003").textContent)
      .toContain("Planning s003");
    expect(screen.queryByTestId("plan-problem-s003")).toBeNull();

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "Process completed successfully." });
    });

    expect(onSelectBeats).toHaveBeenCalledTimes(1);
    expect(onSelectBeats).toHaveBeenCalledWith(["s003"]);
    expect(screen.queryByTestId("plan-running-s003")).toBeNull();
    expect(screen.queryByTestId("plan-problem-s003")).toBeNull();
  });

  test("the running job's own log is shown, not a substitute for it", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockImplementation((_key, opts) => {
      opts?.onLog?.("[DIRECTOR_PLAN:S003] Critiquing 7 shots across 1 beat(s)");
      return job.promise;
    });

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    expect(onSelectBeats).not.toHaveBeenCalled();
    expect(screen.getByTestId("plan-running-s003").textContent)
      .toContain("Critiquing 7 shots");

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
    expect(onSelectBeats).toHaveBeenCalledTimes(1);
  });
});

// --- path 2: the job fails ---------------------------------------------------

describe("the job fails", () => {
  test("a plan that ran and failed does not open a workspace", async () => {
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockResolvedValue({
      ok: false,
      status: "error",
      log: "planner.plan_scene raised: overloaded_error from Anthropic",
    });

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    // THE DEFECT. The old catch/success branches were identical, so a job that
    // produced no plan opened the workspace exactly as a successful one did.
    expect(onSelectBeats).not.toHaveBeenCalled();

    const problem = screen.getByTestId("plan-problem-s003");
    expect(problem.getAttribute("data-kind")).toBe("failed");
    // Named in terms of the real cause, and it says no plan was written — the
    // thing the user needs in order to know whether to re-click.
    expect(problem.textContent).toContain("no coverage plan was written");
    expect(problem.textContent).toContain("overloaded_error from Anthropic");
    expect(screen.queryByTestId("plan-running-s003")).toBeNull();
  });

  test("a job still running at the timeout is reported as unfinished, not as failed work", async () => {
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockResolvedValue({ ok: false, status: "timeout", log: "" });

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    expect(onSelectBeats).not.toHaveBeenCalled();
    expect(screen.getByTestId("plan-problem-s003").textContent)
      .toContain("still running");
  });

  test("started but unnamed: the panel cannot know it finished, so it does not say so", async () => {
    // {ok, started} with no job name leaves nothing to watch. The direction of
    // error is fixed: unfinished is never presented as finished.
    mockPlan.mockResolvedValue({ ok: true, started: true });

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    expect(onSelectBeats).not.toHaveBeenCalled();
    expect(mockWait).not.toHaveBeenCalled();
    expect(screen.getByTestId("plan-problem-s003").getAttribute("data-kind")).toBe("failed");
  });
});

// --- path 3: the POST is refused with 409 ------------------------------------

describe("the POST returns 409", () => {
  test("a refusal is not a plan, and is not an error the user caused", async () => {
    mockPlan.mockRejectedValue(httpError(409, "a plan for s003 is already running"));

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    // THE DEFECT. 409 landed in a catch whose only statement was
    // onSelectBeats(beatList) — "another plan is already running" became
    // indistinguishable from "your plan is ready".
    expect(onSelectBeats).not.toHaveBeenCalled();
    expect(mockWait).not.toHaveBeenCalled();

    const problem = screen.getByTestId("plan-problem-s003");
    // Distinguishable from a failure — same UI element, different claim.
    expect(problem.getAttribute("data-kind")).toBe("busy");
    expect(problem.textContent).toContain("a plan for s003 is already running");
    expect(problem.textContent).toContain("nothing was lost");
  });

  test("a real POST failure is told apart from a 409", async () => {
    // Both arrive down the same catch. If they were not distinguished, the 409
    // test above would pass on a branch that calls everything a failure.
    mockPlan.mockRejectedValue(httpError(500, "Planning failed with status 500"));

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    expect(onSelectBeats).not.toHaveBeenCalled();
    const problem = screen.getByTestId("plan-problem-s003");
    expect(problem.getAttribute("data-kind")).toBe("failed");
    expect(problem.textContent).toContain("Planning failed with status 500");
  });
});

// --- the button, while a job is running --------------------------------------

describe("re-clicking is what earned the 409 in the first place", () => {
  test("the button is held while its job runs, and a second click starts nothing", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockReturnValue(job.promise);

    const { onSelectBeats, button } = await mountPanel();
    fireEvent.click(button);
    await flush();

    expect(onSelectBeats).not.toHaveBeenCalled();
    expect((button as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(button);
    await flush();
    expect(mockPlan).toHaveBeenCalledTimes(1);

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
    expect((screen.getByTestId("plan-beat-s003") as HTMLButtonElement).disabled).toBe(false);
  });
});
