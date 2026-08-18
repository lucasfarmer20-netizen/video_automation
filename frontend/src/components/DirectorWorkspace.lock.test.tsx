/**
 * LOCK & GENERATE COVERAGE says what happened, including when it was refused.
 *
 * The human clicked it and nothing happened. Their words: "lock and generate
 * coverage isn't working on click." The button was not dead — the server was
 * refusing, correctly and usefully, with
 *
 *     400 {"ok": false, "error": "nothing was locked",
 *          "problems": ["s001: 6 critic warning(s) awaiting a decision (…)"]}
 *
 * and `handleToggleLock` had no try/catch, no error state and no busy state. The
 * rejection was unhandled, no state changed, and the click was indistinguishable
 * from a dead control. The most useful sentence the backend produces, at the
 * moment it is most useful, reached nobody.
 *
 * So THE DEFECT-PROVING ASSERTION IN EVERY REFUSAL TEST BELOW IS THAT THE
 * SERVER'S OWN SENTENCE IS IN THE DOM, and it runs first. Not that a catch block
 * exists, not that a state variable was set, not that some panel rendered — the
 * whole defect is that the human sees nothing. It is asserted against
 * `document.body.textContent` rather than a testid, deliberately: under a
 * regression that renders no panel at all, `getByTestId` would throw "unable to
 * find an element" and report a missing element instead of a lost sentence.
 *
 * One test per refusal path the route can answer on. They are not symmetric:
 * `lock_scene` validates every beat before it writes any, so a refused LOCK
 * changed nothing; unlocking has no bulk route, fans out per beat, and can come
 * back part done.
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
import { fetchCoveragePlan, setCoverageStatus } from "../lib/directorApi";
import type { DirectorCoveragePlan, DirectorShot, DirectorWarning } from "../types/director";

const mockFetchPlan = vi.mocked(fetchCoveragePlan);
const mockSetStatus = vi.mocked(setCoverageStatus);

// --- fixture -----------------------------------------------------------------

function shot(n: number): DirectorShot {
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
    motion_type: "parallax",
    backend: "nano2",
    prompt: "…",
    motion_prompt: "…",
    draft_variations: [],
    estimated_cost: 0.15,
  };
}

function warning(id: string, detail: string): DirectorWarning {
  return { id, kind: "identity_risk", detail };
}

/** s001 as it sits in front of the human: a draft, nine shots, ~$1.85. */
function plan(over: Partial<DirectorCoveragePlan> = {}): DirectorCoveragePlan {
  return {
    plan_id: "plan_s001",
    scene_id: "s001",
    scene_title: "s001 — The Mountain Takes Its Toll",
    scene_beats: ["s001"],
    status: "draft",
    total_duration: 28.2,
    beat_duration: 28.2,
    live_beat_duration: 28.2,
    profile: "historical_docudrama",
    coverage: [1, 2, 3, 4, 5, 6, 7, 8, 9].map(shot),
    warnings: [],
    warning_dispositions: {},
    estimated_cost: 1.85,
    paid_shots: 0,
    approved_signature: "",
    ...over,
  };
}

/** A refusal shaped exactly as `setCoverageStatus` throws one. */
function refusal(
  status: number,
  message: string,
  extra: { problems?: string[]; warnings?: DirectorWarning[]; changed?: string[] } = {}
): Error {
  return Object.assign(new Error(message), { status }, extra);
}

/** A promise whose settling this test controls — i.e. a request still in flight. */
function deferred<T>() {
  let settle!: (v: T) => void;
  let fail!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    settle = res;
    fail = rej;
  });
  return { promise, settle, fail };
}

/**
 * The sentinel every refusal assertion fails with.
 *
 * A mutation that removes the catch, the panel, or the `problems` list must be
 * killed by THIS message. A mutation killed by a missing element, or by a state
 * check that runs earlier, has not proved that the human was told anything.
 */
const REFUSAL_NEVER_REACHED_THE_HUMAN =
  "the lock was refused and the server's sentence never reached the screen: " +
  "the click is indistinguishable from a dead button";

