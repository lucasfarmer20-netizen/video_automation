/**
 * What the server compiled is still on screen after the scene is re-read.
 *
 * The second symptom of the same defect: "the compile coverage status block
 * doesn't persist at scene level director, but it does below assembling timeline
 * proxy". The timeline proxy persists because it reads the project directly and
 * never passes through `fetchCoveragePlan`.
 *
 * The Director's own account of a compile lived entirely in React state —
 * `compileDone`, set once when the job returns — and the one durable record of
 * it, `CoveragePlan.compiled` ({beat_clip, runtime, shots, sub_clips}, written
 * by `director.compile` and saved), was dropped by the mapper. So the screen
 * could say a scene had compiled exactly once, and never again: leaving the
 * scene erased the only copy.
 *
 * `status` was always carried, so the *badge* did persist. That is why the
 * symptom is "the status block", not "the status" — and why this file asserts
 * both, separately. A test that only checked the badge would have passed
 * throughout.
 *
 * Same level as the dispositions suite, and for the same reason: `global.fetch`
 * is stubbed rather than `fetchCoveragePlan`, because the defect IS that
 * mapping. And the assertion is what the human sees after the refetch, not that
 * the field survived it — a field can arrive intact and be rendered by nothing.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import DirectorWorkspace from "./DirectorWorkspace";

// --- the server, as far as this screen can tell ------------------------------

/** One beat's persisted state, in the fields a compile actually moves. */
interface StoredPlan {
  status: string;
  compiled: Record<string, unknown>;
  scene_beats: string[];
}

let stored: Record<string, StoredPlan>;

