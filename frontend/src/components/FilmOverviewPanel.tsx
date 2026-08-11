"use client";

import React from "react";
import { SceneSummary } from "../types/director";
import { Film, Lock, AlertTriangle, CheckCircle2, DollarSign, Clock, Layers } from "lucide-react";

interface FilmOverviewPanelProps {
  scenes: SceneSummary[];
  activeSceneId: string;
  onSelectScene: (sceneId: string) => void;
}

export default function FilmOverviewPanel({
  scenes,
  activeSceneId,
  onSelectScene,
}: FilmOverviewPanelProps) {
  const totalDuration = scenes.reduce((acc, s) => acc + s.duration, 0);
  const totalCost = scenes.reduce((acc, s) => acc + s.estimated_cost, 0);
  const lockedCount = scenes.filter((s) => s.status === "locked" || s.status === "compiled").length;
  const totalWarnings = scenes.reduce((acc, s) => acc + s.warnings_count, 0);

  const formatMinSec = (sec: number) => {
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${s.toString().padStart(2, "0")}`;
  };

  const getStatusBadge = (status: SceneSummary["status"]) => {
    switch (status) {
      case "compiled":
        return <span className="text-[10px] font-mono font-bold text-cyan-400 bg-cyan-500/10 border border-cyan-500/30 px-1.5 py-0.5 rounded">Compiled</span>;
      case "locked":
        return <span className="text-[10px] font-mono font-bold text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 px-1.5 py-0.5 rounded">Locked</span>;
      case "generating":
        return <span className="text-[10px] font-mono font-bold text-purple-400 bg-purple-500/10 border border-purple-500/30 px-1.5 py-0.5 rounded animate-pulse">Generating</span>;
      case "draft":
        return <span className="text-[10px] font-mono font-bold text-amber-400 bg-amber-500/10 border border-amber-500/30 px-1.5 py-0.5 rounded">Draft</span>;
      case "uncovered":
      default:
        return <span className="text-[10px] font-mono font-bold text-zinc-500 bg-zinc-900 border border-zinc-800 px-1.5 py-0.5 rounded">Not Directed</span>;
    }
  };

  return (
    <div className="w-full bg-zinc-950/90 border-b border-zinc-900 px-4 py-3 glass-surface flex flex-col gap-2.5">
      {/* Top summary stats bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 text-amber-400 font-bold text-sm">
            <Film className="w-4 h-4 text-amber-500" />
            <span>FILM COVERAGE OVERVIEW</span>
          </div>
          <span className="text-zinc-600">|</span>
          <div className="flex items-center gap-1.5 text-xs text-zinc-400 font-mono">
            <Clock className="w-3.5 h-3.5 text-zinc-500" />
            <span>{formatMinSec(totalDuration)} ({scenes.length} Scenes)</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-zinc-400 font-mono">
            <Layers className="w-3.5 h-3.5 text-zinc-500" />
            <span>{lockedCount}/{scenes.length} Scenes Ready</span>
          </div>
        </div>

        <div className="flex items-center gap-4 text-xs font-mono">
          {totalWarnings > 0 && (
            <div className="flex items-center gap-1.5 text-amber-400 bg-amber-500/10 border border-amber-500/30 px-2.5 py-1 rounded-full">
              <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
              <span>{totalWarnings} Warnings</span>
            </div>
          )}
          <div className="flex items-center gap-1 text-emerald-400 font-bold bg-emerald-500/10 border border-emerald-500/30 px-2.5 py-1 rounded-full">
            <DollarSign className="w-3.5 h-3.5 text-emerald-400" />
            <span>Est. Cost: ${totalCost.toFixed(2)}</span>
          </div>
        </div>
      </div>

      {/* Horizontal Scene Strip (8-12 units) */}
      <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-thin">
        {scenes.map((s) => {
          const isActive = s.scene_id === activeSceneId;
          return (
            <button
              key={s.scene_id}
              onClick={() => onSelectScene(s.scene_id)}
              className={`flex flex-col justify-between p-2.5 rounded-xl text-left border shrink-0 transition-all min-w-[170px] ${
                isActive
                  ? "bg-amber-500/15 border-amber-500/60 neon-glow-amber scale-[1.02]"
                  : "bg-zinc-900/60 border-zinc-800/80 hover:bg-zinc-900 hover:border-zinc-700"
              }`}
            >
              <div className="flex items-center justify-between w-full mb-1">
                <span className={`text-xs font-bold truncate max-w-[110px] ${isActive ? "text-amber-300" : "text-zinc-200"}`}>
                  {s.title}
                </span>
                {getStatusBadge(s.status)}
              </div>

              <div className="flex items-center justify-between text-[11px] font-mono text-zinc-400 mt-1 pt-1 border-t border-zinc-800/50 w-full">
                <span>{formatMinSec(s.duration)}</span>
                <span>{s.shots_count} shots</span>
                <span className="text-emerald-400 font-semibold">${s.estimated_cost.toFixed(2)}</span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
