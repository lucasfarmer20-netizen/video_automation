/**
 * The problem queue counts the scene, because the lock gates on the scene.
 *
 * A human planned s004, s005 and s006 as ONE scene — `scene_beats:
 * ['s004','s005','s006']` on all three plans — tried to lock it, got a 400, and
 * the studio told them:
 *
 *     "No finding in this scene is awaiting a decision. Re-check to ask the
 *      critic to look again before locking."
 *
 * Measured from the deployed API at that moment: s004 held 1 finding and 0
 * undecided, s005 held 3 undecided, s006 held 1 undecided. Four findings were
 * refusing the lock. The queue had counted the beat that happened to be selected
 * and then used the word "scene" to describe what it had counted.
 *
 * That is worse than silence. It stands in front of a gate and sends the human
 * off to look for the wrong problem — and a separate defect had already taught
 * them that a failed lock says nothing, so this was the replacement for that
 * silence: a confident wrong answer. Contract §11.4.
 *
 * `POST /api/director/lock_scene` accumulates a `problems[]` entry per beat in
 * `beats[]`, so the queue's scope and the lock's scope have to be the same
 * scope. THE DEFECT-PROVING ASSERTION IN EACH TEST BELOW IS ABOUT THE SENTENCE
 * ON SCREEN, and it runs first — not about a count a helper returned. The defect
 * was that the screen made a claim about a scope it had not examined, and a test
 * that asserted `undecidedFindings.length === 4` would pass while the sentence
 * beside it went on saying the opposite.
 *
 * Nothing is mocked below the network, for the reason the dispositions suite
 * gives: the other DirectorWorkspace suites hand the component a plan object
 * outright, and the defect lives partly in `fetchCoveragePlan`, which reads one
 * beat and returns a plan that speaks for a scene.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import DirectorWorkspace from "./DirectorWorkspace";
import type { DirectorWarning, WarningDisposition } from "../types/director";

/**
 * The sentence the studio told the human, and every re-scoped variant of it.
 *
 * Matched as a pattern rather than as the literal string, because the defect is
 * the CLAIM, not its wording: "No finding in this scene…", "No finding in s004…"
 * and anything else of that shape are all a screen saying nothing is
 * outstanding, and none of them may appear while something is.
 */
const NOTHING_IS_OUTSTANDING = /No finding in [^.]+ is awaiting a decision/;
/** The exact line the human was shown. */
const THE_FALSE_CLAIM = "No finding in this scene is awaiting a decision";

const FOUR_FINDINGS_BLOCKED_THE_LOCK =
  "the studio told the human nothing in this scene was awaiting a decision " +
  "while four undecided findings on s005 and s006 were refusing the lock";

// --- the server, as far as this screen can tell ------------------------------

interface StoredPlan {
  scene_beats: string[];
  warnings: DirectorWarning[];
  warning_dispositions: Record<string, WarningDisposition>;
}

let stored: Record<string, StoredPlan>;
let sceneReads: string[];
let decisionWrites: string[];
/** Beats whose scene read fails, so "unread" can be told from "clean". */
let unreadable: Set<string>;
/** Beats whose disposition write fails, so a part-written decision can be driven. */
let writeRefuses: Set<string>;

function warning(over: Partial<DirectorWarning> & { id: string; detail: string }): DirectorWarning {
  return { kind: "repeated_framing", ...over };
}