async function mountAndClickLock() {
  render(<DirectorWorkspace sceneId="s001" activeProjectTitle="Heney" mediaUrl={(p) => p} />);
  const button = await screen.findByTestId("lock-toggle");
  fireEvent.click(button);
  await act(async () => {});
  return button;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFetchPlan.mockResolvedValue(plan());
  mockSetStatus.mockResolvedValue({ ok: true });
});

afterEach(cleanup);

// --- one test per refusal path -----------------------------------------------

describe("a refused lock reaches the human, in the server's own words", () => {
  test("undecided findings — the count and the ids, not just 'nothing was locked'", async () => {
    // THE DEFECT, exactly as the human hit it.
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: [
          "s001: 6 critic warning(s) awaiting a decision " +
            "(w_3f2a11, w_91bc04, w_5de720, w_0a8831 and more)",
        ],
      })
    );

    await mountAndClickLock();

    // FIRST, AND AGAINST THE WHOLE DOCUMENT. The sentence that names the beat,
    // the count and the finding ids is the entire content of the refusal; the
    // headline above it names none of the three.
    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain(
      "s001: 6 critic warning(s) awaiting a decision " +
        "(w_3f2a11, w_91bc04, w_5de720, w_0a8831 and more)"
    );
    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("nothing was locked");

    // Only then: it is presented as a refusal, and nothing claims success.
    expect(screen.getByTestId("lock-problem")).toBeTruthy();
    expect(screen.queryByTestId("lock-done")).toBeNull();
    // …and the plan on screen is untouched: lock_scene locks none if any fails.
    expect(screen.getByText(/Status: draft/)).toBeTruthy();
  });

  test("every beat that refused is listed, not just the first", async () => {
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: [
          "s001: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)",
          "s002: currently compiling",
          "s003: no plan",
        ],
      })
    );
    mockFetchPlan.mockResolvedValue(plan({ scene_beats: ["s001", "s002", "s003"] }));

    await mountAndClickLock();

    const body = document.body.textContent || "";
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s002: currently compiling");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s003: no plan");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)");

    // Each on its own line: a joined blob is how three different next actions
    // become one wall of text nobody reads to the end of.
    expect(screen.getAllByTestId("lock-problem-detail")).toHaveLength(3);
  });

  test("no plan for the beat — the refusal that is not about warnings at all", async () => {
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", { problems: ["s001: no plan"] })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: no plan");
    expect(screen.queryByTestId("lock-done")).toBeNull();
  });

  test("the beat is compiling — nothing is wrong and nothing was lost", async () => {
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", { problems: ["s001: currently compiling"] })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: currently compiling");
  });

  test("the stale snapshot — THE refusal the human actually hit", async () => {
    // The one that sent them looking. `director.validate` compares the duration
    // the plan was written against to the beat's duration now, and the sentence
    // it raises is an exact diagnosis: 6.0s of coverage against 34.5s of
    // narration. The UI threw the whole list away, so what they eventually
    // found instead was the Stale Plan Snapshot banner elsewhere on the screen.
    // Their words: "it wasn't obvious since there was no warning from the
    // failed lock."
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: [
          "s002: plan was made for 6.00s but the beat is now 34.50s — " +
            "narration changed, replan this beat",
        ],
      })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain(
      "s002: plan was made for 6.00s but the beat is now 34.50s — " +
        "narration changed, replan this beat"
    );
    // Not a warning refusal, so nothing sends them to the problem queue: the
    // instruction is in the sentence, and it is to replan the beat.
    expect(screen.queryByTestId("lock-open-queue")).toBeNull();
  });

  test("a whole scene of stale plans — one sentence per beat, all of them", async () => {
    // s002, s010 and s018 each carry exactly two shots because they were
    // planned when every beat was pinned at 6 seconds, before the VO existed.
    // Two ~3s shots IS a complete 6s beat, so nothing about them looks wrong
    // until the beat's real duration arrives. The refusal is the system
    // correctly detecting a whole class of stale plans at once, and it can only
    // report the class if every entry survives.
    mockFetchPlan.mockResolvedValue(plan({ scene_beats: ["s002", "s010", "s018"] }));
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: [
          "s002: plan was made for 6.00s but the beat is now 34.50s — narration changed, replan this beat",
          "s010: plan was made for 6.00s but the beat is now 22.10s — narration changed, replan this beat",
          "s018: plan was made for 6.00s but the beat is now 41.80s — narration changed, replan this beat",
        ],
      })
    );

    await mountAndClickLock();

    const body = document.body.textContent || "";
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s002: plan was made for 6.00s");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s010: plan was made for 6.00s");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s018: plan was made for 6.00s");
    expect(screen.getAllByTestId("lock-problem-detail")).toHaveLength(3);
  });

  test("coverage that does not fill the beat — validate's other arithmetic", async () => {
    // The same cause, a different check: the plan's own shots no longer sum to
    // the beat. Its signed delta is the whole advice.
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: [
          "s001: coverage totals 31.40s but the beat is 28.20s (+3.20s) — " +
            "coverage must fill the beat exactly",
        ],
      })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain(
      "s001: coverage totals 31.40s but the beat is 28.20s (+3.20s) — " +
        "coverage must fill the beat exactly"
    );
  });

  test("beats failing for DIFFERENT reasons at once, each in its own words", async () => {
    // A scene holds several beats and `lock_scene` accumulates one entry per
    // beat, so the list can carry all four causes simultaneously. Any collapse
    // to a single sentence has to pick one and discard three.
    mockFetchPlan.mockResolvedValue(
      plan({ scene_beats: ["s001", "s002", "s003", "s004"] })
    );
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: [
          "s001: no plan",
          "s002: currently compiling",
          "s003: plan was made for 6.00s but the beat is now 34.50s — narration changed, replan this beat",
          "s004: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)",
        ],
      })
    );

    await mountAndClickLock();

    const body = document.body.textContent || "";
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s001: no plan");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s002: currently compiling");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s003: plan was made for 6.00s but the beat is now 34.50s");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s004: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)");
    expect(screen.getAllByTestId("lock-problem-detail")).toHaveLength(4);
  });

  test("the auth 401 — the refusal with no `problems` list at all", async () => {
    // What an unconfigured browser gets for every lock it ever attempts, with
    // no other symptom anywhere. The headline IS the whole message here, so a
    // renderer that only prints `problems` would show an empty box.
    mockSetStatus.mockRejectedValue(refusal(401, "Missing or invalid X-Studio-Key."));

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("Missing or invalid X-Studio-Key.");
    expect(screen.queryByTestId("lock-problem-detail")).toBeNull();
  });

  test("the catch-all 400 — whatever actually raised, in its own words", async () => {
    mockSetStatus.mockRejectedValue(refusal(400, "no active project"));

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("no active project");
  });

  test("a body that was not JSON still says what status it failed with", async () => {
    mockSetStatus.mockRejectedValue(
      refusal(502, "Locking s001 failed with status 502.")
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("Locking s001 failed with status 502.");
  });

  test("a transport failure with no server sentence still says something", async () => {
    // `fetch` rejecting outright — no status, no body, no message worth reading.
    // The one case where there is nothing of the server's to render, and the
    // click must STILL not look like a dead button.
    mockSetStatus.mockRejectedValue(Object.assign(new Error(""), { status: undefined }));

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("Locking s001 failed before the server answered.");
  });
});

