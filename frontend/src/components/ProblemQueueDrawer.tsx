"use client";

import React from "react";
import { DirectorWarning, DirectorShot } from "../types/director";
import { AlertTriangle, ShieldAlert, X, CheckCircle2, ArrowRight, Wrench, Check } from "lucide-react";

interface ProblemQueueDrawerProps {
  warnings: DirectorWarning[];
  shots: DirectorShot[];
  isOpen: boolean;
  onClose: () => void;
  onSelectShot: (shotId: string) => void;
  /** Records a durable decision about a finding. "resolved" = the plan was
   *  changed to answer it; "accepted" = understood and deliberately kept. */
  onResolveWarning: (warningId: string, decision?: "resolved" | "accepted" | "") => void;
  /** Decisions already recorded, keyed by warning id. */
  dispositions?: Record<string, { decision: string; note?: string }>;
}

export default function ProblemQueueDrawer({
  warnings,
  shots,
  isOpen,
  onClose,
  onSelectShot,
  onResolveWarning,
  dispositions = {},
}: ProblemQueueDrawerProps) {
  if (!isOpen) return null;

  const warningMap: Record<string, DirectorWarning[]> = {
    identity: warnings.filter((w) => w.type === "identity"),
    generation: warnings.filter((w) => w.type === "generation"),
    timing: warnings.filter((w) => w.type === "timing"),
    continuity: warnings.filter((w) => w.type === "continuity"),
    coverage: warnings.filter((w) => w.type === "coverage"),
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex justify-end animate-in fade-in duration-200">
      <div className="w-[450px] bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-200">
        {/* Header */}
        <div className="p-4 border-b border-zinc-800 flex items-center justify-between bg-zinc-900/50">
          <div className="flex items-center gap-2">
            <div className="p-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400">
              <AlertTriangle className="w-5 h-5" />
            </div>
            <div>
              <h3 className="font-bold text-sm text-zinc-100">Coverage Problem Queue</h3>
              <p className="text-xs text-zinc-400">{warnings.length} items require director attention</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Warning Category Summary Pills */}
        <div className="p-4 border-b border-zinc-900 flex flex-wrap gap-2 bg-zinc-950">
          {Object.entries(warningMap).map(([cat, list]) => {
            if (list.length === 0) return null;
            return (
              <span
                key={cat}
                className="text-xs font-mono font-semibold px-2.5 py-1 rounded-full bg-amber-500/10 border border-amber-500/30 text-amber-300 flex items-center gap-1.5 capitalize"
              >
                <span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse" />
                {list.length} {cat} issues
              </span>
            );
          })}
        </div>

        {/* Issue List */}
        <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3">
          {warnings.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-64 text-center text-zinc-500 gap-2">
              <CheckCircle2 className="w-10 h-10 text-emerald-500/50" />
              <p className="text-sm font-semibold text-zinc-300">All Coverage Checks Passed</p>
              <p className="text-xs">No technical compromises or timing issues detected.</p>
            </div>
          ) : (
            warnings.map((w) => {
              const relatedShot = shots.find((s) => s.id === w.shot_id);

              const decided = w.id ? dispositions[w.id] : undefined;
              return (
                <div
                  key={w.id}
                  className="p-3.5 rounded-xl border bg-zinc-900/60 border-zinc-800 flex flex-col gap-2 hover:border-amber-500/50 transition-colors"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-mono font-bold text-amber-400 bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 rounded capitalize">
                      {String(w.kind || w.type || "warning").replace(/_/g, " ")}
                    </span>
                    {relatedShot && (
                      <button
                        onClick={() => {
                          onSelectShot(relatedShot.id);
                          onClose();
                        }}
                        className="text-xs font-mono text-zinc-400 hover:text-amber-400 flex items-center gap-1 transition-colors"
                      >
                        <span>Shot {relatedShot.shot_number || relatedShot.id}</span>
                        <ArrowRight className="w-3 h-3" />
                      </button>
                    )}
                  </div>

                  <p className="text-xs text-zinc-200 leading-relaxed font-sans">{w.detail || w.message}</p>

                  <div className="mt-1 pt-2 border-t border-zinc-800/60 flex items-center justify-between gap-3 flex-wrap">
                    {(w.suggestion || w.suggested_action) ? (
                      <span className="text-[11px] text-zinc-400 flex items-center gap-1 font-mono">
                        <Wrench className="w-3 h-3 text-amber-500" />
                        {w.suggestion || w.suggested_action}
                      </span>
                    ) : (
                      <span className="text-[11px] text-zinc-500 font-mono">
                        No suggested fix — decide how to proceed.
                      </span>
                    )}

                    {decided ? (
                      <span className="px-2.5 py-1 bg-emerald-500/15 border border-emerald-500/40 text-emerald-300 text-[11px] font-mono rounded flex items-center gap-1.5">
                        <Check className="w-3 h-3" />
                        {decided.decision === "accepted" ? "Kept as-is" : "Marked resolved"}
                        <button
                          onClick={() => onResolveWarning(w.id || "", "")}
                          className="ml-1 text-emerald-400/70 hover:text-emerald-200 underline"
                          title="Undo this decision — the finding will block locking again"
                        >
                          undo
                        </button>
                      </span>
                    ) : (
                      <span className="flex items-center gap-1.5">
                        <button
                          onClick={() => onResolveWarning(w.id || "", "resolved")}
                          disabled={!w.id}
                          className="px-2.5 py-1 bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-300 text-[11px] font-mono rounded transition-colors disabled:opacity-40"
                          title="The plan has been changed to answer this finding"
                        >
                          Mark resolved
                        </button>
                        <button
                          onClick={() => onResolveWarning(w.id || "", "accepted")}
                          disabled={!w.id}
                          className="px-2.5 py-1 bg-zinc-800/80 hover:bg-zinc-700/80 border border-zinc-700 text-zinc-300 text-[11px] font-mono rounded transition-colors disabled:opacity-40"
                          title="Understood and deliberately kept"
                        >
                          Keep as-is
                        </button>
                      </span>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
