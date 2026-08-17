/**
 * No fixture reaches the screen, and no figure on it belongs to another film.
 *
 * `DirectorWorkspace` passed `MOCK_SCENES` to the montage matrix —
 * `directorApi.ts`'s hardcoded fixture, whose own comment says it is "for unit
 * testing / UI preview". It is an entire invented film: "s004 — The Mountain
 * Takes Its Toll", 11 shots, `estimated_cost: 3.82`.
 *
 * In production the human opened the Director and was shown that scene, and that
 * $3.82, while their real locked plan — s001, 9 shots, $1.85 — was not on screen.
 * Worse, the fixture's `shots_count` then sliced the REAL shot list
 * (`allShots.slice(0, scene.shots_count)`), so genuine coverage was truncated to
 * the shape of the mock.
 *
 * Same rule as the compile gate: a figure the human might act on comes from the
 * server's summary for the project actually loaded, or it is not rendered.
 *
 * These tests deliberately do NOT stub `MOCK_SCENES`. `DirectorWorkspace`'s other
 * suites mock the whole module with `MOCK_SCENES: []`, which would make this
 * defect invisible — an empty array renders nothing, so the mock could come back
 * tomorrow and every one of those tests would still pass. Here the real fixture
 * is loaded and asserted absent.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

// Only the network functions are replaced. `MOCK_SCENES` is the REAL export —
// that is the whole point of this file.
vi.mock("../lib/directorApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/directorApi")>();
  return {
    ...actual,
    fetchCoveragePlan: vi.fn(),
    redirectSceneCoverage: vi.fn(),
    setCoverageStatus: vi.fn(),
    performShotAction: vi.fn(),
    updateShot: vi.fn(),
    waitForJob: vi.fn(),
    critiqueCoverage: vi.fn(),
    decideDirectorWarning: vi.fn(),
    compileCoverage: vi.fn(),
  };
});

import DirectorWorkspace from "./DirectorWorkspace";
import { MOCK_SCENES, fetchCoveragePlan } from "../lib/directorApi";
import type { DirectorCoveragePlan, DirectorShot } from "../types/director";

const mockFetchPlan = vi.mocked(fetchCoveragePlan);

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

/** The human's real scene: s001, 9 shots, $1.85 — none of it like the fixture. */
function plan(over: Partial<DirectorCoveragePlan> = {}): DirectorCoveragePlan {
  return {
    plan_id: "plan_s001",
    scene_id: "s001",
    scene_title: "s001 — The Shaft Collapse",
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

/**
 * Mount, switch to the montage matrix, and hand back THAT PANEL.
 *
 * The panel and not `document.body`: the workspace header renders the same cost
 * from the same plan, so a body-wide `toContain("1.85")` passes even when the
 * matrix itself is quoting something else entirely. A mutation that put 3.82
 * back into the matrix row was caught by the wrong assertion because of exactly
 * that. Presence is asserted here, on the element under test.
 */
async function openMatrix(p: DirectorCoveragePlan = plan()): Promise<HTMLElement> {
  mockFetchPlan.mockResolvedValue(p);
  render(<DirectorWorkspace sceneId="s001" activeProjectTitle="Heney" mediaUrl={(x) => x} />);
  const tab = await screen.findByText("Montage Matrix");
  fireEvent.click(tab);
  await act(async () => {});
  return screen.getByTestId("montage-matrix");
}

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe("the montage matrix shows this film, not the fixture", () => {
  test("no part of MOCK_SCENES reaches the screen", async () => {
    await openMatrix();
    const screenText = document.body.textContent || "";

    // THE DEFECT, asserted against the real fixture rather than a copy of its
    // strings — so editing the fixture cannot quietly make this test vacuous.
    //
    // Each assertion carries the offending value in its own message. Vitest
    // truncates the diff of a `not.toContain` against a whole document, so
    // without this the failure output never actually names the fabricated film
    // that appeared — and a mutation run could not tell this test firing from
    // some unrelated element going missing.
    for (const mock of MOCK_SCENES) {
      expect(
        screenText.includes(mock.title),
        `the Director is showing the fixture film "${mock.title}" — not the loaded plan`
      ).toBe(false);
      expect(
        screenText.includes(`$${mock.estimated_cost.toFixed(2)}`),
        `the Director is quoting the fixture's $${mock.estimated_cost.toFixed(2)} ` +
          `for "${mock.title}", a film nobody planned`
      ).toBe(false);
      expect(
        screenText.includes(`${mock.shots_count} shots`),
        `the Director is stating the fixture's ${mock.shots_count} shots ` +
          `for "${mock.title}"`
      ).toBe(false);
    }

    // And the fixture is not empty, or the loop above asserts nothing at all.
    expect(MOCK_SCENES.length).toBeGreaterThan(0);
    expect(MOCK_SCENES[0].title).toContain("s004");
  });

  test("the row is the loaded plan, with the server's own numbers", async () => {
    const matrix = await openMatrix();
    const panelText = matrix.textContent || "";

    // Scoped to the matrix. The workspace header quotes the same plan, so a
    // body-wide search finds "1.85" even when this panel says something else.
    expect(panelText).toContain("s001 — The Shaft Collapse");
    expect(panelText).toContain("9 shots");
    expect(panelText).toContain("$1.85");
  });

  test("the shot count is counted, not asserted", async () => {
    // It read "158-Shot Bird's-Eye View" as a hardcoded string, beside rows that
    // were themselves a fixture — a shot count belonging to no film at all.
    await openMatrix();

    expect(screen.getByTestId("matrix-shot-count").textContent).toBe(
      "9-Shot Bird's-Eye View"
    );
  });

  test("real coverage is not truncated to a fixture's shape", async () => {
    // `allShots.slice(0, scene.shots_count)` sliced the genuine shot list by the
    // MOCK's count of 11. With a real 9-shot plan that hid nothing; with any
    // plan larger than the fixture it silently dropped shots from the overview.
    const matrix = await openMatrix(
      plan({ coverage: Array.from({ length: 14 }, (_, i) => shot(i + 1, "parallax")) })
    );

    expect(screen.getByTestId("matrix-shot-count").textContent).toBe(
      "14-Shot Bird's-Eye View"
    );
    expect(matrix.textContent).toContain("14 shots");
  });

  test("a scene that genuinely costs nothing is quoted as nothing", async () => {
    // Zero is a real answer, and the panel must be able to state it. The
    // *derivation* of that zero — `??` rather than `||` in `fetchCoveragePlan`,
    // because 0.00 is falsy — is not testable from here: this file mocks that
    // function, so nothing it asserts could ever exercise it. That belongs one
    // layer down and lives in `directorApi.scene.test.ts`.
    const matrix = await openMatrix(
      plan({
        estimated_cost: 0,
        paid_shots: 0,
        coverage: [1, 2, 3].map((n) => shot(n, "parallax")),
      })
    );

    expect(matrix.textContent).toContain("$0.00");
  });
});
