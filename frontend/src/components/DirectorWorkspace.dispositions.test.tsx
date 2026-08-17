/**
 * A finding the human resolved stays resolved when the scene is re-read.
 *
 * The decision is durable on the server: `POST /api/director/warning/...` writes
 * `warning_dispositions` into the saved plan, `GET /api/director/scene` returns
 * it, and `director.unresolved_warnings` enforces it at lock time. The screen
 * still forgot it, because `fetchCoveragePlan` rebuilt the plan field by field
 * and never copied that key — so the six findings a human had resolved on s001
 * came back every time they switched scene and switched back.
 *
 * That is why nothing here is mocked below the network. `DirectorWorkspace`'s
 * other suites replace `fetchCoveragePlan` outright and hand the component a
 * plan object that already carries its dispositions; every one of them passes
 * with the defect in place, because the defect IS that mapping. `global.fetch`
 * is stubbed instead, with a small server that remembers decisions the way the
 * real one does, and the component reads it through the real mapper.
 *
 * The assertion that proves the defect is about what the human sees — the
 * findings awaiting a decision, after a refetch — and it runs first. A test that
 * asserted only "the mapper copies the key" would pass while the filter that
 * consumes it was broken, and a test that asserted only "the key survives" would
 * pass while nothing on screen read it.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import DirectorWorkspace from "./DirectorWorkspace";
import type { DirectorWarning, WarningDisposition } from "../types/director";

// --- the server, as far as this screen can tell ------------------------------

const FINDING: DirectorWarning = {
  id: "5a6b36245735",
  shot_id: "s001.01",
  kind: "identity_risk",
  detail: "s001.01 holds the face for 28.2s — the drift risk is the whole beat.",
};

/** One beat's persisted state: its findings and the decisions taken about them. */
interface StoredPlan {
  warnings: DirectorWarning[];
  warning_dispositions: Record<string, WarningDisposition>;
}

let stored: Record<string, StoredPlan>;
let sceneReads: string[];

/** Every key `asdict(CoveragePlan)` puts on the wire, so the mapper sees the
 *  real shape rather than the subset this test happens to care about. */
function wirePlan(beatId: string) {
  return {
    beat_id: beatId,
    beat_duration: 28.2,
    version: 3,
    plan_id: `plan_${beatId}`,
    scene_beats: [beatId],
    status: "draft",
    profile: "historical_docudrama",
    created_by: "planner",
    coverage: [
      {
        id: `${beatId}.01`,
        beat_id: beatId,
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
      },
    ],
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

function json(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: "200",
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

/**
 * `GET /api/director/scene` and `POST /api/director/warning/...`, for real.
 *
 * Anything else throws rather than resolving: a screen that quietly reached for
 * another endpoint would otherwise pass this test on a fabricated reply.
 */
function studioServer() {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/api/director/scene")) {
      const beats = decodeURIComponent(
        (url.split("beats=")[1] || "").split("&")[0]
      ).split(",");
      sceneReads.push(beats.join(","));
      return json({
        ok: true,
        beats: beats.map((bid) => ({
          beat_id: bid,
          beat_duration: 28.2,
          coverage_total: 28.2,
          plan: wirePlan(bid),
        })),
        summary: { shots: 1, paid_shots: 0, estimated_cost: 0.15 },
        tier: "all",
      });
    }
    if (url.includes("/api/director/warning/")) {
      const [beatId, warningId] = url.split("/api/director/warning/")[1].split("/");
      const { decision, note } = JSON.parse(String(init?.body || "{}"));
      const plan = stored[beatId];
      if (decision) {
        plan.warning_dispositions[warningId] = { decision, note: note || "", by: "human" };
      } else {
        delete plan.warning_dispositions[warningId];
      }
      return json({
        ok: true,
        warnings: plan.warnings,
        warning_dispositions: plan.warning_dispositions,
        unresolved: plan.warnings.filter(
          (w) => !(w.id && plan.warning_dispositions[w.id])
        ).length,
      });
    }
    throw new Error(`unexpected request: ${url}`);
  });
}

// --- driving the screen ------------------------------------------------------

function workspace(sceneId: string) {
  return (
    <DirectorWorkspace sceneId={sceneId} activeProjectTitle="Heney" mediaUrl={(x) => x} />
  );
}

