/**
 * Round 2: the plan generated correctly and the screen did not say so.
 *
 * Round 1 made the panel wait for the job, and 12 green tests said so while the
 * live screen still misbehaved. Every one of them asserted against state the
 * same mount had set. That is the gap these cover: the outcome has to survive
 * the panel being torn down, and it has to be visible where the user is looking.
 *
 *   Finding 1  a finished plan is stated at the button, not only by a component
 *              below the fold quietly changing which beat it shows
 *   Finding 2  the running plan survives a remount, because the job lives on the
 *              server and the React state watching it does not
 *   Finding 3  an empty survey says it is empty and why — an empty list rendered
 *              as an empty list is why a working Direct stage read as broken
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

/** A survey for a project with no narration beats in it. */
const EMPTY_SURVEY: CoverageSurvey = {
  episode_seconds: 1,
  frozen_if_nothing_covered: 0,
  frozen_pct: 0,
  recommended: [],
  scenes: [],
  beats: [],
};

function deferred<T>() {
  let settle!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    settle = res;
  });
  return { promise, settle };
}

const flush = () => act(async () => {});

beforeEach(() => {
  vi.clearAllMocks();
  mockSurvey.mockResolvedValue(SURVEY);
  mockRunning.mockResolvedValue({});
});

afterEach(cleanup);

// --- Finding 2: the running plan survives a remount --------------------------

describe("Finding 2 — a plan in flight outlives the component watching it", () => {
  test("a fresh mount re-attaches to the job the server still has running", async () => {
    // The panel is torn down mid-plan. Whatever caused it — a stage round trip,
    // a re-render that dropped this subtree, or a page reload — `planningBeats`
    // is gone and the pending waitForJob promise is orphaned. The ninety-second
    // job on the server carries on regardless.
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockRunning.mockResolvedValue({ s003: "director_plan:s003" });
    mockWait.mockReturnValue(job.promise);

    const onSelectBeats = vi.fn();
    render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
    await screen.findByTestId("plan-beat-s003");
    await flush();

    // THE DEFECT. This mount never clicked anything. Before the re-attach, a
    // fresh mount knew nothing about the running job: every button enabled, no
    // running row, and the plan would land with nobody watching for it.
    expect(screen.queryByTestId("plan-running-s003")).not.toBeNull();
    expect((screen.getByTestId("plan-beat-s003") as HTMLButtonElement).disabled).toBe(true);

    // …and it is watching the job the SERVER named, not one it invented.
    expect(mockWait).toHaveBeenCalledTimes(1);
    expect(mockWait.mock.calls[0][0]).toBe("director_plan:s003");
    // It did not start a second plan; re-attaching is not re-requesting.
    expect(mockPlan).not.toHaveBeenCalled();

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "Process completed successfully." });
    });

    // The outcome reaches the user from a mount that never issued the request.
    expect(onSelectBeats).toHaveBeenCalledTimes(1);
    expect(onSelectBeats).toHaveBeenCalledWith(["s003"]);
    expect(screen.getByTestId("plan-done-banner").textContent).toContain("s003");
  });

  test("the same plan survives an actual unmount/remount cycle", async () => {
    // The first mount starts the plan the way the user does. Then the subtree is
    // destroyed outright and rebuilt — the mechanism, whatever caused it.
    const first = deferred<{ ok: boolean; status: string; log: string }>();
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockReturnValue(first.promise);

    const onSelectBeats = vi.fn();
    const view = render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s003"));
    await flush();
    expect(screen.getByTestId("plan-running-s003")).toBeTruthy();

    view.unmount();

    // The server still has it running — nothing about the browser stopped the job.
    const second = deferred<{ ok: boolean; status: string; log: string }>();
    mockRunning.mockResolvedValue({ s003: "director_plan:s003" });
    mockWait.mockReturnValue(second.promise);

    render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
    await screen.findByTestId("plan-beat-s003");
    await flush();

    // THE DEFECT: the rebuilt panel had forgotten the plan entirely.
    expect(screen.queryByTestId("plan-running-s003")).not.toBeNull();
    expect(onSelectBeats).not.toHaveBeenCalled();

    await act(async () => {
      second.settle({ ok: true, status: "done", log: "done" });
    });
    expect(onSelectBeats).toHaveBeenCalledTimes(1);
  });

  test("nothing running means nothing is claimed to be running", async () => {
    // The other direction, or "re-attach" would just be a spinner that never ends.
    mockRunning.mockResolvedValue({});

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);
    await screen.findByTestId("plan-beat-s003");
    await flush();

    expect(screen.queryByTestId("plan-running-s003")).toBeNull();
    expect((screen.getByTestId("plan-beat-s003") as HTMLButtonElement).disabled).toBe(false);
    expect(mockWait).not.toHaveBeenCalled();
  });

  test("an unreadable job registry does not take the survey down with it", async () => {
    mockRunning.mockRejectedValue(new Error("status endpoint unreachable"));

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);

    expect(await screen.findByTestId("plan-beat-s003")).toBeTruthy();
    await flush();
    expect(screen.queryByTestId("plan-running-s003")).toBeNull();
  });
});

