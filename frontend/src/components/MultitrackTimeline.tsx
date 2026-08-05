"use client";

import React, { useState, useMemo } from "react";
import { Lock, Unlock, Film, Mic, Waves, Music, ZoomIn, ZoomOut } from "lucide-react";

interface Shot {
  scene_id: string;
  narration: string;
  motion_type: string;
  camera: { move: string; duration: number; duration_locked?: boolean };
  draft_image: string | null;
  has_narration?: boolean;
  has_sfx?: boolean;
  sfx?: string;
}

interface MultitrackTimelineProps {
  shots: Shot[];
  /** { scene_id: { narration?: number[], sfx?: number[] } } from /api/audio/peaks */
  peaks?: Record<string, { narration?: number[]; sfx?: number[] }>;
  musicTrack?: string;
  mediaUrl: (p: string) => string;
  onUpdateCamera: (sceneId: string, camera: Record<string, number | boolean | string>) => void;
}

const MOVES = ["static", "push_in", "push_out", "pan_left", "pan_right"];

const TIER_COLOR: Record<string, string> = {
  parallax: "bg-blue-500/40 border-blue-400/50",
  ai_video: "bg-purple-500/40 border-purple-400/50",
  static: "bg-zinc-600/40 border-zinc-500/50",
};

const tc = (s: number) => {
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
};

/** Step 3 — the cut, as tracks.
 *
 *  Deliberately NOT a browser compositor: it lays out what the server will
 *  render from the manifest, and nothing here composites video. A second
 *  renderer in the browser would drift from the real one and cannot reproduce
 *  the depth-warp parallax. Phase 6 syncs a playhead to the server preview MP4;
 *  until then this is an editor, not a player. */
/** Peak envelope as an SVG. Normalised per clip, so this shows shape and timing
 *  -- where the voice actually starts and stops inside its beat -- not level.
 *  Level is what the trims and the mixer are for. */
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

