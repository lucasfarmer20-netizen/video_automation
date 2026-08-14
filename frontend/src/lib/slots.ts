/**
 * The cut, as the server describes it. Client-side mirror of `backend/slots.py`.
 *
 * Contract §7.1: a timeline visual clip is a slot tied to a DirectorShot, not a
 * file path. Everything here is a *reader* of `GET /api/timeline/slots` — the
 * client never derives slot state it was not told, because two of the fields the
 * server sends are derived there precisely so the two sides cannot disagree:
 *
 *   - `placeholder` is `not media`, computed per request. A stored flag left
 *     true after media arrived is how a filled slot goes on rendering as a
 *     placeholder; a client flag mirroring it is the same bug with a longer
 *     fuse. §11.4.
 *   - `coverage.summary` is the §6.2 sentence, written server-side. A client
 *     that counts for itself will eventually disagree with the server, and the
 *     UI would then be stating something no authoritative state supports.
 *
 * So this module computes exactly one thing the server does not: horizontal
 * positions, which are a property of the screen and of nothing else.
 */

/** One visual position in the cut. Field-for-field `TimelineSlot.to_dict()`. */
export interface TimelineSlot {
  /** Stable identity, `beat_id::shot_id` — derived, not random, so a slot
   *  rebuilt from the same plan is the SAME slot and keeps its trims. */
  id: string;
  /** The narration beat this covers. C5's "source beat". */
  beat_id: string;
  /** The DirectorShot; "" for a whole-beat clip. */
  shot_id: string;
  index: number;
  intended_duration: number;
  /** "video" | "still" — C5's "expected media type". */
  expected_media: string;
  /** Media-root-relative; "" while a placeholder. */
  media: string;
  /** The GenerationAttempt that produced `media`. */
  source_attempt: string;
  trim_in: number;
  /** 0 means "to the end of the media". */
  trim_out: number;
  note?: string;
  /** DERIVED SERVER-SIDE. Never recompute, never mirror in client state. */
  placeholder?: boolean;
  /** DERIVED SERVER-SIDE: trim if set, else intended_duration - trim_in. */
  duration?: number;
}

/** `coverage()` — how complete the cut is, in the words §6.2 asks for. */
export interface SlotCoverage {
  slots: number;
  ready: number;
  placeholders: number;
  runtime: number;
  /** e.g. "3/5 visuals ready · Draft 1 will use 2 placeholders". Render it. */
  summary: string;
}

export interface SlotBlock {
  slot: TimelineSlot;
  start: number;
  dur: number;
}

/** The takes the server holds for one slot's DirectorShot, as it reports them. */
export interface SlotTakes {
  variations: string[];
  /** 0-based, as everywhere on the server; null when nothing is chosen. */
  chosen: number | null;
}

export const STILL = "still";

/**
 * Whether the server says this slot has media in it.
 *
 * Fails closed on purpose: filled only when the server explicitly said
 * `placeholder: false`. An older or partial payload that omits the field is
 * therefore drawn as a placeholder rather than as a clip we cannot vouch for,
 * which is what §11.4 asks — the UI may not claim a slot is filled unless
 * authoritative backend state agrees.
 *
 * Deliberately does NOT look at `slot.media`, and must not: re-deriving the flag
 * client-side is how the two come to disagree.
 */
export function isFilled(slot: TimelineSlot): boolean {
  return slot.placeholder === false;
}

/** What a slot occupies in the cut. Server-derived; `duration` is authoritative.
 *
 * The fallback is `TimelineSlot.duration`'s arithmetic, clause for clause: a
 * trim governs when one is set, otherwise the intended length less trim_in.
 * Anything else would make this module disagree with the server inside a file
 * whose whole premise is that the two must not.
 */
export function slotDuration(slot: TimelineSlot): number {
  if (typeof slot.duration === "number") return slot.duration;
  if (slot.trim_out && slot.trim_out > slot.trim_in) {
    return Math.round((slot.trim_out - slot.trim_in) * 1000) / 1000;
  }
  return Math.round(Math.max(0, slot.intended_duration - slot.trim_in) * 1000) / 1000;
}

/** True when an editor has trimmed this slot away from its intended length. */
export function isTrimmed(slot: TimelineSlot): boolean {
  return slot.trim_in > 0 || slot.trim_out > 0;
}

/** How a slot names itself: the DirectorShot, or the beat it covers whole. */
export function slotLabel(slot: TimelineSlot): string {
  return slot.shot_id || `${slot.beat_id} · whole beat`;
}

/**
 * What a placeholder is waiting for, in words (C5).
 *
 * A placeholder that is only a grey rectangle carries none of what the slot
 * knows, so a user cannot tell what is missing. This sentence is the difference.
 */
export function waitingFor(slot: TimelineSlot): string {
  const kind = slot.expected_media === STILL ? "a still" : "a video clip";
  const owner = slot.shot_id ? `shot ${slot.shot_id}` : `beat ${slot.beat_id}`;
  return `waiting for ${kind} for ${owner}`;
}

/**
 * Where each slot sits on screen, in seconds.
 *
 * Slots are laid out inside their beat: `beatStarts` anchors the first slot of
 * each beat to where that beat already begins on the audio tracks, and the rest
 * follow it. Without the anchor the video track would accumulate its own
 * timebase and drift away from narration the moment a trim shortened a slot —
 * the cut would look retimed when only one clip had been shortened, which is
 * the silent stretch §7.1 forbids.
 *
 * Slots are read in `index` order: `index` is the position in the cut and is
 * the server's, not the array's.
 */
export function layoutSlots(
  slots: TimelineSlot[],
  beatStarts: Record<string, number> = {},
): { blocks: SlotBlock[]; total: number } {
  const ordered = [...slots].sort((a, b) => a.index - b.index);
  const blocks: SlotBlock[] = [];
  let acc = 0;
  let beat: string | null = null;
  for (const slot of ordered) {
    if (slot.beat_id !== beat) {
      beat = slot.beat_id;
      const anchored = beatStarts[slot.beat_id];
      if (typeof anchored === "number") acc = anchored;
    }
    const dur = slotDuration(slot);
    blocks.push({ slot, start: acc, dur });
    acc += dur;
  }
  return { blocks, total: acc };
}

// --- whose cut is this? ----------------------------------------------------

/**
 * Everything the studio holds about one project's cut, stamped with whose it is.
 *
 * Slot ids are `beat_id::shot_id`, which is unique within a film and emphatically
 * not across films. Held unstamped, a slot's takes from film A are served for a
 * slot in film B whose beat and shot ids happen to coincide — and beat ids are
 * `s001`, `s002`, so they coincide constantly. The page already refuses replies
 * that arrive for the wrong project (`isStaleReply`); this is the same rule for
 * state that has already arrived and is merely still on screen.
 */
export interface SlotView {
  /** The project this was read for. Never inferred; set when it was read. */
  projectId: string | null;
  slots: TimelineSlot[] | null;
  coverage: SlotCoverage | null;
  error: string | null;
  takes: Record<string, SlotTakes>;
}

/** No cut read yet — which is not the same claim as a cut with no clips. */
export const NO_SLOT_VIEW: SlotView = {
  projectId: null, slots: null, coverage: null, error: null, takes: {},
};

/**
 * The held cut, but only if it belongs to the project on screen.
 *
 * Returns the unread view rather than someone else's film. The timeline draws
 * `slots: null` as "reading the cut…", which is the truthful thing to say in the
 * moment between switching project and the new cut arriving.
 */
export function viewForProject(held: SlotView, projectId: string | null): SlotView {
  return held.projectId === projectId ? held : NO_SLOT_VIEW;
}