function wirePlan(beatId: string) {
  const s = stored[beatId];
  return {
    beat_id: beatId,
    beat_duration: 28.2,
    version: 3,
    plan_id: `plan_${beatId}`,
    scene_beats: s.scene_beats,
    status: s.status,
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
    warnings: [],
    warning_dispositions: {},
    beat_signature: "beatsig0000000000",
    approved_signature: "ab12cd34ef567890",
    approved_at: "2026-08-16T10:00:00Z",
    approved_by: "human",
    approval_history: [],
    compiled: s.compiled,
    visual_strategy: "",
    blocking: {},
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

/** The three routes a compile touches, and nothing else. */
function studioServer() {
  return vi.fn(async (url: string) => {
    if (url.includes("/api/director/scene")) {
      const beats = decodeURIComponent(
        (url.split("beats=")[1] || "").split("&")[0]
      ).split(",");
      return json({
        ok: true,
        beats: beats.map((bid) => ({
          beat_id: bid,
          beat_duration: 28.2,
          coverage_total: 28.2,
          plan: wirePlan(bid),
        })),
        summary: { shots: 1, paid_shots: 1, estimated_cost: 0.8 },
        tier: "all",
      });
    }
    if (url.includes("/api/director/compile/")) {
      const beat = url.split("/api/director/compile/")[1].split("?")[0];
      // What `director.compile` writes when the render finishes.
      stored[beat].status = "compiled";
      stored[beat].compiled = {
        beat_clip: `render/${beat}/${beat}.mp4`,
        runtime: 28.2,
        shots: 1,
        sub_clips: [`render/${beat}/${beat}.01.mp4`],
      };
      return json({ ok: true, started: true, job: `director_compile:${beat}` });
    }
    if (url.includes("/api/assemble/status")) {
      const jobs: Record<string, unknown> = {};
      Object.keys(stored).forEach((b) => {
        jobs[`director_compile:${b}`] = { status: "done", log: `${b}: 1 shot -> 28.20s` };
      });
      return json({ ok: true, jobs });
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

async function settled() {
  await waitFor(() => expect(screen.queryByText(/Querying GET/)).toBeNull());
}

async function openScene(sceneId = "s001") {
  const view = render(workspace(sceneId));
  await settled();
  return view;
}

/** Open the spend gate and confirm it — the whole paid path, as a human runs it. */
async function compileScene() {
  fireEvent.click(screen.getByTestId("compile-open-gate"));
  fireEvent.click(screen.getByTestId("compile-confirm"));
  await screen.findByTestId("compile-done");
}

async function switchTo(rerender: (ui: React.ReactElement) => void, sceneId: string) {
  rerender(workspace(sceneId));
  await settled();
}

/** The scene's compile state as the screen states it, or "none". */
function compiledRecord(): string {
  return screen.queryByTestId("compiled-record")?.textContent?.trim() || "none";
}

function statusBadge(): string {
  return screen.getByText(/^Status:/).textContent?.trim() || "";
}

beforeEach(() => {
  stored = {
    s001: { status: "locked", compiled: {}, scene_beats: ["s001"] },
    s002: { status: "draft", compiled: {}, scene_beats: ["s002"] },
  };
  vi.stubGlobal("fetch", studioServer());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// --- the defect --------------------------------------------------------------

describe("a compiled scene still reads as compiled after a scene switch", () => {
  test("the compile record survives the re-read", async () => {
    const { rerender } = await openScene();
    expect(compiledRecord()).toBe("none");

    await compileScene();
    expect(stored.s001.status).toBe("compiled");

    await switchTo(rerender, "s002");
    await switchTo(rerender, "s001");

    // THE DEFECT. `CoveragePlan.compiled` is what the server durably wrote about
    // this compile; the mapper dropped it, so the only account of it on this
    // screen was the one-shot `compileDone` message — erased by leaving the
    // scene, which is why the block did not persist while the timeline proxy,
    // which reads the project directly, did.
    const record = compiledRecord();
    expect(
      record,
      "the Director has no account of a compile the server recorded"
    ).not.toBe("none");
    expect(record).toContain("render/s001/s001.mp4");
    expect(record).toContain("1 shot");
    expect(record).toContain("28.2s");

    // The half that always worked, asserted separately so the two cannot be
    // confused again: `status` was carried all along, so the badge persisted.
    expect(statusBadge()).toContain("compiled");

    // And the one-shot message is correctly NOT back. It reports an event —
    // "the job you just ran finished" — and re-showing it on a plain re-read
    // would claim a compile that did not happen in this visit.
    expect(screen.queryByTestId("compile-done")).toBeNull();
  });

  test("the compile message does not follow the human to another scene", async () => {
    // Found by the test above. `compileDone` is React state that nothing reset
    // on a scene change, so "s001 compiled — open Cinema Scrubber to review the
    // takes" was rendered on top of s002, about a scene the human had left.
    const { rerender } = await openScene();
    await compileScene();
    expect(screen.getByTestId("compile-done").textContent).toContain("s001 compiled");

    await switchTo(rerender, "s002");

    expect(
      screen.queryByTestId("compile-done"),
      "a compile message about s001 is on screen while the human is looking at s002"
    ).toBeNull();
    // s002 is a draft and says nothing about compiling at all.
    expect(compiledRecord()).toBe("none");
  });

  test("an uncompiled scene claims nothing", async () => {
    // The control. A block that renders unconditionally would pass the test
    // above and tell every human their draft scene was compiled.
    const { rerender } = await openScene();
    await switchTo(rerender, "s002");

    expect(compiledRecord()).toBe("none");
    expect(statusBadge()).toContain("draft");
  });

  test("the record names the beat it belongs to, not the beats that were asked for", async () => {
    // A scene read spans every beat in `scene_beats`, and the mapper returns the
    // FIRST beat that has a plan. The compile record is that one beat's produced
    // state, so attributing it to "s001,s002" would claim a beat clip for s002
    // out of s001's record. This is why `beat_id` is carried: `scene_id` here is
    // the set that was requested, which is not the same fact.
    //
    // The assertion has to be made HERE, on the plan the compile handler
    // refetched — `fetchCoveragePlan(scene_beats)`, the studio's only two-beat
    // read. Asserting after a scene switch instead would prove nothing: the
    // scene effect re-reads a single beat, so `scene_id` and `beat_id` agree and
    // a mapper that conflated them would pass.
    stored.s001.scene_beats = ["s001", "s002"];
    stored.s002.scene_beats = ["s001", "s002"];
    const { rerender } = await openScene();

    await compileScene();

    expect(compiledRecord()).toContain("s001 is compiled");
    expect(
      compiledRecord(),
      "one beat's compile record is being attributed to the whole scene"
    ).not.toContain("s001,s002");

    // And it still names the beat after the single-beat re-read.
    await switchTo(rerender, "s002");
    await switchTo(rerender, "s001");
    expect(compiledRecord()).toContain("s001 is compiled");
  });

  test("a plan that reads compiled with no record says so, rather than inventing one", async () => {
    // Plans compiled before `compiled` was written, and any future path that
    // sets the status without the record. §11.4: the screen states what the
    // server said, and states the absence when there is nothing to state.
    stored.s001 = { status: "compiled", compiled: {}, scene_beats: ["s001"] };
    await openScene();

    const record = compiledRecord();
    expect(record).not.toBe("none");
    expect(record).toContain("no record");
    // Nothing invented to fill the gap.
    expect(record).not.toContain(".mp4");
  });
});
