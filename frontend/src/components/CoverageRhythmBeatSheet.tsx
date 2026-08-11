"use client";

import React from "react";
import { DirectorShot } from "../types/director";
import { List, Eye, Clock, DollarSign, AlertCircle, HelpCircle, ShieldAlert, Sparkles } from "lucide-react";

interface CoverageRhythmBeatSheetProps {
  shots: DirectorShot[];
  onSelectShot: (shot: DirectorShot) => void;
}

export default function CoverageRhythmBeatSheet({
  shots,
  onSelectShot,
}: CoverageRhythmBeatSheetProps) {
  // Compute shot size rhythm string (e.g. "WS ➔ ECU ➔ WS ➔ CU ➔ CU ➔ MCU ➔ MW")
  const rhythmSequence = shots.map((s) => s.shot_size.toUpperCase()).join(" ➔ ");

  // Count consecutive close-ups for visual rhythm warning
  let consecutiveCloseups = 0;
  let hasThreeConsecutiveCloseups = false;
  shots.forEach((s) => {
    const size = s.shot_size.toLowerCase();
    if (size === "cu" || size === "ecu") {
      consecutiveCloseups++;
      if (consecutiveCloseups >= 3) hasThreeConsecutiveCloseups = true;
    } else {
      consecutiveCloseups = 0;
    }
  });

  return (
    <div className="w-full glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800 animate-in fade-in duration-200">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-zinc-800/80">
        <div>
          <div className="flex items-center gap-2">
            <span className="p-2 rounded-lg bg-amber-500/15 text-amber-400 font-bold text-xs font-mono">
              PRE-GENERATION PLAN REVIEW
            </span>
            <h3 className="font-bold text-sm text-zinc-100">
              Coverage Rhythm & Beat Sheet
            </h3>
          </div>
          <p className="text-xs text-zinc-400 mt-0.5">
            Review framing order, cut density, and reasons before spending money on generation
          </p>
        </div>

        {/* Total Cost Summary */}
        <div className="flex items-center gap-2 font-mono text-xs text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 px-3 py-1.5 rounded-xl font-bold">
          <DollarSign className="w-4 h-4 text-emerald-400" />
          <span>Plan Spend: ${shots.reduce((acc, s) => acc + s.estimated_cost, 0).toFixed(2)}</span>
        </div>
      </div>

      {/* Rhythm Sequence Bar */}
      <div className="bg-zinc-950/80 p-3.5 rounded-xl border border-zinc-800/80 flex flex-col gap-2">
        <div className="flex justify-between items-center text-xs font-mono text-zinc-400">
          <span className="font-bold text-amber-400">FRAMING RHYTHM SEQUENCE:</span>
          <span>{shots.length} Shots</span>
        </div>
        <div className="text-xs font-mono font-bold text-zinc-200 bg-zinc-900/90 p-2.5 rounded-lg border border-zinc-800 tracking-wider overflow-x-auto whitespace-nowrap">
          {rhythmSequence}
        </div>
        {hasThreeConsecutiveCloseups && (
          <div className="text-[11px] font-mono text-amber-300 flex items-center gap-1.5 mt-1">
            <AlertCircle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            <span>Rhythm Warning: 3 consecutive close-ups detected in sequence. Consider widening middle shot.</span>
          </div>
        )}
      </div>

      {/* Shot Beat Sheet Table */}
      <div className="flex flex-col gap-2">
        {shots.map((shot, idx) => {
          const isTier1 = shot.tier === 1;
          const isTier2 = shot.tier === 2;
          const isTier3 = shot.tier === 3;

          const tierBadgeClass = isTier3
            ? "bg-amber-500/20 text-amber-300 border-amber-500/40"
            : isTier2
            ? "bg-purple-500/20 text-purple-300 border-purple-500/40"
            : "bg-emerald-500/10 text-emerald-400 border-emerald-500/30";

          const tierLabel = isTier3
            ? "Tier 3 Creative (Face Anchor)"
            : isTier2
            ? "Tier 2 Check (AI Video / Limit)"
            : `Tier 1 Standard ($${shot.estimated_cost.toFixed(2)} draft image)`;

          return (
            <div
              key={shot.id}
              onClick={() => onSelectShot(shot)}
              className="p-3.5 rounded-xl border bg-zinc-950/60 border-zinc-800/80 hover:border-amber-500/50 hover:bg-zinc-900/80 cursor-pointer transition-all flex flex-col md:flex-row md:items-center justify-between gap-3 group"
            >
              {/* Left Column: Number, Purpose, Framing */}
              <div className="flex items-start gap-3 min-w-[240px]">
                <span className="text-xs font-mono font-bold text-amber-400 bg-zinc-900 border border-zinc-800 px-2 py-1 rounded">
                  {shot.shot_number || shot.id}
                </span>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold text-zinc-100 uppercase font-mono">
                      {shot.shot_size} · {shot.angle}
                    </span>
                    <span className="text-[10px] font-mono text-zinc-400 capitalize px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800">
                      {shot.purpose}
                    </span>
                  </div>
                  <p className="text-xs font-semibold text-amber-300/90 mt-0.5">
                    {shot.subject}
                  </p>
                </div>
              </div>

              {/* Center Column: Creative Rationale Reason */}
              <div className="flex-1 text-xs text-zinc-300 italic font-serif leading-relaxed px-2 border-l border-r border-zinc-900 hidden md:block">
                "{shot.reason || shot.purpose}"
              </div>

              {/* Right Column: Timing, Tier & Cost */}
              <div className="flex items-center gap-3 justify-between md:justify-end shrink-0">
                <span className="text-xs font-mono text-zinc-400 flex items-center gap-1">
                  <Clock className="w-3.5 h-3.5 text-zinc-500" />
                  {shot.camera.duration.toFixed(1)}s
                </span>

                <span
                  className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded border ${tierBadgeClass}`}
                  title={shot.tier_reason || tierLabel}
                >
                  {tierLabel}
                </span>

                <span className="text-xs font-mono font-bold text-emerald-400">
                  ${shot.estimated_cost.toFixed(2)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
