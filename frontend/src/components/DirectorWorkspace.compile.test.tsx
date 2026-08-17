/**
 * The locked coverage plan can be compiled, and every refusal says which one it is.
 *
 * `POST /api/director/compile/{beat_id}` has existed since Spike B — fully
 * implemented, carefully gated, and called by nothing. A user could survey the
 * episode, plan a scene, decide its warnings and lock it, and then find no
 * control anywhere that turned that plan into assets. Production, 22:55:42:
 * `POST /api/director/lock_scene 200`, and no compile request ever followed,
 * because none could be issued.
 *
 * Two failures are guarded here, and they are the ones this project has already
 * paid for twice:
 *
 * 1. **Fire-and-forget.** The POST answers the instant `start_job` spawns a
 *    thread. A compile renders every shot and waits on a paid video model. So
 *    the first assertion of the happy-path test is that nothing claims success
 *    while the job is still running.
 *
 * 2. **Collapsed refusals.** The route returns five distinct refusals, each with
 *    a sentence that says what to do next. "Lock it first" is misleading advice
 *    for a plan that WAS locked until someone edited it, and "compile failed"
 *    says neither. So every refusal below has its own test, and in each the
 *    FIRST assertion is that the server's own sentence reached the screen —
 *    the assertion that a flattening regression fails. A cheaper assertion put
 *    ahead of it (that "an error appeared", say) would fail first and hide which
 *    refusal had been lost.
 *
 * And the money: compiling s001 buys two paid video shots. The cost is asserted
 * to be on screen BEFORE the request is issued, because Gate 1's premise is that
 * spending is a deliberate, informed human decision.
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
import { compileCoverage, fetchCoveragePlan, waitForJob } from "../lib/directorApi";
import type { DirectorCoveragePlan, DirectorShot } from "../types/director";

const mockFetchPlan = vi.mocked(fetchCoveragePlan);
const mockCompile = vi.mocked(compileCoverage);
const mockWait = vi.mocked(waitForJob);

// --- fixture -----------------------------------------------------------------

function shot(n: number, motion: DirectorShot["motion_type"]): DirectorShot {
  return {
    id: `s001.0${n}`,
    beat_id: "s001",
    tier: 1,
    purpose: "master",
    subject: "the mill yard",
    shot_size: "mw",
    angle: "front",
    camera: { move: "push", duration: 3.1, speed: 1, amount: 4 },
    identity_critical: false,
    motion_type: motion,
    backend: motion === "ai_video" ? "kling3" : "nano2",
    prompt: "…",
    motion_prompt: "…",
    draft_variations: [],
    estimated_cost: motion === "ai_video" ? 0.8 : 0.15,
  };
}

/** s001 as the human actually locked it: 9 shots, 2 of them paid, ~$1.85. */
function plan(over: Partial<DirectorCoveragePlan> = {}): DirectorCoveragePlan {
  return {
    plan_id: "plan_s001",
    scene_id: "s001",
    scene_title: "s001 — The Mountain Takes Its Toll",
    scene_beats: ["s001"],
    status: "locked",
    total_duration: 28.2,
    beat_duration: 28.2,
    live_beat_duration: 28.2,
    profile: "historical_docudrama",
    coverage: [1, 2, 3, 4, 5, 6, 7].map((n) => shot(n, "parallax"))
      .concat([shot(8, "ai_video"), shot(9, "ai_video")]),
    warnings: [],
    warning_dispositions: {},
    estimated_cost: 1.85,
    paid_shots: 2,
    approved_signature: "ab12cd34ef567890",
    ...over,
  };
}

/** A promise whose settling this test controls — i.e. a job still running. */
function deferred<T>() {
  let settle!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    settle = res;
  });
  return { promise, settle };
}

/** A refusal shaped exactly as `compileCoverage` throws one. */
function refusal(
  status: number,
  message: string,
  extra: Record<string, unknown> = {}
): Error {
  return Object.assign(new Error(message), { status }, extra);
}

/**
 * The sentinel the money assertion fails with.
 *
 * It exists so a mutation run can prove WHY a test died. `M5` in
 * `scratch/mutate_compile_control.py` wires COMPILE COVERAGE straight to the
 * dispatch, and it was being killed by a missing-`compile-cost-gate` element
 * long before anything asserted that no money had been spent — so it proved the
 * gate element renders, not that money cannot be spent without it. The harness
 * now requires this string in the failure output.
 */
const SPENT_WITHOUT_A_PRICE =
  "COMPILE COVERAGE dispatched without the cost gate: money committed with no price shown";