export default function MultitrackTimeline({
  shots, musicTrack, mediaUrl, onUpdateCamera, peaks
}: MultitrackTimelineProps) {
  const [pxPerSec, setPxPerSec] = useState(4);
  const [selected, setSelected] = useState<string | null>(null);

  const { blocks, total } = useMemo(() => {
    let acc = 0;
    const b = shots.map((s) => {
      const start = acc;
      const dur = s.camera?.duration || 0;
      acc += dur;
      return { shot: s, start, dur };
    });
    return { blocks: b, total: acc };
  }, [shots]);

  const width = Math.max(total * pxPerSec, 320);
  const sel = blocks.find((b) => b.shot.scene_id === selected);

  // A tick every 30s at normal zoom, wider apart when zoomed out.
  const tickEvery = pxPerSec >= 6 ? 15 : pxPerSec >= 3 ? 30 : 60;
  const ticks = Array.from({ length: Math.floor(total / tickEvery) + 1 }, (_, i) => i * tickEvery);

  const TRACKS = [
    { key: "V1", label: "V1 Stills", icon: Film, colour: "text-zinc-300" },
    { key: "A1", label: "A1 Narration", icon: Mic, colour: "text-emerald-400" },
    { key: "A2", label: "A2 SFX", icon: Waves, colour: "text-amber-400" },
    { key: "A3", label: "A3 Music", icon: Music, colour: "text-purple-400" },
  ];

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 flex flex-col">
      <div className="px-4 py-3 border-b border-zinc-900 flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider font-mono">
          Timeline
        </h3>
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

      <div className="flex">
        {/* Track headers stay put while the tracks scroll. */}
        <div className="shrink-0 w-28 border-r border-zinc-900 bg-zinc-950/80">
          <div className="h-6 border-b border-zinc-900" />
          {TRACKS.map((t) => (
            <div key={t.key} className={`h-14 flex items-center gap-1.5 px-2 border-b border-zinc-900 text-[10px] font-mono ${t.colour}`}>
              <t.icon className="h-3 w-3 shrink-0" />
              <span className="truncate">{t.label}</span>
            </div>
          ))}
        </div>

        <div className="flex-1 overflow-x-auto">
          <div style={{ width }} className="relative">
            {/* ruler */}
            <div className="h-6 border-b border-zinc-900 relative">
              {ticks.map((t) => (
                <div key={t} className="absolute top-0 h-full border-l border-zinc-800/70"
                     style={{ left: t * pxPerSec }}>
                  <span className="pl-1 text-[9px] font-mono text-zinc-600 tabular-nums">{tc(t)}</span>
                </div>
              ))}
            </div>

            {/* V1 — stills */}
            <div className="h-14 border-b border-zinc-900 relative">
              {blocks.map(({ shot, start, dur }) => (
                <button
                  key={shot.scene_id}
                  onClick={() => setSelected(shot.scene_id === selected ? null : shot.scene_id)}
                  title={`${shot.scene_id} · ${shot.motion_type} · ${shot.camera.move} · ${dur.toFixed(1)}s`}
                  className={`absolute top-1 bottom-1 rounded border overflow-hidden transition ${
                    TIER_COLOR[shot.motion_type] || TIER_COLOR.static
                  } ${selected === shot.scene_id ? "ring-2 ring-amber-500 z-10" : "hover:brightness-125"}`}
                  style={{ left: start * pxPerSec, width: Math.max(dur * pxPerSec - 2, 3) }}
                >
                  {shot.draft_image && dur * pxPerSec > 40 && (
                    <img src={mediaUrl(shot.draft_image)} alt=""
                         className="absolute inset-0 w-full h-full object-cover opacity-45" />
                  )}
                  <span className="relative text-[9px] font-mono text-zinc-100 font-bold pl-1 drop-shadow">
                    {dur * pxPerSec > 28 ? shot.scene_id : ""}
                  </span>
                  {shot.camera?.duration_locked && (
                    <Lock className="absolute right-0.5 bottom-0.5 h-2.5 w-2.5 text-amber-300" />
                  )}
                </button>
              ))}
            </div>

            {/* A1 / A2 — per-beat audio */}
            {(["narration", "sfx"] as const).map((kind) => (
              <div key={kind} className="h-14 border-b border-zinc-900 relative">
                {blocks.map(({ shot, start, dur }) => {
                  const present = kind === "narration" ? shot.has_narration : shot.has_sfx;
                  const wanted = kind === "narration" ? Boolean(shot.narration?.trim()) : Boolean(shot.sfx?.trim());
                  if (!wanted) return null;
                  const env = peaks?.[shot.scene_id]?.[kind];
                  return (
                    <div
                      key={shot.scene_id}
                      title={present ? `${shot.scene_id} ${kind}` : `${shot.scene_id}: ${kind} not generated yet`}
                      className={`absolute top-2 bottom-2 rounded border overflow-hidden ${
                        !present ? "border-dashed border-zinc-700 bg-zinc-900/40"
                        : kind === "narration" ? "bg-emerald-500/15 border-emerald-400/40"
                        : "bg-amber-500/10 border-amber-400/40"
                      }`}
                      style={{ left: start * pxPerSec, width: Math.max(dur * pxPerSec - 2, 3) }}
                    >
                      {present && (
                        <Waveform data={env}
                          colour={kind === "narration" ? "rgba(52,211,153,0.75)" : "rgba(251,191,36,0.65)"} />
                      )}
                      {present && !env && dur * pxPerSec > 40 && (
                        <span className="absolute inset-0 flex items-center justify-center text-[8px] font-mono text-zinc-500">
                          no waveform
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            ))}

            {/* A3 — the bed is one looped clip under the whole episode */}
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

      {/* Inspector for the selected beat — this is where retiming happens. */}
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
          <span className="text-[10px] font-mono text-zinc-600 ml-auto">
            {tc(sel.start)} → {tc(sel.start + sel.dur)}
          </span>
        </div>
      ) : (
        <div className="px-4 py-2.5 border-t border-zinc-900 text-[11px] text-zinc-600">
          Select a clip on V1 to retime it. Durations are narration-led — lock a beat
          to hold a trim against the next voiceover run.
        </div>
      )}
    </div>
  );
}