// --- Finding 1: the completion is visible where the button is ----------------

describe("Finding 1 — a finished plan says so at the button that started it", () => {
  test("completion is stated in the panel, not only by an off-screen scene change", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockReturnValue(job.promise);

    const onSelectBeats = vi.fn();
    render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s003"));
    await flush();

    // Nothing to celebrate yet.
    expect(screen.queryByTestId("plan-done-banner")).toBeNull();

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });

    // THE DEFECT. `onSelectBeats` fired and the only visible consequence was a
    // component below the fold changing which beat it showed — which the user
    // reported as "we never made it to the workspace".
    const banner = screen.getByTestId("plan-done-banner");
    expect(banner.textContent).toContain("s003");
    expect(banner.textContent).toContain("planned");
    // It names where the coverage went, so the next move is obvious.
    expect(banner.textContent).toContain("Director");
  });

  test("a failed plan is never announced as a finished one", async () => {
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockResolvedValue({ ok: false, status: "error", log: "planner raised" });

    const onSelectBeats = vi.fn();
    render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s003"));
    await flush();

    expect(screen.queryByTestId("plan-done-banner")).toBeNull();
    expect(onSelectBeats).not.toHaveBeenCalled();
    expect(screen.getByTestId("plan-problem-s003").getAttribute("data-kind")).toBe("failed");
  });

  test("starting a new plan withdraws the previous completion notice", async () => {
    // A banner still saying "s003 is planned" over a fresh run for another beat
    // is the same false-success shape in slower motion.
    const first = deferred<{ ok: boolean; status: string; log: string }>();
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s003" });
    mockWait.mockReturnValue(first.promise);

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s003"));
    await flush();
    await act(async () => {
      first.settle({ ok: true, status: "done", log: "done" });
    });
    expect(screen.getByTestId("plan-done-banner")).toBeTruthy();

    const second = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(second.promise);
    fireEvent.click(screen.getByTestId("plan-beat-s003"));
    await flush();

    expect(screen.queryByTestId("plan-done-banner")).toBeNull();
  });
});

// --- Finding 3: an empty survey explains itself ------------------------------

describe("Finding 3 — a survey with no beats says so rather than rendering blank", () => {
  test("no beats is stated as no beats, not as an empty panel", async () => {
    // GET /api/director/survey answers ok:true with beats:[] for a project that
    // has no narration beats — confirmed against backend/planner.py. The old
    // panel drew its header and nothing else: no rows, no error, no console
    // error, and no way to tell "nothing to cover" from "this screen failed".
    mockSurvey.mockResolvedValue(EMPTY_SURVEY);

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);

    const empty = await screen.findByTestId("survey-empty");
    expect(empty.textContent).toContain("No beats to survey");
    // It distinguishes the two readings explicitly.
    expect(empty.textContent).toContain("no narration beats");
    expect(empty.textContent).toContain("not broken");
    // And it is genuinely the empty case, not a row that failed to render.
    expect(screen.queryByTestId("plan-beat-s003")).toBeNull();
  });

  test("a survey with beats does not claim to be empty", async () => {
    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);

    expect(await screen.findByTestId("plan-beat-s003")).toBeTruthy();
    expect(screen.queryByTestId("survey-empty")).toBeNull();
  });

  test("a survey that could not be read is still an error, not an empty list", async () => {
    // The three states must stay distinct: failed, empty, populated.
    mockSurvey.mockRejectedValue(new Error("Survey endpoint returned 503"));

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);

    expect(await screen.findByText(/503/)).toBeTruthy();
    expect(screen.queryByTestId("survey-empty")).toBeNull();
  });
});