// --- unlocking is a different route with a different failure list ------------

describe("a refused unlock is not the mirror of a refused lock", () => {
  const locked = () => plan({ status: "locked", approved_signature: "ab12cd34" });

  test("the compiling beat refuses, and says so", async () => {
    mockFetchPlan.mockResolvedValue(locked());
    mockSetStatus.mockRejectedValue(
      refusal(409, "s001 is compiling; cannot change its status", {
        problems: ["s001: s001 is compiling; cannot change its status"],
        changed: [],
      })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001 is compiling; cannot change its status");
    expect(screen.queryByTestId("lock-done")).toBeNull();
  });

  test("no plan for the beat is a 404 the human can read", async () => {
    mockFetchPlan.mockResolvedValue(locked());
    mockSetStatus.mockRejectedValue(
      refusal(404, "no plan for s001", { problems: ["s001: no plan for s001"] })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("no plan for s001");
  });

  test("a part-done unlock says which beats it already changed", async () => {
    // The asymmetry the brief warned about. There is no bulk unlock route, so
    // the beats go one request each and fail independently: s001 and s003 are
    // now drafts while s002 is still locked. Reporting only "s002 is compiling"
    // would leave a screen claiming the whole scene is locked when it is not.
    mockFetchPlan.mockResolvedValue(
      plan({ status: "locked", scene_beats: ["s001", "s002", "s003"] })
    );
    mockSetStatus.mockRejectedValue(
      refusal(409, "s002 is compiling; cannot change its status", {
        problems: ["s002: s002 is compiling; cannot change its status"],
        changed: ["s001", "s003"],
      })
    );

    await mountAndClickLock();

    const body = document.body.textContent || "";
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s002: s002 is compiling; cannot change its status");
    expect(body, REFUSAL_NEVER_REACHED_THE_HUMAN).toContain("s001, s003 did unlock");
    expect(body).toContain("part locked and part draft");

    // …and the status badge is re-read, because it is describing beats this
    // very click has already changed.
    expect(mockFetchPlan).toHaveBeenCalledTimes(2);
  });

  test("a part-done unlock whose re-read fails keeps the refusal on screen", async () => {
    mockFetchPlan.mockResolvedValueOnce(plan({ status: "locked" }))
      .mockRejectedValueOnce(new Error("scene endpoint returned 503"));
    mockSetStatus.mockRejectedValue(
      refusal(409, "s002 is compiling; cannot change its status", {
        problems: ["s002: s002 is compiling; cannot change its status"],
        changed: ["s001"],
      })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s002 is compiling; cannot change its status");
    // A failed refresh must not overwrite the refusal with a message about the
    // refresh.
    expect(document.body.textContent).not.toContain("503");
  });
});

// --- the refusal points at where the findings get decided --------------------

describe("the refusal points at the queue where findings are decided", () => {
  const withFindings = () =>
    plan({
      warnings: [
        warning("w_3f2a11", "s001.03 shows the face at 3.1s"),
        warning("w_91bc04", "s001.05 repeats s001.02"),
      ],
      warning_dispositions: {},
    });

  test("a refused lock offers the queue, with the count from the plan on screen", async () => {
    // "nothing was locked" tells them the lock failed. It does not tell them
    // WHERE the findings are — and the queue is the only place a decision can
    // be recorded. The count is the client's own fact, read off the plan it is
    // displaying, never parsed out of the refusal's prose: a reworded message
    // must not be able to change what this says.
    mockFetchPlan.mockResolvedValue(withFindings());
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", {
        problems: ["s001: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)"],
      })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: 2 critic warning(s) awaiting a decision (w_3f2a11, w_91bc04)");

    const queue = screen.getByTestId("lock-open-queue");
    expect(queue.textContent).toContain("Review 2 problems");
    fireEvent.click(queue);
    await act(async () => {});
    expect(document.body.textContent).toContain("s001.03 shows the face at 3.1s");
  });

  test("no findings outstanding, no pointer — it would be pointing at nothing", async () => {
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", { problems: ["s001: currently compiling"] })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: currently compiling");
    expect(screen.queryByTestId("lock-open-queue")).toBeNull();
  });

  test("a refused UNLOCK does not offer it — warnings do not block unlocking", async () => {
    // The per-beat route skips the warning check entirely when locked=false:
    // returning a plan for more work is not an approval. Telling someone to go
    // decide findings here would be advice for a rule that is not being applied.
    mockFetchPlan.mockResolvedValue({ ...withFindings(), status: "locked" });
    mockSetStatus.mockRejectedValue(
      refusal(409, "s001 is compiling; cannot change its status", {
        problems: ["s001: s001 is compiling; cannot change its status"],
      })
    );

    await mountAndClickLock();

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001 is compiling; cannot change its status");
    expect(screen.queryByTestId("lock-open-queue")).toBeNull();
  });

  test("outstanding findings never disable the button — the server decides", async () => {
    // A greyed-out control with no explanation is the failure this project has
    // rejected five times. The refusal is what teaches the human what to do,
    // and it is unreachable if the request is never sent.
    mockFetchPlan.mockResolvedValue(withFindings());
    render(<DirectorWorkspace sceneId="s001" activeProjectTitle="Heney" mediaUrl={(p) => p} />);
    const button = (await screen.findByTestId("lock-toggle")) as HTMLButtonElement;

    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    await act(async () => {});
    expect(mockSetStatus).toHaveBeenCalled();
  });
});

