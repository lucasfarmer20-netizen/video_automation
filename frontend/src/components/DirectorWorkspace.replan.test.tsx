/**
 * The re-plan controls say they are working, and say why when they are not.
 *
 * From the human, immediately after the lock refusal: "The re-plan button also
 * should provide some feedback that it is working, like at least greying out."
 *
 * Two separate defects sat behind that sentence.
 *
 * 1. **The button in the Stale Plan Snapshot banner had no state at all** — no
 *    `disabled`, no spinner, no label change. It is the button attached to the
 *    banner that eventually told them what was wrong, so it is the one they
 *    reached for, and clicking it started a job of tens of seconds while the
 *    control sat completely unchanged. A second click was a second request.
 *
 * 2. **REDIRECT SCENE had the state and never said it.** `disabled` and a
 *    spinner were already wired to `redirecting`, but the label read "REDIRECT
 *    SCENE" throughout. `CoverageSurveyPanel` had already converged on the
 *    opposite: `PLANNING (s003)…`, `RE-PLANNING…`. Weak feedback on a long job
 *    reads as no feedback, which is the whole complaint.
 *
 * And the locked half of `disabled={isLocked || redirecting}`, which is the
 * greyed-control-without-a-sentence failure this project keeps rejecting: on a
 * locked plan the button sat dead with nothing anywhere saying why or what to do
 * instead. It is stated now, and the statement is checkable — `plan_scene` plans
 * AROUND locked beats when `replan` is false, which is what `redirectSceneCoverage`
 * sends, so a redirect issued from here would return success having changed
 * nothing at all.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/directorApi", () => ({
  fetchCoveragePlan: vi.fn(),
  redirectSceneCoverage: vi.fn(),
  setCoverageStatus: vi.fn(),
  performShotAction: vi.fn(),
  updateShot: vi.fn(),
  waitForJob: vi.fn(),
  critiqueCoverage: vi.fn(),
  decideDirectorWarning: vi.fn(),
  compileCoverage: vi.fn(),
  MOCK_SCENES: [],
}));

import DirectorWorkspace from "./DirectorWorkspace";
import { fetchCoveragePlan, redirectSceneCoverage, waitForJob } from "../lib/directorApi";
import type { DirectorCoveragePlan, DirectorShot } from "../types/director";

const mockFetchPlan = vi.mocked(fetchCoveragePlan);
const mockRedirect = vi.mocked(redirectSceneCoverage);
const mockWait = vi.mocked(waitForJob);

// --- fixture -----------------------------------------------------------------

function shot(n: number): DirectorShot {
  return {
    id: `s002.0${n}`,
    beat_id: "s002",
    tier: 1,
    purpose: "master",
    subject: "the mill yard",
    shot_size: "mw",
    angle: "front",
    camera: { move: "push", duration: 3.0, speed: 1, amount: 4 },
    identity_critical: false,
    motion_type: "parallax",
    backend: "nano2",
    prompt: "…",
    motion_prompt: "…",
    draft_variations: [],
    estimated_cost: 0.15,
  };
}

/**
 * s002 exactly as the human's project holds it.
 *
 * Two shots of 3.0s against a 6.0s snapshot — which is a COMPLETE plan for the
 * beat it was written for. The beat is now 34.5s of recorded narration, so the
 * plan is stale, and nothing about the two shots looks wrong on its own. That
 * divergence is what makes `isSnapshotStale` true and puts the banner on screen.
 */
function stalePlan(over: Partial<DirectorCoveragePlan> = {}): DirectorCoveragePlan {
  return {
    plan_id: "plan_s002",
    scene_id: "s002",
    scene_title: "s002 — The Mountain Takes Its Toll",
    scene_beats: ["s002"],
    status: "draft",
    total_duration: 6.0,
    beat_duration: 6.0,
    live_beat_duration: 34.5,
    profile: "historical_docudrama",
    coverage: [1, 2].map(shot),
    warnings: [],
    warning_dispositions: {},
    estimated_cost: 0.3,
    paid_shots: 0,
    approved_signature: "",
    ...over,
  };
}

function deferred<T>() {
  let settle!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    settle = res;
  });
  return { promise, settle };
}

/**
 * The sentinel the feedback assertions fail with.
 *
 * The defect is not "a state variable was unset" — it is that a running job
 * looked identical to a button that did nothing. So what has to fail is the
 * reading of the control itself.
 */
const LOOKED_LIKE_NOTHING_HAPPENED =
  "a re-plan job is running and the control says nothing about it: " +
  "indistinguishable from a button that did nothing";

async function mount(plan: DirectorCoveragePlan = stalePlan()) {
  mockFetchPlan.mockResolvedValue(plan);
  render(<DirectorWorkspace sceneId="s002" activeProjectTitle="Heney" mediaUrl={(p) => p} />);
  return screen.findByTestId("redirect-scene");
}

beforeEach(() => {
  vi.clearAllMocks();
  mockRedirect.mockResolvedValue({ ok: true, job: "director_plan:s002" });
  mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });
});

afterEach(cleanup);

// --- the banner's own button, which had no state whatever --------------------

