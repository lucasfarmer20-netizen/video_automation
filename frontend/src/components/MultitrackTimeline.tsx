"use client";

import React, { useState, useMemo, useEffect } from "react";
import { Lock, Unlock, Film, Mic, Waves, Music, ZoomIn, ZoomOut, RefreshCw, Play, Pause, AlertTriangle, Plus, Trash2 } from "lucide-react";

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
  parallax: "bg-blue-500/40 border-blue-400/50",
  ai_video: "bg-purple-500/40 border-purple-400/50",
  static: "bg-zinc-600/40 border-zinc-500/50",
};

const tc = (s: number) => {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
};

/** Peak envelope as an SVG. Normalised per clip, so this shows shape and timing. */
function Waveform({ data, colour }: { data?: number[]; colour: string }) {
  if (!data || !data.length) return null;
  const n = data.length;
  const pts = data.map((v, i) => `${(i / (n - 1)) * 100},${50 - v * 46}`).join(" ")
    + " " + data.map((v, i) => `${((n - 1 - i) / (n - 1)) * 100},${50 + data[n - 1 - i] * 46}`).join(" ");
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none"
         className="absolute inset-0 w-full h-full pointer-events-none">
      <polygon points={pts} fill={colour} />
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
        <polygon points={`0,100 ${finPct},0 0,0`} fill="rgba(0,0,0,0.5)" />
      )}
      {fout > 0 && (
        <polygon points={`100,100 ${100 - foutPct},0 100,0`} fill="rgba(0,0,0,0.5)" />
      )}
    </svg>
  );
}

const toDb = (g: number) => (g <= 0.0001 ? -60 : 20 * Math.log10(g));
const fromDb = (db: number) => (db <= -39.5 ? 0 : Math.pow(10, db / 20));

const TONE: Record<string, string> = {
  emerald: "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  amber: "bg-amber-500/15 text-amber-400 border-amber-500/25",
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
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <span className={`px-1.5 py-0.5 rounded border shrink-0 ${TONE[tone]}`}>{label}</span>
      {present ? (
        <>
          <button onClick={toggle} className="text-zinc-400 hover:text-zinc-100 w-4 shrink-0"
                  title="Audition">{playing ? "■" : <Play className="h-3 w-3" />}</button>
          <input type="range" min={-40} max={12} step={0.5}
                 value={Math.max(-40, Math.min(12, db))}
                 onChange={(e) => setLocal(parseFloat(e.target.value))}
                 onPointerUp={commit} onKeyUp={commit} onBlur={commit}
                 className="w-40 accent-amber-500 h-1" title="Trim on top of the episode bus" />
          <span className={`w-12 text-right tabular-nums ${
            Math.abs(db) < 0.25 ? "text-zinc-600" : "text-zinc-300"}`}>
            {db <= -39.5 ? "−∞" : `${db >= 0 ? "+" : ""}${db.toFixed(1)}`}
          </span>
        </>
      ) : (
        <span className="text-zinc-600 italic">{wanted ? "not generated" : "none for this beat"}</span>
      )}
      {onRegen && wanted && (
        <button onClick={onRegen} disabled={busy}
          className="ml-1 flex items-center gap-1 px-2 py-1 rounded border border-zinc-800 text-zinc-400 hover:text-zinc-100 hover:border-zinc-700 disabled:opacity-40 transition shrink-0">
          <RefreshCw className={`h-3 w-3 ${busy ? "animate-spin" : ""}`} />
          {busy ? "working…" : regenLabel}
        </button>
      )}
      {onDelete && (
        <button onClick={onDelete}
          className="text-zinc-600 hover:text-red-400 p-1 transition shrink-0" title="Delete layer">
          <Trash2 className="h-3 w-3" />
        </button>
      )}
      <span className="text-zinc-600 truncate ml-1">{note}</span>
    </div>
  );
}