// --- the request is in flight, and it is only sent once ----------------------

describe("while the request is in flight", () => {
  test("the control says so, and a second click sends nothing", async () => {
    const inFlight = deferred<{ ok: boolean }>();
    mockSetStatus.mockReturnValue(inFlight.promise);

    const button = (await mountAndClickLock()) as HTMLButtonElement;

    // Said, not merely disabled: a button that greys out with its old label
    // still reads as "that did nothing".
    expect(button.textContent).toContain("LOCKING…");
    expect(button.disabled).toBe(true);
    expect(screen.queryByTestId("lock-done")).toBeNull();
    expect(screen.queryByTestId("lock-problem")).toBeNull();

    fireEvent.click(button);
    await act(async () => {});
    expect(mockSetStatus).toHaveBeenCalledTimes(1);

    mockFetchPlan.mockResolvedValue(plan({ status: "locked" }));
    await act(async () => {
      inFlight.settle({ ok: true });
    });

    expect((screen.getByTestId("lock-toggle") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId("lock-done")).toBeTruthy();
  });

  test("an unlock in flight says UNLOCKING, not LOCKING", async () => {
    mockFetchPlan.mockResolvedValue(plan({ status: "locked" }));
    const inFlight = deferred<{ ok: boolean }>();
    mockSetStatus.mockReturnValue(inFlight.promise);

    const button = (await mountAndClickLock()) as HTMLButtonElement;

    expect(button.textContent).toContain("UNLOCKING…");

    mockFetchPlan.mockResolvedValue(plan({ status: "draft" }));
    await act(async () => {
      inFlight.settle({ ok: true });
    });
  });

  test("the control comes back after a refusal, so it can be retried", async () => {
    const inFlight = deferred<{ ok: boolean }>();
    mockSetStatus.mockReturnValue(inFlight.promise);

    await mountAndClickLock();

    await act(async () => {
      inFlight.fail(refusal(400, "nothing was locked", { problems: ["s001: no plan"] }));
    });

    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: no plan");
    expect((screen.getByTestId("lock-toggle") as HTMLButtonElement).disabled).toBe(false);
  });
});

// --- §11.4: the status shown is the server's ---------------------------------

describe("what is claimed after the click is what the server saved", () => {
  test("a lock that took reads the plan back, and says what it reads", async () => {
    mockFetchPlan
      .mockResolvedValueOnce(plan())
      .mockResolvedValueOnce(plan({ status: "locked", approved_signature: "ab12cd34" }));

    await mountAndClickLock();

    expect(screen.getByTestId("lock-done").textContent).toContain("s001 is locked");
    expect(screen.getByTestId("lock-done").textContent).toContain('reads "locked"');
    expect(screen.queryByTestId("lock-problem")).toBeNull();
    // The badge is the refetched plan's, not a status written here from the
    // fact that a promise resolved.
    expect(screen.getByText(/Status: locked/)).toBeTruthy();
  });

  test("the refetched plan carries the signature the compile gate has to send", async () => {
    // Locking is what mints `approved_signature`. Written locally it stayed
    // empty, so the very next click — COMPILE — was refused as unsigned.
    mockFetchPlan
      .mockResolvedValueOnce(plan({ approved_signature: "" }))
      .mockResolvedValueOnce(plan({ status: "locked", approved_signature: "ab12cd34ef567890" }));

    await mountAndClickLock();

    fireEvent.click(screen.getByTestId("compile-open-gate"));
    await act(async () => {});
    expect(screen.getByTestId("compile-cost-gate")).toBeTruthy();
    expect(mockFetchPlan).toHaveBeenCalledTimes(2);
  });

  test("accepted, but the saved plan still says draft — that is not a success", async () => {
    mockFetchPlan
      .mockResolvedValueOnce(plan())
      .mockResolvedValueOnce(plan({ status: "draft" }));

    await mountAndClickLock();

    expect(screen.queryByTestId("lock-done")).toBeNull();
    expect(screen.getByTestId("lock-problem").textContent)
      .toContain('the saved plan still reads "draft"');
  });

  test("an unlock that took says the plan can be edited again", async () => {
    mockFetchPlan
      .mockResolvedValueOnce(plan({ status: "locked" }))
      .mockResolvedValueOnce(plan({ status: "draft" }));

    await mountAndClickLock();

    expect(screen.getByTestId("lock-done").textContent).toContain("s001 is unlocked");
    expect(screen.getByTestId("lock-done").textContent).toContain("can be edited again");
  });

  test("a lock that took, whose read-back failed, is not reported as a refusal", async () => {
    // The opposite lie to a false success, and just as bad: the plan IS locked,
    // and telling the human it was refused sends them to fix nothing.
    mockFetchPlan
      .mockResolvedValueOnce(plan())
      .mockRejectedValueOnce(new Error("scene endpoint returned 503"));

    await mountAndClickLock();

    const problem = screen.getByTestId("lock-problem");
    expect(problem.textContent).toContain("s001 was locked");
    expect(problem.textContent).toContain("could not read the plan back");
    expect(problem.textContent).toContain("scene endpoint returned 503");
    expect(screen.queryByTestId("lock-done")).toBeNull();
  });

  test("a compiled plan unlocks; it does not try to lock again", async () => {
    mockFetchPlan
      .mockResolvedValueOnce(plan({ status: "compiled" }))
      .mockResolvedValueOnce(plan({ status: "draft" }));

    await mountAndClickLock();

    expect(mockSetStatus.mock.calls[0][1]).toBe(false);
  });

  test("a multi-beat scene locks every beat it covers", async () => {
    mockFetchPlan.mockResolvedValue(plan({ scene_beats: ["s001", "s002"] }));

    await mountAndClickLock();

    expect(mockSetStatus.mock.calls[0][0]).toEqual(["s001", "s002"]);
  });
});

// --- the message belongs to the scene it happened on -------------------------

describe("the outcome does not follow the human to the next scene", () => {
  test("a refusal about s001 is gone when s002 is opened", async () => {
    mockSetStatus.mockRejectedValue(
      refusal(400, "nothing was locked", { problems: ["s001: no plan"] })
    );
    const { rerender } = render(
      <DirectorWorkspace sceneId="s001" activeProjectTitle="Heney" mediaUrl={(p) => p} />
    );
    fireEvent.click(await screen.findByTestId("lock-toggle"));
    await act(async () => {});
    expect(document.body.textContent, REFUSAL_NEVER_REACHED_THE_HUMAN)
      .toContain("s001: no plan");

    mockFetchPlan.mockResolvedValue(plan({ scene_id: "s002", scene_beats: ["s002"] }));
    rerender(
      <DirectorWorkspace sceneId="s002" activeProjectTitle="Heney" mediaUrl={(p) => p} />
    );
    await act(async () => {});

    expect(screen.queryByTestId("lock-problem")).toBeNull();
  });
});
