/**
 * The film coverage overview shows the loaded film, or shows nothing.
 *
 * It was fed `MOCK_SCENES`, and a human working on the Saugus Iron Works read:
 *
 *     s004 — The Mountain Takes Its Toll | Draft | 1:12 | 11 shots | $3.82
 *
 * A title, a runtime, a shot count and a PRICE, none of them from their film,
 * while their own locked coverage (s001, 9 shots) was not on the screen. The
 * rule: state the block, never substitute — and never substitute a cost.
 */
import { describe, expect, test } from "vitest";
import { beatTitle, defaultSelectedBeat, filmCoverageView } from "./filmCoverage";
import type { FilmBeat } from "./filmCoverage";
import type { BeatCoverageState } from "./directorApi";

/** The film the human was actually working on. */
const FILM: FilmBeat[] = [
  { scene_id: "s001", narration: "The ironworks stood where the river met the tide.", camera: { duration: 17.7 } },
  { scene_id: "s002", narration: "Nobody recorded what the night shift heard.", camera: { duration: 18.4 } },
  { scene_id: "s003", narration: "", camera: { duration: 31.4 } },
];

const state = (over: Partial<BeatCoverageState> = {}): BeatCoverageState => ({
  status: "locked",
  shots: 9,
  locked: true,
  estimatedCost: 1.85,
  warnings: 0,
  durationSeconds: 17.7,
  ...over,
});

describe("only the loaded film appears", () => {
  test("a covered beat is drawn with the server's own numbers", () => {
    const view = filmCoverageView(FILM, { s001: state() });

    expect(view.rows).toHaveLength(1);
    const row = view.rows[0];
    expect(row.scene_id).toBe("s001");
    // The cost is the server's figure for THIS beat — the whole point.
    expect(row.estimated_cost).toBe(1.85);
    expect(row.shots_count).toBe(9);
    expect(row.status).toBe("locked");
    expect(row.duration).toBe(17.7);
  });

  test("nothing is invented when the film has no coverage", () => {
    // THE DEFECT. This is where MOCK_SCENES used to appear: an empty result got
    // a fixture instead of an empty result.
    const view = filmCoverageView(FILM, {});

    expect(view.rows).toEqual([]);
    expect(view.coveredBeats).toBe(0);
    expect(view.totalBeats).toBe(3);
  });

  test("a beat the server did not report is not drawn", () => {
    const view = filmCoverageView(FILM, { s002: state({ shots: 4, estimatedCost: 0.8 }) });

    expect(view.rows.map((r) => r.scene_id)).toEqual(["s002"]);
    expect(view.rows[0].shots_count).toBe(4);
  });

  test("no film means no rows, not a sample film", () => {
    expect(filmCoverageView([], { s001: state() }).rows).toEqual([]);
    expect(filmCoverageView(null, null).rows).toEqual([]);
    expect(filmCoverageView(undefined, { s001: state() }).totalBeats).toBe(0);
  });

  test("rows follow the film's own beat order", () => {
    const view = filmCoverageView(FILM, {
      s003: state({ estimatedCost: 2 }),
      s001: state(),
    });
    expect(view.rows.map((r) => r.scene_id)).toEqual(["s001", "s003"]);
  });
});

describe("a cost is the server's or it is absent", () => {
  test("an unpriced beat is withheld rather than shown as $0.00", () => {
    // THE DEFECT, in its most dangerous form. Zero is a number a human acts on.
    const view = filmCoverageView(FILM, { s001: state({ estimatedCost: null }) });

    expect(view.rows).toEqual([]);
    expect(view.unpriced).toHaveLength(1);
    expect(view.unpriced[0].scene_id).toBe("s001");
    expect(view.unpriced[0].shots).toBe(9);
    // …and it is still counted as covered, so the panel cannot claim the beat
    // has no coverage either.
    expect(view.coveredBeats).toBe(1);
  });

  test("a zero cost the server really reported is kept", () => {
    // $0.00 is a true statement about an all-local plan. Only *absent* is
    // withheld; suppressing a real zero would be its own substitution.
    const view = filmCoverageView(FILM, { s001: state({ estimatedCost: 0 }) });

    expect(view.rows).toHaveLength(1);
    expect(view.rows[0].estimated_cost).toBe(0);
    expect(view.unpriced).toEqual([]);
  });

  test("priced and unpriced beats are separated, not summed together", () => {
    const view = filmCoverageView(FILM, {
      s001: state({ estimatedCost: 1.85 }),
      s002: state({ estimatedCost: null, shots: 3 }),
    });

    expect(view.rows.map((r) => r.scene_id)).toEqual(["s001"]);
    expect(view.unpriced.map((u) => u.scene_id)).toEqual(["s002"]);
    expect(view.coveredBeats).toBe(2);
  });
});

describe("a scene's title is its own", () => {
  test("the title is the beat id and the beat's real narration", () => {
    expect(beatTitle(FILM[0])).toBe(
      "s001 — The ironworks stood where the river met the tide."
    );
    // Never an invented one.
    expect(beatTitle(FILM[0])).not.toContain("The Mountain Takes Its Toll");
  });

  test("a beat with no narration is just its id", () => {
    expect(beatTitle(FILM[2])).toBe("s003");
    expect(beatTitle({ scene_id: "s009" })).toBe("s009");
  });

  test("a long line is truncated, not replaced", () => {
    const long = "x".repeat(200);
    const title = beatTitle({ scene_id: "s001", narration: long });
    expect(title.startsWith("s001 — xxx")).toBe(true);
    expect(title.endsWith("…")).toBe(true);
    expect(title.length).toBeLessThan(80);
  });
});

describe("the beat the Director opens with comes from the film", () => {
  test("it is the first beat that already has coverage", () => {
    expect(defaultSelectedBeat(FILM, { s002: state() })).toBe("s002");
  });

  test("with no coverage it is the film's first beat", () => {
    expect(defaultSelectedBeat(FILM, {})).toBe("s001");
  });

  test("it is never the fixture's beat id", () => {
    // THE DEFECT: `useState<string>("s004")` was MOCK_SCENES' scene_id, so the
    // workspace asked for a plan for a beat that does not exist in this film
    // and correctly answered "No coverage plan found".
    expect(defaultSelectedBeat(FILM, {})).not.toBe("s004");
    expect(defaultSelectedBeat(FILM, { s003: state() })).not.toBe("s004");
  });

  test("no film means no selection, not a guess", () => {
    expect(defaultSelectedBeat([], {})).toBe("");
    expect(defaultSelectedBeat(null, null)).toBe("");
    expect(defaultSelectedBeat(undefined, { s004: state() })).toBe("");
  });

  test("coverage for a beat this film does not have is ignored", () => {
    // Exactly the s004 case: a stale or foreign id must not select anything.
    expect(defaultSelectedBeat(FILM, { s004: state() })).toBe("s001");
  });
});