/**
 * Mount and click COMPILE COVERAGE.
 *
 * Deliberately does NOT look for the gate. A `getByTestId("compile-cost-gate")`
 * here would throw under exactly the mutation that matters — the one that
 * bypasses the gate — before any test could assert that nothing was dispatched.
 * The cheap assertion must never be the one that runs first.
 */
async function mountAndClickCompile() {
  render(<DirectorWorkspace sceneId="s001" activeProjectTitle="Heney" mediaUrl={(p) => p} />);
  const open = await screen.findByTestId("compile-open-gate");
  fireEvent.click(open);
  await act(async () => {});
}

/** Mount, click COMPILE COVERAGE, and hand back the gate it must have opened. */
async function mountAndOpenGate() {
  await mountAndClickCompile();
  return screen.getByTestId("compile-cost-gate");
}

/** Mount, open the gate, and commit the spend. */
async function compileIt() {
  await mountAndClickCompile();
  fireEvent.click(screen.getByTestId("compile-confirm"));
  await act(async () => {});
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFetchPlan.mockResolvedValue(plan());
});

afterEach(cleanup);

// --- the control exists at all -----------------------------------------------

describe("the control the studio did not have", () => {
  test("pressing COMPILE COVERAGE commits no money", async () => {
    await mountAndClickCompile();

    // THE MONEY ASSERTION, AND IT RUNS FIRST. Under a bypass of the gate this
    // is the only assertion that demonstrates the defect; anything cheaper
    // ahead of it — the gate element, the price text — fails first and reports
    // a missing element instead of a spend.
    expect(mockCompile.mock.calls, SPENT_WITHOUT_A_PRICE).toEqual([]);

    // Only then: the control exists at all, which is the original gap. Eleven
    // references to `compile` in the frontend and every one was a type, a
    // status comparison or a comment; nothing could ever issue this request.
    expect(screen.getByTestId("compile-cost-gate")).toBeTruthy();
  });

  test("the price is on screen before the request, from the server's own count", async () => {
    await mountAndClickCompile();

    expect(mockCompile.mock.calls, SPENT_WITHOUT_A_PRICE).toEqual([]);

    // What is bought, and what it costs — before any of it is committed.
    const gate = screen.getByTestId("compile-cost-gate");
    expect(gate.textContent).toContain("2 paid video shots");
    expect(gate.textContent).toContain("$1.85");
    expect(gate.textContent).toContain("9 shots");
    expect(screen.getByTestId("compile-confirm").textContent).toContain("$1.85");
  });

  test("cancelling closes the gate without issuing anything", async () => {
    await mountAndOpenGate();
    fireEvent.click(screen.getByTestId("compile-cancel"));
    await act(async () => {});

    expect(mockCompile.mock.calls, SPENT_WITHOUT_A_PRICE).toEqual([]);
    expect(screen.queryByTestId("compile-cost-gate")).toBeNull();
  });

  test("the paid count falls back to the plan's own ai_video shots", async () => {
    // `summary.paid_shots` is absent on the direct-plan reply shape. The quote
    // must still name what will be bought rather than quietly saying nothing.
    mockFetchPlan.mockResolvedValue(plan({ paid_shots: undefined }));

    const gate = await mountAndOpenGate();

    expect(gate.textContent).toContain("2 paid video shots");
  });
});

// --- it waits for the job it started -----------------------------------------

