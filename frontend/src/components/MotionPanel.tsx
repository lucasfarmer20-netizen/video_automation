"use client";

import React, { useState, useEffect, useCallback } from "react";
import { Move3d, Save, Play, RotateCcw, Info } from "lucide-react";

export interface MotionConfig {
  speed: number;
  zoom_rate: number;
  pan_rate: number;
  zoom_max: number;
  pan_max: number;
}

export interface MotionBeat {
  scene_id: string;
  motion_type: string;
  move: string;
  duration: number;
  speed: number;
  amount: number;
  travel: number;
  rate_pct_per_sec: number;
}

interface MotionPanelProps {
  /** GET /api/motion */
  fetchMotion: () => Promise<{ motion: MotionConfig; beats: MotionBeat[] } | null>;
  /** POST /api/motion */
  saveMotion: (cfg: Partial<MotionConfig>) => Promise<any>;
  /** POST /api/shot/{id} with { camera: {...} } */
  saveBeatCamera: (sceneId: string, camera: Record<string, number | string>) => Promise<any>;
  /** POST /api/motion/preview/{id} — re-renders one beat only */
  previewBeat: (sceneId: string) => Promise<any>;
  mediaUrl: (path: string) => string;
  epSlug?: string;
}

const MOVES = ["static", "push_in", "push_out", "pan_left", "pan_right"];

// Below roughly 0.7 %/s a slow move stops reading as movement at all — which is
// exactly how the first pass shipped, at 0.20 %/s. Surface it rather than let
// someone re-render 15 beats to discover it.
const SLOW_THRESHOLD = 0.7;

