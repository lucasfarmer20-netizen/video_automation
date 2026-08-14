/**
 * The timeline UI, bound to the cut it actually has (slice 5b).
 *
 * These are behavioural, not "it renders": each one is written so that a
 * faithful mutation of the binding fails it.
 *
 *   §7.1  selecting a different take replaces media IN the existing slot,
 *         without destroying slot identity, valid trims, surrounding edit
 *         relationships or scene placement
 *   §6.2  incomplete coverage is stated in the server's words, and Draft 1 is
 *         buildable from that state
 *   C5    a placeholder carries shot id, slot identity, intended duration,
 *         expected media type and source beat — not a grey rectangle
 *   §11.4 no UI state claims a slot is filled unless the server says so
 */
import React from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import MultitrackTimeline, { Shot } from "./MultitrackTimeline";
import { TimelineSlot, SlotCoverage } from "../lib/slots";

afterEach(cleanup);

const beat = (scene_id: string, duration: number): Shot => ({
  scene_id,
  narration: "",
  motion_type: "parallax",
  camera: { move: "static", duration },
  draft_image: null,
});

const slot = (over: Partial<TimelineSlot>): TimelineSlot => ({
  id: "b1::beat",
  beat_id: "b1",
  shot_id: "",
  index: 0,
  intended_duration: 4,
  expected_media: "video",
  media: "",
  source_attempt: "",
  trim_in: 0,
  trim_out: 0,
  placeholder: true,
  duration: 4,
  ...over,
});

const coverageOf = (over: Partial<SlotCoverage> = {}): SlotCoverage => ({
  slots: 2, ready: 1, placeholders: 1, runtime: 8,
  summary: "1/2 visuals ready · Draft 1 will use 1 placeholder",
  ...over,
});

const mediaUrl = (p: string) => (p ? `/media/${p}` : "");

const mount = (props: Partial<React.ComponentProps<typeof MultitrackTimeline>> = {}) =>
  render(
    <MultitrackTimeline
      shots={[beat("b1", 4)]}
      mediaUrl={mediaUrl}
      onUpdateCamera={() => {}}
      {...props}
    />,
  );

// --- §11.4 no false success ------------------------------------------------

describe("§11.4 — the UI does not claim a slot is filled unless the server does", () => {
  test("media present but the server still calls it a placeholder", () => {
    // The exact disagreement the derived flag exists to prevent. A client that
    // decides "filled" from `media` being non-empty would draw this as a clip.
    mount({
      slots: [slot({ media: "render/b1.mp4", placeholder: true })],
      coverage: coverageOf(),
    });

    const clip = screen.getByTestId("slot-b1::beat");
    expect(clip.getAttribute("data-placeholder")).toBe("true");
    expect(screen.queryByTestId("slot-media-b1::beat")).toBeNull();
    expect(clip.querySelector("img")).toBeNull();
    expect(screen.getByTestId("slot-placeholder-b1::beat")).toBeTruthy();
    expect(screen.queryByTestId("slot-ready-b1::beat")).toBeNull();
  });

  test("a payload that never states the flag is drawn as a placeholder", () => {
    const unstated = slot({ media: "render/b1.mp4" });
    delete unstated.placeholder;
    mount({ slots: [unstated], coverage: coverageOf() });

    expect(screen.getByTestId("slot-b1::beat").getAttribute("data-placeholder")).toBe("true");
    expect(screen.queryByTestId("slot-media-b1::beat")).toBeNull();
  });

  test("an unread cut is not drawn as an empty one", () => {
    mount({ slots: null, slotsError: "timeline_slots.json is unreadable" });

    expect(screen.getByTestId("slots-unread").textContent)
      .toContain("timeline_slots.json is unreadable");
    expect(screen.queryByTestId("slots-empty")).toBeNull();
    expect(screen.getByTestId("coverage-unread")).toBeTruthy();
  });
});

// --- §6.2 incomplete coverage ----------------------------------------------

