/**
 * The mapper carries what it says it carries.
 *
 * `fetchCoveragePlan` rebuilds the plan field by field out of the
 * `{beats: [{plan}]}` envelope. That normalisation is deliberate, but a
 * hand-written copy loses fields silently whenever either side grows one — and
 * it did: `warning_dispositions` was written durably by the backend, returned by
 * `GET /api/director/scene`, and never copied here, so every refetch re-raised
 * findings the human had already decided.
 *
 * `PLAN_FIELDS_CARRIED` and `PLAN_FIELDS_DROPPED` are the mapper's account of
 * the server's plan keys, and there are two guards on them, in two languages
 * because the two facts live on opposite sides:
 *
 *   - here: everything declared CARRIED actually arrives, so a name in that
 *     record cannot stand in for a line of code;
 *   - `tests/test_director_scene_mapping.py`: the two records account for every
 *     field of `director.CoveragePlan`, so a key the server grows fails until
 *     someone classifies it.
 *
 * Neither is a substitute for the behavioural test in
 * `DirectorWorkspace.dispositions.test.tsx`, which is about what the human sees
 * after a refetch. A field can be copied faithfully and still be read by nothing.
 */
import { afterEach, describe, expect, test, vi } from "vitest";
import {
  PLAN_FIELDS_CARRIED,
  PLAN_FIELDS_DROPPED,
  fetchCoveragePlan,
} from "./directorApi";
import type { DirectorCoveragePlan } from "../types/director";

/** A plan carrying a distinguishable value in every field the server sends. */
const WIRE_PLAN: Record<string, unknown> = {
  beat_id: "s001",
  beat_duration: 28.2,
  version: 3,
  plan_id: "plan_s001",
  scene_beats: ["s001", "s002"],
  status: "locked",
  profile: "historical_docudrama",
  created_by: "planner",
  coverage: [{ id: "s001.01", motion_type: "parallax" }],
  warnings: [{ id: "5a6b36245735", kind: "identity_risk", detail: "…" }],
  warning_dispositions: {
    "5a6b36245735": { decision: "resolved", note: "", by: "human" },
    "47b42e496da9": { decision: "accepted", note: "deliberate", by: "human" },
  },
  beat_signature: "beatsig0000000000",
  approved_signature: "ab12cd34ef567890",
  approved_at: "2026-08-16T10:00:00Z",
  approved_by: "human",
  approval_history: [{ signature: "0000000000000000" }],
  compiled: { beat_clip: "render/s001/s001.mp4", runtime: 28.2 },
  visual_strategy: "chiaroscuro, shadow-play reveal",
  blocking: { environment: "the mill yard at dusk" },
};

function sceneReply(plan: Record<string, unknown> = WIRE_PLAN) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "200",
    headers: new Headers(),
    json: async () => ({
      ok: true,
      beats: [{ beat_id: "s001", beat_duration: 28.2, coverage_total: 28.2, plan }],
      summary: { shots: 1, paid_shots: 0, estimated_cost: 0.15 },
    }),
  } as unknown as Response);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the human's decisions survive the mapping", () => {
  test("warning_dispositions arrives with the plan", async () => {
    vi.stubGlobal("fetch", sceneReply());

    const plan = await fetchCoveragePlan("s001");

    // THE DEFECT. The server sent both, and only `warnings` was copied — so the
    // findings came back and the record of deciding them did not.
    expect(plan.warning_dispositions).toEqual(WIRE_PLAN.warning_dispositions);
    expect(plan.warnings).toEqual(WIRE_PLAN.warnings);
  });

  test("a plan with no decisions maps to an empty record, never undefined", async () => {
    // Every reader treats "no entry" as undecided (§5.4). An absent map and an
    // empty one have to mean the same thing to them, or the first reader to
    // iterate it instead of indexing it throws on a plan nobody has reviewed.
    vi.stubGlobal("fetch", sceneReply({ ...WIRE_PLAN, warning_dispositions: undefined }));

    const plan = await fetchCoveragePlan("s001");

    expect(plan.warning_dispositions).toEqual({});
  });
});

describe("the mapper's account of the server's plan keys", () => {
  test("every field declared carried actually arrives", async () => {
    vi.stubGlobal("fetch", sceneReply());

    const plan = (await fetchCoveragePlan("s001")) as unknown as Record<string, unknown>;

    for (const [serverKey, mappedKey] of Object.entries(PLAN_FIELDS_CARRIED)) {
      expect(
        plan[mappedKey],
        `PLAN_FIELDS_CARRIED claims the server's "${serverKey}" reaches the plan ` +
          `as "${mappedKey}", and it does not — which is the shape of the ` +
          `warning_dispositions defect, one field along`
      ).toEqual(WIRE_PLAN[serverKey]);
    }
  });

  test("no key is both carried and dropped, and neither record is empty", async () => {
    const both = Object.keys(PLAN_FIELDS_CARRIED).filter(
      (k) => k in PLAN_FIELDS_DROPPED
    );
    expect(both).toEqual([]);
    expect(Object.keys(PLAN_FIELDS_CARRIED).length).toBeGreaterThan(0);
    expect(Object.keys(PLAN_FIELDS_DROPPED).length).toBeGreaterThan(0);
  });

  test("every dropped key states a reason, not just a name", async () => {
    // The record exists so an omission is a decision on the record. A blank
    // reason is an omission wearing the costume of one.
    for (const [key, why] of Object.entries(PLAN_FIELDS_DROPPED)) {
      expect(why.trim().length, `"${key}" is dropped without saying why`).toBeGreaterThan(20);
    }
  });

  test("the fixture covers every declared key, or these tests assert less than they read", async () => {
    for (const key of [
      ...Object.keys(PLAN_FIELDS_CARRIED),
      ...Object.keys(PLAN_FIELDS_DROPPED),
    ]) {
      expect(key in WIRE_PLAN, `WIRE_PLAN has no "${key}"`).toBe(true);
    }
  });
});

describe("the fields the mapper does not need are genuinely absent", () => {
  test("a dropped key is dropped, not accidentally spread in", async () => {
    // Not a demand that they stay dropped forever — it is what makes
    // PLAN_FIELDS_DROPPED true today. Carrying one means moving it to CARRIED,
    // where the test above then insists it arrives.
    vi.stubGlobal("fetch", sceneReply());

    const plan = (await fetchCoveragePlan("s001")) as unknown as Record<string, unknown>;

    for (const key of Object.keys(PLAN_FIELDS_DROPPED)) {
      expect(key in plan, `"${key}" is declared dropped but is on the plan`).toBe(false);
    }
  });

  test("the mapped plan is the studio's shape, not the server's", async () => {
    vi.stubGlobal("fetch", sceneReply());

    const plan: DirectorCoveragePlan = await fetchCoveragePlan("s001");

    // The three fields that exist only because this mapping happens: the read
    // is per-scene, the plan is per-beat.
    expect(plan.scene_id).toBe("s001");
    expect(plan.scene_title).toBe("Scene s001");
    expect(plan.live_beat_duration).toBe(28.2);
  });
});
