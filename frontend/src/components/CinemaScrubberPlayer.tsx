"use client";

import React, { useState, useEffect, useRef } from "react";
import { DirectorShot } from "../types/director";
import { resolveShotMedia, compileMix } from "../lib/shotMedia";
import { Play, Pause, FastForward, Compass, Sparkles, Layers, Image as ImageIcon, Film, AlertTriangle } from "lucide-react";

interface CinemaScrubberPlayerProps {
  shots: DirectorShot[];
  onSelectShot: (shot: DirectorShot) => void;
  onQuickAction: (action: string, shot: DirectorShot) => void;
  mediaUrl: (path: string) => string;
}

/**
 * Per-shot `sub_clips`, not the assembled `beat_clip`.
 *
 * `plan.compiled` carries both, and the beat clip is the truer artefact — it is
 * what the cut looks like including the joins, which per-shot playback cannot
 * show. It still loses, for three reasons that are about this surface rather
 * than about which file is nicer:
 *
 * 1. **The beat clip does not exist when it is most needed.** `director.compile`
 *    refuses to concat if any shot failed ("the beat clip was left untouched")
 *    and leaves the finished sub-clips in place. So exactly in the mixed state
 *    — the one this component now has to present honestly — there is no beat
 *    clip to play, while every compiled shot is sitting there playable.
 * 2. **Seeking into it would require an offset this client cannot honestly
 *    compute.** The scrubber's whole interaction is per-shot: seek buttons
 *    sized by `camera.duration`, an elapsed-in-shot readout, quick actions
 *    bound to `activeShot`. Mapping a shot to a timecode in the beat clip means
 *    summing PLANNED durations, but the file is the post-`fit_clip` render whose
 *    runtime the server reports separately (37.417s for s001). Those two drift,
 *    and the drift would land as "the scrubber jumps to the wrong shot".
 * 3. **It is already surfaced, correctly, elsewhere.** `compiledRecordLine` in
 *    DirectorWorkspace names `compiled.beat_clip` and its runtime, and the
 *    assembly surfaces play the whole cut. This component is the shot-by-shot
 *    review; making it a second whole-cut player would duplicate that badly.
 *
 * The cost is real and is not papered over: per-shot playback shows the shots,
 * not the joins. That is stated on screen rather than left for the reviewer to
 * discover after approving.
 */