describe("the compile is a long-running job", () => {
  test("nothing claims compiled while the job is still running", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001", shots: 9 });
    mockWait.mockReturnValue(job.promise);

    await compileIt();

    // THE DEFECT the two previous rounds cost. The POST has returned
    // {started: true} and every shot is still un-rendered.
    expect(screen.queryByTestId("compile-done")).toBeNull();
    expect(screen.queryByTestId("compile-problem")).toBeNull();

    // …and the wait is on the job the server named, not on a guessed duration.
    expect(mockWait).toHaveBeenCalledTimes(1);
    expect(mockWait.mock.calls[0][0]).toBe("director:s001");

    // While it runs, the user is told it is running.
    expect(screen.getByTestId("compile-running").textContent).toContain("Compiling s001");

    mockFetchPlan.mockResolvedValue(plan({ status: "compiled" }));
    await act(async () => {
      job.settle({ ok: true, status: "done", log: "Beat clip written: render/s001.mp4" });
    });

    expect(screen.getByTestId("compile-done").textContent).toContain("compiled");
    expect(screen.queryByTestId("compile-running")).toBeNull();
  });

  test("the running job's own log is shown, not a substitute for it", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001" });
    mockWait.mockImplementation((_key, opts) => {
      opts?.onLog?.("Compiling coverage for s001 (9 shots)...");
      return job.promise;
    });

    await compileIt();

    expect(screen.getByTestId("compile-running").textContent)
      .toContain("Compiling coverage for s001 (9 shots)");

    mockFetchPlan.mockResolvedValue(plan({ status: "compiled" }));
    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
    expect(screen.getByTestId("compile-done")).toBeTruthy();
  });

  test("a second click cannot start a second compile for the same beat", async () => {
    const job = deferred<{ ok: boolean; status: string; log: string }>();
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001" });
    mockWait.mockReturnValue(job.promise);

    await compileIt();

    expect((screen.getByTestId("compile-open-gate") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("compile-open-gate"));
    await act(async () => {});
    expect(mockCompile).toHaveBeenCalledTimes(1);

    mockFetchPlan.mockResolvedValue(plan({ status: "compiled" }));
    await act(async () => {
      job.settle({ ok: true, status: "done", log: "done" });
    });
    expect((screen.getByTestId("compile-open-gate") as HTMLButtonElement).disabled).toBe(false);
  });

  test("§11.4 — a finished job is not the claim; the saved plan is", async () => {
    // The thread ended. If the plan still does not say `compiled`, no beat clip
    // was written, and congratulating the user here is exactly the false success
    // the contract forbids.
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });
    mockFetchPlan
      .mockResolvedValueOnce(plan())
      .mockResolvedValueOnce(plan({ status: "locked" }));

    await compileIt();

    expect(screen.queryByTestId("compile-done")).toBeNull();
    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain('still reads "locked"');
    expect(problem.getAttribute("data-kind")).toBe("failed");
  });

  test("a compile that ran and failed says the beat clip was not written", async () => {
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001" });
    mockWait.mockResolvedValue({
      ok: false,
      status: "error",
      log: "director.compile_coverage raised: fal returned 422 for kling3",
    });

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain("the beat clip was not written");
    expect(problem.textContent).toContain("fal returned 422 for kling3");
    expect(screen.queryByTestId("compile-done")).toBeNull();
  });

  test("a job still running at the timeout is unfinished, not failed work", async () => {
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001" });
    mockWait.mockResolvedValue({ ok: false, status: "timeout", log: "" });

    await compileIt();

    expect(screen.getByTestId("compile-problem").textContent).toContain("still running");
    expect(screen.queryByTestId("compile-done")).toBeNull();
  });

  test("started but unnamed: it cannot know it finished, so it does not say so", async () => {
    mockCompile.mockResolvedValue({ ok: true, started: true });

    await compileIt();

    expect(screen.queryByTestId("compile-done")).toBeNull();
    expect(mockWait).not.toHaveBeenCalled();
    expect(screen.getByTestId("compile-problem").textContent)
      .toContain("did not name the job");
  });
});

// --- one test per refusal path -----------------------------------------------
//
// Every one of these asserts the server's own sentence, verbatim, first.

