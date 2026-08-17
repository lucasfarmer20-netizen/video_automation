/**
 * The Cinema Scrubber plays the compiled clip, and says when it is not.
 *
 * s001 compiled into ten real sub-clips plus a 28.9MB assembled beat; every one
 * of them returned HTTP 200 and the reviewer saw a slideshow, because this
 * component had no <video> element in it at all. It advanced an index on a timer
 * and swapped an <img>.
 *
 * These are written so a faithful mutation of the fix fails them, and the
 * defect-proving assertion comes FIRST in each: what is RENDERED for a shot that
 * has a clip. A test about what a resolver function returns would have passed
 * throughout the entire life of this defect, which is how it survived.
 *
 *   §11.4  no false success — a still is never silently shown as the render, and
 *          a transport never reads "playing" over a frame that will not play
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import CinemaScrubberPlayer from "./CinemaScrubberPlayer";
import { DirectorShot } from "../types/director";

afterEach(cleanup);

// jsdom implements no media pipeline: play() raises "Not implemented" through
// the virtual console. Stubbing it with a resolved promise is what a browser
// that CAN play does, so the component's own play/pause path is under test
// rather than jsdom's absence of one.
let playSpy: () => Promise<void>;
let pauseSpy: () => void;

beforeEach(() => {
  playSpy = vi.fn(() => Promise.resolve());
  pauseSpy = vi.fn();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => playSpy());
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => pauseSpy());
});

afterEach(() => vi.restoreAllMocks());

const shot = (over: Partial<DirectorShot> = {}): DirectorShot => ({
  id: "s001.01",
  beat_id: "s001",
  shot_number: "s001.01",
  tier: 1,
  purpose: "establishing",
  subject: "the drowned bell tower",
  shot_size: "ews",
  angle: "front",
  camera: { move: "static", duration: 4, speed: 1, amount: 0 },
  identity_critical: false,
  motion_type: "parallax",
  backend: "nano2",
  prompt: "",
  motion_prompt: "",
  draft_variations: [],
  estimated_cost: 0,
  ...over,
});

const mediaUrl = (p: string) => (p ? `/media/${p}` : "");

const mount = (shots: DirectorShot[]) =>
  render(
    <CinemaScrubberPlayer
      shots={shots}
      onSelectShot={() => {}}
      onQuickAction={() => {}}
      mediaUrl={mediaUrl}
    />,
  );

/** The compiled s001: every shot carries both a clip and the still it was drafted from. */
const compiledShot = (n: number) =>
  shot({
    id: `s001.0${n}`,
    shot_number: `s001.0${n}`,
    clip: `render/s001/s001.0${n}.mp4`,
    thumbnail_url: `assets/s001/s001.0${n}_v0.png`,
  });

// --- the defect ------------------------------------------------------------