describe("the Re-plan button in the Stale Plan Snapshot banner", () => {
  test("it is there because the plan IS stale — 6.0s of coverage, 34.5s of beat", async () => {
    await mount();

    // The banner the human found for themselves, and the numbers that put it
    // there: the same divergence `director.validate` refuses the lock on.
    const body = document.body.textContent || "";
    expect(body).toContain("Stale Plan Snapshot");
    expect(body).toContain("34.5s");
    expect(body).toContain("6.0s");
    expect(screen.getByTestId("replan-stale")).toBeTruthy();
  });

  test("clicking it says a job is running, and greys the control out", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    await mount();

    const button = screen.getByTestId("replan-stale") as HTMLButtonElement;
    fireEvent.click(button);
    await act(async () => {});

    // THE ASSERTION THIS FILE EXISTS FOR, and it is about what the control
    // READS, not about what state was set behind it.
    expect(button.textContent, LOOKED_LIKE_NOTHING_HAPPENED).toContain("RE-PLANNING (s002)…");
    expect(button.disabled, LOOKED_LIKE_NOTHING_HAPPENED).toBe(true);
    expect(document.body.textContent, LOOKED_LIKE_NOTHING_HAPPENED)
      .toContain("Re-planning s002 — this runs on the server");

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
  });

  test("a second click cannot start a second planning job", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    await mount();

    const button = screen.getByTestId("replan-stale");
    fireEvent.click(button);
    await act(async () => {});
    fireEvent.click(button);
    await act(async () => {});

    expect(mockRedirect).toHaveBeenCalledTimes(1);

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
  });

  test("on a locked plan it says what to do instead of sitting dead", async () => {
    await mount(stalePlan({ status: "locked" }));

    const button = screen.getByTestId("replan-stale") as HTMLButtonElement;
    // Still disabled — the planner would plan around this beat and change
    // nothing — but no longer silent about it.
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain("UNLOCK TO RE-PLAN");
  });
});

// --- REDIRECT SCENE: the state existed, the label never said it --------------

describe("REDIRECT SCENE while the planner runs", () => {
  test("the label changes, matching the pattern the survey panel already uses", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    const button = (await mount()) as HTMLButtonElement;

    fireEvent.click(button);
    await act(async () => {});

    expect(button.textContent, LOOKED_LIKE_NOTHING_HAPPENED).toContain("RE-PLANNING (s002)…");
    expect(button.textContent, LOOKED_LIKE_NOTHING_HAPPENED).not.toContain("REDIRECT SCENE");
    expect(button.disabled).toBe(true);

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
  });

  test("a multi-beat scene names every beat the job is running on", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    const button = (await mount(stalePlan({ scene_beats: ["s002", "s003"] }))) as HTMLButtonElement;

    fireEvent.click(button);
    await act(async () => {});

    expect(button.textContent, LOOKED_LIKE_NOTHING_HAPPENED).toContain("RE-PLANNING (s002, s003)…");

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
  });

  test("the running line appears before the server has logged anything", async () => {
    // `waitForJob`'s `onLog` fires when the server first writes a line, which is
    // not immediately. The old panel rendered only `redirecting && redirectLog`,
    // so the first stretch of the wait showed nothing.
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    const button = await mount();

    fireEvent.click(button);
    await act(async () => {});

    expect(screen.getByTestId("redirect-running").textContent, LOOKED_LIKE_NOTHING_HAPPENED)
      .toContain("Re-planning s002");

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
  });

  test("the control comes back when the job ends", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    const button = (await mount()) as HTMLButtonElement;

    fireEvent.click(button);
    await act(async () => {});
    expect(button.disabled).toBe(true);

    mockFetchPlan.mockResolvedValue(stalePlan({ beat_duration: 34.5, total_duration: 34.5 }));
    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });

    const after = screen.getByTestId("redirect-scene") as HTMLButtonElement;
    expect(after.disabled).toBe(false);
    expect(after.textContent).toContain("REDIRECT SCENE");
    expect(screen.queryByTestId("redirect-running")).toBeNull();
  });

  test("a planning failure is reported, not left as a silent stop", async () => {
    mockWait.mockResolvedValue({ ok: false, status: "error", log: "planner raised: 429 from anthropic" });
    const button = await mount();

    fireEvent.click(button);
    await act(async () => {});

    expect(document.body.textContent).toContain("Planning failed");
    expect(document.body.textContent).toContain("429 from anthropic");
  });
});

// --- the locked half of the disable, which was a dead control ----------------

describe("a locked plan says why re-planning is off", () => {
  test("the button names the action, and the note names the button", async () => {
    await mount(stalePlan({ status: "locked" }));

    const button = screen.getByTestId("redirect-scene") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain("UNLOCK TO REDIRECT");

    // The sentence, and it points at a control that is on this same screen.
    const note = screen.getByTestId("redirect-locked-note");
    expect(note.textContent).toContain("UNLOCK TO EDIT");
    expect(note.textContent).toContain("plans around locked beats");
    expect(note.textContent).toContain("without discarding its coverage");
  });

  test("a compiled plan is locked for this purpose too", async () => {
    await mount(stalePlan({ status: "compiled" }));

    expect((screen.getByTestId("redirect-scene") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("redirect-locked-note")).toBeTruthy();
  });

  test("a draft plan carries no such note — there is nothing to explain", async () => {
    await mount();

    expect((screen.getByTestId("redirect-scene") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId("redirect-locked-note")).toBeNull();
  });
});

// --- the unplanned-beat view, same class ------------------------------------

describe("the plan button on an unplanned beat", () => {
  test("says it is planning rather than repeating the route it posts to", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockWait.mockReturnValue(job.promise);
    mockFetchPlan.mockRejectedValue(new Error("no plan for s002"));
    render(<DirectorWorkspace sceneId="s002" activeProjectTitle="Heney" mediaUrl={(p) => p} />);

    const button = (await screen.findByTestId("plan-unplanned-beat")) as HTMLButtonElement;
    fireEvent.click(button);
    await act(async () => {});

    expect(button.textContent, LOOKED_LIKE_NOTHING_HAPPENED).toContain("PLANNING (s002)…");
    expect(button.disabled).toBe(true);

    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
  });
});