describe("the refusals reach the screen as themselves", () => {
  test("draft — the plan was never locked", async () => {
    mockCompile.mockRejectedValue(
      refusal(
        409,
        "plan for s001 is a draft; lock it first - approval is what allocates the render budget.",
        { approvalDrifted: false }
      )
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "plan for s001 is a draft; lock it first - approval is what allocates the render budget."
    );
    expect(problem.getAttribute("data-kind")).toBe("draft");
    expect(problem.textContent).toContain("nothing was charged");
    expect(mockWait).not.toHaveBeenCalled();
    expect(screen.queryByTestId("compile-done")).toBeNull();
  });

  test("approval drift — it WAS locked, then edited", async () => {
    mockCompile.mockRejectedValue(
      refusal(
        409,
        "plan for s001 changed after it was approved, so the approval no longer covers it; review and lock it again.",
        { approvalDrifted: true }
      )
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "plan for s001 changed after it was approved, so the approval no longer covers it; review and lock it again."
    );
    // Told apart from the plain draft above — same status, same route, and the
    // backend goes to deliberate trouble to distinguish them because "lock it
    // first" is the wrong instruction here.
    expect(problem.getAttribute("data-kind")).toBe("approval_drifted");
    expect(problem.textContent).not.toContain("lock it first");
  });

  test("staleness — the script line changed under the plan", async () => {
    mockCompile.mockRejectedValue(
      refusal(
        409,
        "the narration for s001 was rewritten to the same length after this plan was approved. Review and lock it again.",
        { stale: { reason: "line_rewritten", beat_id: "s001" } }
      )
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "the narration for s001 was rewritten to the same length after this plan was approved. Review and lock it again."
    );
    expect(problem.getAttribute("data-kind")).toBe("stale");
  });

  test("unresolved warnings — a locked plan carrying an undecided finding", async () => {
    mockCompile.mockRejectedValue(
      refusal(
        409,
        "s001 has 2 critic warning(s) with no recorded decision; resolve or accept them first.",
        {
          warnings: [
            { kind: "identity_risk", detail: "s001.03 shows the face at 3.1s" },
            { kind: "repeated_framing", detail: "s001.05 repeats s001.02" },
          ],
        }
      )
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "s001 has 2 critic warning(s) with no recorded decision; resolve or accept them first."
    );
    expect(problem.getAttribute("data-kind")).toBe("unresolved_warnings");
  });

  test("already running — a refusal that is not the user's mistake", async () => {
    mockCompile.mockRejectedValue(refusal(409, "a compile for s001 is already running"));

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain("a compile for s001 is already running");
    // Not dressed as a failure: nothing has gone wrong and nothing was lost.
    expect(problem.getAttribute("data-kind")).toBe("busy");
    expect(problem.textContent).toContain("nothing was charged");
  });

  test("no plan at all — the 404 the route raises as an HTTPException", async () => {
    mockCompile.mockRejectedValue(refusal(404, "no director plan for s001"));

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain("no director plan for s001");
    expect(problem.getAttribute("data-kind")).toBe("missing");
  });

  test("the request named no plan — told apart from a quote that went stale", async () => {
    mockCompile.mockRejectedValue(
      refusal(
        400,
        "this compile did not say which plan it was approving, so nothing was compiled and nothing was charged. s001 is locked; re-open it, confirm the cost, and send that plan's signature with the request.",
        { signatureMissing: true }
      )
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "this compile did not say which plan it was approving, so nothing was compiled and nothing was charged. s001 is locked; re-open it, confirm the cost, and send that plan's signature with the request."
    );
    expect(problem.getAttribute("data-kind")).toBe("unsigned");
    // NOT quote_changed: nothing has changed, and telling them it has would
    // send them off to re-approve a plan that is fine. Also not the generic
    // `failed` its 400 status would otherwise earn it.
    expect(problem.textContent).not.toContain("changed after you were quoted");
    expect(problem.textContent).toContain("nothing was charged");
    // A refusal, so nothing was retried and no new quote was fetched.
    expect(mockCompile).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("compile-done")).toBeNull();
  });

  test("the plan changed after the quote — consent, checked before anything else", async () => {
    mockCompile.mockRejectedValue(
      refusal(
        409,
        "the plan for s001 changed after you were quoted a price, so nothing was compiled and nothing was charged. Review the current plan and confirm its cost again.",
        { signatureMismatch: true, planSignature: "ffff0000ffff0000" }
      )
    );
    // The re-quote reads the plan that is actually there now.
    mockFetchPlan.mockResolvedValueOnce(plan()).mockResolvedValueOnce(
      plan({ estimated_cost: 12.0, paid_shots: 7 })
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "the plan for s001 changed after you were quoted a price, so nothing was compiled and nothing was charged. Review the current plan and confirm its cost again."
    );
    expect(problem.getAttribute("data-kind")).toBe("quote_changed");
    // Re-quoted, because they cannot consent to a price they have not seen.
    expect(problem.textContent).toContain("$12.00");
    expect(problem.textContent).toContain("7 paid");
    // And nothing is retried at the new price: a new price is a new decision.
    expect(mockCompile).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("compile-cost-gate")).toBeNull();
    expect(screen.queryByTestId("compile-done")).toBeNull();
  });

  test("the refreshed plan is what the next attempt quotes, and what it sends", async () => {
    // A message that re-quotes while the screen still shows the old plan is
    // worse than no re-quote: the gate would keep quoting the stale price and
    // keep sending the stale signature, so every retry is refused for ever and
    // the two numbers on screen disagree.
    mockCompile.mockRejectedValueOnce(
      refusal(409, "the plan for s001 changed after you were quoted a price.", {
        signatureMismatch: true,
      })
    );
    mockFetchPlan
      .mockResolvedValueOnce(plan())
      .mockResolvedValueOnce(
        plan({
          estimated_cost: 12.0,
          paid_shots: 7,
          approved_signature: "ffff0000ffff0000",
        })
      );

    await compileIt();

    // The gate now quotes the plan that is actually on the server.
    fireEvent.click(screen.getByTestId("compile-open-gate"));
    await act(async () => {});
    expect(screen.getByTestId("compile-cost-gate").textContent).toContain("$12.00");

    // …and the next attempt carries the new identity, not the stale one it was
    // already refused for.
    mockCompile.mockResolvedValueOnce({ ok: true, started: true, job: "director:s001" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });
    mockFetchPlan.mockResolvedValue(plan({ status: "compiled" }));
    fireEvent.click(screen.getByTestId("compile-confirm"));
    await act(async () => {});

    expect(mockCompile.mock.calls[1]).toEqual(["s001", "ffff0000ffff0000"]);
  });

  test("a re-quote that itself fails does not overwrite the refusal", async () => {
    mockCompile.mockRejectedValue(
      refusal(409, "the plan for s001 changed after you were quoted a price.", {
        signatureMismatch: true,
      })
    );
    mockFetchPlan
      .mockResolvedValueOnce(plan())
      .mockRejectedValueOnce(new Error("scene endpoint returned 503"));

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "the plan for s001 changed after you were quoted a price."
    );
    expect(problem.getAttribute("data-kind")).toBe("quote_changed");
  });

  test("the catch-all 400 — whatever actually broke, in its own words", async () => {
    mockCompile.mockRejectedValue(
      refusal(400, "[Errno 2] No such file or directory: 'render/s001'")
    );

    await compileIt();

    const problem = screen.getByTestId("compile-problem");
    expect(problem.textContent).toContain(
      "[Errno 2] No such file or directory: 'render/s001'"
    );
    expect(problem.getAttribute("data-kind")).toBe("failed");
    // A 400 is not one of the pre-`start_job` refusals, so this must not claim
    // on the user's behalf that nothing was charged.
    expect(problem.textContent).not.toContain("nothing was charged");
  });
});

