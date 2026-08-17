/**
 * Re-checking the critic says which of three things happened — including "it broke".
 *
 * `POST /api/director/critique` is synchronous, so there is no job race here.
 * The defect was on the other side of the call:
 *
 *     } catch (e: any) { console.log("Critique check complete"); }
 *
 * A failure was logged as "complete" and nothing reached the screen. The user
 * clicks Re-check, the request fails, the spinner stops, the warning list is
 * unchanged — and that is indistinguishable from "the critic ran and found
 * nothing new". It is the most dangerous wrong answer this endpoint can give,
 * because the human's next two actions are LOCK and then SPEND.
 *
 * The quiet path is the same defect with no exception thrown: `if (res.ok &&
 * res.warnings)` silently kept the old list when either was missing. An absent
 * warning list has re-checked NOTHING; treating it as an empty one clears a
 * scene on the strength of a reply that never said so.
 *
 * So there is one test per outcome, and in each of the failure tests the FIRST
 * assertion is that the human SEES the failure — not that nothing crashed.
 * "No crash" is what the old code already delivered. Contract §11.4.
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
import { critiqueCoverage, fetchCoveragePlan } from "../lib/directorApi";
import type {
  DirectorCoveragePlan,
  DirectorShot,
  DirectorWarning,
} from "../types/director";

const mockFetchPlan = vi.mocked(fetchCoveragePlan);
const mockCritique = vi.mocked(critiqueCoverage);

// --- fixture -----------------------------------------------------------------

const SHOT: DirectorShot = {
  id: "s001.01",
  beat_id: "s001",
  tier: 1,
  purpose: "master",
  subject: "the mill yard",
  shot_size: "mw",
  angle: "front",
  camera: { move: "push", duration: 28.2, speed: 1, amount: 4 },
  identity_critical: false,
  motion_type: "parallax",
  backend: "nano2",
  prompt: "…",
  motion_prompt: "…",
  draft_variations: [],
  estimated_cost: 0.15,
};

const OLD_WARNING: DirectorWarning = {
  id: "w-old",
  shot_id: "s001.01",
  kind: "identity_risk",
  detail: "s001.01 holds the face for 28.2s — the drift risk is the whole beat.",
};

/** The scene spans two beats: the critic is asked about both, never just one. */
function plan(over: Partial<DirectorCoveragePlan> = {}): DirectorCoveragePlan {
  return {
    plan_id: "plan_s001",
    scene_id: "s001",
    scene_title: "s001 — The Mountain Takes Its Toll",
    scene_beats: ["s001", "s002"],
    status: "draft",
    total_duration: 28.2,
    beat_duration: 28.2,
    live_beat_duration: 28.2,
    profile: "historical_docudrama",
    coverage: [SHOT],
    warnings: [OLD_WARNING],
    warning_dispositions: {},
    estimated_cost: 0.15,
    paid_shots: 0,
    ...over,
  };
}

/** The same scene after the human has decided every finding it carried. */
function decidedPlan(): DirectorCoveragePlan {
  return plan({
    warning_dispositions: { "w-old": { decision: "accepted", note: "deliberate" } },
  });
}

/** Mount and wait for the plan to land. */
async function mount(p: DirectorCoveragePlan = plan()) {
  mockFetchPlan.mockResolvedValue(p);
  render(<DirectorWorkspace sceneId="s001" activeProjectTitle="Heney" mediaUrl={(x) => x} />);
  await screen.findByTestId(
    p.warnings.some((w) => !(w.id && p.warning_dispositions?.[w.id]?.decision))
      ? "recheck-warnings"
      : "recheck-warnings-idle"
  );
}

/** Click whichever Re-check control this state puts on screen. */
async function clickRecheck() {
  const button =
    screen.queryByTestId("recheck-warnings") ||
    screen.getByTestId("recheck-warnings-idle");
  fireEvent.click(button);
  await act(async () => {});
  return button as HTMLButtonElement;
}

/**
 * The findings themselves, as the human reads them.
 *
 * They live in the Problem Queue drawer, which renders nothing while closed —
 * so asserting on the plan's warning list means opening it. That is the point:
 * what matters is not the value in state but the text a human would see.
 */
function openProblemQueue() {
  fireEvent.click(screen.getByText("Review Problems"));
}

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

// --- state 1: it ran and found something -------------------------------------

describe("the critic raised findings", () => {
  test("the server's list replaces ours, and the run is stated as a run", async () => {
    await mount();
    mockCritique.mockResolvedValue({
      ok: true,
      warnings: [
        { id: "w-1", kind: "repeated_framing", detail: "s001.02 repeats s001.01" },
        { id: "w-2", kind: "insufficient_cutaway", detail: "no cutaway in 28.2s" },
      ],
    });

    await clickRecheck();

    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.getAttribute("data-kind")).toBe("found");
    expect(outcome.textContent).toContain("raised 2 findings");

    // The findings on screen are this run's, not the previous run's.
    expect(screen.getByTestId("unresolved-count").textContent).toContain("2 coverage issues");
    openProblemQueue();
    expect(screen.getByText("s001.02 repeats s001.01")).toBeTruthy();
    expect(screen.queryByText(OLD_WARNING.detail)).toBeNull();
  });
});

// --- state 2: it ran and there was nothing -----------------------------------