export default function CinemaScrubberPlayer({
  shots,
  onSelectShot,
  onQuickAction,
  mediaUrl,
}: CinemaScrubberPlayerProps) {
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [currentIndex, setCurrentIndex] = useState<number>(0);
  const [playbackSpeed, setPlaybackSpeed] = useState<number>(1.0);
  const [elapsedInShot, setElapsedInShot] = useState<number>(0);
  const [clipDuration, setClipDuration] = useState<number | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  // The still clock reads its own last value from the timer callback rather
  // than from a state updater, so `elapsedInShot` needs a mirror the callback
  // can see without re-subscribing. See the clock effect below.
  const elapsedRef = useRef<number>(0);

  const activeShot = shots[currentIndex] || shots[0];
  const media = activeShot ? resolveShotMedia(activeShot) : { kind: "none" as const, path: null };
  const isClip = media.kind === "clip";
  const mix = compileMix(shots);

  const setElapsed = (value: number) => {
    elapsedRef.current = value;
    setElapsedInShot(value);
  };

  /** Move to the next shot, forgetting everything that belonged to this one.
   *  A new shot is a new file: a 404 on shot 3 must not stay on screen over
   *  shot 4, and shot 3's runtime must not be quoted against shot 4's frame. */
  const advance = () => {
    setElapsed(0);
    setClipDuration(null);
    setPlaybackError(null);
    if (currentIndex < shots.length - 1) setCurrentIndex((idx) => idx + 1);
    else setIsPlaying(false);
  };

  // The still clock. A drawing has no playhead of its own, so a timer has to
  // invent one — but only for stills. Left running over a clip it would race
  // the video's own currentTime and advance off the PLANNED duration: shots are
  // quantized at plan time and the render is fit afterwards, so a 4.0s plan
  // routinely yields a 4.3s file and the reviewer would never see a shot end.
  //
  // The tick reads a local counter rather than `setElapsedInShot(prev => ...)`.
  // Inside a state updater the advance branch is a side effect during render;
  // it also mis-fires when many ticks batch into one render pass, walking the
  // index past the end of `shots` in a single flush.
  useEffect(() => {
    if (isClip || !isPlaying || !activeShot) return;
    let elapsed = elapsedRef.current;
    let finished = false;
    const timer = setInterval(() => {
      if (finished) return;
      elapsed += 0.1 * playbackSpeed;
      if (elapsed >= activeShot.camera.duration) {
        finished = true;
        advance();
      } else {
        setElapsed(elapsed);
      }
    }, 100);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isClip, isPlaying, currentIndex, activeShot, playbackSpeed, shots.length]);

  // For a clip the VIDEO is the clock, so play/pause has to reach the element
  // rather than only a boolean. If the browser refuses to play, that refusal
  // becomes visible state: a transport that reads "playing" over a frozen frame
  // is the same false-success this surface was built to stop claiming (§11.4).
  useEffect(() => {
    const el = videoRef.current;
    if (!el || !isClip) return;
    if (isPlaying) {
      const started = el.play();
      if (started && typeof started.catch === "function") {
        started.catch((err: unknown) => {
          setPlaybackError(err instanceof Error ? err.message : String(err));
          setIsPlaying(false);
        });
      }
    } else {
      el.pause();
    }
  }, [isPlaying, isClip, currentIndex]);

  useEffect(() => {
    const el = videoRef.current;
    if (el && isClip) el.playbackRate = playbackSpeed;
  }, [playbackSpeed, isClip, currentIndex]);

  const togglePlay = () => setIsPlaying(!isPlaying);

  const handleSeek = (index: number) => {
    setCurrentIndex(index);
    setElapsed(0);
    setClipDuration(null);
    setPlaybackError(null);
  };

  if (!activeShot) return null;

  const shownDuration = isClip && clipDuration ? clipDuration : activeShot.camera.duration;

  return (
    <div className="w-full glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800">
      {/* Player Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="p-2 rounded-lg bg-amber-500/15 text-amber-400 font-bold text-xs font-mono">
            CINEMA SCRUBBER
          </span>
          <h3 className="font-bold text-sm text-zinc-100">Rough-Cut Review</h3>
        </div>

        {/* Speed Controls */}
        <div className="flex items-center gap-1.5 font-mono text-xs">
          {[1.0, 1.5, 2.0].map((spd) => (
            <button
              key={spd}
              onClick={() => setPlaybackSpeed(spd)}
              className={`px-2.5 py-1 rounded border transition-colors ${
                playbackSpeed === spd
                  ? "bg-amber-500/20 border-amber-500 text-amber-300 font-bold"
                  : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:bg-zinc-800"
              }`}
            >
              {spd}x
            </button>
          ))}
        </div>
      </div>

      {/* What proportion of this is actually the render.
          Unstated, a mix of moving and frozen frames reads as a broken render,
          and — worse — a reviewer can approve a beat believing they watched it. */}
      <p
        data-testid="scrubber-compile-mix"
        className={`text-xs font-mono px-3 py-2 rounded-lg border ${
          mix.allCompiled
            ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-300"
            : "bg-amber-500/10 border-amber-500/40 text-amber-200"
        }`}
      >
        {mix.compiled === 0
          ? `Nothing in this beat is compiled. All ${mix.total} shot(s) below are ` +
            `storyboard stills, not the render.`
          : mix.allCompiled
          ? `All ${mix.total} shot(s) compiled — you are watching the render, ` +
            `shot by shot. Cuts between shots are not the assembled joins.`
          : `${mix.compiled} of ${mix.total} shot(s) compiled. The other ` +
            `${mix.uncompiled} are storyboard stills, not the render — each is ` +
            `labelled where it appears.`}
      </p>

      {/* Screen Frame */}
      <div
        data-testid="scrubber-screen"
        className="relative w-full aspect-video max-h-[420px] bg-zinc-950 rounded-xl overflow-hidden border border-zinc-800 flex items-center justify-center shadow-2xl"
      >
        {isClip && !playbackError ? (
          /* A VIDEO element, because DirectorShot.clip is render/<beat>/<shot>.mp4.
             Keyed by shot id so a seek swaps the file rather than leaving the
             previous one's decoded state under a new src. */
          <video
            key={activeShot.id}
            ref={videoRef}
            data-testid="scrubber-clip"
            src={mediaUrl(media.path || "")}
            playsInline
            preload="metadata"
            className="w-full h-full object-contain bg-black"
            onTimeUpdate={(e) => setElapsed(e.currentTarget.currentTime)}
            onLoadedMetadata={(e) => {
              const d = e.currentTarget.duration;
              setClipDuration(Number.isFinite(d) ? d : null);
              e.currentTarget.playbackRate = playbackSpeed;
            }}
            onEnded={advance}
            onError={() =>
              /* No silent fall-back to the still. A frame that fails to load is
                 a broken clip, and swapping the drawing in over it would hand
                 the reviewer the exact substitution this component was fixed to
                 stop making. */
              setPlaybackError(`the clip for ${activeShot.shot_number || activeShot.id} failed to load`)
            }
          />
        ) : playbackError ? (
          <div
            data-testid="scrubber-playback-error"
            className="flex flex-col items-center gap-2 text-amber-300 px-6 text-center"
          >
            <AlertTriangle className="w-8 h-8" />
            <span className="text-xs font-mono">
              Cannot play {activeShot.shot_number || activeShot.id}: {playbackError}
            </span>
            <span className="text-[11px] font-mono text-zinc-500 break-all">
              {mediaUrl(media.path || "")}
            </span>
          </div>
        ) : media.kind === "still" ? (
          <img
            data-testid="scrubber-still"
            src={mediaUrl(media.path || "")}
            alt={activeShot.subject}
            className="w-full h-full object-cover"
          />
        ) : (
          <div className="flex flex-col items-center gap-2 text-zinc-600" data-testid="scrubber-empty">
            <ImageIcon className="w-10 h-10 text-zinc-700 animate-pulse" />
            <span className="text-xs font-mono">
              No clip and no still for {activeShot.shot_number || activeShot.id}
            </span>
          </div>
        )}

        {/* Active Shot Overlay Badge */}
        <div className="absolute top-3 left-3 flex items-center gap-2">
          <span className="text-xs font-mono font-bold text-amber-400 bg-zinc-950/85 backdrop-blur px-3 py-1 rounded border border-amber-500/40">
            {activeShot.shot_number || activeShot.id} • {activeShot.shot_size} ({activeShot.angle})
          </span>
          <span className="text-xs font-mono font-bold text-zinc-300 bg-zinc-950/85 backdrop-blur px-3 py-1 rounded border border-zinc-800">
            {elapsedInShot.toFixed(1)}s / {shownDuration.toFixed(1)}s
          </span>
          {/* Says which of the two things the frame under it is. Without this
              the only difference between a compiled shot and a drafted one is
              whether it happens to be moving, which is not something a reviewer
              should have to infer. */}
          <span
            data-testid="scrubber-source-badge"
            className={`text-xs font-mono font-bold px-3 py-1 rounded border backdrop-blur flex items-center gap-1.5 ${
              isClip
                ? "text-emerald-300 bg-zinc-950/85 border-emerald-500/40"
                : "text-amber-300 bg-zinc-950/85 border-amber-500/40"
            }`}
          >
            {isClip ? <Film className="w-3.5 h-3.5" /> : <ImageIcon className="w-3.5 h-3.5" />}
            {isClip ? "COMPILED CLIP" : "STILL — NOT COMPILED"}
          </span>
        </div>

        {/* Pause Action HUD */}
        {!isPlaying && (
          <div className="absolute inset-0 bg-zinc-950/70 backdrop-blur-sm flex flex-col items-center justify-center gap-3 animate-in fade-in duration-150">
            <p className="text-xs font-mono text-zinc-300 font-semibold">
              Paused on Shot {activeShot.shot_number || activeShot.id} ({activeShot.purpose})
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => onQuickAction("swap_angle", activeShot)}
                className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <Compass className="w-3.5 h-3.5 text-blue-400" />
                Swap Angle
              </button>
              <button
                onClick={() => onQuickAction("extend_1s", activeShot)}
                className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <FastForward className="w-3.5 h-3.5 text-amber-400" />
                Extend +1s
              </button>
              <button
                onClick={() => onQuickAction("downgrade_parallax", activeShot)}
                className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <Layers className="w-3.5 h-3.5 text-emerald-400" />
                Set Parallax ($0)
              </button>
              <button
                onClick={() => onSelectShot(activeShot)}
                className="px-3 py-1.5 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5 shadow"
              >
                <Sparkles className="w-3.5 h-3.5" />
                Inspect Shot
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Controls & Sub-Shot Scrubber Bar */}
      <div className="flex items-center gap-3">
        <button
          onClick={togglePlay}
          data-testid="scrubber-transport"
          className="p-2.5 rounded-xl bg-amber-500 hover:bg-amber-400 text-zinc-950 font-bold transition shadow"
        >
          {isPlaying ? <Pause className="w-5 h-5 fill-current" /> : <Play className="w-5 h-5 fill-current ml-0.5" />}
        </button>

        {/* Sub-shot Scrubber Track */}
        <div className="flex-1 flex items-center gap-1 bg-zinc-950 p-1.5 rounded-xl border border-zinc-800 overflow-x-auto">
          {shots.map((shot, idx) => {
            const isActive = idx === currentIndex;
            const compiled = resolveShotMedia(shot).kind === "clip";
            return (
              <button
                key={shot.id}
                onClick={() => handleSeek(idx)}
                style={{ flexGrow: shot.camera.duration }}
                data-testid={`scrubber-seek-${shot.id}`}
                data-compiled={compiled ? "true" : "false"}
                title={
                  compiled
                    ? `${shot.shot_number || shot.id} — compiled clip`
                    : `${shot.shot_number || shot.id} — still, not compiled`
                }
                /* Dashed on an uncompiled shot: the track is the one place the
                   whole beat is visible at once, so which shots are real render
                   is readable without stepping through them. */
                className={`h-7 px-2 rounded font-mono text-[10px] font-bold truncate transition-all border ${
                  compiled ? "border-solid" : "border-dashed"
                } ${
                  isActive
                    ? "bg-amber-500 text-zinc-950 border-amber-300 neon-glow-amber"
                    : compiled
                    ? "bg-zinc-900 text-zinc-300 border-emerald-600/60 hover:bg-zinc-800 hover:text-zinc-100"
                    : "bg-zinc-900 text-zinc-500 border-zinc-700 hover:bg-zinc-800 hover:text-zinc-300"
                }`}
              >
                {shot.shot_number || shot.id} ({shot.camera.duration.toFixed(1)}s)
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
