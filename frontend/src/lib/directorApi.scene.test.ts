/**
 * The scene read model quotes the server's summary, or it quotes nothing.
 *
 * Everything downstream of `fetchCoveragePlan` — the compile gate's price, the
 * montage matrix's row — is only as honest as this mapping. And the component
 * suites mock this function outright, so a defect here is invisible to every one
 * of them: `DirectorWorkspace.nomock.test.tsx` can prove the panel renders
 * whatever cost it is handed, and nothing about where that cost came from.
 *
 * The specific hole: `data.summary?.estimated_cost || plan.estimated_cost || 0`.
 * A scene of nothing but parallax and static shots costs 0.00, which is FALSY —
 * so `||` discarded the server's own answer and fell through to the plan's
 * stored `estimated_cost`, a number computed when the plan was written rather
 * than for the beats being asked about. A free scene could be quoted at a stale
 * non-zero price, in the gate where the human commits to spending.
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import { fetchCoveragePlan } from "./directorApi";

/** One canned reply from GET /api/director/scene. */
function reply(body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "200",
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response);
}

/** The endpoint's real shape: per-beat entries plus a scene-wide summary. */
function sceneReply(
  planFields: Record<string, unknown>,
  summary: Record<string, unknown> | undefined
) {
  return reply({
    ok: true,
    beats: [{
      beat_id: "s001",
      beat_duration: 28.2,
      coverage_total: 28.2,
      plan: {
        plan_id: "plan_s001",
        status: "locked",
        beat_duration: 28.2,
        scene_beats: ["s001"],
        coverage: [],
        warnings: [],
        ...planFields,
      },
    }],
    summary,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("estimated_cost comes from the summary, or from nothing", () => {
  test("a free scene is quoted at zero, not at the plan's stale figure", async () => {
    // THE DEFECT. `summary.estimated_cost` is 0 — a real answer for an
    // all-parallax scene — and `plan.estimated_cost` is a leftover 3.82.
    // Under `||` the human was quoted $3.82 to compile something that is free.
    vi.stubGlobal("fetch", sceneReply(
      { estimated_cost: 3.82 },
      { shots: 9, paid_shots: 0, estimated_cost: 0 }
    ));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.estimated_cost).toBe(0);
  });

  test("the summary wins over the plan's own figure in general", async () => {
    vi.stubGlobal("fetch", sceneReply(
      { estimated_cost: 3.82 },
      { shots: 9, paid_shots: 2, estimated_cost: 1.85 }
    ));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.estimated_cost).toBe(1.85);
  });

  test("with no summary at all it falls back, rather than inventing a number", async () => {
    vi.stubGlobal("fetch", sceneReply({ estimated_cost: 1.85 }, undefined));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.estimated_cost).toBe(1.85);
  });

  test("zero paid shots is a count, not a missing one", async () => {
    // Same falsy trap one field along, which is why `paid_shots` was written
    // with `??` from the start. Pinned so it cannot regress to `||`.
    vi.stubGlobal("fetch", sceneReply(
      { coverage: [{ id: "s001.01", motion_type: "ai_video" }] },
      { shots: 1, paid_shots: 0, estimated_cost: 0 }
    ));

    const plan = await fetchCoveragePlan("s001");

    // The server says none of these beats buys video. Counting the coverage
    // ourselves would contradict it — and would quote the human for a purchase
    // the server does not think is happening.
    expect(plan.paid_shots).toBe(0);
  });

  test("without a summary the paid count falls back to the plan's own shots", async () => {
    vi.stubGlobal("fetch", sceneReply(
      { coverage: [
        { id: "s001.01", motion_type: "ai_video" },
        { id: "s001.02", motion_type: "parallax" },
      ] },
      undefined
    ));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.paid_shots).toBe(1);
  });
});

describe("the approved signature is carried, so a compile can name its plan", () => {
  test("a locked plan's signature survives the mapping", async () => {
    vi.stubGlobal("fetch", sceneReply(
      { approved_signature: "ab12cd34ef567890" },
      { shots: 0, paid_shots: 0, estimated_cost: 0 }
    ));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.approved_signature).toBe("ab12cd34ef567890");
  });

  test("an unapproved plan carries an empty signature, never undefined", async () => {
    // `compileCoverage` omits the query parameter on an empty string. Leaving
    // this undefined would work by accident today and break the moment someone
    // reads it as a boolean.
    vi.stubGlobal("fetch", sceneReply({}, { shots: 0, paid_shots: 0, estimated_cost: 0 }));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.approved_signature).toBe("");
  });
});
