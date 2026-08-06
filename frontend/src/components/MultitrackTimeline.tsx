"use client";

import React, { useState, useMemo, useEffect } from "react";
import { createPortal } from "react-dom";
import { Lock, Unlock, Film, Mic, Waves, Music, ZoomIn, ZoomOut, RefreshCw, Play, Pause, AlertTriangle, Plus, Trash2, Maximize2, Minimize2, Crosshair } from "lucide-react";

export interface SfxLayer {
  id: string;
  label?: string;
  prompt?: string;
  source?: string;
  gain?: number;
  offset?: number;
  fade_in?: number;
  fade_out?: number;
  url?: string | null;
}

export interface Shot {
  scene_id: string;
  narration: string;
  motion_type: string;
  camera: { move: string; duration: number; duration_locked?: boolean };
  draft_image: string | null;
  has_narration?: boolean;
  has_sfx?: boolean;
  sfx?: string;
  narration_url?: string | null;
  sfx_url?: string | null;
  gain_narration?: number;
  gain_sfx?: number;
  offset_narration?: number;
  fade_in_narration?: number;
  fade_out_narration?: number;
  sfx_layers_resolved?: SfxLayer[];
}

export interface MultitrackTimelineProps {
  shots: Shot[];
  /** { scene_id: { narration?: number[], sfx?: number[] } } from /api/audio/peaks */
  peaks?: Record<string, { narration?: number[]; sfx?: number[] }>;
  musicTrack?: string;
  mediaUrl: (p: string) => string;
  onUpdateCamera: (sceneId: string, camera: Record<string, number | boolean | string>) => void;
  onUpdateGain?: (sceneId: string, field: "gain_narration" | "gain_sfx", v: number) => void;
  onRegenNarration?: (sceneId: string) => void;
  onRegenSfx?: (sceneId: string) => void;
  busy?: Record<string, boolean>;
  /** The server-rendered preview and the timing it was actually built with. */
  previewUrl?: string | null;
  previewMeta?: {
    runtime: number; built_at: number; stale?: boolean; live_runtime?: number;
    beats: { scene_id: string; start: number; duration: number }[];
  } | null;
  onPatchNarration?: (sceneId: string, patch: Record<string, any>) => void;
  onPatchLayer?: (sceneId: string, layerId: string, patch: Record<string, any>) => void;
  onAddLayer?: (sceneId: string, prompt: string) => void;
  onDeleteLayer?: (sceneId: string, layerId: string) => void;
  onGenerateLayer?: (sceneId: string, layerId: string) => void;
  onAssemble?: (stage: string) => void;
}

const MOVES = ["static", "push_in", "push_out", "pan_left", "pan_right"];

const MOVE_BADGES: Record<string, string> = {
  push_in: "↘ Push In",
  push_out: "↖ Push Out",
  pan_left: "◄ Pan L",
  pan_right: "► Pan R",
  static: "▪ Static",
};

const TIER_COLOR: Record<string, string> = {
  parallax: "bg-gradient-to-r from-blue-950 via-blue-900 to-slate-900 border-blue-400 text-blue-100",
  ai_video: "bg-gradient-to-r from-purple-950 via-purple-900 to-slate-900 border-purple-400 text-purple-100",
  static: "bg-gradient-to-r from-zinc-900 via-zinc-800 to-zinc-900 border-zinc-500 text-zinc-200",
};

const tc = (s: number) => {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
};

/** Peak envelope as a smooth SVG gradient. */
function Waveform({ data, type = "amber" }: { data?: number[]; type?: "emerald" | "amber" | "purple" }) {
  if (!data || !data.length) return null;
  const n = data.length;
  const pts = data.map((v, i) => `${(i / (n - 1)) * 100},${50 - v * 46}`).join(" ")
    + " " + data.map((v, i) => `${((n - 1 - i) / (n - 1)) * 100},${50 + data[n - 1 - i] * 46}`).join(" ");

  const gradientId = `wf-grad-${type}`;

  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none"
         className="absolute inset-0 w-full h-full pointer-events-none">
      <defs>
        <linearGradient id={gradientId} x1="0%" y1="0%" x2="0%" y2="100%">
          {type === "emerald" ? (
            <>
              <stop offset="0%" stopColor="#34d399" stopOpacity="0.9" />
              <stop offset="50%" stopColor="#10b981" stopOpacity="0.65" />
              <stop offset="100%" stopColor="#059669" stopOpacity="0.35" />
            </>
          ) : type === "purple" ? (
            <>
              <stop offset="0%" stopColor="#e879f9" stopOpacity="0.9" />
              <stop offset="50%" stopColor="#c084fc" stopOpacity="0.65" />
              <stop offset="100%" stopColor="#9333ea" stopOpacity="0.35" />
            </>
          ) : (
            <>
              <stop offset="0%" stopColor="#fde047" stopOpacity="0.95" />
              <stop offset="50%" stopColor="#fbbf24" stopOpacity="0.7" />
              <stop offset="100%" stopColor="#ea580c" stopOpacity="0.4" />
            </>
          )}
        </linearGradient>
      </defs>
      <polygon points={pts} fill={`url(#${gradientId})`} />
    </svg>
  );
}

/** SVG Volume Fade Envelope overlay for fade_in / fade_out visualization */
function FadeEnvelope({ fadeIn, fadeOut, duration }: { fadeIn?: number; fadeOut?: number; duration: number }) {
  const fin = Math.max(0, fadeIn || 0);
  const fout = Math.max(0, fadeOut || 0);
  if (fin <= 0 && fout <= 0) return null;
  const dur = Math.max(0.1, duration);
  const finPct = Math.min(100, (fin / dur) * 100);
  const foutPct = Math.min(100, (fout / dur) * 100);

  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 w-full h-full pointer-events-none z-10">
      {fin > 0 && (
        <polygon points={`0,100 ${finPct},0 0,0`} fill="rgba(9,9,11,0.65)" />
      )}
      {fout > 0 && (
        <polygon points={`100,100 ${100 - foutPct},0 100,0`} fill="rgba(9,9,11,0.65)" />
      )}
    </svg>
  );
}

const toDb = (g: number) => (g <= 0.0001 ? -60 : 20 * Math.log10(g));
const fromDb = (db: number) => (db <= -39.5 ? 0 : Math.pow(10, db / 20));

const TONE: Record<string, string> = {
  emerald: "bg-emerald-500/20 text-emerald-300 border-emerald-400/40",
  amber: "bg-amber-500/20 text-amber-300 border-amber-400/40",
};