describe("a shot that has a compiled clip is played, not shown as a picture", () => {
  test("the frame for a compiled shot is a video element bound to the clip", () => {
    // THE DEFECT, stated about rendered output. Before the fix this component
    // contained no <video> at all, and `getThumbnailSrc` resolved thumbnail
    // first — so with both fields set, as every compiled shot has, the still
    // won and the clip was unreachable even had a player existed.
    mount([compiledShot(1)]);

    const frame = screen.getByTestId("scrubber-clip");
    expect(frame.tagName).toBe("VIDEO");
    expect(frame.getAttribute("src")).toBe("/media/render/s001/s001.01.mp4");

    // Not merely "a video exists somewhere" — the still must NOT be what the
    // screen is showing. A version that rendered both would pass the assertion
    // above and still hand the reviewer a picture over the top of the clip.
    expect(screen.queryByTestId("scrubber-still")).toBeNull();
    expect(screen.getByTestId("scrubber-screen").querySelector("img")).toBeNull();
  });

  test("the still is not used as a poster behind the clip either", () => {
    // A poster would put the drawing on screen for as long as the clip took to
    // buffer, indistinguishably from the clip itself.
    mount([compiledShot(1)]);
    expect(screen.getByTestId("scrubber-clip").getAttribute("poster")).toBeNull();
  });

  test("pressing play starts the clip and the speed control reaches playbackRate", () => {
    // The old timer advanced an index while the picture never moved. If the
    // transport does not reach the element, the button animates and nothing
    // plays — a state that reads as "playing" and is not.
    mount([compiledShot(1)]);
    const transport = screen.getByTestId("scrubber-transport");
    expect(playSpy).not.toHaveBeenCalled();

    act(() => {
      fireEvent.click(transport);
    });
    expect(playSpy).toHaveBeenCalled();

    const frame = screen.getByTestId("scrubber-clip") as HTMLVideoElement;
    act(() => {
      fireEvent.click(screen.getByText("2x"));
    });
    expect(frame.playbackRate).toBe(2);

    act(() => {
      fireEvent.click(transport);
    });
    expect(pauseSpy).toHaveBeenCalled();
  });

  test("the still timer does not run over a clip and cut it short", () => {
    // The planned `camera.duration` is not the rendered runtime: shots are
    // quantized at plan time and the render is fit afterwards, so a 4.0s plan
    // routinely produces a 4.3s file. Left running, the still timer would
    // advance at 4.0s while the picture still had a third of a second to go,
    // and the reviewer would never see the end of any shot. Only `onEnded`
    // moves a clip on.
    vi.useFakeTimers();
    try {
      mount([compiledShot(1), compiledShot(2)]);
      act(() => {
        fireEvent.click(screen.getByTestId("scrubber-transport"));
      });
      // Just past the planned 4.0s, deliberately. Run far enough and the timer
      // over-advances past the last shot in a single batch and `shots[i] ||
      // shots[0]` lands back on shot 1 — which would make this assertion pass
      // while the timer was demonstrably still driving the clip.
      act(() => {
        vi.advanceTimersByTime(4500);
      });
      expect(screen.getByTestId("scrubber-clip").getAttribute("src")).toBe(
        "/media/render/s001/s001.01.mp4",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  test("the clip's own playhead drives the readout, and its end advances the shot", () => {
    // The planned `camera.duration` is not the rendered runtime — s001 planned
    // to 4.0s a shot and the beat came back 37.417s after fit_clip. Driving the
    // readout from the timer over a clip would drift against the picture.
    mount([compiledShot(1), compiledShot(2)]);
    const frame = screen.getByTestId("scrubber-clip") as HTMLVideoElement;

    Object.defineProperty(frame, "currentTime", { value: 2.5, configurable: true });
    act(() => {
      fireEvent.timeUpdate(frame);
    });
    expect(screen.getByText(/2\.5s \//)).toBeTruthy();

    act(() => {
      fireEvent.ended(frame);
    });
    expect(screen.getByTestId("scrubber-clip").getAttribute("src")).toBe(
      "/media/render/s001/s001.02.mp4",
    );
  });
});

// --- §11.4 the still never stands in silently ------------------------------

describe("a still is labelled a still", () => {
  test("an uncompiled shot is shown as a still and named as one", () => {
    mount([shot({ thumbnail_url: "assets/s001/s001.01_v0.png" })]);

    expect(screen.queryByTestId("scrubber-clip")).toBeNull();
    expect(screen.getByTestId("scrubber-still").getAttribute("src")).toBe(
      "/media/assets/s001/s001.01_v0.png",
    );
    expect(screen.getByTestId("scrubber-source-badge").textContent).toContain(
      "STILL — NOT COMPILED",
    );
  });

  test("a compiled shot is named as the render", () => {
    mount([compiledShot(1)]);
    expect(screen.getByTestId("scrubber-source-badge").textContent).toContain("COMPILED CLIP");
  });

  test("a shot with neither says so rather than showing an empty player", () => {
    mount([shot()]);
    expect(screen.queryByTestId("scrubber-clip")).toBeNull();
    expect(screen.queryByTestId("scrubber-still")).toBeNull();
    expect(screen.getByTestId("scrubber-empty").textContent).toContain("No clip and no still");
  });

  test("a clip that fails to load reports the failure instead of falling back to the still", () => {
    // The substitution this codebase keeps having to un-write. Swapping the
    // drawing in on error would show a reviewer a frame they would reasonably
    // take for the render, on a shot whose render is missing.
    mount([compiledShot(1)]);

    act(() => {
      fireEvent.error(screen.getByTestId("scrubber-clip"));
    });

    expect(screen.queryByTestId("scrubber-still")).toBeNull();
    expect(screen.getByTestId("scrubber-playback-error").textContent).toContain(
      "Cannot play s001.01",
    );
  });
});

// --- the mixed scene -------------------------------------------------------

describe("a scene that is part compiled says which part", () => {
  test("mixed coverage names the count, and each shot carries its own label", () => {
    // Silent, this is a beat where some frames move and some do not, which reads
    // as a rendering fault. It is a plan half-compiled, and that is a different
    // thing to do about it.
    mount([
      compiledShot(1),
      shot({ id: "s001.02", shot_number: "s001.02", thumbnail_url: "assets/s001/s001.02_v0.png" }),
      compiledShot(3),
    ]);

    const mix = screen.getByTestId("scrubber-compile-mix").textContent || "";
    expect(mix).toContain("2 of 3");
    expect(mix).toContain("storyboard stills, not the render");

    // The track is where the whole beat is visible at once, so it is where the
    // split has to be legible without stepping through every shot.
    expect(screen.getByTestId("scrubber-seek-s001.01").getAttribute("data-compiled")).toBe("true");
    expect(screen.getByTestId("scrubber-seek-s001.02").getAttribute("data-compiled")).toBe("false");
    expect(screen.getByTestId("scrubber-seek-s001.03").getAttribute("data-compiled")).toBe("true");

    // Seeking to the uncompiled shot swaps the player for a labelled still.
    act(() => {
      fireEvent.click(screen.getByTestId("scrubber-seek-s001.02"));
    });
    expect(screen.queryByTestId("scrubber-clip")).toBeNull();
    expect(screen.getByTestId("scrubber-source-badge").textContent).toContain("NOT COMPILED");

    // ...and back again to a real player, not a stale one.
    act(() => {
      fireEvent.click(screen.getByTestId("scrubber-seek-s001.03"));
    });
    expect(screen.getByTestId("scrubber-clip").getAttribute("src")).toBe(
      "/media/render/s001/s001.03.mp4",
    );
  });

  test("a fully compiled beat still says the joins are not what is being watched", () => {
    // Per-shot playback is not the assembled cut. Claiming "you are watching the
    // render" without that qualifier would be the false success one level up.
    mount([compiledShot(1), compiledShot(2)]);
    const mix = screen.getByTestId("scrubber-compile-mix").textContent || "";
    expect(mix).toContain("All 2 shot(s) compiled");
    expect(mix).toContain("not the assembled joins");
  });

  test("a beat with nothing compiled does not imply anything was rendered", () => {
    mount([shot({ thumbnail_url: "assets/s001/s001.01_v0.png" })]);
    expect(screen.getByTestId("scrubber-compile-mix").textContent).toContain(
      "Nothing in this beat is compiled",
    );
  });
});

// --- the still clock still works ------------------------------------------

describe("stills still advance on their own clock", () => {
  test("an uncompiled shot advances on the timer, because a drawing has no playhead", () => {
    vi.useFakeTimers();
    try {
      const shots = [
        shot({ id: "s001.01", shot_number: "s001.01", thumbnail_url: "a.png", camera: { move: "static", duration: 1, speed: 1, amount: 0 } }),
        shot({ id: "s001.02", shot_number: "s001.02", thumbnail_url: "b.png" }),
      ];
      mount(shots);
      act(() => {
        fireEvent.click(screen.getByTestId("scrubber-transport"));
      });
      act(() => {
        vi.advanceTimersByTime(1200);
      });
      expect(screen.getByTestId("scrubber-still").getAttribute("src")).toBe("/media/b.png");
    } finally {
      vi.useRealTimers();
    }
  });
});