/** Wait out the load the scene effect starts; the spinner replaces everything. */
async function settled() {
  await waitFor(() => expect(screen.queryByText(/Querying GET/)).toBeNull());
}

/** Mount on s001 and wait for the plan to land. */
async function openScene() {
  const view = render(workspace("s001"));
  await settled();
  return view;
}

/** Open the Problem Queue and record a decision about the only finding. */
async function markResolved() {
  fireEvent.click(screen.getByText("Review Problems"));
  fireEvent.click(screen.getByText("Mark resolved"));
  await screen.findByText("Marked resolved");
}

/** Leave for another scene and come back — the refetch the human performs. */
async function switchTo(rerender: (ui: React.ReactElement) => void, sceneId: string) {
  rerender(workspace(sceneId));
  await settled();
}

/** How many findings the screen says are awaiting a decision. */
function awaitingDecision(): string {
  return screen.queryByTestId("unresolved-count")?.textContent?.trim() || "none";
}

beforeEach(() => {
  stored = {
    s001: { warnings: [FINDING], warning_dispositions: {} },
    s002: { warnings: [], warning_dispositions: {} },
  };
  sceneReads = [];
  vi.stubGlobal("fetch", studioServer());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// --- the defect --------------------------------------------------------------

describe("a resolved finding survives a scene switch", () => {
  test("it is not raised again when the scene is re-read", async () => {
    const { rerender } = await openScene();
    expect(awaitingDecision()).toContain("1 coverage issue");

    await markResolved();
    // The decision reached the server, which is the half that already worked.
    expect(stored.s001.warning_dispositions[FINDING.id as string]?.decision).toBe(
      "resolved"
    );

    await switchTo(rerender, "s002");
    await switchTo(rerender, "s001");

    // THE DEFECT, and the first assertion. `fetchCoveragePlan` dropped
    // `warning_dispositions`, so the filter behind this banner had nothing to
    // filter with and every decided finding came back demanding a decision.
    expect(
      awaitingDecision(),
      "a finding the human resolved is being raised again after a scene switch"
    ).toBe("none");

    // And the finding is still THERE, with the decision attached — the record of
    // the review having happened. Resolving does not hide it, so a screen with
    // nothing on it could not satisfy the assertion above by accident.
    expect(screen.getByText(FINDING.detail)).toBeTruthy();
    expect(screen.getByText("Marked resolved")).toBeTruthy();
    expect(screen.queryByText("Mark resolved")).toBeNull();

    // The scene really was re-read; the assertions above are about a fresh plan
    // from the server, not the state left over from the click.
    expect(sceneReads).toEqual(["s001", "s002", "s001"]);
  });

  test("a finding nobody decided still blocks, after the same switch", async () => {
    // The control. Without it, a mapper that dropped `warnings` as well — or a
    // banner that had simply stopped rendering — would pass the test above.
    const { rerender } = await openScene();
    await switchTo(rerender, "s002");
    await switchTo(rerender, "s001");

    expect(awaitingDecision()).toContain("1 coverage issue");
    fireEvent.click(screen.getByText("Review Problems"));
    expect(screen.getByText("Mark resolved")).toBeTruthy();
  });

  test("undoing a decision brings the finding back, across a re-read too", async () => {
    // A disposition is a record, not a delete: reopening one has to survive the
    // round trip in the same way resolving it does.
    const { rerender } = await openScene();
    await markResolved();
    fireEvent.click(screen.getByText("undo"));
    await screen.findByText("Mark resolved");

    await switchTo(rerender, "s002");
    await switchTo(rerender, "s001");

    expect(awaitingDecision()).toContain("1 coverage issue");
    expect(stored.s001.warning_dispositions[FINDING.id as string]).toBeUndefined();
  });

  test("a decision taken on one beat is not read onto another", async () => {
    // Dispositions are keyed by a content-derived warning id and one read
    // returns one beat's plan. Switching scene must bring the other beat's
    // record, not keep the one already on screen.
    stored.s002 = {
      warnings: [{ ...FINDING, beat_id: "s002", shot_id: "s002.01" }],
      warning_dispositions: {},
    };
    const { rerender } = await openScene();
    await markResolved();

    await switchTo(rerender, "s002");

    // s002's own finding was never decided, and s001's decision says nothing
    // about it.
    expect(awaitingDecision()).toContain("1 coverage issue");
  });
});
