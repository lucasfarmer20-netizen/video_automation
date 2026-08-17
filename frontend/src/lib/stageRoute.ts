/**
 * Which stage the studio is on, kept in the URL.
 *
 * `activeStage` was `useState<StageId>("script")` with no persistence and no
 * routing, so every reload landed on Script regardless of where the user was.
 * That is not cosmetic: a human who had locked a coverage plan on the Direct
 * stage, refreshed, and then reported "I don't see a way back into the director
 * after refresh" was right — their plan was safe on the server and the studio
 * had no route back to it.
 *
 * The URL rather than localStorage, deliberately:
 *
 *   - It is the one piece of this state the user can SEE. "Where am I" stops
 *     being a thing you deduce from the page.
 *   - Back and forward then mean what they look like they mean.
 *   - It can be bookmarked and pasted to someone else.
 *   - Stored state relocates a *new* tab to wherever some other tab was last,
 *     which is a different film's worth of confusion. A reload should land
 *     where you were; a fresh tab should not inherit where you were.
 *
 * The hash rather than a query parameter: the studio is served as static assets
 * by FastAPI (`run_studio.py`), so the fragment is the part of the URL that is
 * unambiguously the client's and never reaches the server or a rewrite rule.
 */

/** The stages that can appear in a URL. Mirrors `StageHeader.STAGE_ORDER`. */
export const ROUTABLE_STAGES = [
  "script", "direct", "generate", "roughcut", "refine", "export",
] as const;

export type RoutableStage = (typeof ROUTABLE_STAGES)[number];

/** Whether a string is a stage this app knows how to render. */
export function isRoutableStage(value: string): value is RoutableStage {
  return (ROUTABLE_STAGES as readonly string[]).includes(value);
}

/**
 * The stage a URL fragment names, or null.
 *
 * Null for anything unrecognised rather than a guess: a hash from a future
 * version, a deep link someone mistyped, or the `#section` of an unrelated
 * anchor must leave the studio where it already is instead of navigating
 * somewhere it invented.
 */
export function stageFromHash(hash: string): RoutableStage | null {
  // Trim before stripping the "#", not after: a hash read back with surrounding
  // whitespace would otherwise keep its "#" and match nothing.
  const raw = (hash || "").trim().replace(/^#/, "").trim().toLowerCase();
  if (!raw) return null;
  return isRoutableStage(raw) ? raw : null;
}

/** The fragment that names a stage, including the leading `#`. */
export function hashForStage(stage: string): string {
  return `#${stage}`;
}

/**
 * Whether the address bar already says this stage.
 *
 * Used to avoid rewriting an identical URL, which would otherwise add a history
 * entry per render and make Back appear broken.
 */
export function hashAlreadyNames(hash: string, stage: string): boolean {
  return stageFromHash(hash) === stage;
}
