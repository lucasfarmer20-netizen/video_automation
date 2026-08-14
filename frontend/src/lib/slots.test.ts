import { describe, expect, test } from "vitest";
import {
  TimelineSlot, isFilled, isTrimmed, layoutSlots, slotDuration, slotLabel, waitingFor,
} from "./slots";

const slot = (over: Partial<TimelineSlot>): TimelineSlot => ({
  id: "b1::beat", beat_id: "b1", shot_id: "", index: 0,
  intended_duration: 4, expected_media: "video", media: "", source_attempt: "",
  trim_in: 0, trim_out: 0, placeholder: true, duration: 4, ...over,
});

describe("isFilled — §11.4, the server's flag and only the server's", () => {
  test("filled only when the server says placeholder is false", () => {
    expect(isFilled(slot({ media: "render/b1.mp4", placeholder: false }))).toBe(true);
    expect(isFilled(slot({ media: "", placeholder: true }))).toBe(false);
  });

  test("media without the server's word is NOT filled", () => {
    // The disagreement that matters: a payload carrying media while the server
    // still calls the slot a placeholder. Re-deriving the flag from `media`
    // would make the UI claim a clip the server has not vouched for.
    expect(isFilled(slot({ media: "render/b1.mp4", placeholder: true }))).toBe(false);
  });

  test("a payload that omits the flag fails closed", () => {
    const partial = slot({ media: "render/b1.mp4" });
    delete partial.placeholder;
    expect(isFilled(partial)).toBe(false);
  });
});

describe("layoutSlots", () => {
  test("lays slots out in index order, not array order", () => {
    const { blocks } = layoutSlots([
      slot({ id: "b1::c", index: 2, duration: 1 }),
      slot({ id: "b1::a", index: 0, duration: 2 }),
      slot({ id: "b1::b", index: 1, duration: 3 }),
    ]);
    expect(blocks.map((b) => b.slot.id)).toEqual(["b1::a", "b1::b", "b1::c"]);
    expect(blocks.map((b) => b.start)).toEqual([0, 2, 5]);
  });

  test("anchors each beat's first slot to where that beat starts", () => {
    // Without the anchor a trimmed slot would drag every later beat's video out
    // of line with its narration — the cut would look retimed when only one clip
    // had been shortened.
    const { blocks } = layoutSlots(
      [
        slot({ id: "b1::x", beat_id: "b1", index: 0, duration: 1.5, intended_duration: 4 }),
        slot({ id: "b2::y", beat_id: "b2", index: 1, duration: 3 }),
      ],
      { b1: 0, b2: 4 },
    );
    expect(blocks[0].start).toBe(0);
    expect(blocks[1].start).toBe(4);
  });

  test("without an anchor it simply accumulates", () => {
    const { blocks, total } = layoutSlots([
      slot({ id: "b1::x", beat_id: "b1", index: 0, duration: 2 }),
      slot({ id: "b2::y", beat_id: "b2", index: 1, duration: 3 }),
    ]);
    expect(blocks.map((b) => b.start)).toEqual([0, 2]);
    expect(total).toBe(5);
  });
});

describe("what a slot says about itself", () => {
  test("duration prefers the server's derived value", () => {
    expect(slotDuration(slot({ duration: 2.5, intended_duration: 4 }))).toBe(2.5);
  });

  test("isTrimmed reads either end", () => {
    expect(isTrimmed(slot({ trim_in: 0.5 }))).toBe(true);
    expect(isTrimmed(slot({ trim_out: 3 }))).toBe(true);
    expect(isTrimmed(slot({}))).toBe(false);
  });

  test("a whole-beat slot names its beat; a coverage slot names its shot", () => {
    expect(slotLabel(slot({ shot_id: "" }))).toBe("b1 · whole beat");
    expect(slotLabel(slot({ shot_id: "b1.2" }))).toBe("b1.2");
  });

  test("waitingFor states the media type and whose it is (C5)", () => {
    expect(waitingFor(slot({ shot_id: "b1.2", expected_media: "still" })))
      .toBe("waiting for a still for shot b1.2");
    expect(waitingFor(slot({ shot_id: "", expected_media: "video" })))
      .toBe("waiting for a video clip for beat b1");
  });
});