describe("§6.2 — incomplete coverage is stated, and Draft 1 is buildable", () => {
  test("the coverage sentence rendered is the server's, not a recount", () => {
    // The coverage deliberately disagrees with the slots handed to the
    // component: five slots reported, two given. A component that counts for
    // itself would print "1/2"; the server's sentence is what must appear.
    mount({
      slots: [
        slot({ id: "b1::beat", index: 0, media: "render/b1.mp4", placeholder: false }),
        slot({ id: "b2::beat", beat_id: "b2", index: 1 }),
      ],
      coverage: coverageOf({
        slots: 5, ready: 3, placeholders: 2,
        summary: "3/5 visuals ready · Draft 1 will use 2 placeholders",
      }),
      onBuildDraft1: () => {},
    });

    expect(screen.getByTestId("coverage-summary").textContent)
      .toBe("3/5 visuals ready · Draft 1 will use 2 placeholders");
    expect(screen.getByTestId("slot-coverage").textContent).not.toContain("1/2");
  });

  test("Build Draft 1 stays available while coverage is incomplete", () => {
    const onBuildDraft1 = vi.fn();
    mount({
      slots: [slot({ id: "b1::beat" })],
      coverage: coverageOf({ slots: 1, ready: 0, placeholders: 1,
                             summary: "0/1 visuals ready · Draft 1 will use 1 placeholder" }),
      onBuildDraft1,
    });

    const build = screen.getByTestId("build-draft-1") as HTMLButtonElement;
    expect(build.disabled).toBe(false);
    fireEvent.click(build);
    expect(onBuildDraft1).toHaveBeenCalledTimes(1);
  });
});

// --- C5 what a placeholder carries ------------------------------------------

describe("C5 — a placeholder is a slot, not a grey rectangle", () => {
  test("it carries shot id, slot identity, intended duration, media type and beat", () => {
    mount({
      slots: [slot({
        id: "b2::b2.3", beat_id: "b2", shot_id: "b2.3", index: 0,
        intended_duration: 3.5, expected_media: "still", duration: 3.5,
      })],
      coverage: coverageOf(),
      shots: [beat("b2", 3.5)],
    });

    const clip = screen.getByTestId("slot-b2::b2.3");
    expect(clip.getAttribute("data-slot-id")).toBe("b2::b2.3");
    expect(clip.getAttribute("data-shot-id")).toBe("b2.3");
    expect(clip.getAttribute("data-beat-id")).toBe("b2");
    expect(clip.getAttribute("data-expected-media")).toBe("still");
    expect(clip.getAttribute("data-intended-duration")).toBe("3.5");

    // …and says all of it in words, which is the half a data attribute cannot do.
    const body = screen.getByTestId("slot-placeholder-b2::b2.3").textContent || "";
    expect(body).toContain("waiting for a still for shot b2.3");
    expect(body).toContain("3.5s");
    expect(body).toContain("from beat b2");
    expect(body).toContain("slot b2::b2.3");
  });
});

// --- §7.1 the slot invariant ------------------------------------------------

/** A three-shot beat, with the middle slot filled and trimmed by the editor. */
const cutBeforeSwap = (): TimelineSlot[] => [
  slot({ id: "b2::b2.1", beat_id: "b2", shot_id: "b2.1", index: 0,
         intended_duration: 3, duration: 3,
         media: "render/b2/b2.1.mp4", source_attempt: "att-1", placeholder: false }),
  slot({ id: "b2::b2.2", beat_id: "b2", shot_id: "b2.2", index: 1,
         intended_duration: 4, trim_in: 0.5, trim_out: 3, duration: 2.5,
         media: "render/b2/b2.2-takeA.mp4", source_attempt: "att-A", placeholder: false }),
  slot({ id: "b2::b2.3", beat_id: "b2", shot_id: "b2.3", index: 2,
         intended_duration: 2, duration: 2 }),
];