/** One track of the selected clip: audition, trim, regenerate. */
function ClipAudio({ label, tone, url, present, wanted, gain, busy, onGain, onRegen,
                     regenLabel, note, onDelete }: any) {
  const [playing, setPlaying] = React.useState(false);
  const [local, setLocal] = React.useState<number | null>(null);
  const ref = React.useRef<HTMLAudioElement | null>(null);
  const db = local ?? toDb(gain ?? 1);
  const commit = () => { if (local !== null) { onGain?.(fromDb(local)); setLocal(null); } };

  const toggle = () => {
    if (!url) return;
    if (!ref.current) { ref.current = new Audio(url); ref.current.onended = () => setPlaying(false); }
    if (playing) { ref.current.pause(); ref.current.currentTime = 0; setPlaying(false); }
    else { ref.current.volume = Math.min(1, gain ?? 1); ref.current.play(); setPlaying(true); }
  };

  return (
    <div className="flex items-center gap-2.5 text-[10px] font-mono bg-zinc-900/80 p-2.5 rounded-xl border border-zinc-800 shadow-sm">
      <span className={`px-2 py-1 rounded-md border font-extrabold shrink-0 shadow-sm ${TONE[tone]}`}>{label}</span>
      {present ? (
        <>
          <button onClick={toggle} className="text-zinc-300 hover:text-white transition-colors w-5 shrink-0 flex items-center justify-center"
                  title="Audition">{playing ? "■" : <Play className="h-3.5 w-3.5 fill-current text-amber-400" />}</button>
          <input type="range" min={-40} max={12} step={0.5}
                 value={Math.max(-40, Math.min(12, db))}
                 onChange={(e) => setLocal(parseFloat(e.target.value))}
                 onPointerUp={commit} onKeyUp={commit} onBlur={commit}
                 className="w-40 accent-amber-400 h-1.5 rounded-lg cursor-pointer bg-zinc-800" title="Trim on top of the episode bus" />
          <span className={`w-12 text-right tabular-nums font-bold ${
            Math.abs(db) < 0.25 ? "text-zinc-500" : "text-amber-400"}`}>
            {db <= -39.5 ? "−∞" : `${db >= 0 ? "+" : ""}${db.toFixed(1)}`}
          </span>
        </>
      ) : (
        <span className="text-zinc-500 italic">{wanted ? "not generated" : "none for this beat"}</span>
      )}
      {onRegen && wanted && (
        <button onClick={onRegen} disabled={busy}
          className="ml-1 flex items-center gap-1.5 px-3 py-1 rounded-lg border border-zinc-700 bg-zinc-950 text-zinc-200 hover:text-white hover:border-zinc-500 disabled:opacity-40 transition-all hover:scale-[1.02] active:scale-[0.98] shrink-0 shadow-sm">
          <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin text-amber-400" : ""}`} />
          {busy ? "working…" : regenLabel}
        </button>
      )}
      {onDelete && (
        <button onClick={onDelete}
          className="text-zinc-400 hover:text-red-400 p-1.5 transition-colors shrink-0 rounded-lg hover:bg-red-500/10" title="Delete layer">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      )}
      <span className="text-zinc-500 truncate ml-auto">{note}</span>
    </div>
  );
}

