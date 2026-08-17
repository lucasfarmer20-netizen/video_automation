import { DirectorShot } from "../types/director";

/**
 * What a shot has to show, and what kind of element can decode it.
 *
 * Every Director review surface resolved this itself, and every one of them
 * resolved it the same wrong way:
 *
 *     if (shot.thumbnail_url) return mediaUrl(shot.thumbnail_url);
 *     if (shot.clip) return mediaUrl(shot.clip);
 *     ...
 *     <img src={thumbnail} />
 *
 * That is two defects wearing one line. The `clip` branch feeds an `.mp4` into
 * an `<img>`, which decodes nothing; and thumbnail-first means the branch never
 * runs anyway, because after a compile a shot has BOTH — so the still always
 * wins and the render it was drafted for is unreachable. Before `director.compile`
 * existed only `thumbnail_url` was ever set, so neither half had anything to be
 * wrong about. The s001 compile produced ten real clips and the reviewer watched
 * a slideshow.
 *
 * So the question is answered once, here, and it is answered as two facts rather
 * than one string: WHICH file, and WHAT it is. A caller cannot pick the right
 * element from a URL alone, which is precisely how the `<img src="...mp4">` in
 * CompactMontageMatrix got written.
 *
 * Clip first. The clip IS the render; the still is the plan for it. A surface
 * that wants the cheap frame (a dense index grid, say) can ask for `still`
 * explicitly via `shotStill` — but it then owes the human a label, because
 * showing a still where the render belongs, unlabelled, is the failure mode
 * this whole file exists to prevent (§11.4).
 */
export type ShotMediaKind = "clip" | "still" | "none";

export interface ShotMedia {
  kind: ShotMediaKind;
  /** Server-relative path, NOT run through mediaUrl — the caller owns that. */
  path: string | null;
}

export function resolveShotMedia(shot: DirectorShot): ShotMedia {
  if (shot.clip) return { kind: "clip", path: shot.clip };
  if (shot.thumbnail_url) return { kind: "still", path: shot.thumbnail_url };
  return { kind: "none", path: null };
}

/** The draft still alone. Never the clip — an `.mp4` is not a thumbnail. */
export function shotStill(shot: DirectorShot): string | null {
  return shot.thumbnail_url || null;
}

/** Has this shot been compiled into a clip of its own? */
export function isCompiled(shot: DirectorShot): boolean {
  return Boolean(shot.clip);
}

export interface CompileMix {
  total: number;
  compiled: number;
  /** Shots with no clip — stills or nothing at all. */
  uncompiled: number;
  /** True only when every shot carries a clip. */
  allCompiled: boolean;
  /** True when some do and some do not: the case that reads as a render fault. */
  mixed: boolean;
}

/**
 * How much of what is on screen is the actual render.
 *
 * A silent mix of moving and still images reads as a rendering failure, so the
 * surfaces state this rather than leaving the human to infer it from which
 * frames happen to move.
 */
export function compileMix(shots: DirectorShot[]): CompileMix {
  const total = shots.length;
  const compiled = shots.filter(isCompiled).length;
  return {
    total,
    compiled,
    uncompiled: total - compiled,
    allCompiled: total > 0 && compiled === total,
    mixed: compiled > 0 && compiled < total,
  };
}