/**
 * The same cut after the server re-read it: the middle slot now holds a
 * different take, and Director has since added a shot at the head of the beat,
 * so every index moved. Slot ids did not — they are derived from what the slot
 * covers, which is the whole point.
 */
const cutAfterSwap = (): TimelineSlot[] => [
  slot({ id: "b2::b2.0", beat_id: "b2", shot_id: "b2.0", index: 0,
         intended_duration: 1, duration: 1 }),
  slot({ id: "b2::b2.1", beat_id: "b2", shot_id: "b2.1", index: 1,
         intended_duration: 3, duration: 3,
         media: "render/b2/b2.1.mp4", source_attempt: "att-1", placeholder: false }),
  slot({ id: "b2::b2.2", beat_id: "b2", shot_id: "b2.2", index: 2,
         intended_duration: 4, trim_in: 0.5, trim_out: 3, duration: 2.5,
         media: "render/b2/b2.2-takeD.mp4", source_attempt: "att-D", placeholder: false }),
  slot({ id: "b2::b2.3", beat_id: "b2", shot_id: "b2.3", index: 3,
         intended_duration: 2, duration: 2 }),
];

describe("§7.1 — a take swap replaces media in the slot that already exists", () => {
  test("the swap is asked for on the selected slot, by slot identity", () => {
    const onSelectTake = vi.fn();
    mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),
      coverage: coverageOf(),
      takes: { "b2::b2.2": { variations: ["assets/b2.2-a.png", "assets/b2.2-d.png"], chosen: 0 } },
      onSelectTake,
    });

    fireEvent.click(screen.getByTestId("slot-b2::b2.2"));
    expect(screen.getByTestId("slot-inspector-id").textContent).toBe("b2::b2.2");

    fireEvent.click(screen.getByTestId("slot-take-1"));
    expect(onSelectTake).toHaveBeenCalledTimes(1);
    const [passedSlot, index] = onSelectTake.mock.calls[0];
    expect(passedSlot.id).toBe("b2::b2.2");
    expect(passedSlot.shot_id).toBe("b2.2");
    expect(index).toBe(1);

    // Nothing is marked in use until the server says so: the click alone must
    // not move the "in use" badge onto the take that was merely requested.
    expect(screen.getByTestId("slot-take-0").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("slot-take-1").getAttribute("aria-pressed")).toBe("false");
  });

  test("the slot survives the swap: identity, trims, neighbours and placement", () => {
    const { rerender } = mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),
      coverage: coverageOf(),
      takes: { "b2::b2.2": { variations: ["assets/b2.2-a.png", "assets/b2.2-d.png"], chosen: 0 } },
      onSelectTake: () => {},
    });

    fireEvent.click(screen.getByTestId("slot-b2::b2.2"));
    const before = screen.getByTestId("slot-b2::b2.2");
    expect(before.getAttribute("data-selected")).toBe("true");
    expect(before.getAttribute("data-media")).toBe("render/b2/b2.2-takeA.mp4");

    // The server answers with the new take, and with a re-planned beat that has
    // shifted every index by one.
    rerender(
      <MultitrackTimeline
        shots={[beat("b2", 10)]}
        slots={cutAfterSwap()}
        coverage={coverageOf()}
        takes={{ "b2::b2.2": { variations: ["assets/b2.2-a.png", "assets/b2.2-d.png"], chosen: 1 } }}
        onSelectTake={() => {}}
        mediaUrl={mediaUrl}
        onUpdateCamera={() => {}}
      />,
    );

    const after = screen.getByTestId("slot-b2::b2.2");

    // Slot identity: the same clip, not a rebuilt one. Keyed on anything but the
    // slot id — an array index, say — the element that carried this slot would
    // now be carrying its neighbour.
    expect(after).toBe(before);
    expect(after.getAttribute("data-slot-id")).toBe("b2::b2.2");
    expect(after.getAttribute("data-shot-id")).toBe("b2.2");

    // Media replaced IN the slot.
    expect(after.getAttribute("data-media")).toBe("render/b2/b2.2-takeD.mp4");
    expect(after.getAttribute("data-source-attempt")).toBe("att-D");

    // Valid trims survived.
    expect(after.getAttribute("data-trim-in")).toBe("0.5");
    expect(after.getAttribute("data-trim-out")).toBe("3");
    expect(screen.getByTestId("slot-trim-b2::b2.2").textContent).toContain("0.5");

    // The editor is still on the slot they chose, not on whatever now sits in
    // that position.
    expect(after.getAttribute("data-selected")).toBe("true");
    expect(screen.getByTestId("slot-b2::b2.1").getAttribute("data-selected")).toBe("false");
    expect(screen.getByTestId("slot-inspector-id").textContent).toBe("b2::b2.2");

    // Surrounding edit relationships and scene placement.
    const ids = Array.from(
      screen.getByTestId("v1-track").querySelectorAll("[data-slot-id]"),
    ).map((el) => el.getAttribute("data-slot-id"));
    expect(ids).toEqual(["b2::b2.0", "b2::b2.1", "b2::b2.2", "b2::b2.3"]);
    expect(after.getAttribute("data-beat-id")).toBe("b2");
  });

  test("a filled slot draws the media the server reported, and only then", () => {
    mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),
      coverage: coverageOf(),
    });

    const frame = screen.getByTestId("slot-media-b2::b2.2");
    expect(frame.getAttribute("src")).toBe("/media/render/b2/b2.2-takeA.mp4");

    // A frame that fails to load is a broken frame, not an empty slot. If the
    // component demoted the slot on error, the cut would lose a clip to a 404.
    fireEvent.error(frame);
    expect(screen.getByTestId("slot-b2::b2.2").getAttribute("data-placeholder")).toBe("false");
    expect(screen.queryByTestId("slot-placeholder-b2::b2.2")).toBeNull();
  });

  test("slot media is drawn by an element that can actually decode it", () => {
    // Slot media is always a clip: DirectorShot.clip is render/<beat>/<shot>.mp4
    // and the whole-beat fallback is render/<beat>.mp4. Drawn into an <img>,
    // every filled slot fires onError and hides its own frame, so V1 shows no
    // imagery at all — which looks like a styling choice rather than a bug.
    mount({
      shots: [{ ...beat("b3", 4), draft_image: "assets/b3/take1.png" }],
      slots: [slot({
        id: "b3::beat", beat_id: "b3", index: 0,
        media: "render/b3.mp4", source_attempt: "att-3", placeholder: false,
      })],
      coverage: coverageOf(),
    });

    const frame = screen.getByTestId("slot-media-b3::beat");
    expect(frame.tagName).toBe("VIDEO");
    expect(frame.getAttribute("src")).toBe("/media/render/b3.mp4");
    // The still the clip was rendered from, where the studio already has it.
    expect(frame.getAttribute("poster")).toBe("/media/assets/b3/take1.png");
    expect(screen.getByTestId("slot-b3::beat").querySelector("img")).toBeNull();
  });

  test("a take is chosen, not in use — the slot still holds its own media", () => {
    // chosen_variation records which draft still the shot uses; the media in the
    // slot is DirectorShot.clip and only moves when the beat is rendered again.
    mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),
      coverage: coverageOf(),
      takes: { "b2::b2.2": { variations: ["a.png", "d.png"], chosen: 1 } },
      onSelectTake: () => {},
    });

    fireEvent.click(screen.getByTestId("slot-b2::b2.2"));
    const takes = screen.getByTestId("slot-takes").textContent || "";
    expect(takes).toContain("chosen");
    expect(takes).not.toContain("in use");
    expect(screen.getByTestId("slot-takes-note").textContent)
      .toContain("applies the next time this beat is rendered");
    // …and the Media field still names the clip the server reported.
    expect(screen.getByTestId("slot-inspector-media").textContent)
      .toContain("render/b2/b2.2-takeA.mp4");
  });
});