// --- the quote binds the plan it was quoted for ------------------------------

describe("the confirmed price is bound to the plan it was quoted for", () => {
  test("the plan's identity travels with the request, not just its beat id", async () => {
    // Without this the request named only a beat, and the route dispatched
    // whatever load_plan(beat_id) returned at the moment it ran — so a plan
    // replaced and re-locked in another tab between the gate opening and the
    // confirmation compiled at the newer price on consent for the older one.
    mockFetchPlan.mockResolvedValue(plan({ approved_signature: "ab12cd34ef567890" }));
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:s001" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });

    await compileIt();

    expect(mockCompile).toHaveBeenCalledWith("s001", "ab12cd34ef567890");
  });

  test("every beat of a scene carries the identity of the plan that was quoted", async () => {
    mockFetchPlan.mockResolvedValue(
      plan({ scene_beats: ["s001", "s002"], approved_signature: "ab12cd34ef567890" })
    );
    mockCompile.mockResolvedValue({ ok: true, started: true, job: "director:x" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });

    await compileIt();

    expect(mockCompile.mock.calls).toEqual([
      ["s001", "ab12cd34ef567890"],
      ["s002", "ab12cd34ef567890"],
    ]);
  });
});

// --- multi-beat scenes -------------------------------------------------------

describe("a scene is locked per scene, so it compiles per scene", () => {
  test("every beat in the plan is compiled, in order", async () => {
    mockFetchPlan.mockResolvedValue(plan({ scene_beats: ["s001", "s002"] }));
    mockCompile
      .mockResolvedValueOnce({ ok: true, started: true, job: "director:s001" })
      .mockResolvedValueOnce({ ok: true, started: true, job: "director:s002" });
    mockWait.mockResolvedValue({ ok: true, status: "done", log: "done" });

    await mountAndOpenGate();
    fireEvent.click(screen.getByTestId("compile-confirm"));
    await act(async () => {});

    expect(mockCompile.mock.calls.map((c) => c[0])).toEqual(["s001", "s002"]);
  });

  test("a beat that refuses stops the run, rather than spending on the next one", async () => {
    mockFetchPlan.mockResolvedValue(plan({ scene_beats: ["s001", "s002"] }));
    mockCompile.mockRejectedValueOnce(
      refusal(409, "s001 has 1 critic warning(s) with no recorded decision; resolve or accept them first.", {
        warnings: [{ kind: "identity_risk", detail: "s001.03" }],
      })
    );

    await mountAndOpenGate();
    fireEvent.click(screen.getByTestId("compile-confirm"));
    await act(async () => {});

    expect(mockCompile).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("compile-problem").textContent)
      .toContain("s001 has 1 critic warning(s) with no recorded decision");
  });
});
