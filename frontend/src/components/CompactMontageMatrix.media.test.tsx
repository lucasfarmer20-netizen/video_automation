/**
 * The montage matrix draws each tile with an element that can decode it.
 *
 * `<img src={mediaUrl(shot.thumbnail_url || shot.clip || "")} />` put an .mp4
 * into an image tag for any shot that had a clip and no still. An <img> cannot
 * decode video, the onError handler set display:none, and the tile went blank —
 * reporting nothing, and reading as a shot that was never drawn.
 *
 * The defect-proving assertion is about what is RENDERED for a shot that has a
 * clip, and it comes first.
 *
 *   §11.4  a still in a tile never stands in silently for the render
 */
import React from "react";
import { afterEach, describe, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import CompactMontageMatrix from "./CompactMontageMatrix";
import { DirectorShot, SceneSummary } from "../types/director";

afterEach(cleanup);

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

const scene = (shots_count: number): SceneSummary => ({
  scene_id: "s001",
  title: "The Drowned Bell",
  duration: 37,
  beats_count: 1,
  shots_count,
  estimated_cost: 0,
  status: "compiled",
  warnings_count: 0,
});

const mediaUrl = (p: string) => (p ? `/media/${p}` : "");

const mount = (allShots: DirectorShot[]) =>
  render(
    <CompactMontageMatrix
      scenes={[scene(allShots.length)]}
      activeSceneId="s001"
      allShots={allShots}
      onSelectScene={() => {}}
      onSelectShot={() => {}}
      mediaUrl={mediaUrl}
    />,
  );

describe("a tile never asks an <img> to decode an .mp4", () => {
  test("a compiled shot with no draft still is drawn by a video element", () => {
    // THE DEFECT, about rendered output. The old expression fell through to
    // `shot.clip` here and handed the path to an <img>.
    mount([shot({ clip: "render/s001/s001.01.mp4" })]);

    const tile = screen.getByTestId("matrix-tile-s001.01");
    expect(tile.querySelector("img")).toBeNull();

    const frame = screen.getByTestId("matrix-clip-s001.01");
    expect(frame.tagName).toBe("VIDEO");
    expect(frame.getAttribute("src")).toBe("/media/render/s001/s001.01.mp4#t=0.1");
  });

  test("a shot with a still uses the still — but the tile still says it is compiled", () => {
    // This grid is an index, not a player: 158 autoloading videos is its own
    // failure. The cheap frame is allowed, and the compiled marker is what
    // keeps it from being a silent substitution.
    mount([shot({ clip: "render/s001/s001.01.mp4", thumbnail_url: "assets/s001/v0.png" })]);

    const tile = screen.getByTestId("matrix-tile-s001.01");
    expect(tile.querySelector("img")?.getAttribute("src")).toBe("/media/assets/s001/v0.png");
    expect(tile.getAttribute("data-compiled")).toBe("true");
    expect(screen.getByTestId("matrix-compiled-s001.01")).toBeTruthy();
    expect(tile.getAttribute("title")).toContain("compiled clip");
  });

  test("an uncompiled shot is marked as not compiled", () => {
    mount([shot({ thumbnail_url: "assets/s001/v0.png" })]);

    const tile = screen.getByTestId("matrix-tile-s001.01");
    expect(tile.getAttribute("data-compiled")).toBe("false");
    expect(screen.queryByTestId("matrix-compiled-s001.01")).toBeNull();
    expect(tile.getAttribute("title")).toContain("not compiled");
  });

  test("a mixed scene distinguishes the two in the same row", () => {
    mount([
      shot({ id: "s001.01", shot_number: "s001.01", clip: "render/s001/s001.01.mp4", thumbnail_url: "a.png" }),
      shot({ id: "s001.02", shot_number: "s001.02", thumbnail_url: "b.png" }),
      shot({ id: "s001.03", shot_number: "s001.03", clip: "render/s001/s001.03.mp4" }),
    ]);

    expect(screen.getByTestId("matrix-tile-s001.01").getAttribute("data-compiled")).toBe("true");
    expect(screen.getByTestId("matrix-tile-s001.02").getAttribute("data-compiled")).toBe("false");
    expect(screen.getByTestId("matrix-tile-s001.03").getAttribute("data-compiled")).toBe("true");

    // The one with a clip and no still is the tile the old code blanked.
    expect(screen.getByTestId("matrix-tile-s001.03").querySelector("img")).toBeNull();
    expect(screen.getByTestId("matrix-clip-s001.03").tagName).toBe("VIDEO");
    // The one with a still and no clip must not have grown a video.
    expect(screen.queryByTestId("matrix-clip-s001.02")).toBeNull();
  });

  test("a shot with neither draws no media element at all", () => {
    mount([shot()]);
    const tile = screen.getByTestId("matrix-tile-s001.01");
    expect(tile.querySelector("img")).toBeNull();
    expect(tile.querySelector("video")).toBeNull();
  });
});