// --- trims are the slot's, and are written through the server ---------------

describe("trims belong to the slot", () => {
  test("editing the trim asks the server; the UI does not move it first", () => {
    const onTrimSlot = vi.fn();
    mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),
      coverage: coverageOf(),
      onTrimSlot,
    });

    fireEvent.click(screen.getByTestId("slot-b2::b2.2"));
    const trimOut = screen.getByTestId("slot-trim-out") as HTMLInputElement;
    fireEvent.change(trimOut, { target: { value: "2.4" } });
    fireEvent.blur(trimOut);

    expect(onTrimSlot).toHaveBeenCalledTimes(1);
    const [passedSlot, trim] = onTrimSlot.mock.calls[0];
    expect(passedSlot.id).toBe("b2::b2.2");
    expect(trim).toEqual({ trim_in: 0.5, trim_out: 2.4 });

    // Still what the server last said, because the server has not answered yet.
    expect(screen.getByTestId("slot-b2::b2.2").getAttribute("data-trim-out")).toBe("3");
  });

  test("a trim that would empty the slot is refused, not sent", () => {
    // POST /api/timeline/slot/{id}/trim accepts a trim_in past the intended
    // duration and hands back a zero-length slot. This inspector is the first
    // thing that makes that reachable, so it does not author one.
    const onTrimSlot = vi.fn();
    mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),   // b2.2 is intended 4s
      coverage: coverageOf(),
      onTrimSlot,
    });

    fireEvent.click(screen.getByTestId("slot-b2::b2.2"));
    const trimIn = screen.getByTestId("slot-trim-in") as HTMLInputElement;
    fireEvent.change(trimIn, { target: { value: "9" } });
    fireEvent.blur(trimIn);

    expect(onTrimSlot).not.toHaveBeenCalled();
    expect(screen.getByTestId("slot-trim-error").textContent)
      .toContain("trim in must be under the intended 4.00s");
  });

  test("trim out at or before trim in is refused too", () => {
    const onTrimSlot = vi.fn();
    mount({
      shots: [beat("b2", 9)],
      slots: cutBeforeSwap(),   // b2.2 has trim_in 0.5
      coverage: coverageOf(),
      onTrimSlot,
    });

    fireEvent.click(screen.getByTestId("slot-b2::b2.2"));
    const trimOut = screen.getByTestId("slot-trim-out") as HTMLInputElement;
    fireEvent.change(trimOut, { target: { value: "0.4" } });
    fireEvent.blur(trimOut);

    expect(onTrimSlot).not.toHaveBeenCalled();
    expect(screen.getByTestId("slot-trim-error").textContent)
      .toContain("trim out must be after trim in");
  });
});

// --- §6.2 vs the storyboard gate --------------------------------------------

describe("Build Draft 1 says which gate it will stop at", () => {
  test("an uncleared gate is stated, and still does not disable the action", () => {
    const onBuildDraft1 = vi.fn();
    mount({
      slots: [slot({ id: "b1::beat" })],
      coverage: coverageOf(),
      onBuildDraft1,
      draftGateNote: "Gate 1 not cleared — this run stops at the storyboard gate",
    });

    expect(screen.getByTestId("draft-gate-note").textContent)
      .toContain("stops at the storyboard gate");
    const build = screen.getByTestId("build-draft-1") as HTMLButtonElement;
    expect(build.disabled).toBe(false);
    fireEvent.click(build);
    expect(onBuildDraft1).toHaveBeenCalledTimes(1);
  });

  test("no note when there is no gate to report", () => {
    mount({
      slots: [slot({ id: "b1::beat" })],
      coverage: coverageOf(),
      onBuildDraft1: () => {},
    });
    expect(screen.queryByTestId("draft-gate-note")).toBeNull();
  });
});
