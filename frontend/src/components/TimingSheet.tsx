"use client";

import React from "react";
import { Lock, Unlock, Clock } from "lucide-react";

interface Shot {
  scene_id: string;
  narration: string;
  motion_type: string;
  camera: { move: string; duration: number; duration_locked?: boolean };
  draft_image: string | null;
  has_narration?: boolean;
}

interface TimingSheetProps {
  shots: Shot[];
  mediaUrl: (p: string) => string;
  /** POST /api/shot/{id} with a partial camera object. */
  onUpdateCamera: (sceneId: string, camera: Record<string, number | boolean>) => void;
}

const tc = (s: number) => {
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${m}:${sec.toFixed(1).padStart(4, "0")}`;
};

const MOTION_BAR: Record<string, string> = {
  parallax: "bg-blue-500/60",
  ai_video: "bg-purple-500/60",
  static: "bg-zinc-600",
};

/** Step 3 — Editing. Where the episode's time actually goes.
 *
 *  This is the step's own surface, not another copy of the beat grid: duration
 *  and duration_locked are editable here and nowhere else in the card view.
 *  It is also the seed of the Phase 5 multitrack timeline — same data, same
 *  proportional layout, just not yet scrubbable. */
export default function TimingSheet({ shots, mediaUrl, onUpdateCamera }: TimingSheetProps) {
  const total = shots.reduce((a, s) => a + (s.camera?.duration || 0), 0);
  const locked = shots.filter((s) => s.camera?.duration_locked).length;
  let acc = 0;

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950/60 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-900 flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-zinc-200 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
          <Clock className="h-4 w-4 text-amber-500" />
          Timing Sheet
        </h3>
        <div className="flex items-center gap-4 text-[11px] font-mono">
          <span className="text-zinc-500">
            {shots.length} beat{shots.length === 1 ? "" : "s"}
          </span>
          <span className="text-zinc-500">
            {locked} locked
          </span>
          <span className="text-amber-500 font-bold">
            runtime {tc(total)} ({(total / 60).toFixed(1)} min)
          </span>
        </div>
      </div>

      <div className="divide-y divide-zinc-900">
        {shots.map((s) => {
          const start = acc;
          const dur = s.camera?.duration || 0;
          acc += dur;
          const isLocked = !!s.camera?.duration_locked;
          const pct = total > 0 ? (dur / total) * 100 : 0;
          return (
            <div key={s.scene_id} className="px-3 py-2 flex items-center gap-3">
              <span className="font-mono text-[11px] text-zinc-200 font-bold w-12 shrink-0">
                {s.scene_id}
              </span>

              {s.draft_image ? (
                <img src={mediaUrl(s.draft_image)} alt="" className="w-14 h-8 object-cover rounded border border-zinc-800 shrink-0" />
              ) : (
                <div className="w-14 h-8 rounded border border-zinc-900 bg-zinc-900/50 shrink-0" />
              )}

              <span className="font-mono text-[10px] text-zinc-600 w-24 shrink-0 tabular-nums">
                {tc(start)} → {tc(start + dur)}
              </span>

              {/* Proportional bar: shows at a glance which beats own the runtime. */}
              <div className="flex-1 min-w-[3rem] h-4 bg-zinc-900/60 rounded overflow-hidden relative">
                <div
                  className={`h-full ${MOTION_BAR[s.motion_type] || MOTION_BAR.static}`}
                  style={{ width: `${Math.max(pct, 1)}%` }}
                  title={`${s.motion_type} · ${s.camera?.move}`}
                />
                <span className="absolute inset-0 flex items-center px-2 text-[9px] font-mono text-zinc-300 truncate pointer-events-none">
                  {s.narration?.slice(0, 70) || "—"}
                </span>
              </div>

              {!s.has_narration && (
                <span className="text-[9px] font-mono text-amber-500 shrink-0" title="No narration generated for this beat">
                  no VO
                </span>
              )}

              <input
                type="number" step={0.1} min={0.2}
                defaultValue={dur}
                key={`${s.scene_id}-${dur}`}
                onBlur={(e) => {
                  const v = parseFloat(e.target.value);
                  if (!Number.isFinite(v) || Math.abs(v - dur) < 0.05) return;
                  onUpdateCamera(s.scene_id, { duration: v });
                }}
                onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                className="w-16 bg-zinc-900 border border-zinc-800 rounded px-1.5 py-1 text-[11px] font-mono text-right shrink-0"
              />

              <button
                onClick={() => onUpdateCamera(s.scene_id, { duration_locked: !isLocked })}
                className={`shrink-0 transition ${isLocked ? "text-amber-500" : "text-zinc-600 hover:text-zinc-400"}`}
                title={isLocked
                  ? "Locked — re-running narration will not change this duration"
                  : "Unlocked — re-running narration refits this beat to its voiceover"}
              >
                {isLocked ? <Lock className="h-3.5 w-3.5" /> : <Unlock className="h-3.5 w-3.5" />}
              </button>
            </div>
          );
        })}
      </div>

      <div className="px-4 py-2.5 border-t border-zinc-900 text-[11px] text-zinc-500 leading-relaxed">
        Durations are narration-led: re-running voiceover refits every{" "}
        <span className="text-zinc-400">unlocked</span> beat to its VO length.
        Lock a beat to hold a trim against that. A locked beat shorter than its own
        narration will let the voice run into the next shot — the narration stage
        names any beat that does.
      </div>
    </div>
  );
}