/** The human's real scene: one decided finding, then three, then the cross-beat one. */
function theirScene(): Record<string, StoredPlan> {
  const beats = ["s004", "s005", "s006"];
  return {
    s004: {
      scene_beats: beats,
      warnings: [
        warning({
          id: "w004a",
          beat_id: "s004",
          shot_id: "s004.02",
          kind: "identity_risk",
          detail: "s004.02 holds the face for 9.4s.",
        }),
      ],
      // Decided — which is the whole reason the old queue thought it was clear.
      warning_dispositions: { w004a: { decision: "accepted", note: "deliberate" } },
    },
    s005: {
      scene_beats: beats,
      warnings: [
        warning({ id: "w005a", beat_id: "s005", shot_id: "s005.01", detail: "s005.01 repeats s005.03." }),
        warning({
          id: "w005b",
          beat_id: "s005",
          shot_id: "s005.04",
          kind: "insufficient_cutaway",
          detail: "no cutaway across 31.0s of s005.",
        }),
        warning({
          id: "w005c",
          beat_id: "s005",
          shot_id: "s005.06",
          kind: "impractical_duration",
          detail: "s005.06 is a paid shot of 2.1s no model can produce.",
        }),
      ],
      warning_dispositions: {},
    },
    s006: {
      scene_beats: beats,
      warnings: [
        // The finding that belongs to the SCENE and exists only because these
        // beats were planned together.
        warning({
          id: "w006a",
          beat_id: "s006",
          shot_id: "s006.01",
          detail: "s004.06 and s006.01 are the same subject at the same shot size.",
        }),
      ],
      warning_dispositions: {},
    },
  };
}

function shotOf(beatId: string, n: number) {
  return {
    id: `${beatId}.0${n}`,
    beat_id: beatId,
    tier: 1,
    purpose: "master",
    subject: "the mill yard",
    shot_size: "mw",
    angle: "front",
    camera: { move: "push", duration: 14.1, speed: 1, amount: 4 },
    identity_critical: false,
    motion_type: "parallax",
    backend: "nano2",
    prompt: "…",
    motion_prompt: "…",
    draft_variations: [],
    estimated_cost: 0.04,
  };
}

/** Every key `asdict(CoveragePlan)` puts on the wire, so the mapper sees the real shape. */
function wirePlan(beatId: string) {
  return {
    beat_id: beatId,
    beat_duration: 28.2,
    version: 3,
    plan_id: `plan_${beatId}`,
    scene_beats: stored[beatId].scene_beats,
    status: "draft",
    profile: "historical_docudrama",
    created_by: "planner",
    coverage: [shotOf(beatId, 1), shotOf(beatId, 2)],
    warnings: stored[beatId].warnings,
    warning_dispositions: stored[beatId].warning_dispositions,
    beat_signature: "beatsig0000000000",
    approved_signature: "",
    approved_at: "",
    approved_by: "",
    approval_history: [],
    compiled: {},
    visual_strategy: "chiaroscuro, shadow-play reveal",
    blocking: { environment: "the mill yard at dusk" },
  };
}