export default function MotionPanel({
  fetchMotion, saveMotion, saveBeatCamera, previewBeat, mediaUrl, epSlug
}: MotionPanelProps) {
  const [cfg, setCfg] = useState<MotionConfig | null>(null);
  const [beats, setBeats] = useState<MotionBeat[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [shown, setShown] = useState<string | null>(null);   // beat whose clip is open
  const [bust, setBust] = useState(0);                        // cache-buster for that clip

  const load = useCallback(async () => {
    const d = await fetchMotion();
    if (d) { setCfg(d.motion); setBeats(d.beats); setDirty(false); }
  }, [fetchMotion]);

  useEffect(() => { load(); }, [load]);

  if (!cfg) {
    return (
      <div className="p-4 text-xs text-zinc-500 font-mono">Loading motion settings…</div>
    );
  }

  const applyProject = async () => {
    setBusy("project");
    await saveMotion(cfg);
    await load();
    setBusy(null);
  };

  const updateBeat = async (b: MotionBeat, camera: Record<string, number | string>) => {
    setBusy(b.scene_id);
    await saveBeatCamera(b.scene_id, camera);
    await load();
    setBusy(null);
  };

  // The render runs as a background job, so this returns as soon as it is
  // queued -- it does not mean the clip is ready. The app's job banner reports
  // completion; "Reload clip" refetches once it is.
  const doPreview = async (b: MotionBeat) => {
    setBusy(b.scene_id);
    await previewBeat(b.scene_id);
    setShown(b.scene_id);
    setBusy(null);
  };

  const num = (v: string, fallback: number) => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : fallback;
  };

  return (
    <div className="flex flex-col gap-5 text-zinc-300">
      {/* ---------------------------------------------------- project defaults */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
            <Move3d className="h-4 w-4 text-amber-500" />
            Parallax — Project Default
          </h3>
          <button
            onClick={applyProject}
            disabled={busy === "project"}
            className="bg-amber-500 hover:bg-amber-600 disabled:opacity-40 text-zinc-950 px-3 py-1.5 rounded-lg text-xs font-bold transition flex items-center gap-1 shadow-md shadow-amber-500/10"
          >
            <Save className="h-3.5 w-3.5" />
            {busy === "project" ? "Saving…" : "Apply"}
          </button>
        </div>

        <label className="block text-[11px] text-zinc-400 font-mono mb-1">
          Speed <span className="text-amber-500">{cfg.speed.toFixed(2)}×</span>
        </label>
        <input
          type="range" min={0.25} max={4} step={0.05} value={cfg.speed}
          onChange={(e) => { setCfg({ ...cfg, speed: num(e.target.value, 1) }); setDirty(true); }}
          className="w-full accent-amber-500"
        />

        <div className="grid grid-cols-2 gap-3 mt-3">
          <div>
            <label className="block text-[11px] text-zinc-400 font-mono mb-1">Zoom rate %/s</label>
            <input
              type="number" step={0.001} min={0} max={0.1} value={cfg.zoom_rate}
              onChange={(e) => { setCfg({ ...cfg, zoom_rate: num(e.target.value, 0.011) }); setDirty(true); }}
              className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2 py-1.5 text-xs font-mono"
            />
          </div>
          <div>
            <label className="block text-[11px] text-zinc-400 font-mono mb-1">Pan rate %/s</label>
            <input
              type="number" step={0.001} min={0} max={0.1} value={cfg.pan_rate}
              onChange={(e) => { setCfg({ ...cfg, pan_rate: num(e.target.value, 0.009) }); setDirty(true); }}
              className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2 py-1.5 text-xs font-mono"
            />
          </div>
        </div>

        <p className="mt-3 text-[11px] text-zinc-500 leading-relaxed flex gap-1.5">
          <Info className="h-3.5 w-3.5 shrink-0 mt-0.5 text-zinc-600" />
          <span>
            Rates are travel <em>per second</em>, so a 6s and a 25s beat drift at the
            same on-screen speed. Frame-average motion lands near 60% of these
            numbers because displacement scales with depth — that difference is the
            parallax.
          </span>
        </p>
        {dirty && (
          <p className="mt-2 text-[11px] text-amber-500 font-mono">
            Unsaved — Apply, then re-render to see it.
          </p>
        )}
      </div>

      {/* ------------------------------------------------------- per-beat rows */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 overflow-hidden">
        <div className="px-4 py-3 border-b border-zinc-900 flex items-center justify-between">
          <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider font-mono">
            Per-Beat Override
          </h3>
          <button
            onClick={load}
            className="text-zinc-500 hover:text-zinc-300 transition"
            title="Reload from server"
          >
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="divide-y divide-zinc-900">
          {beats.map((b) => {
            const moves = b.motion_type === "parallax" && b.move !== "static";
            const slow = moves && b.rate_pct_per_sec < SLOW_THRESHOLD;
            return (
              <div key={b.scene_id} className="p-3 flex flex-col gap-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-xs text-zinc-200 font-bold">{b.scene_id}</span>
                  <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                    b.motion_type === "parallax" ? "bg-blue-500/15 text-blue-400"
                    : b.motion_type === "ai_video" ? "bg-purple-500/15 text-purple-400"
                    : "bg-zinc-800 text-zinc-400"}`}>
                    {b.motion_type}
                  </span>
                  <span className="text-[10px] font-mono text-zinc-500">{b.duration.toFixed(1)}s</span>
                  {moves ? (
                    <span className={`text-[10px] font-mono ml-auto ${slow ? "text-amber-500" : "text-emerald-500"}`}>
                      {(b.travel * 100).toFixed(1)}% · {b.rate_pct_per_sec.toFixed(2)} %/s
                      {slow && " — may not read as motion"}
                    </span>
                  ) : (
                    <span className="text-[10px] font-mono ml-auto text-zinc-600">
                      {b.motion_type === "ai_video" ? "Tier-C clip — parallax N/A" : "frozen plate"}
                    </span>
                  )}
                </div>

                {b.motion_type !== "ai_video" && (
                  <div className="grid grid-cols-4 gap-2 items-end">
                    <div>
                      <label className="block text-[10px] text-zinc-500 font-mono mb-1">Move</label>
                      <select
                        value={b.move}
                        onChange={(e) => updateBeat(b, { move: e.target.value })}
                        className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-1.5 py-1 text-[11px] font-mono"
                      >
                        {MOVES.map((m) => <option key={m} value={m}>{m}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="block text-[10px] text-zinc-500 font-mono mb-1">Speed ×</label>
                      <input
                        type="number" step={0.05} min={0} max={4} defaultValue={b.speed}
                        onBlur={(e) => {
                          const v = num(e.target.value, b.speed);
                          if (v !== b.speed) updateBeat(b, { speed: v });
                        }}
                        className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-1.5 py-1 text-[11px] font-mono"
                      />
                    </div>
                    <div>
                      <label className="block text-[10px] text-zinc-500 font-mono mb-1" title="0 = inherit project rate">
                        End scale
                      </label>
                      <input
                        type="number" step={0.01} min={0} max={0.6} defaultValue={b.amount}
                        onBlur={(e) => {
                          const v = num(e.target.value, b.amount);
                          if (v !== b.amount) updateBeat(b, { amount: v });
                        }}
                        placeholder="auto"
                        className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-1.5 py-1 text-[11px] font-mono"
                      />
                    </div>
                    <button
                      onClick={() => doPreview(b)}
                      disabled={busy === b.scene_id}
                      className="bg-zinc-800 hover:bg-zinc-700 disabled:opacity-40 text-zinc-200 px-2 py-1 rounded-lg text-[11px] font-bold transition flex items-center justify-center gap-1"
                      title="Re-render just this beat"
                    >
                      <Play className="h-3 w-3" />
                      {busy === b.scene_id ? "…" : "Test"}
                    </button>
                  </div>
                )}

                {shown === b.scene_id && (
                  <div className="flex items-center gap-2 text-[10px] font-mono text-zinc-500">
                    <span>Render queued — watch the job banner, then</span>
                    <button
                      onClick={() => setBust((n) => n + 1)}
                      className="text-amber-500 hover:text-amber-400 underline underline-offset-2"
                    >
                      reload clip
                    </button>
                    <button
                      onClick={() => setShown(null)}
                      className="ml-auto text-zinc-600 hover:text-zinc-400"
                    >
                      close
                    </button>
                  </div>
                )}

                {b.amount > 0 && (
                  <p className="text-[10px] text-amber-500/80 font-mono">
                    End scale {(100 + b.amount * 100).toFixed(0)}% is absolute — project speed
                    and beat speed do not apply. Set 0 to go back to the project rate.
                  </p>
                )}

                {shown === b.scene_id && (
                  <video
                    // `bust` changes each time Test is pressed, so the browser
                    // refetches instead of showing the cached previous render.
                    key={`${b.scene_id}-${bust}`}
                    src={`${mediaUrl(`render/${b.scene_id}.mp4`)}?v=${bust}`}
                    controls muted autoPlay loop
                    className="w-full rounded-lg border border-zinc-800 mt-1"
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