export default function MultitrackTimeline({
  shots, musicTrack, mediaUrl, onUpdateCamera, peaks,
  onUpdateGain, onRegenNarration, onRegenSfx, busy, previewUrl, previewMeta,
  onPatchNarration, onPatchLayer, onAddLayer, onDeleteLayer, onGenerateLayer
}: MultitrackTimelineProps) {
  const videoRef = React.useRef<HTMLVideoElement | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [pxPerSec, setPxPerSec] = useState(4);
  const [selected, setSelected] = useState<string | null>(null);
  const [drag, setDrag] = useState<{ id: string; dur: number } | null>(null);

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

  // Keyboard Shortcuts (Space for Play/Pause, Left/Right Arrows to jump beats)
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

      if (e.code === "Space") {
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
        const curTime = v.currentTime;
        const prevBlock = [...blocks].reverse().find(b => b.start < curTime - 0.2);
        if (prevBlock) {
          v.currentTime = prevBlock.start;
        } else {
          v.currentTime = 0;
        }
      } else if (e.code === "ArrowRight") {
        e.preventDefault();
        const v = videoRef.current;
        if (!v || !blocks.length) return;
        const curTime = v.currentTime;
        const nextBlock = blocks.find(b => b.start > curTime + 0.1);
        if (nextBlock) {
          v.currentTime = nextBlock.start;
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [blocks]);

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

  const atPlayhead = React.useMemo(() => {
    const beats = previewMeta?.beats;
    if (!beats?.length) return null;
    const b = beats.find((x) => playhead >= x.start && playhead < x.start + x.duration);
    return b ? `${b.scene_id}  ${(playhead - b.start).toFixed(1)}s in` : null;
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

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 flex flex-col">
      <div className="px-4 py-3 border-b border-zinc-900 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider font-mono">
            Timeline
          </h3>
          <span className="text-[10px] font-mono text-zinc-600 bg-zinc-900 px-2 py-0.5 rounded border border-zinc-800">
            [Space] Play/Pause · [←/→] Jump Beats
          </span>
        </div>
        <div className="flex items-center gap-4 text-[11px] font-mono">
          <span className="text-zinc-500">{shots.length} beats</span>
          <span className="text-amber-500 font-bold">{tc(total)} ({(total / 60).toFixed(1)} min)</span>
          <div className="flex items-center gap-1">
            <button onClick={() => setPxPerSec((z) => Math.max(1, z - 1))}
              className="text-zinc-500 hover:text-zinc-200 transition" title="Zoom out">
              <ZoomOut className="h-3.5 w-3.5" />
            </button>
            <span className="text-zinc-600 w-10 text-center tabular-nums">{pxPerSec}px/s</span>
            <button onClick={() => setPxPerSec((z) => Math.min(20, z + 1))}
              className="text-zinc-500 hover:text-zinc-200 transition" title="Zoom in">
              <ZoomIn className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>

      {previewUrl && (
        <div className="px-4 py-3 border-b border-zinc-900 flex flex-col gap-2">
          {previewMeta?.stale && (
            <div className="bg-amber-950/25 border border-amber-500/30 rounded-lg px-3 py-2 text-[11px] text-amber-200/90 font-mono flex gap-2">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px text-amber-400" />
              <span>
                This preview was built from a different cut
                ({previewMeta.runtime.toFixed(1)}s vs {previewMeta.live_runtime?.toFixed(1)}s now).
                The playhead follows the <em>video</em>, so it will not line up with the
                beats above until you rebuild the preview.
              </span>
            </div>
          )}
          <div className="flex items-start gap-3">
            <video
              ref={videoRef}
              src={previewUrl}
              className="w-72 rounded-lg border border-zinc-800 bg-black shrink-0"
              onTimeUpdate={(e) => setPlayhead((e.target as HTMLVideoElement).currentTime)}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              controls
            />
            <div className="flex flex-col gap-1.5 text-[11px] font-mono text-zinc-500 pt-1">
              <button
                onClick={() => { const v = videoRef.current; if (!v) return; playing ? v.pause() : v.play(); }}
                className="flex items-center gap-1.5 px-2 py-1 rounded border border-zinc-800 text-zinc-300 hover:border-zinc-700 transition w-fit"
              >
                {playing ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}
                {playing ? "Pause" : "Play"}
              </button>
              <span className="tabular-nums text-amber-500">{tc(playhead)}</span>
              {atPlayhead && <span className="text-zinc-400">{atPlayhead}</span>}
              <span className="text-zinc-600 leading-relaxed max-w-[16rem]">
                Click anywhere on the tracks or beat markers to seek. Rendering happens on the
                server — this is the real cut, not a browser mock-up.
              </span>
            </div>
          </div>
        </div>
      )}

      <div className="flex">
        {/* Track headers */}
        <div className="shrink-0 w-28 border-r border-zinc-900 bg-zinc-950/80">
          <div className="h-8 border-b border-zinc-900" />
          
          {/* V1 Header */}
          <div className="h-14 flex items-center gap-1.5 px-2 border-b border-zinc-900 text-[10px] font-mono text-zinc-300">
            <Film className="h-3 w-3 shrink-0" />
            <span className="truncate">V1 Stills</span>
          </div>

          {/* A1 Header */}
          <div className="h-14 flex items-center gap-1.5 px-2 border-b border-zinc-900 text-[10px] font-mono text-emerald-400">
            <Mic className="h-3 w-3 shrink-0" />
            <span className="truncate">A1 Narration</span>
          </div>

          {/* A2 SFX Headers (dynamic lanes) */}
          {sfxLaneKeys.map((lane) => (
            <div key={lane.key} className="h-14 flex items-center justify-between px-2 border-b border-zinc-900 text-[10px] font-mono text-amber-400">
              <div className="flex items-center gap-1.5 truncate">
                <Waves className="h-3 w-3 shrink-0" />
                <span className="truncate">{lane.label}</span>
              </div>
              {onAddLayer && lane.index === 0 && (
                <button
                  onClick={() => handleAddLayerPrompt(selected || shots[0]?.scene_id)}
                  className="hover:text-amber-200 transition text-amber-500/80 p-0.5"
                  title="Add sound effect layer (+ layer)"
                >
                  <Plus className="h-3 w-3" />
                </button>
              )}
            </div>
          ))}

          {/* A3 Header */}
          <div className="h-14 flex items-center gap-1.5 px-2 border-b border-zinc-900 text-[10px] font-mono text-purple-400">
            <Music className="h-3 w-3 shrink-0" />
            <span className="truncate">A3 Music</span>
          </div>
        </div>

        <div className="flex-1 overflow-x-auto">
          <div
            style={{ width }}
            className="relative"
            onClick={(e) => {
              const v = videoRef.current;
              if (!v || !previewMeta) return;
              const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
              const t = (e.clientX - rect.left) / pxPerSec;
              v.currentTime = Math.max(0, Math.min(previewMeta.runtime, t));
            }}
          >
            {previewUrl && previewMeta && (
              <div
                className="absolute top-0 bottom-0 w-px bg-amber-400 z-20 pointer-events-none"
                style={{ left: playhead * pxPerSec }}
              >
                <div className="absolute -top-0.5 -left-1 w-2 h-2 rotate-45 bg-amber-400" />
              </div>
            )}
            
            {/* Ruler with Prominent Clickable Beat Markers */}
            <div className="h-8 border-b border-zinc-900 relative bg-zinc-950/40">
              {ticks.map((t) => (
                <div key={t} className="absolute top-0 h-full border-l border-zinc-800/70"
                     style={{ left: t * pxPerSec }}>
                  <span className="pl-1 text-[8px] font-mono text-zinc-600 tabular-nums">{tc(t)}</span>
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
                  className="absolute top-1 bottom-1 flex items-center gap-1 px-1.5 rounded bg-amber-500/20 hover:bg-amber-500/40 border border-amber-500/50 text-[9px] font-mono text-amber-300 font-bold transition z-10 shadow-sm"
                  style={{ left: start * pxPerSec }}
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" />
                  <span className="truncate">{shot.scene_id}</span>
                </button>
              ))}
            </div>

            {/* V1 — stills */}
            <div className="h-14 border-b border-zinc-900 relative">
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
                    className={`absolute top-1 bottom-1 rounded border overflow-hidden transition ${
                      TIER_COLOR[shot.motion_type] || TIER_COLOR.static
                    } ${selected === shot.scene_id ? "ring-2 ring-amber-500 z-10" : "hover:brightness-125"}`}
                    style={{ left: start * pxPerSec, width: Math.max(dur * pxPerSec - 2, 3) }}
                  >
                    {shot.draft_image && (
                      <img src={mediaUrl(shot.draft_image)} alt=""
                           className="absolute inset-0 w-full h-full object-cover opacity-45" />
                    )}
                    <div className="relative flex flex-col justify-between h-full p-1">
                      <div className="flex items-center justify-between">
                        <span className="text-[9px] font-mono text-zinc-100 font-bold pl-0.5 drop-shadow">
                          {shot.scene_id}
                        </span>
                        {shot.camera?.duration_locked && (
                          <Lock className="h-2.5 w-2.5 text-amber-300 shrink-0" />
                        )}
                      </div>
                      {moveBadge && (
                        <span className="text-[8px] font-mono text-amber-200 bg-black/80 px-1 rounded w-fit border border-amber-500/30 backdrop-blur-sm drop-shadow">
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
                      className="absolute right-0 top-0 bottom-0 w-1.5 cursor-ew-resize bg-amber-500/0 hover:bg-amber-500/70 transition"
                    />
                  </button>
                );
              })}
            </div>

            {/* A1 — Narration Track */}
            <div className="h-14 border-b border-zinc-900 relative">
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
                    className={`group absolute top-2 bottom-2 rounded border overflow-hidden cursor-grab active:cursor-grabbing ${
                      !present ? "border-dashed border-zinc-700 bg-zinc-900/40"
                      : "bg-emerald-500/15 border-emerald-400/40"
                    } ${isDragging ? "ring-2 ring-emerald-400 z-10" : "hover:border-emerald-300"}`}
                    style={{ left: blockLeft, width: Math.max(dur * pxPerSec - 2, 3) }}
                  >
                    {present && <Waveform data={env} colour="rgba(52,211,153,0.75)" />}

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
                      className="absolute top-0 left-0 w-2.5 h-2.5 bg-emerald-400/80 hover:bg-emerald-300 cursor-ew-resize opacity-0 group-hover:opacity-100 transition z-30 rounded-br"
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
                      className="absolute top-0 right-0 w-2.5 h-2.5 bg-emerald-400/80 hover:bg-emerald-300 cursor-ew-resize opacity-0 group-hover:opacity-100 transition z-30 rounded-bl"
                    />

                    {present && !env && dur * pxPerSec > 40 && (
                      <span className="absolute inset-0 flex items-center justify-center text-[8px] font-mono text-zinc-500">
                        no waveform
                      </span>
                    )}
                    <div className="absolute inset-0 flex items-center justify-between px-1.5 pointer-events-none z-20">
                      <span className="text-[8px] font-mono text-emerald-200 font-semibold drop-shadow">
                        Narration
                      </span>
                      {offsetSec !== 0 && (
                        <span className="text-[8px] font-mono text-emerald-300/90 bg-black/60 px-1 rounded ml-1 shrink-0">
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
              <div key={lane.key} className="h-14 border-b border-zinc-900 relative">
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
                      className={`group absolute top-2 bottom-2 rounded border overflow-hidden ${
                        layer ? "cursor-grab active:cursor-grabbing" : ""
                      } ${
                        !present ? "border-dashed border-zinc-700 bg-zinc-900/40"
                        : "bg-amber-500/10 border-amber-400/40"
                      } ${isDragging ? "ring-2 ring-amber-400 z-10" : "hover:border-amber-300"}`}
                      style={{ left: blockLeft, width: Math.max(dur * pxPerSec - 2, 3) }}
                    >
                      {/* Waveform */}
                      {present && <Waveform data={env} colour="rgba(251,191,36,0.65)" />}

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
                            className="absolute top-0 left-0 w-2.5 h-2.5 bg-amber-400/80 hover:bg-amber-300 cursor-ew-resize opacity-0 group-hover:opacity-100 transition z-30 rounded-br"
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
                            className="absolute top-0 right-0 w-2.5 h-2.5 bg-amber-400/80 hover:bg-amber-300 cursor-ew-resize opacity-0 group-hover:opacity-100 transition z-30 rounded-bl"
                          />
                        </>
                      )}

                      {/* Label & offset display */}
                      <div className="absolute inset-0 flex items-center justify-between px-1.5 pointer-events-none z-20">
                        <span className="text-[8px] font-mono text-amber-200/90 truncate font-semibold drop-shadow">
                          {layer?.label || layer?.prompt || (shot.sfx ? "SFX" : "")}
                        </span>
                        {offsetSec !== 0 && (
                          <span className="text-[8px] font-mono text-amber-300/90 bg-black/60 px-1 rounded ml-1 shrink-0">
                            {offsetSec > 0 ? `+${offsetSec.toFixed(1)}s` : `${offsetSec.toFixed(1)}s`}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ))}

            {/* A3 — Music */}
            <div className="h-14 border-b border-zinc-900 relative">
              {musicTrack ? (
                <div
                  className="absolute top-2 bottom-2 left-0 rounded border bg-purple-500/25 border-purple-400/40 flex items-center px-2"
                  style={{ width: Math.max(total * pxPerSec - 2, 3) }}
                  title={`${musicTrack} — looped to cover the runtime`}
                >
                  <span className="text-[9px] font-mono text-purple-200 truncate">
                    {musicTrack} · looped
                  </span>
                </div>
              ) : (
                <span className="absolute left-2 top-4 text-[10px] font-mono text-zinc-600">
                  no music bed selected
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Inspector for the selected beat */}
      {sel ? (
        <div className="px-4 py-3 border-t border-zinc-900 flex items-end gap-4 flex-wrap">
          <div>
            <span className="block text-[10px] text-zinc-500 font-mono mb-1">Beat</span>
            <span className="text-xs font-mono text-amber-500 font-bold">{sel.shot.scene_id}</span>
          </div>
          <div>
            <label className="block text-[10px] text-zinc-500 font-mono mb-1">Duration (s)</label>
            <input
              type="number" step={0.1} min={0.2}
              defaultValue={sel.dur} key={`${sel.shot.scene_id}-${sel.dur}`}
              onBlur={(e) => {
                const v = parseFloat(e.target.value);
                if (Number.isFinite(v) && Math.abs(v - sel.dur) > 0.05)
                  onUpdateCamera(sel.shot.scene_id, { duration: v });
              }}
              className="w-20 bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[11px] font-mono"
            />
          </div>
          <div>
            <label className="block text-[10px] text-zinc-500 font-mono mb-1">Move</label>
            <select
              value={sel.shot.camera.move}
              onChange={(e) => onUpdateCamera(sel.shot.scene_id, { move: e.target.value })}
              className="bg-zinc-900 border border-zinc-800 rounded px-2 py-1 text-[11px] font-mono"
            >
              {MOVES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>
          <button
            onClick={() => onUpdateCamera(sel.shot.scene_id,
              { duration_locked: !sel.shot.camera.duration_locked })}
            className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[11px] font-mono transition ${
              sel.shot.camera.duration_locked
                ? "border-amber-500/40 bg-amber-500/10 text-amber-400"
                : "border-zinc-800 text-zinc-500 hover:text-zinc-300"}`}
            title="Locked durations survive a narration re-run"
          >
            {sel.shot.camera.duration_locked ? <Lock className="h-3 w-3" /> : <Unlock className="h-3 w-3" />}
            {sel.shot.camera.duration_locked ? "Locked" : "Unlocked"}
          </button>

          {onAddLayer && (
            <button
              onClick={() => handleAddLayerPrompt(sel.shot.scene_id)}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 text-[11px] font-mono transition"
              title="Add a new SFX layer to this beat (+ layer)"
            >
              <Plus className="h-3.5 w-3.5" />
              + Layer
            </button>
          )}

          <span className="text-[10px] font-mono text-zinc-600 ml-auto">
            {tc(sel.start)} → {tc(sel.start + sel.dur)}
          </span>

          <div className="w-full border-t border-zinc-900 pt-3 mt-1 flex flex-col gap-2">
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
        <div className="px-4 py-2.5 border-t border-zinc-900 text-[11px] text-zinc-600 flex items-center justify-between">
          <span>
            Select a clip on V1 to retime it or manage SFX layers. Drag audio blocks horizontally to set timing offsets; drag top corners for fades.
          </span>
        </div>
      )}
    </div>
  );
}
