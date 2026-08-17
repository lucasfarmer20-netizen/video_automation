/**
 * PLAN SCENE on a beat that already has coverage.
 *
 * The human clicked PLAN SCENE on s001, which they had locked, and got a red
 * "Pipeline Execution Warning / Failure Log" carrying a raw Python message:
 *
 *   ValueError: every requested beat already has locked coverage (s001).
 *               Pass replan=true to plan over it.
 *
 * The backend is right. Locked coverage is work that was reviewed, had its
 * warnings resolved, and was locked on purpose; refusing to overwrite it is the
 * guard doing its job, and `replan=true` is the deliberate override. Everything
 * wrong here is presentation: the survey offered PLAN SCENE on a locked beat as
 * though it were unplanned, so the user was invited into the refusal — and then
 * the refusal was dressed as a failure and leaked a stack-trace phrase and an
 * API parameter at them.
 *
 * `GET /api/director/survey` is pure arithmetic over narration and never reads a
 * plan, which is why the panel could not tell the difference. It now asks.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/directorApi", () => ({
  fetchCoverageSurvey: vi.fn(),
  redirectSceneCoverage: vi.fn(),
  waitForJob: vi.fn(),
  fetchRunningPlanJobs: vi.fn(),
  fetchBeatCoverageStates: vi.fn(),
}));

import CoverageSurveyPanel from "./CoverageSurveyPanel";
import {
  fetchCoverageSurvey,
  redirectSceneCoverage,
  waitForJob,
  fetchRunningPlanJobs,
  fetchBeatCoverageStates,
} from "../lib/directorApi";
import type { CoverageSurvey } from "../types/director";

const mockSurvey = vi.mocked(fetchCoverageSurvey);
const mockPlan = vi.mocked(redirectSceneCoverage);
const mockWait = vi.mocked(waitForJob);
const mockRunning = vi.mocked(fetchRunningPlanJobs);
const mockCoverage = vi.mocked(fetchBeatCoverageStates);

const SURVEY: CoverageSurvey = {
  episode_seconds: 600,
  frozen_if_nothing_covered: 480,
  frozen_pct: 80,
  recommended: ["s001"],
  scenes: [["s001"]],
  beats: [
    {
      beat_id: "s001",
      seconds: 17.7,
      motion_type: "ai_video",
      frozen_if_left: 7.7,
      recommend: 3,
      reason: "8s of this paid beat would be a frozen frame",
    },
  ],
};

const flush = () => act(async () => {});

/** Render and wait for the coverage read to land. */
async function mountPanel(onSelectBeats = vi.fn()) {
  render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
  await screen.findByTestId("beat-coverage-s001").catch(() => null);
  await flush();
  return onSelectBeats;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockSurvey.mockResolvedValue(SURVEY);
  mockRunning.mockResolvedValue({});
  // s001 is locked, with the nine shots the production run actually produced.
  mockCoverage.mockResolvedValue({
    s001: { status: "locked", shots: 9, locked: true, estimatedCost: 1.85, warnings: 0, durationSeconds: 17.7 },
  });
});

afterEach(cleanup);

// --- the offer matches what the server will accept ---------------------------

describe("a beat with locked coverage is not offered as an unplanned one", () => {
  test("PLAN SCENE is not on offer for a locked beat", async () => {
    await mountPanel();

    // THE DEFECT. This button is what walked the user into a refusal: the
    // planner will decline the request it sends, every time.
    expect(screen.queryByTestId("plan-beat-s001")).toBeNull();
    // …and what replaces it is an action the server will honour.
    expect(screen.getByTestId("open-beat-s001")).toBeTruthy();
  });

  test("the row says it is locked, and how much coverage it holds", async () => {
    await mountPanel();

    const badge = screen.getByTestId("beat-coverage-s001");
    expect(badge.getAttribute("data-locked")).toBe("true");
    expect(badge.textContent).toContain("Locked");
    // The shot count, which nothing in this panel had ever shown.
    expect(badge.textContent).toContain("9 shots");
  });

  test("opening a locked beat asks for no plan at all", async () => {
    const onSelectBeats = await mountPanel();

    fireEvent.click(screen.getByTestId("open-beat-s001"));

    expect(mockPlan).not.toHaveBeenCalled();
    expect(onSelectBeats).toHaveBeenCalledWith(["s001"]);
  });

  test("a planned-but-unlocked beat is also not re-planned by accident", async () => {
    // Draft coverage is not protected by the planner, but it is still work the
    // user has. The offer is the same shape; only the badge differs.
    mockCoverage.mockResolvedValue({
      s001: { status: "draft", shots: 6, locked: false, estimatedCost: 0.9, warnings: 1, durationSeconds: 17.7 },
    });
    await mountPanel();

    expect(screen.queryByTestId("plan-beat-s001")).toBeNull();
    const badge = screen.getByTestId("beat-coverage-s001");
    expect(badge.getAttribute("data-locked")).toBe("false");
    expect(badge.textContent).toContain("Planned");
    expect(badge.textContent).toContain("6 shots");
  });

  test("a beat with no coverage is still offered normally", async () => {
    // The other direction: this must not turn into "nothing can ever be planned".
    mockCoverage.mockResolvedValue({});
    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);
    await screen.findByTestId("plan-beat-s001");
    await flush();

    expect(screen.getByTestId("plan-beat-s001")).toBeTruthy();
    expect(screen.queryByTestId("open-beat-s001")).toBeNull();
    expect(screen.queryByTestId("beat-coverage-s001")).toBeNull();
  });
});