function json(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    statusText: String(status),
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

/**
 * `GET /api/director/scene` and `POST /api/director/warning/...`, for real.
 *
 * Anything else throws rather than resolving, so a screen that quietly reached
 * for another endpoint cannot pass on a fabricated reply.
 */
function studioServer() {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/api/director/scene")) {
      const beats = decodeURIComponent((url.split("beats=")[1] || "").split("&")[0]).split(",");
      sceneReads.push(beats.join(","));
      if (beats.some((b) => unreadable.has(b))) {
        return json({ ok: false, error: "director scene read failed" }, 500);
      }
      return json({
        ok: true,
        beats: beats.map((bid) => ({
          beat_id: bid,
          beat_duration: 28.2,
          coverage_total: 28.2,
          plan: stored[bid] ? wirePlan(bid) : null,
        })),
        summary: { shots: 2, paid_shots: 0, estimated_cost: 0.08 },
        tier: "all",
      });
    }
    if (url.includes("/api/director/lock_scene")) {
      // The route's own refusal: it accumulates one `problems[]` entry per beat
      // in `beats[]` and locks none of them if any beat objects.
      const { beats } = JSON.parse(String(init?.body || "{}"));
      const problems = (beats as string[])
        .map((bid) => {
          const plan = stored[bid];
          if (!plan) return `${bid}: no plan`;
          const undecided = plan.warnings.filter(
            (w) => !(w.id && plan.warning_dispositions[w.id]?.decision)
          );
          return undecided.length
            ? `${bid}: ${undecided.length} critic warning(s) awaiting a decision ` +
              `(${undecided.map((w) => w.id).join(", ")})`
            : "";
        })
        .filter(Boolean);
      if (problems.length > 0) {
        return json({ ok: false, error: "nothing was locked", problems }, 400);
      }
      return json({ ok: true, locked: beats, estimated_cost: 0.08 });
    }
    if (url.includes("/api/director/shot/")) {
      const shotId = url.split("/api/director/shot/")[1];
      const patch = JSON.parse(String(init?.body || "{}"));
      const [bid, n] = shotId.split(".");
      return json({
        ok: true,
        shot: { ...shotOf(bid, Number(n)), ...patch },
        notes: [],
        problems: [],
        coverage_total: 28.2,
        beat_duration: 28.2,
      });
    }
    if (url.includes("/api/director/warning/")) {
      const [beatId, warningId] = url.split("/api/director/warning/")[1].split("/");
      const { decision, note } = JSON.parse(String(init?.body || "{}"));
      decisionWrites.push(`${beatId}/${warningId}/${decision}`);
      if (writeRefuses.has(beatId)) {
        return json({ ok: false, error: `${beatId} is compiling; cannot change its review state` }, 409);
      }
      const plan = stored[beatId];
      if (!plan || !plan.warnings.some((w) => w.id === warningId)) {
        return json({ ok: false, error: `${beatId}: no warning ${warningId} on this plan` }, 400);
      }
      if (decision) {
        plan.warning_dispositions[warningId] = { decision, note: note || "", by: "human" };
      } else {
        delete plan.warning_dispositions[warningId];
      }
      return json({
        ok: true,
        warnings: plan.warnings,
        warning_dispositions: plan.warning_dispositions,
        unresolved: plan.warnings.filter((w) => !(w.id && plan.warning_dispositions[w.id])).length,
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
}

// --- driving the screen ------------------------------------------------------

/** Mount on a beat and wait out the load; the spinner replaces everything. */
async function openOn(beatId: string) {
  render(<DirectorWorkspace sceneId={beatId} activeProjectTitle="Heney" mediaUrl={(x) => x} />);
  await waitFor(() => expect(screen.queryByText(/Querying GET/)).toBeNull());
  // Precondition, not an assertion about the defect: the workspace really did
  // render, so the absence-of-a-sentence checks below cannot pass on an empty
  // screen.
  await screen.findByText(/COVERAGE REVIEW SURFACE/);
}

function openQueue() {
  fireEvent.click(screen.getByText("Review Problems"));
}

/** The finding card carrying this text, as the human reads it. */
function cardFor(detail: string): HTMLElement {
  const cards = screen.getAllByTestId("queue-finding");
  const found = cards.find((c) => (c.textContent || "").includes(detail));
  if (!found) throw new Error(`no finding card mentions: ${detail}`);
  return found;
}

/** Click a decision button inside one card, and wait for the write to land. */
async function decide(detail: string, label: string) {
  const card = cardFor(detail);
  const button = Array.from(card.querySelectorAll("button")).find(
    (b) => (b.textContent || "").trim() === label
  );
  if (!button) throw new Error(`no "${label}" button on the card for: ${detail}`);
  fireEvent.click(button);
  await waitFor(() => expect(cardFor(detail).textContent).not.toContain(label));
}

beforeEach(() => {
  stored = theirScene();
  sceneReads = [];
  decisionWrites = [];
  unreadable = new Set();
  writeRefuses = new Set();
  vi.stubGlobal("fetch", studioServer());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// --- the defect --------------------------------------------------------------

describe("a scene with undecided findings on a beat nobody is looking at", () => {
  test("it does not report itself clear", async () => {
    await openOn("s004");

    // THE DEFECT, and the first assertion. Every finding on s004 — the selected
    // beat — is decided, so a queue scoped to s004 rendered exactly this line
    // while s005 and s006 held four findings that were refusing the lock. It is
    // asserted as a pattern so that re-scoping the sentence without re-scoping
    // the count cannot satisfy it: no wording of "nothing is outstanding" is
    // allowed on screen while four findings are.
    expect(document.body.textContent || "", FOUR_FINDINGS_BLOCKED_THE_LOCK).not.toMatch(
      NOTHING_IS_OUTSTANDING
    );
    expect(document.body.textContent || "", FOUR_FINDINGS_BLOCKED_THE_LOCK).not.toContain(
      THE_FALSE_CLAIM
    );

    // And it says the true thing instead, so the assertion above cannot be
    // satisfied by a screen that renders nothing at all.
    expect(screen.getByTestId("unresolved-count").textContent).toContain(
      "4 coverage issues awaiting a decision in this scene"
    );
  });

  test("it says which beats they are on, because the lock checks all of them", async () => {
    await openOn("s004");

    const breakdown = screen.getByTestId("unresolved-by-beat").textContent || "";
    expect(breakdown).toContain("s005 (3)");
    expect(breakdown).toContain("s006 (1)");
    // s004's one finding is decided, so it contributes nothing and is not listed.
    expect(breakdown).not.toContain("s004");
    expect(breakdown).toContain("the lock checks every beat in this scene");
  });

  test("the findings themselves are in the queue, not just their count", async () => {
    await openOn("s004");
    openQueue();

    // The scope the human can ACT in. A count they cannot act on would send them
    // hunting through beats for findings the screen already knows about.
    expect(cardFor("no cutaway across 31.0s of s005")).toBeTruthy();
    expect(
      cardFor("s004.06 and s006.01 are the same subject at the same shot size")
    ).toBeTruthy();
    expect(screen.getByTestId("findings-elsewhere").textContent).toContain(
      "4 of them are on other beats in this scene"
    );
  });

  test("the cross-beat finding is tagged with the beat that carries it", async () => {
    // It names s004.06 and sits on s006, and it exists only because the beats
    // were planned together. A per-beat queue has nowhere correct to put it.
    await openOn("s004");
    openQueue();

    const card = cardFor("s004.06 and s006.01 are the same subject");
    expect(card.getAttribute("data-beats")).toBe("s006");
    expect(card.textContent).toContain("s006");
    // s006.01 has no card on this screen — s004's coverage is what is loaded —
    // so the shot is named without offering a jump that goes nowhere.
    expect(card.textContent).toContain("Shot s006.01");
    expect(
      Array.from(card.querySelectorAll("button")).map((b) => (b.textContent || "").trim())
    ).not.toContain("Shot s006.01");
  });
});

// --- deciding them -----------------------------------------------------------

describe("a decision is written to the beat that carries the finding", () => {
  test("a sibling beat's finding is recorded against that beat, not the open one", async () => {
    await openOn("s004");
    openQueue();

    await decide("no cutaway across 31.0s of s005", "Mark resolved");

    // The old handler sent every decision to `sceneId`. The route answers
    // `s004: no warning w005b on this plan` — a decision that never lands, on a
    // finding that goes on refusing the lock.
    expect(decisionWrites).toEqual(["s005/w005b/resolved"]);
    expect(stored.s005.warning_dispositions.w005b?.decision).toBe("resolved");
    expect(stored.s004.warning_dispositions.w005b).toBeUndefined();
  });

  test("once every beat's findings are decided, the scene says so and names them", async () => {
    await openOn("s004");
    openQueue();

    await decide("s005.01 repeats s005.03", "Mark resolved");
    await decide("no cutaway across 31.0s of s005", "Keep as-is");
    await decide("s005.06 is a paid shot of 2.1s", "Mark resolved");
    await decide("s004.06 and s006.01 are the same subject", "Keep as-is");

    const outcome = await screen.findByTestId("critique-outcome");
    // It may now say nothing is outstanding, because now nothing is — and it
    // names the beats it counted rather than the word "scene".
    expect(outcome.textContent).toContain(
      "No finding in s004, s005, s006 is awaiting a decision"
    );
    expect(outcome.textContent).toContain("that is every beat in this scene");
    expect(screen.queryByTestId("unresolved-count")).toBeNull();
  });
});

// --- the finding the critic filed against the scene itself -------------------

describe("a finding the critic filed against no single beat", () => {
  /** `critique` stores a `beat_id: ""` finding onto EVERY beat it was asked about. */
  function sceneWideFinding() {
    const shared = warning({
      id: "wscene",
      beat_id: "",
      shot_id: "s006.01",
      detail: "s004.06 and s006.01 are the same subject at the same shot size.",
    });
    for (const bid of ["s004", "s005", "s006"]) {
      stored[bid].warnings = [shared];
      stored[bid].warning_dispositions = {};
    }
  }

  test("it is listed once, not once per beat, and shows the beats it spans", async () => {
    sceneWideFinding();
    await openOn("s004");
    openQueue();

    expect(screen.getAllByTestId("queue-finding")).toHaveLength(1);
    const card = cardFor("s004.06 and s006.01 are the same subject");
    expect(card.getAttribute("data-beats")).toBe("s004,s005,s006");
    expect(card.textContent).toContain("s004 + s005 + s006 (scene)");
    expect(screen.getByTestId("unresolved-count").textContent).toContain("1 coverage issue");
  });

  test("deciding it reaches every beat holding a copy — the lock checks each one", async () => {
    sceneWideFinding();
    await openOn("s004");
    openQueue();

    await decide("s004.06 and s006.01 are the same subject", "Keep as-is");

    expect(decisionWrites).toEqual([
      "s004/wscene/accepted",
      "s005/wscene/accepted",
      "s006/wscene/accepted",
    ]);
    // Deciding only the open beat's copy would leave s005 and s006 refusing the
    // lock, with the queue showing the finding as settled.
    expect(stored.s006.warning_dispositions.wscene?.decision).toBe("accepted");
  });

  test("a decision that reaches only some of them leaves the finding undecided, and says so", async () => {
    sceneWideFinding();
    // s006 went into a compile between the read and the click, so the route
    // refuses that one write: 409, "cannot change its review state".
    writeRefuses = new Set(["s006"]);
    await openOn("s004");
    openQueue();

    const card = cardFor("s004.06 and s006.01 are the same subject");
    fireEvent.click(
      Array.from(card.querySelectorAll("button")).find(
        (b) => (b.textContent || "").trim() === "Keep as-is"
      )!
    );

    // The write partly landed. Congratulating the human here — or showing the
    // finding as settled — is the same class of wrong answer as the sentence
    // this suite exists for, pointing the other way.
    const problem = await screen.findByTestId("finding-problem");
    expect(problem.textContent).toContain("recorded on s004, s005 but not on s006");
    expect(problem.textContent).toContain("the scene will still refuse to lock");
    expect(problem.textContent).toContain("cannot change its review state");
    expect(screen.getByTestId("unresolved-count").textContent).toContain("1 coverage issue");
    // …and the two that DID take it are recorded, because they did.
    expect(stored.s004.warning_dispositions.wscene?.decision).toBe("accepted");
    expect(stored.s006.warning_dispositions.wscene).toBeUndefined();
  });
});

// --- the gate itself ---------------------------------------------------------

describe("the lock refusal and the queue agree about what is blocking", () => {
  test("the count beside the refusal is the scene's, matching what the route named", async () => {
    // The moment the human was actually in: they clicked lock, the route refused
    // over s005 and s006, and the screen beside that refusal was counting s004.
    await openOn("s004");
    fireEvent.click(screen.getByTestId("lock-toggle"));

    const note = await screen.findByTestId("lock-undecided-note");
    expect(note.textContent).toContain("This scene carries 4 findings with no recorded decision");
    expect(note.textContent).toContain("s005: 3, s006: 1");

    // The server's own sentences, unchanged — and the client's count agrees with
    // them instead of contradicting them one line down.
    const body = document.body.textContent || "";
    expect(body).toContain("s005: 3 critic warning(s) awaiting a decision");
    expect(body).toContain("s006: 1 critic warning(s) awaiting a decision");
    expect(screen.getByTestId("lock-open-queue").textContent).toContain("Review 4 problems");
  });
});

// --- an edit under a finding -------------------------------------------------

describe("a shot edited out from under a finding", () => {
  test("the finding is flagged as predating the edit, in the scene's list", async () => {
    // Addendum 5.3. The flag used to be written onto `coveragePlan.warnings`,
    // which is the open beat's list; the banner reads the SCENE's list now, and
    // a flag written to only one of them would leave the human editing against
    // a critic verdict with nothing saying the verdict is older than the edit.
    stored.s004.warning_dispositions = {};
    await openOn("s004");
    openQueue();

    fireEvent.click(screen.getByText("Shot s004.02"));
    fireEvent.click(await screen.findByText("CU"));

    await waitFor(() =>
      expect(screen.getByTestId("unresolved-count").textContent).toContain(
        "Edits made — click Re-check"
      )
    );
  });
});

// --- the beat that could not be read -----------------------------------------

describe("a beat of the scene that could not be read", () => {
  test("the screen declines to speak for it rather than counting it clean", async () => {
    // Every finding on the beat we CAN read is decided, so the temptation to
    // call the scene clear is at its strongest exactly here.
    stored.s005.warning_dispositions = {
      w005a: { decision: "accepted" },
      w005b: { decision: "accepted" },
      w005c: { decision: "accepted" },
    };
    stored.s006.warning_dispositions = { w006a: { decision: "accepted" } };
    unreadable = new Set(["s005", "s006"]);

    await openOn("s004");

    expect(document.body.textContent, FOUR_FINDINGS_BLOCKED_THE_LOCK).not.toContain(
      THE_FALSE_CLAIM
    );
    const outcome = screen.getByTestId("critique-outcome");
    expect(outcome.textContent).toContain("No finding in s004 is awaiting a decision");
    expect(outcome.textContent).toContain("s005, s006 could not be read");
    expect(outcome.textContent).toContain("nothing here speaks for them");
    expect(outcome.textContent).toContain("Reopen the scene before locking");
  });
});

// --- the control: one beat is still one beat ---------------------------------

describe("a scene of a single beat", () => {
  function soloScene() {
    stored = {
      s004: {
        scene_beats: ["s004"],
        warnings: [
          warning({ id: "w004a", beat_id: "s004", shot_id: "s004.02", detail: "s004.02 repeats s004.01." }),
        ],
        warning_dispositions: {},
      },
    };
  }

  test("its one finding is counted once, and no sibling beat is read", async () => {
    soloScene();
    await openOn("s004");

    expect(screen.getByTestId("unresolved-count").textContent).toContain("1 coverage issue");
    // No breakdown: there is nothing to break down, and a line saying "s004 (1)"
    // beside "1 coverage issue" is noise.
    expect(screen.queryByTestId("unresolved-by-beat")).toBeNull();
    // One read. Widening the scope must not cost a request per mount on a scene
    // that has nowhere to widen to.
    expect(sceneReads).toEqual(["s004"]);
  });

  test("deciding it clears the scene, and the sentence names the beat it counted", async () => {
    soloScene();
    await openOn("s004");
    openQueue();

    await decide("s004.02 repeats s004.01", "Mark resolved");
    expect(decisionWrites).toEqual(["s004/w004a/resolved"]);

    const outcome = await screen.findByTestId("critique-outcome");
    expect(outcome.textContent).toContain("No finding in s004 is awaiting a decision");
    // A one-beat scene has no other beat to be wrong about, so it must not
    // acquire the multi-beat wording either.
    expect(outcome.textContent).not.toContain("every beat in this scene");
    expect(outcome.textContent).not.toContain("could not be read");
  });
});
