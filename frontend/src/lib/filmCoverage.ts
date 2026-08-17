/**
 * The film coverage overview, built from the film that is actually loaded.
 *
 * This panel was fed `MOCK_SCENES` — a fixture whose own comment says "Real beat
 * ID mock constant for unit testing / UI preview if needed" — wired straight
 * into production. A human working on the Saugus Iron Works was shown:
 *
 *     FILM COVERAGE OVERVIEW
 *     1:12 (1 Scenes) | 0/1 Scenes Ready | 2 Warnings | Est. Cost: $3.82
 *     s004 — The Mountain Takes Its Toll | Draft | 1:12 | 11 shots | $3.82
 *
 * Not one of those numbers came from their film, and one of them was a price.
 * Their real locked coverage — s001, 9 shots — was not on the screen at all.
 *
 * The rule this codebase keeps arriving at: state the block, never substitute.
 * A borrowed stand-in is worse than an empty panel, and a borrowed stand-in
 * carrying a dollar figure is worse again — it is a quote for work that does not
 * exist. So everything here is derived from the loaded manifest and the server's
 * own summary for that project, and a beat the server did not price is not
 * given a price by this module.
 */
import type { SceneSummary } from "../types/director";
import type { BeatCoverageState } from "./directorApi";

/** As much of a manifest beat as the overview needs. */
export interface FilmBeat {
  scene_id: string;
  narration?: string;
  camera?: { duration?: number } | null;
}

/** A row that could not be priced, and therefore is not shown as priced. */
export interface UnpricedBeat {
  scene_id: string;
  shots: number;
  reason: string;
}

export interface FilmCoverageView {
  /** Scenes with coverage the server priced. Safe to render with costs. */
  rows: SceneSummary[];
  /** Covered beats the server did not price. Rendered as "not priced", never as $0. */
  unpriced: UnpricedBeat[];
  /** How many of the film's beats have any coverage at all. */
  coveredBeats: number;
  /** The film's total beat count, from the manifest. */
  totalBeats: number;
}

const EMPTY: FilmCoverageView = { rows: [], unpriced: [], coveredBeats: 0, totalBeats: 0 };

/** A scene's label: its beat id and its own narration, never an invented title. */
export function beatTitle(beat: FilmBeat): string {
  const line = (beat.narration || "").replace(/\s+/g, " ").trim();
  if (!line) return beat.scene_id;
  const short = line.length > 60 ? `${line.slice(0, 57)}…` : line;
  return `${beat.scene_id} — ${short}`;
}

/** `SceneSummary.status` only accepts these; anything else is reported as-is. */
function asSceneStatus(status: string): SceneSummary["status"] {
  switch (status) {
    case "locked":
    case "generating":
    case "compiled":
    case "draft":
      return status;
    default:
      return "draft";
  }
}

/**
 * The overview for the loaded film.
 *
 * `beats` is the manifest's own shot list; `coverage` is what
 * `fetchBeatCoverageStates` read back for those same ids. A beat appears only
 * if the server reported coverage for it — there is deliberately no path that
 * invents a row, and no default that fills one in.
 */
export function filmCoverageView(
  beats: FilmBeat[] | null | undefined,
  coverage: Record<string, BeatCoverageState> | null | undefined
): FilmCoverageView {
  if (!beats || beats.length === 0) return EMPTY;
  const states = coverage || {};

  const rows: SceneSummary[] = [];
  const unpriced: UnpricedBeat[] = [];
  let coveredBeats = 0;

  beats.forEach((beat) => {
    const state = states[beat.scene_id];
    if (!state) return;
    coveredBeats += 1;

    if (state.estimatedCost === null) {
      // The server holds coverage but did not price it. Rendering $0.00 here
      // would be the same defect in a smaller font.
      unpriced.push({
        scene_id: beat.scene_id,
        shots: state.shots,
        reason: "the server did not return a cost for this beat",
      });
      return;
    }

    rows.push({
      scene_id: beat.scene_id,
      title: beatTitle(beat),
      duration: state.durationSeconds ?? beat.camera?.duration ?? 0,
      beats_count: 1,
      shots_count: state.shots,
      estimated_cost: state.estimatedCost,
      status: asSceneStatus(state.status),
      warnings_count: state.warnings,
    });
  });

  return { rows, unpriced, coveredBeats, totalBeats: beats.length };
}

/**
 * Which beat the Director should open with.
 *
 * This was `useState<StageId>("s004")` — the mock fixture's `scene_id`. So the
 * workspace opened by asking for a plan for a beat that did not exist in the
 * user's film and correctly answered "No coverage plan found", which read as the
 * Director being broken. The default has to come from the film: the first beat
 * that already has coverage, else the film's first beat, else nothing.
 *
 * Empty string means "no film loaded yet", and callers must render that as
 * nothing rather than substituting an id.
 */
export function defaultSelectedBeat(
  beats: FilmBeat[] | null | undefined,
  coverage: Record<string, BeatCoverageState> | null | undefined
): string {
  if (!beats || beats.length === 0) return "";
  const states = coverage || {};
  const covered = beats.find((b) => states[b.scene_id]);
  return (covered || beats[0]).scene_id || "";
}