// --- re-planning is deliberate, and names what it discards -------------------

describe("re-planning is a deliberate act, not a retry", () => {
  test("RE-PLAN alone sends nothing; it asks first, naming what is lost", async () => {
    await mountPanel();

    fireEvent.click(screen.getByTestId("replan-beat-s001"));
    await flush();

    // THE DEFECT this guards: a client that quietly resent with replan=true
    // would turn the server's guard into a formality.
    expect(mockPlan).not.toHaveBeenCalled();

    const confirm = screen.getByTestId("replan-confirm-s001");
    expect(confirm.textContent).toContain("discards its 9 existing shots");
    expect(confirm.textContent).toContain("warnings resolved");
    expect(confirm.textContent).toContain("locked");
    expect(confirm.textContent).toContain("cannot be undone");
  });

  test("confirming sends replan explicitly", async () => {
    const job = { ok: true, status: "done", log: "done" };
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s001" });
    mockWait.mockResolvedValue(job);

    await mountPanel();
    fireEvent.click(screen.getByTestId("replan-beat-s001"));
    await flush();
    fireEvent.click(screen.getByTestId("replan-go-s001"));
    await flush();

    expect(mockPlan).toHaveBeenCalledTimes(1);
    // The sixth argument is `replan`. It is true only here.
    expect(mockPlan.mock.calls[0][5]).toBe(true);
  });

  test("declining keeps the coverage and sends nothing", async () => {
    await mountPanel();

    fireEvent.click(screen.getByTestId("replan-beat-s001"));
    await flush();
    fireEvent.click(screen.getByTestId("replan-cancel-s001"));
    await flush();

    expect(mockPlan).not.toHaveBeenCalled();
    expect(screen.queryByTestId("replan-confirm-s001")).toBeNull();
    expect(screen.getByTestId("beat-coverage-s001").getAttribute("data-locked")).toBe("true");
  });

  test("an ordinary first plan never sets replan", async () => {
    // The default must stay false on the path the user takes most often.
    mockCoverage.mockResolvedValue({});
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s001" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s001"));
    await flush();

    expect(mockPlan).toHaveBeenCalledTimes(1);
    expect(mockPlan.mock.calls[0][5]).toBe(false);
  });

  test("a refusal that still gets through is not dressed as a pipeline failure", async () => {
    // Belt and braces: the offer should prevent this, but if the planner ever
    // refuses anyway the user must see the refusal, not a stack trace, and the
    // panel must not open a workspace over it.
    mockCoverage.mockResolvedValue({});
    const err = new Error(
      "every requested beat already has locked coverage (s001). Pass replan=true to plan over it."
    ) as Error & { status?: number };
    err.status = 400;
    mockPlan.mockRejectedValue(err);

    const onSelectBeats = vi.fn();
    render(<CoverageSurveyPanel onSelectBeats={onSelectBeats} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s001"));
    await flush();

    expect(onSelectBeats).not.toHaveBeenCalled();
    const problem = screen.getByTestId("plan-problem-s001");
    expect(problem.getAttribute("data-kind")).toBe("failed");
    expect(problem.textContent).toContain("already has locked coverage");
  });
});

// --- the offer keeps up with what the server holds ---------------------------

describe("the offer is re-read after a plan lands", () => {
  test("a beat just planned stops being offered as unplanned", async () => {
    mockCoverage.mockResolvedValue({});
    mockPlan.mockResolvedValue({ ok: true, started: true, job: "director_plan:s001" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("plan-beat-s001"));

    // The plan lands, and the server now holds 9 shots for this beat.
    mockCoverage.mockResolvedValue({
      s001: { status: "draft", shots: 9, locked: false, estimatedCost: 1.85, warnings: 0, durationSeconds: 17.7 },
    });
    await flush();
    await flush();

    expect(await screen.findByTestId("beat-coverage-s001")).toBeTruthy();
    expect(screen.queryByTestId("plan-beat-s001")).toBeNull();
  });

  test("a coverage read that fails leaves the survey usable", async () => {
    mockCoverage.mockRejectedValue(new Error("scene endpoint unreachable"));

    render(<CoverageSurveyPanel onSelectBeats={vi.fn()} />);

    // Unknown coverage means the beat is offered as it always was, rather than
    // the panel refusing to show an action because one read failed.
    expect(await screen.findByTestId("plan-beat-s001")).toBeTruthy();
    await flush();
    expect(screen.queryByTestId("beat-coverage-s001")).toBeNull();
  });
});