export default function MultitrackTimeline({
  shots, musicTrack, mediaUrl, onUpdateCamera, peaks,
  onUpdateGain, onRegenNarration, onRegenSfx, busy, previewUrl, previewMeta,
  onPatchNarration, onPatchLayer, onAddLayer, onDeleteLayer, onGenerateLayer, onAssemble
}: MultitrackTimelineProps) {
  const videoRef = React.useRef<HTMLVideoElement | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [pxPerSec, setPxPerSec] = useState(4);
  const [selected, setSelected] = useState<string | null>(null);
  const [drag, setDrag] = useState<{ id: string; dur: number } | null>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [mounted, setMounted] = useState(false);
  React.useEffect(() => setMounted(true), []);
  const scrollRef = React.useRef<HTMLDivElement | null>(null);
  const [followPlayhead, setFollowPlayhead] = useState(true);
  // Set while we are scrolling programmatically, so our own scroll events do not
  // read as the user grabbing the bar.
  const autoScrolling = React.useRef(false);

  const [isFloatingPlayer, setIsFloatingPlayer] = useState(true);
  const [playerMinimized, setPlayerMinimized] = useState(false);

  const handleZoomIn = () => {
    setPxPerSec((z) => {
      if (z < 10) return z + 2;
      if (z < 30) return z + 5;
      if (z < 80) return z + 15;
      return Math.min(200, z + 25);
    });
  };

  const handleZoomOut = () => {
    setPxPerSec((z) => {
      if (z <= 10) return Math.max(1, z - 2);
      if (z <= 30) return z - 5;
      if (z <= 80) return z - 15;
      return Math.max(1, z - 25);
    });
  };

  // Audio drag state for offset adjustment & fade handle dragging
  const [audioDrag, setAudioDrag] = useState<{
    type: "narration" | "sfx";
    sceneId: string;
    layerId?: string;
    initialOffset: number;
    currentOffset: number;
    startX: number;
  } | null>(null);

  const [fadeDrag, setFadeDrag] = useState<{
    type: "narration" | "sfx";
    sceneId: string;
    layerId?: string;
    field: "fade_in" | "fade_out";
    initialVal: number;
    currentVal: number;
    startX: number;
    dur: number;
  } | null>(null);

  const { blocks, total } = useMemo(() => {
    let acc = 0;
    const b = shots.map((s) => {
      const start = acc;
      const dur = (drag && drag.id === s.scene_id ? drag.dur : s.camera?.duration) || 0;
      acc += dur;
      return { shot: s, start, dur };
    });
    return { blocks: b, total: acc };
  }, [shots, drag]);

  // Keyboard Shortcuts (Space for Play/Pause, Left/Right Arrows to jump beats, Esc to exit Fullscreen)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const active = document.activeElement;
      if (active && (
        active.tagName === "INPUT" ||
        active.tagName === "SELECT" ||
        active.tagName === "TEXTAREA" ||
        (active as HTMLElement).isContentEditable
      )) {
        return;
      }

      if (e.code === "Escape" && isFullscreen) {
        e.preventDefault();
        setIsFullscreen(false);
      } else if (e.code === "Space") {
        // A focused button or link owns Space -- stealing it makes controls look
        // dead to keyboard users.
        const tag = active?.tagName;
        if (tag === "BUTTON" || tag === "A" ||
            (active as HTMLElement | null)?.getAttribute?.("role") === "button") return;
        e.preventDefault();
        const v = videoRef.current;
        if (v) {
          if (v.paused) {
            v.play();
          } else {
            v.pause();
          }
        }
      } else if (e.code === "ArrowLeft") {
        e.preventDefault();
        const v = videoRef.current;
        if (!v || !blocks.length) return;
        const target = beatSeek(-1, v.currentTime);
        v.currentTime = target ?? 0;
      } else if (e.code === "ArrowRight") {
        e.preventDefault();
        const v = videoRef.current;
        if (!v || !blocks.length) return;
        const target = beatSeek(1, v.currentTime);
        if (target !== null) v.currentTime = target;
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [blocks, isFullscreen]);

  // Determine maximum SFX lanes across all shots
  const sfxLanesCount = useMemo(() => {
    let maxL = 1;
    shots.forEach((s) => {
      if (s.sfx_layers_resolved && s.sfx_layers_resolved.length > maxL) {
        maxL = s.sfx_layers_resolved.length;
      }
    });
    return maxL;
  }, [shots]);

  const sfxLaneKeys = useMemo(() => {
    if (sfxLanesCount <= 1) return [{ key: "A2", label: "A2 SFX", index: 0 }];
    const lanes = [];
    for (let i = 0; i < sfxLanesCount; i++) {
      const char = String.fromCharCode(97 + i);
      lanes.push({ key: `A2${char}`, label: `A2${char} SFX`, index: i });
    }
    return lanes;
  }, [sfxLanesCount]);

  // ---- time-base bridge -------------------------------------------------
  // The tracks are laid out in LIVE manifest time; the video plays in PREVIEW
  // time (whatever the durations were when it was rendered). They coincide only
  // while the preview is in sync. Mapping beat-by-beat keeps the playhead over
  // the right clip either way, instead of the readout naming one beat while the
  // line sits over another.
  const previewToX = React.useCallback((t: number): number => {
    const pb = previewMeta?.beats;
    if (!pb?.length) return t * pxPerSec;
    const i = pb.findIndex((x) => t >= x.start && t < x.start + x.duration);
    if (i < 0 || !blocks[i]) return t * pxPerSec;
    const lb = blocks.find((b) => b.shot.scene_id === pb[i].scene_id) ?? blocks[i];
    const frac = pb[i].duration > 0 ? (t - pb[i].start) / pb[i].duration : 0;
    return (lb.start + frac * lb.dur) * pxPerSec;
  }, [previewMeta, blocks, pxPerSec]);

  const xToPreview = React.useCallback((x: number): number => {
    const tLive = x / pxPerSec;
    const pb = previewMeta?.beats;
    if (!pb?.length) return tLive;
    const lb = blocks.find((b) => tLive >= b.start && tLive < b.start + b.dur);
    if (!lb) return tLive;
    const p = pb.find((y) => y.scene_id === lb.shot.scene_id);
    if (!p) return tLive;                       // beat added since the render
    const frac = lb.dur > 0 ? (tLive - lb.start) / lb.dur : 0;
    return p.start + frac * p.duration;
  }, [previewMeta, blocks, pxPerSec]);

  const zoomAt = React.useCallback((clientX: number, dir: -1 | 1) => {
    const el = scrollRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cursorX = clientX - rect.left + el.scrollLeft;   // px into the timeline
    const tAtCursor = cursorX / pxPerSec;                  // live seconds under the pointer
    setPxPerSec((z) => {
      const next = Math.max(1, Math.min(200, dir > 0 ? z * 1.15 : z / 1.15));
      // Re-anchor after React paints at the new scale.
      requestAnimationFrame(() => {
        const e2 = scrollRef.current;
        if (e2) e2.scrollLeft = Math.max(0, tAtCursor * next - (clientX - rect.left));
      });
      return next;
    });
  }, [pxPerSec]);

  // Keep the playhead on screen while it moves. Without this the timeline is
  // unusable at the higher zooms: at 200 px/s a ~1200px viewport shows about six
  // seconds of a five-and-a-half minute episode, so the playhead leaves the
  // screen within moments of pressing play and never returns.
  React.useEffect(() => {
    if (!followPlayhead) return;
    const el = scrollRef.current;
    if (!el) return;
    const x = previewToX(playhead);
    const left = el.scrollLeft;
    const right = left + el.clientWidth;
    // A margin so the playhead is not pinned to the very edge, and so a clip
    // just ahead of it stays readable.
    const margin = Math.min(160, el.clientWidth * 0.2);
    if (x < left + margin || x > right - margin) {
      autoScrolling.current = true;
      el.scrollTo({
        left: Math.max(0, x - el.clientWidth / 2),
        behavior: playing ? "smooth" : "auto",
      });
      // Clear on the next frame; "smooth" keeps firing scroll events after this
      // but they are ours, and re-arming on each playhead tick is enough.
      requestAnimationFrame(() => { autoScrolling.current = false; });
    }
  }, [playhead, followPlayhead, previewToX, playing]);

  // Grabbing the scrollbar mid-playback means you want to look somewhere else;
  // yanking the view back would fight you.
  React.useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      if (!(ev.ctrlKey || ev.metaKey)) return;   // plain wheel still scrolls
      ev.preventDefault();
      zoomAt(ev.clientX, ev.deltaY < 0 ? 1 : -1);
    };
    el.addEventListener("wheel", onWheel, { passive: false });

    const onUserScroll = () => {
      if (autoScrolling.current) return;
      if (playing) setFollowPlayhead(false);
    };
    el.addEventListener("wheel", onUserScroll, { passive: true });
    el.addEventListener("pointerdown", onUserScroll);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("wheel", onUserScroll);
      el.removeEventListener("pointerdown", onUserScroll);
    };
  }, [playing, zoomAt]);

  /** Preview time at the start of the beat before/after the playhead. */
  const beatSeek = React.useCallback((dir: -1 | 1, cur: number): number | null => {
    const pb = previewMeta?.beats;
    if (!pb?.length) return null;
    if (dir < 0) {
      const prev = [...pb].reverse().find((b) => b.start < cur - 0.2);
      return prev ? prev.start : 0;
    }
    const next = pb.find((b) => b.start > cur + 0.1);
    return next ? next.start : null;
  }, [previewMeta]);

  const atPlayhead = React.useMemo(() => {
    const beats = previewMeta?.beats;
    if (!beats?.length) return null;
    const b = beats.find((x) => playhead >= x.start && playhead < x.start + x.duration);
    return b ? `${b.scene_id} · ${(playhead - b.start).toFixed(1)}s in` : null;
  }, [playhead, previewMeta]);

  const width = Math.max(total * pxPerSec, 320);
  const sel = blocks.find((b) => b.shot.scene_id === selected);

  const tickEvery = pxPerSec >= 6 ? 15 : pxPerSec >= 3 ? 30 : 60;
  const ticks = Array.from({ length: Math.floor(total / tickEvery) + 1 }, (_, i) => i * tickEvery);

  const handleAddLayerPrompt = (sceneId: string) => {
    const prompt = window.prompt("Enter sound effect prompt (e.g. 'wind through sawali'):");
    if (prompt && prompt.trim()) {
      onAddLayer?.(sceneId, prompt.trim());
    }
  };

  const seekToBeat = (startSec: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = Math.max(0, startSec);
    }
  };

  // Rendered through a portal when floating: an ancestor with backdrop-filter
  // (.glass-surface) becomes the containing block for position:fixed, so a
  // "floating" player was being pinned inside the timeline card instead of the
  // viewport. Portalling to <body> escapes that.
  const playerFloats = isFullscreen || isFloatingPlayer;
  const playerNode = previewUrl ? (
  <div className={`transition-all duration-300 ${
            isFullscreen || isFloatingPlayer
              ? playerMinimized
                ? "fixed bottom-6 right-6 z-50 glass-surface px-4 py-2.5 rounded-full border border-amber-500/40 shadow-2xl flex items-center gap-3"
                : "fixed top-20 right-6 z-50 w-80 sm:w-96 glass-surface p-3.5 rounded-2xl border border-white/20 shadow-2xl flex flex-col gap-2.5 backdrop-blur-3xl animate-in fade-in zoom-in-95"
              : "px-5 py-3.5 border-b border-zinc-900 flex flex-col gap-2.5 bg-zinc-950/60"
          }`}>
            {/* Minimized Pill Bar */}
            {(isFullscreen || isFloatingPlayer) && playerMinimized ? (
              <div className="flex items-center gap-3 w-full justify-between font-mono text-xs">
                <div className="flex items-center gap-2 cursor-pointer" onClick={() => setPlayerMinimized(false)}>
                  <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                  <span className="font-extrabold text-zinc-100">Preview Player</span>
                  <span className="text-amber-400 font-bold">{tc(playhead)}</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={() => { const v = videoRef.current; if (!v) return; playing ? v.pause() : v.play(); }}
                    className="p-1.5 rounded-full bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 transition"
                    title="Play / Pause"
                  >
                    {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5 fill-current" />}
                  </button>
                  <button
                    onClick={() => setPlayerMinimized(false)}
                    className="p-1 text-zinc-400 hover:text-zinc-100 transition"
                    title="Expand Floating Player"
                  >
                    <Maximize2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ) : (
              <>
                {/* Header inside floating widget */}
                <div className="flex items-center justify-between text-xs font-mono">
                  <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                    <span className="font-extrabold text-zinc-100 uppercase tracking-wider">Preview Proxy</span>
                    <span className="text-amber-400 font-bold tabular-nums">{tc(playhead)}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {onAssemble && (
                      <button
                        onClick={() => onAssemble("preview")}
                        disabled={busy?.preview}
                        className="p-1 rounded-md bg-amber-500/20 text-amber-300 hover:bg-amber-500/30 transition border border-amber-500/30 text-[10px]"
                        title="Rebuild server preview proxy"
                      >
                        <RefreshCw className={`h-3 w-3 ${busy?.preview ? "animate-spin" : ""}`} />
                      </button>
                    )}
                    {(isFullscreen || isFloatingPlayer) && (
                      <button
                        onClick={() => setPlayerMinimized(true)}
                        className="p-1 text-zinc-400 hover:text-zinc-100 transition"
                        title="Minimize to Floating Pill"
                      >
                        <Minimize2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>

                {previewMeta?.stale && (
                  <div className="bg-amber-950/40 border border-amber-500/40 rounded-lg px-2.5 py-1.5 text-[10px] text-amber-200/90 font-mono flex items-center justify-between gap-2 shadow-inner">
                    <div className="flex items-center gap-1.5 truncate">
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-400" />
                      <span className="truncate">Cut changed · Rebuild proxy</span>
                    </div>
                    {onAssemble && (
                      <button
                        onClick={() => onAssemble("preview")}
                        disabled={busy?.preview}
                        className="px-2 py-0.5 rounded bg-amber-500 text-zinc-950 font-bold text-[9px] hover:bg-amber-400 transition shrink-0"
                      >
                        Rebuild
                      </button>
                    )}
                  </div>
                )}

                <div className="relative rounded-xl border border-zinc-800 bg-black overflow-hidden shadow-xl w-full">
                  <video
                    ref={videoRef}
                    src={previewUrl}
                    className="w-full h-auto block max-h-[220px] object-contain"
                    onTimeUpdate={(e) => setPlayhead((e.target as HTMLVideoElement).currentTime)}
                    onPlay={() => setPlaying(true)}
                    onPause={() => setPlaying(false)}
                    controls
                  />
                </div>

                <div className="flex items-center justify-between gap-2 text-[10px] font-mono text-zinc-400 pt-0.5">
                  <button
                    onClick={() => { const v = videoRef.current; if (!v) return; playing ? v.pause() : v.play(); }}
                    className="flex items-center gap-1.5 px-3 py-1 rounded-lg border border-zinc-700 bg-zinc-900 text-zinc-100 hover:text-white transition-all font-bold"
                  >
                    {playing ? <Pause className="h-3 w-3 text-amber-400" /> : <Play className="h-3 w-3 text-amber-400 fill-current" />}
                    <span>{playing ? "Pause" : "Play"}</span>
                  </button>
                  {atPlayhead && <span className="text-zinc-300 bg-zinc-900 px-2 py-0.5 rounded border border-zinc-800 font-bold truncate max-w-[160px]">{atPlayhead}</span>}
                </div>
              </>
            )}
          </div>
  ) : null;

  const shell = (
    <div className={`rounded-2xl glass-surface flex flex-col pt-1 transition-all duration-300 ${
      isFullscreen
        ? "fixed inset-0 z-50 p-4 md:p-6 bg-zinc-950/98 backdrop-blur-3xl overflow-y-auto flex flex-col w-screen h-screen rounded-none border-none"
        : "relative"
    }`}>
      {/* Header Controls */}
      <div className="px-5 py-3.5 border-b border-white/10 flex items-center justify-between flex-wrap gap-3 bg-zinc-950/40 backdrop-blur-md">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse neon-glow-amber" />
            <h3 className="text-zinc-100 font-extrabold text-xs uppercase tracking-wider font-mono">
              Timeline Editor {isFullscreen && "(Fullscreen Infinite NLE Studio)"}
            </h3>
          </div>
          <span className="text-[10px] font-mono text-zinc-400 bg-zinc-900 px-3 py-1 rounded-full border border-zinc-800">
            [Space] Play/Pause · [←/→] Jump Beats · [Esc] Exit Fullscreen
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] font-mono flex-wrap">
          <span className="text-zinc-400 font-semibold">{shots.length} beats</span>
          <span className="text-amber-400 font-extrabold bg-amber-500/15 px-3 py-1 rounded-full border border-amber-500/30">{tc(total)} ({(total / 60).toFixed(1)} min)</span>
          
          {/* Zoom Presets & Controls */}
          <div className="flex items-center gap-1.5 bg-zinc-900 px-2.5 py-1 rounded-full border border-zinc-800">
            <button onClick={handleZoomOut}
              className="text-zinc-400 hover:text-zinc-100 transition-colors p-0.5" title="Zoom out">
              <ZoomOut className="h-3.5 w-3.5" />
            </button>
            <span className="text-zinc-200 min-w-12 text-center tabular-nums font-bold">{pxPerSec}px/s</span>
            <button onClick={handleZoomIn}
              className="text-zinc-400 hover:text-zinc-100 transition-colors p-0.5" title="Zoom in">
              <ZoomIn className="h-3.5 w-3.5" />
            </button>
          </div>

          <button
            onClick={() => setFollowPlayhead((f) => !f)}
            title={followPlayhead
              ? "Following the playhead — scrolling away while playing turns this off"
              : "Not following. Turn on to keep the playhead on screen."}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[10px] font-bold transition-all ${
              followPlayhead
                ? "bg-amber-500 border-amber-400 text-zinc-950 shadow-[0_0_10px_rgba(245,158,11,0.3)]"
                : "bg-zinc-950/80 border-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            <Crosshair className="h-3 w-3" />
            Follow
          </button>

          <div className="hidden xl:flex items-center gap-1 bg-zinc-950/80 p-1 rounded-full border border-zinc-800 text-[10px]">
            {[4, 12, 30, 80, 150].map((preset) => (
              <button
                key={preset}
                onClick={() => setPxPerSec(preset)}
                className={`px-2 py-0.5 rounded-full font-bold transition-all ${
                  pxPerSec === preset
                    ? "bg-amber-500 text-zinc-950 font-extrabold shadow-sm"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                {preset}px/s
              </button>
            ))}
          </div>

          <button
            onClick={() => setIsFullscreen(!isFullscreen)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-zinc-800 bg-zinc-900/90 hover:bg-zinc-800 text-zinc-200 hover:text-white transition-all text-[11px] font-mono font-bold shadow-sm active:scale-95"
            title={isFullscreen ? "Exit Fullscreen (Esc)" : "Expand Fullscreen Canvas"}
          >
            {isFullscreen ? <Minimize2 className="h-3.5 w-3.5 text-amber-400" /> : <Maximize2 className="h-3.5 w-3.5 text-amber-400" />}
            <span>{isFullscreen ? "Exit Fullscreen" : "Fullscreen NLE"}</span>
          </button>
        </div>
      </div>

      {/* Floating Picture-in-Picture Preview Player Widget */}
      {playerNode && (playerFloats && mounted
        ? createPortal(playerNode, document.body)
        : playerNode)}

      {/* Main Track Workspace */}
      <div className={`flex ${isFullscreen ? "flex-1 min-h-0" : ""}`}>
        {/* Track Headers Column */}
        <div className="shrink-0 w-32 border-r border-zinc-900 bg-zinc-950 z-20">
          {/* Header ruler blank cell matched to h-11 height */}
          <div className="h-11 border-b border-zinc-900" />
          
          {/* V1 Header */}
          <div className="h-14 flex items-center gap-2 px-3 border-b border-zinc-900 text-[10px] font-mono text-blue-300 font-extrabold bg-blue-950/20">
            <Film className="h-3.5 w-3.5 shrink-0 text-blue-400" />
            <span className="truncate">V1 Stills</span>
          </div>

          {/* A1 Header */}
          <div className="h-14 flex items-center gap-2 px-3 border-b border-zinc-900 text-[10px] font-mono text-emerald-300 font-extrabold bg-emerald-950/20">
            <Mic className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
            <span className="truncate">A1 Narration</span>
          </div>

          {/* A2 SFX Headers (dynamic lanes) */}
          {sfxLaneKeys.map((lane) => (
            <div key={lane.key} className="h-14 flex items-center justify-between px-3 border-b border-zinc-900 text-[10px] font-mono text-amber-300 font-extrabold bg-amber-950/20">
              <div className="flex items-center gap-2 truncate">
                <Waves className="h-3.5 w-3.5 shrink-0 text-amber-400" />
                <span className="truncate">{lane.label}</span>
              </div>
              {onAddLayer && lane.index === 0 && (
                <button
                  onClick={() => handleAddLayerPrompt(selected || shots[0]?.scene_id)}
                  className="hover:text-amber-200 transition-transform hover:scale-110 text-amber-400 p-1 rounded hover:bg-amber-500/20"
                  title="Add sound effect layer (+ layer)"
                >
                  <Plus className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          ))}

          {/* A3 Header */}
          <div className="h-14 flex items-center gap-2 px-3 border-b border-zinc-900 text-[10px] font-mono text-purple-300 font-extrabold bg-purple-950/20">
            <Music className="h-3.5 w-3.5 shrink-0 text-purple-400" />
            <span className="truncate">A3 Music</span>
          </div>
        </div>

        {/* Scrollable Tracks Canvas */}
        <div ref={scrollRef} className="flex-1 min-w-0 overflow-x-auto bg-zinc-950/80">
          <div
            style={{ minWidth: `calc(${width}px + 70vw)`, width: `calc(${width}px + 70vw)` }}
            className="relative"
            onClick={(e) => {
              const v = videoRef.current;
              if (!v || !previewMeta) return;
              const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
              const t = xToPreview(e.clientX - rect.left);
              v.currentTime = Math.max(0, Math.min(previewMeta.runtime, t));
            }}
          >
            {/* Laser Playhead with Diamond Head inside visible ruler boundary */}
            {previewUrl && previewMeta && (
              <div
                className="absolute top-0 bottom-0 w-1 neon-laser-line z-30 pointer-events-none transition-all"
                style={{ left: previewToX(playhead) }}
              >
                <div className="absolute top-1 -left-[4px] w-3 h-3 rotate-45 bg-amber-400 border border-amber-100 neon-glow-amber" />
              </div>
            )}
            
            {/* Ruler with Prominent Clickable Beat Markers (Expanded to h-11 = 44px) */}
            <div className="h-11 border-b border-white/10 relative bg-zinc-950/60 backdrop-blur-md py-1">
              {ticks.map((t) => (
                <div key={t} className="absolute top-0 h-full border-l border-zinc-800/80"
                     style={{ left: t * pxPerSec }}>
                  <span className="pl-1.5 text-[8px] font-mono text-zinc-500 tabular-nums">{tc(t)}</span>
                </div>
              ))}

              {/* Beat Scene ID Marker Badges along ruler */}
              {blocks.map(({ shot, start }) => (
                <button
                  key={`marker-${shot.scene_id}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelected(shot.scene_id);
                    seekToBeat(start);
                  }}
                  title={`Jump playhead to ${shot.scene_id} (${start.toFixed(1)}s)`}
                  className="absolute bottom-1 flex items-center gap-1.5 px-2.5 py-0.5 rounded-full glass-pill text-[9px] font-mono text-amber-300 font-extrabold transition-all duration-150 hover:scale-105 z-20 cursor-pointer"
                  style={{ left: start * pxPerSec }}
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0 neon-glow-amber" />
                  <span className="truncate">{shot.scene_id}</span>
                </button>
              ))}
            </div>

            {/* V1 — Stills Track */}
            <div className="h-14 border-b border-zinc-900 relative bg-slate-950/40">
              {blocks.map(({ shot, start, dur }) => {
                const moveBadge = MOVE_BADGES[shot.camera?.move] || shot.camera?.move;
                return (
                  <button
                    key={shot.scene_id}
                    onClick={() => {
                      setSelected(shot.scene_id === selected ? null : shot.scene_id);
                      seekToBeat(start);
                    }}
                    title={`${shot.scene_id} · ${shot.motion_type} · ${shot.camera.move} · ${dur.toFixed(1)}s — Click to select & seek`}
                    className={`absolute top-1 bottom-1 rounded-lg border overflow-hidden transition-all duration-200 ${
                      TIER_COLOR[shot.motion_type] || TIER_COLOR.static
                    } ${
                      selected === shot.scene_id
                        ? "border-2 border-amber-400 shadow-[0_0_20px_rgba(245,158,11,0.4)] z-10 scale-[1.01]"
                        : "hover:border-blue-400/80 hover:brightness-110 hover:shadow-lg"
                    }`}
                    style={{ left: start * pxPerSec, width: Math.max(dur * pxPerSec - 2, 3) }}
                  >
                    {shot.draft_image && (
                      <div className="absolute inset-0">
                        <img src={mediaUrl(shot.draft_image)} alt="" className="w-full h-full object-cover opacity-60" />
                        <div className="absolute inset-0 bg-gradient-to-t from-zinc-950/90 via-zinc-950/20 to-transparent" />
                      </div>
                    )}
                    <div className="relative flex flex-col justify-between h-full p-1.5 z-10">
                      <div className="flex items-center justify-between">
                        <span className="text-[9px] font-mono text-zinc-100 font-extrabold drop-shadow-md">
                          {shot.scene_id}
                        </span>
                        {shot.camera?.duration_locked && (
                          <Lock className="h-2.5 w-2.5 text-amber-300 shrink-0 drop-shadow" />
                        )}
                      </div>
                      {moveBadge && (
                        <span className="text-[8px] font-mono text-amber-300 font-bold bg-zinc-950/90 px-1.5 py-0.5 rounded-md border border-amber-400/50 shadow-sm w-fit drop-shadow">
                          {moveBadge}
                        </span>
                      )}
                    </div>
                    {/* Trim handle */}
                    <span
                      onPointerDown={(e) => {
                        e.stopPropagation();
                        (e.target as HTMLElement).setPointerCapture(e.pointerId);
                        setDrag({ id: shot.scene_id, dur });
                      }}
                      onPointerMove={(e) => {
                        if (!drag || drag.id !== shot.scene_id) return;
                        const delta = e.movementX / pxPerSec;
                        setDrag({ id: shot.scene_id, dur: Math.max(0.5, drag.dur + delta) });
                      }}
                      onPointerUp={(e) => {
                        e.stopPropagation();
                        if (drag && drag.id === shot.scene_id) {
                          const v = Math.round(drag.dur * 10) / 10;
                          if (Math.abs(v - (shot.camera?.duration || 0)) > 0.05)
                            onUpdateCamera(shot.scene_id, { duration: v });
                        }
                        setDrag(null);
                      }}
                      title="Drag to trim duration"
                      className="absolute right-0 top-0 bottom-0 w-2 cursor-ew-resize bg-amber-400/0 hover:bg-amber-400/60 transition-colors"
                    />
                  </button>
                );
              })}
            </div>

            {/* A1 — Narration Track */}
            <div className="h-14 border-b border-zinc-900 relative bg-emerald-950/20">
              {blocks.map(({ shot, start, dur }) => {
                const present = shot.has_narration;
                const wanted = Boolean(shot.narration?.trim());
                if (!wanted) return null;
                const env = peaks?.[shot.scene_id]?.narration;

                const isDragging = audioDrag?.type === "narration" && audioDrag.sceneId === shot.scene_id;
                const isFadeDragging = fadeDrag?.type === "narration" && fadeDrag.sceneId === shot.scene_id;
                
                const offsetSec = isDragging ? audioDrag.currentOffset : (shot.offset_narration || 0);
                const fadeInSec = (isFadeDragging && fadeDrag.field === "fade_in") ? fadeDrag.currentVal : (shot.fade_in_narration || 0);
                const fadeOutSec = (isFadeDragging && fadeDrag.field === "fade_out") ? fadeDrag.currentVal : (shot.fade_out_narration || 0);
                const blockLeft = (start + offsetSec) * pxPerSec;

                return (
                  <div
                    key={shot.scene_id}
                    onPointerDown={(e) => {
                      if (e.button !== 0) return;
                      e.stopPropagation();
                      (e.target as HTMLElement).setPointerCapture(e.pointerId);
                      setAudioDrag({
                        type: "narration",
                        sceneId: shot.scene_id,
                        initialOffset: shot.offset_narration || 0,
                        currentOffset: shot.offset_narration || 0,
                        startX: e.clientX,
                      });
                    }}
                    onPointerMove={(e) => {
                      if (!audioDrag || audioDrag.type !== "narration" || audioDrag.sceneId !== shot.scene_id) return;
                      const deltaSec = (e.clientX - audioDrag.startX) / pxPerSec;
                      setAudioDrag((prev) => prev ? {
                        ...prev,
                        currentOffset: Math.round((prev.initialOffset + deltaSec) * 100) / 100
                      } : null);
                    }}
                    onPointerUp={(e) => {
                      e.stopPropagation();
                      if (audioDrag && audioDrag.type === "narration" && audioDrag.sceneId === shot.scene_id) {
                        if (Math.abs(audioDrag.currentOffset - audioDrag.initialOffset) > 0.05) {
                          onPatchNarration?.(shot.scene_id, { offset_narration: audioDrag.currentOffset });
                        }
                      }
                      setAudioDrag(null);
                    }}
                    title={
                      present
                        ? `${shot.scene_id} narration (offset: ${offsetSec >= 0 ? "+" : ""}${offsetSec.toFixed(1)}s, fade in: ${fadeInSec.toFixed(1)}s, fade out: ${fadeOutSec.toFixed(1)}s) - Drag block for offset, drag top edges for fades`
                        : `${shot.scene_id}: narration not generated yet`
                    }
                    className={`group absolute top-2 bottom-2 rounded-lg border overflow-hidden cursor-grab active:cursor-grabbing transition-all duration-150 ${
                      !present ? "border-dashed border-zinc-700 bg-zinc-900/40"
                      : "bg-emerald-950/80 border-emerald-500/60 hover:border-emerald-400 hover:shadow-[0_0_15px_rgba(52,211,153,0.3)]"
                    } ${isDragging ? "ring-2 ring-emerald-400 shadow-[0_0_20px_rgba(52,211,153,0.4)] z-10" : ""}`}
                    style={{ left: blockLeft, width: Math.max(dur * pxPerSec - 2, 3) }}
                  >
                    {present && <Waveform data={env} type="emerald" />}

                    {/* Fade Ramps Overlay */}
                    <FadeEnvelope fadeIn={fadeInSec} fadeOut={fadeOutSec} duration={dur} />

                    {/* Interactive Fade Handles (Top Left / Right) */}
                    <span
                      onPointerDown={(e) => {
                        e.stopPropagation();
                        (e.target as HTMLElement).setPointerCapture(e.pointerId);
                        setFadeDrag({
                          type: "narration",
                          sceneId: shot.scene_id,
                          field: "fade_in",
                          initialVal: shot.fade_in_narration || 0,
                          currentVal: shot.fade_in_narration || 0,
                          startX: e.clientX,
                          dur,
                        });
                      }}
                      onPointerMove={(e) => {
                        if (!fadeDrag || fadeDrag.type !== "narration" || fadeDrag.sceneId !== shot.scene_id || fadeDrag.field !== "fade_in") return;
                        const deltaSec = (e.clientX - fadeDrag.startX) / pxPerSec;
                        const newVal = Math.max(0, Math.min(dur, Math.round((fadeDrag.initialVal + deltaSec) * 10) / 10));
                        setFadeDrag((prev) => prev ? { ...prev, currentVal: newVal } : null);
                      }}
                      onPointerUp={(e) => {
                        e.stopPropagation();
                        if (fadeDrag && fadeDrag.type === "narration" && fadeDrag.sceneId === shot.scene_id && fadeDrag.field === "fade_in") {
                          onPatchNarration?.(shot.scene_id, { fade_in_narration: fadeDrag.currentVal });
                        }
                        setFadeDrag(null);
                      }}
                      title="Drag to set Fade In duration"
                      className="absolute top-0 left-0 w-3.5 h-3.5 bg-emerald-400 border border-emerald-100 shadow-[0_0_8px_#34d399] cursor-ew-resize opacity-0 group-hover:opacity-100 transition-opacity z-30 rounded-br-md"
                    />

                    <span
                      onPointerDown={(e) => {
                        e.stopPropagation();
                        (e.target as HTMLElement).setPointerCapture(e.pointerId);
                        setFadeDrag({
                          type: "narration",
                          sceneId: shot.scene_id,
                          field: "fade_out",
                          initialVal: shot.fade_out_narration || 0,
                          currentVal: shot.fade_out_narration || 0,
                          startX: e.clientX,
                          dur,
                        });
                      }}
                      onPointerMove={(e) => {
                        if (!fadeDrag || fadeDrag.type !== "narration" || fadeDrag.sceneId !== shot.scene_id || fadeDrag.field !== "fade_out") return;
                        const deltaSec = (fadeDrag.startX - e.clientX) / pxPerSec;
                        const newVal = Math.max(0, Math.min(dur, Math.round((fadeDrag.initialVal + deltaSec) * 10) / 10));
                        setFadeDrag((prev) => prev ? { ...prev, currentVal: newVal } : null);
                      }}
                      onPointerUp={(e) => {
                        e.stopPropagation();
                        if (fadeDrag && fadeDrag.type === "narration" && fadeDrag.sceneId === shot.scene_id && fadeDrag.field === "fade_out") {
                          onPatchNarration?.(shot.scene_id, { fade_out_narration: fadeDrag.currentVal });
                        }
                        setFadeDrag(null);
                      }}
                      title="Drag to set Fade Out duration"
                      className="absolute top-0 right-0 w-3.5 h-3.5 bg-emerald-400 border border-emerald-100 shadow-[0_0_8px_#34d399] cursor-ew-resize opacity-0 group-hover:opacity-100 transition-opacity z-30 rounded-bl-md"
                    />

                    {present && !env && dur * pxPerSec > 40 && (
                      <span className="absolute inset-0 flex items-center justify-center text-[8px] font-mono text-zinc-500">
                        no waveform
                      </span>
                    )}
                    <div className="absolute inset-0 flex items-center justify-between px-2 pointer-events-none z-20">
                      <span className="text-[8px] font-mono text-emerald-200 font-extrabold drop-shadow">
                        Narration
                      </span>
                      {offsetSec !== 0 && (
                        <span className="text-[8px] font-mono text-emerald-300 font-bold bg-zinc-950/90 border border-emerald-500/40 px-1 rounded ml-1 shrink-0">
                          {offsetSec > 0 ? `+${offsetSec.toFixed(1)}s` : `${offsetSec.toFixed(1)}s`}
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* A2 — Dynamic Multi-Lane SFX Tracks */}
            {sfxLaneKeys.map((lane) => (
              <div key={lane.key} className="h-14 border-b border-zinc-900 relative bg-amber-950/20">
                {blocks.map(({ shot, start, dur }) => {
                  const resolvedLayers = shot.sfx_layers_resolved;
                  const hasResolved = resolvedLayers && resolvedLayers.length > 0;
                  
                  let layer: SfxLayer | null = null;
                  let present = false;
                  let wanted = false;

                  if (hasResolved) {
                    if (lane.index < resolvedLayers.length) {
                      layer = resolvedLayers[lane.index];
                      present = Boolean(layer.url || layer.source === "generated");
                      wanted = Boolean(layer.prompt?.trim());
                    }
                  } else if (lane.index === 0) {
                    present = Boolean(shot.has_sfx);
                    wanted = Boolean(shot.sfx?.trim());
                  }

                  if (!wanted && !layer) return null;

                  const layerId = layer?.id || "legacy";
                  const isDragging = audioDrag?.type === "sfx" && audioDrag.sceneId === shot.scene_id && audioDrag.layerId === layerId;
                  const isFadeDragging = fadeDrag?.type === "sfx" && fadeDrag.sceneId === shot.scene_id && fadeDrag.layerId === layerId;

                  const currentLayerOffset = layer ? (layer.offset || 0) : 0;
                  const offsetSec = isDragging ? audioDrag.currentOffset : currentLayerOffset;
                  
                  const fadeInSec = (isFadeDragging && fadeDrag.field === "fade_in") ? fadeDrag.currentVal : (layer?.fade_in || 0);
                  const fadeOutSec = (isFadeDragging && fadeDrag.field === "fade_out") ? fadeDrag.currentVal : (layer?.fade_out || 0);

                  const blockLeft = (start + offsetSec) * pxPerSec;
                  const env = lane.index === 0 ? peaks?.[shot.scene_id]?.sfx : undefined;

                  return (
                    <div
                      key={`${shot.scene_id}-${layerId}`}
                      onPointerDown={(e) => {
                        if (e.button !== 0 || !layer) return;
                        e.stopPropagation();
                        (e.target as HTMLElement).setPointerCapture(e.pointerId);
                        setAudioDrag({
                          type: "sfx",
                          sceneId: shot.scene_id,
                          layerId: layerId,
                          initialOffset: currentLayerOffset,
                          currentOffset: currentLayerOffset,
                          startX: e.clientX,
                        });
                      }}
                      onPointerMove={(e) => {
                        if (!audioDrag || audioDrag.type !== "sfx" || audioDrag.sceneId !== shot.scene_id || audioDrag.layerId !== layerId) return;
                        const deltaSec = (e.clientX - audioDrag.startX) / pxPerSec;
                        setAudioDrag((prev) => prev ? {
                          ...prev,
                          currentOffset: Math.round((prev.initialOffset + deltaSec) * 100) / 100
                        } : null);
                      }}
                      onPointerUp={(e) => {
                        e.stopPropagation();
                        if (audioDrag && audioDrag.type === "sfx" && audioDrag.sceneId === shot.scene_id && audioDrag.layerId === layerId) {
                          if (Math.abs(audioDrag.currentOffset - audioDrag.initialOffset) > 0.05) {
                            onPatchLayer?.(shot.scene_id, layerId, { offset: audioDrag.currentOffset });
                          }
                        }
                        setAudioDrag(null);
                      }}
                      title={
                        present
                          ? `${shot.scene_id} SFX ${layer?.label ? `(${layer.label})` : ""}: ${layer?.prompt || shot.sfx} (offset: ${offsetSec >= 0 ? "+" : ""}${offsetSec.toFixed(1)}s, fade in: ${fadeInSec.toFixed(1)}s, fade out: ${fadeOutSec.toFixed(1)}s) - Drag block for offset, drag top edges for fades`
                          : `${shot.scene_id}: SFX not generated yet`
                      }
                      className={`group absolute top-2 bottom-2 rounded-lg border overflow-hidden transition-all duration-150 ${
                        layer ? "cursor-grab active:cursor-grabbing" : ""
                      } ${
                        !present ? "border-dashed border-zinc-700 bg-zinc-900/40"
                        : "bg-amber-950/80 border-amber-500/60 hover:border-amber-400 hover:shadow-[0_0_15px_rgba(251,191,36,0.3)]"
                      } ${isDragging ? "ring-2 ring-amber-400 shadow-[0_0_20px_rgba(251,191,36,0.4)] z-10" : ""}`}
                      style={{ left: blockLeft, width: Math.max(dur * pxPerSec - 2, 3) }}
                    >
                      {/* Waveform */}
                      {present && <Waveform data={env} type="amber" />}

                      {/* Fade Ramps Overlay */}
                      <FadeEnvelope fadeIn={fadeInSec} fadeOut={fadeOutSec} duration={dur} />

                      {/* Interactive Fade Handles (Top Left / Right) */}
                      {layer && (
                        <>
                          <span
                            onPointerDown={(e) => {
                              e.stopPropagation();
                              (e.target as HTMLElement).setPointerCapture(e.pointerId);
                              setFadeDrag({
                                type: "sfx",
                                sceneId: shot.scene_id,
                                layerId,
                                field: "fade_in",
                                initialVal: layer.fade_in || 0,
                                currentVal: layer.fade_in || 0,
                                startX: e.clientX,
                                dur,
                              });
                            }}
                            onPointerMove={(e) => {
                              if (!fadeDrag || fadeDrag.type !== "sfx" || fadeDrag.layerId !== layerId || fadeDrag.field !== "fade_in") return;
                              const deltaSec = (e.clientX - fadeDrag.startX) / pxPerSec;
                              const newVal = Math.max(0, Math.min(dur, Math.round((fadeDrag.initialVal + deltaSec) * 10) / 10));
                              setFadeDrag((prev) => prev ? { ...prev, currentVal: newVal } : null);
                            }}
                            onPointerUp={(e) => {
                              e.stopPropagation();
                              if (fadeDrag && fadeDrag.type === "sfx" && fadeDrag.layerId === layerId && fadeDrag.field === "fade_in") {
                                onPatchLayer?.(shot.scene_id, layerId, { fade_in: fadeDrag.currentVal });
                              }
                              setFadeDrag(null);
                            }}
                            title="Drag to set Fade In duration"
                            className="absolute top-0 left-0 w-3.5 h-3.5 bg-amber-400 border border-amber-100 shadow-[0_0_8px_#fbbf24] cursor-ew-resize opacity-0 group-hover:opacity-100 transition-opacity z-30 rounded-br-md"
                          />

                          <span
                            onPointerDown={(e) => {
                              e.stopPropagation();
                              (e.target as HTMLElement).setPointerCapture(e.pointerId);
                              setFadeDrag({
                                type: "sfx",
                                sceneId: shot.scene_id,
                                layerId,
                                field: "fade_out",
                                initialVal: layer.fade_out || 0,
                                currentVal: layer.fade_out || 0,
                                startX: e.clientX,
                                dur,
                              });
                            }}
                            onPointerMove={(e) => {
                              if (!fadeDrag || fadeDrag.type !== "sfx" || fadeDrag.layerId !== layerId || fadeDrag.field !== "fade_out") return;
                              const deltaSec = (fadeDrag.startX - e.clientX) / pxPerSec;
                              const newVal = Math.max(0, Math.min(dur, Math.round((fadeDrag.initialVal + deltaSec) * 10) / 10));
                              setFadeDrag((prev) => prev ? { ...prev, currentVal: newVal } : null);
                            }}
                            onPointerUp={(e) => {
                              e.stopPropagation();
                              if (fadeDrag && fadeDrag.type === "sfx" && fadeDrag.layerId === layerId && fadeDrag.field === "fade_out") {
                                onPatchLayer?.(shot.scene_id, layerId, { fade_out: fadeDrag.currentVal });
                              }
                              setFadeDrag(null);
                            }}
                            title="Drag to set Fade Out duration"
                            className="absolute top-0 right-0 w-3.5 h-3.5 bg-amber-400 border border-amber-100 shadow-[0_0_8px_#fbbf24] cursor-ew-resize opacity-0 group-hover:opacity-100 transition-opacity z-30 rounded-bl-md"
                          />
                        </>
                      )}

                      {/* Label & offset display */}
                      <div className="absolute inset-0 flex items-center justify-between px-2 pointer-events-none z-20">
                        <span className="text-[8px] font-mono text-amber-200 font-extrabold truncate drop-shadow">
                          {layer?.label || layer?.prompt || (shot.sfx ? "SFX" : "")}
                        </span>
                        {offsetSec !== 0 && (
                          <span className="text-[8px] font-mono text-amber-300 font-bold bg-zinc-950/90 border border-amber-500/40 px-1 rounded ml-1 shrink-0">
                            {offsetSec > 0 ? `+${offsetSec.toFixed(1)}s` : `${offsetSec.toFixed(1)}s`}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}

            {/* A3 — Music Track */}
            <div className="h-14 border-b border-zinc-900 relative bg-purple-950/20">
              {musicTrack ? (
                <div
                  className="absolute top-2 bottom-2 left-0 rounded-lg border bg-purple-950/80 border-purple-400/50 shadow-[0_0_15px_rgba(192,132,252,0.2)] flex items-center px-3"
                  style={{ width: Math.max(total * pxPerSec - 2, 3) }}
                  title={`${musicTrack} — looped to cover the runtime`}
                >
                  <span className="text-[9px] font-mono text-purple-200 font-extrabold truncate drop-shadow">
                    {musicTrack} · looped audio bed
                  </span>
                </div>
              ) : (
                <span className="absolute left-3 top-4 text-[10px] font-mono text-zinc-600">
                  no music bed selected
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Inspector for the selected beat */}
      {sel ? (
        <div className="px-5 py-4 border-t border-zinc-900 flex items-end gap-5 flex-wrap bg-zinc-950">
          <div>
            <span className="block text-[10px] text-zinc-400 font-mono mb-1 font-semibold">Selected Beat</span>
            <span className="text-sm font-mono text-amber-400 font-bold bg-amber-500/15 px-2.5 py-1 rounded-md border border-amber-500/40 shadow-sm">{sel.shot.scene_id}</span>
          </div>
          <div>
            <label className="block text-[10px] text-zinc-400 font-mono mb-1 font-semibold">Duration (s)</label>
            <input
              type="number" step={0.1} min={0.2}
              defaultValue={sel.dur} key={`${sel.shot.scene_id}-${sel.dur}`}
              onBlur={(e) => {
                const v = parseFloat(e.target.value);
                if (Number.isFinite(v) && Math.abs(v - sel.dur) > 0.05)
                  onUpdateCamera(sel.shot.scene_id, { duration: v });
              }}
              className="w-20 bg-zinc-900 text-zinc-100 border border-zinc-700 rounded-lg px-2.5 py-1.5 text-[11px] font-mono focus:border-amber-400 focus:outline-none transition-colors font-bold"
            />
          </div>
          <div>
            <label className="block text-[10px] text-zinc-400 font-mono mb-1 font-semibold">Camera Move</label>
            <select
              value={sel.shot.camera.move}
              onChange={(e) => onUpdateCamera(sel.shot.scene_id, { move: e.target.value })}
              className="bg-zinc-900 text-zinc-100 border border-zinc-700 rounded-lg px-2.5 py-1.5 text-[11px] font-mono focus:border-amber-400 focus:outline-none transition-colors cursor-pointer font-bold"
            >
              {MOVES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>
          <button
            onClick={() => onUpdateCamera(sel.shot.scene_id,
              { duration_locked: !sel.shot.camera.duration_locked })}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-[11px] font-mono transition-all hover:scale-[1.02] active:scale-[0.98] ${
              sel.shot.camera.duration_locked
                ? "border-amber-400 bg-amber-500/20 text-amber-300 font-bold shadow-[0_0_12px_rgba(245,158,11,0.25)]"
                : "border-zinc-700 text-zinc-400 hover:text-zinc-200"}`}
            title="Locked durations survive a narration re-run"
          >
            {sel.shot.camera.duration_locked ? <Lock className="h-3.5 w-3.5 text-amber-400" /> : <Unlock className="h-3.5 w-3.5" />}
            {sel.shot.camera.duration_locked ? "Locked" : "Unlocked"}
          </button>

          {onAddLayer && (
            <button
              onClick={() => handleAddLayerPrompt(sel.shot.scene_id)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-amber-400 bg-amber-500/20 text-amber-300 font-bold hover:bg-amber-500/30 text-[11px] font-mono transition-all hover:scale-[1.02] active:scale-[0.98] shadow-sm"
              title="Add a new SFX layer to this beat (+ layer)"
            >
              <Plus className="h-3.5 w-3.5 text-amber-400" />
              + Layer
            </button>
          )}

          <span className="text-[10px] font-mono text-zinc-400 ml-auto font-bold">
            {tc(sel.start)} → {tc(sel.start + sel.dur)}
          </span>

          <div className="w-full border-t border-zinc-900 pt-3.5 mt-1 flex flex-col gap-2.5">
            <ClipAudio
              label="A1 Narration" tone="emerald"
              url={sel.shot.narration_url ? mediaUrl(sel.shot.narration_url) : null}
              present={!!sel.shot.has_narration}
              wanted={!!sel.shot.narration?.trim()}
              gain={sel.shot.gain_narration ?? 1}
              busy={!!busy?.narration}
              onGain={(v: number) => onUpdateGain?.(sel.shot.scene_id, "gain_narration", v)}
              onRegen={onRegenNarration ? () => onRegenNarration(sel.shot.scene_id) : undefined}
              regenLabel="Re-record"
              note={sel.shot.camera.duration_locked
                ? "duration locked — re-recording will not retime this beat"
                : "duration is narration-led — re-recording may change it"}
            />

            {/* Resolved SFX Layers list or legacy SFX row */}
            {sel.shot.sfx_layers_resolved && sel.shot.sfx_layers_resolved.length > 0 ? (
              sel.shot.sfx_layers_resolved.map((lyr, idx) => (
                <ClipAudio
                  key={lyr.id}
                  label={`A2${String.fromCharCode(97 + idx)} SFX`} tone="amber"
                  url={lyr.url ? mediaUrl(lyr.url) : null}
                  present={Boolean(lyr.url || lyr.source === "generated")}
                  wanted={Boolean(lyr.prompt?.trim())}
                  gain={lyr.gain ?? 1}
                  busy={!!busy?.sfx}
                  onGain={(v: number) => onPatchLayer?.(sel.shot.scene_id, lyr.id, { gain: v })}
                  onRegen={onGenerateLayer ? () => onGenerateLayer(sel.shot.scene_id, lyr.id) : undefined}
                  onDelete={onDeleteLayer ? () => onDeleteLayer(sel.shot.scene_id, lyr.id) : undefined}
                  regenLabel="Generate"
                  note={lyr.prompt || "SFX prompt"}
                />
              ))
            ) : (
              <ClipAudio
                label="A2 SFX" tone="amber"
                url={sel.shot.sfx_url ? mediaUrl(sel.shot.sfx_url) : null}
                present={!!sel.shot.has_sfx}
                wanted={!!sel.shot.sfx?.trim()}
                gain={sel.shot.gain_sfx ?? 1}
                busy={!!busy?.sfx}
                onGain={(v: number) => onUpdateGain?.(sel.shot.scene_id, "gain_sfx", v)}
                onRegen={onRegenSfx ? () => onRegenSfx(sel.shot.scene_id) : undefined}
                regenLabel="Regenerate"
                note={sel.shot.sfx?.trim() || "no SFX prompt on this beat"}
              />
            )}
          </div>
        </div>
      ) : (
        <div className="px-5 py-3 border-t border-zinc-900 text-[11px] text-zinc-500 flex items-center justify-between bg-zinc-950">
          <span>
            Select a clip on V1 to retime it or manage SFX layers. Drag audio blocks horizontally to set timing offsets; drag top corners for fades.
          </span>
        </div>
      )}
    </div>
  );

  // Fullscreen must escape the same backdrop-filter containing block, or
  // `fixed inset-0` is measured against the timeline card rather than the screen.
  return isFullscreen && mounted ? createPortal(shell, document.body) : shell;
}