describe("the critic raised nothing", () => {
  test("a clean scene is stated as a result, not left as an absence", async () => {
    await mount(decidedPlan());
    mockCritique.mockResolvedValue({ ok: true, warnings: [] });

    await clickRecheck();

    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.getAttribute("data-kind")).toBe("clear");
    // The distinction the whole fix turns on.
    expect(outcome.textContent).toContain("raised nothing");
    expect(outcome.textContent).toContain("it ran");
  });

  test("the re-check is reachable once every finding has been decided", async () => {
    // THE REACHABILITY GAP. The only Re-check button lived inside the banner
    // that unmounts when nothing is awaiting a decision — which is exactly the
    // moment the human asks the critic to look again before locking.
    await mount(decidedPlan());

    expect(screen.queryByTestId("recheck-warnings")).toBeNull();
    expect(screen.getByTestId("recheck-warnings-idle")).toBeTruthy();

    mockCritique.mockResolvedValue({ ok: true, warnings: [] });
    await clickRecheck();
    expect(mockCritique).toHaveBeenCalledTimes(1);
  });
});

// --- state 3: it failed ------------------------------------------------------

describe("the re-check failed", () => {
  test("the failure is on screen, in the server's words", async () => {
    mockCritique.mockRejectedValue(
      new Error("planner.critique raised: overloaded_error from Anthropic")
    );

    await mount();
    await clickRecheck();

    // THE DEFECT. This is the assertion the old `console.log("Critique check
    // complete")` fails, and the only one that could have caught it — a test
    // that merely asserted no crash is exactly what the old code passed.
    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.getAttribute("data-kind")).toBe("failed");
    expect(outcome.textContent).toContain(
      "planner.critique raised: overloaded_error from Anthropic"
    );

    // And it says what the screen is now showing, because an unchanged list is
    // the thing that reads as "the critic ran and found nothing new".
    expect(outcome.textContent).toContain("Nothing on screen has been re-checked");
    expect(outcome.textContent).toContain("from before this run");
  });

  test("the previous findings survive a failure, neither cleared nor re-claimed", async () => {
    mockCritique.mockRejectedValue(new Error("500 from /api/director/critique"));

    await mount();
    await clickRecheck();

    expect(screen.getByTestId("critique-outcome").getAttribute("data-kind")).toBe("failed");
    // Still there — a failed re-check must not silently empty the scene…
    expect(screen.getByTestId("unresolved-count").textContent).toContain("1 coverage issue");
    openProblemQueue();
    expect(screen.getByText(OLD_WARNING.detail)).toBeTruthy();
    // …and must not be presented as this run's result.
    expect(screen.getByTestId("critique-outcome").textContent).not.toContain("raised");
  });

  test("a reply with no warning list has re-checked nothing, and says so", async () => {
    // The quiet path: no exception, `ok` is true, and `warnings` never arrived.
    // `if (res.ok && res.warnings)` fell through here and said nothing at all.
    mockCritique.mockResolvedValue({ ok: true });

    await mount();
    await clickRecheck();

    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.getAttribute("data-kind")).toBe("failed");
    expect(outcome.textContent).toContain("without a warning list");
    expect(outcome.textContent).toContain("Nothing on screen has been re-checked");
    // Absent is not empty: the scene must not come out of this looking clean.
    expect(screen.getByTestId("unresolved-count").textContent).toContain("1 coverage issue");
    openProblemQueue();
    expect(screen.getByText(OLD_WARNING.detail)).toBeTruthy();
  });

  test("a reply whose ok is falsy is a failure, not a clean scene", async () => {
    mockCritique.mockResolvedValue({ ok: false, warnings: [] });

    await mount();
    await clickRecheck();

    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.getAttribute("data-kind")).toBe("failed");
    // `warnings: []` is right there and must not be believed on a falsy `ok`.
    expect(outcome.textContent).not.toContain("raised nothing");
    expect(screen.getByTestId("unresolved-count").textContent).toContain("1 coverage issue");
    openProblemQueue();
    expect(screen.getByText(OLD_WARNING.detail)).toBeTruthy();
  });
});

// --- scope and mechanics -----------------------------------------------------

describe("the re-check covers the scene, and only runs once at a time", () => {
  test("every beat in the scene is sent, not just the open one", async () => {
    mockCritique.mockResolvedValue({ ok: true, warnings: [] });

    await mount();
    await clickRecheck();

    expect(mockCritique).toHaveBeenCalledWith(["s001", "s002"]);
  });

  test("the button is held while the request is in flight", async () => {
    let settle!: (v: { ok: boolean; warnings: DirectorWarning[] }) => void;
    mockCritique.mockReturnValue(
      new Promise((res) => {
        settle = res;
      })
    );

    await mount();
    const button = await clickRecheck();

    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    await act(async () => {});
    expect(mockCritique).toHaveBeenCalledTimes(1);

    await act(async () => {
      settle({ ok: true, warnings: [] });
    });
    expect(screen.getByTestId("critique-outcome").getAttribute("data-kind")).toBe("clear");
  });

  test("a new run clears the previous outcome rather than stacking on it", async () => {
    mockCritique.mockRejectedValueOnce(new Error("first attempt: 503"));

    await mount();
    await clickRecheck();
    expect(screen.getByTestId("critique-outcome").getAttribute("data-kind")).toBe("failed");

    mockCritique.mockResolvedValueOnce({ ok: true, warnings: [] });
    await clickRecheck();

    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.getAttribute("data-kind")).toBe("clear");
    expect(outcome.textContent).not.toContain("first attempt: 503");
  });
});
