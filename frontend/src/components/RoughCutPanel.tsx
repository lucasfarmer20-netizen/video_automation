"use client";

import React, { useEffect, useState, useCallback } from "react";
import { Play, Check, Lock, Loader2, AlertTriangle, Film } from "lucide-react";

interface PlanStep {
  key: string;
  label: string;
  done: boolean;
  detail: string;
  blocked: string | null;
  manual?: boolean;
}

interface RoughCutPanelProps {
  /** GET /api/roughcut/plan */
  fetchPlan: () => Promise<any>;
  /** POST /api/assemble/rough_cut */
  onBuild: () => Promise<any>;
  /** Approval is a human decision; this jumps to where it is made. */
  onGoApprove: () => void;
  running: boolean;
  /** Latest line of the rough_cut job log. */
  logLine?: string;
  refreshKey?: unknown;
}

/** One button for the whole rough cut, plus the order it happens in.
 *
 *  The pipeline's dependencies are fixed — stills, approval, narration, render,
 *  assemble — but nothing said so, and the steps live on different tabs. This
 *  states the order, marks what is done, and names what is blocking, so the next
 *  action is never something you have to go looking for. */
export default function RoughCutPanel({
  fetchPlan, onBuild, onGoApprove, running, logLine, refreshKey,
}: RoughCutPanelProps) {
  const [plan, setPlan] = useState<{ steps: PlanStep[]; complete: boolean;
                                     blocked_on: string | null; needs_human: boolean } | null>(null);

  const load = useCallback(async () => {
    const d = await fetchPlan();
    if (d?.ok) setPlan(d);
  }, [fetchPlan]);

  useEffect(() => { load(); }, [load, refreshKey]);
  // While a build runs, the plan is the progress display.
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [running, load]);

  const steps = plan?.steps ?? [];
  const doneCount = steps.filter((s) => s.done).length;

  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-950/10 p-4 flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-zinc-100 font-bold text-xs uppercase tracking-wider flex items-center gap-2 font-mono">
          <Film className="h-4 w-4 text-amber-500" />
          Rough Cut
          {steps.length > 0 && (
            <span className="text-zinc-500 font-normal">{doneCount}/{steps.length}</span>
          )}
        </h3>

        {plan?.needs_human ? (
          <button
            onClick={onGoApprove}
            className="bg-amber-500 hover:bg-amber-400 text-zinc-950 px-4 py-2 rounded-xl text-xs font-extrabold transition flex items-center gap-2"
          >
            <Lock className="h-3.5 w-3.5" />
            Approve the storyboard →
          </button>
        ) : (
          <button
            onClick={onBuild}
            disabled={running || plan?.complete}
            className="bg-amber-500 hover:bg-amber-400 disabled:opacity-40 disabled:hover:bg-amber-500 text-zinc-950 px-4 py-2 rounded-xl text-xs font-extrabold transition flex items-center gap-2"
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5 fill-current" />}
            {running ? "Building…" : plan?.complete ? "Rough cut is ready" : doneCount ? "Continue rough cut" : "Build rough cut"}
          </button>
        )}
      </div>

      <ol className="flex flex-col gap-1">
        {steps.map((s, i) => {
          const isNext = !s.done && steps.slice(0, i).every((p) => p.done);
          return (
            <li key={s.key}
                className={`flex items-center gap-2 text-[11px] font-mono px-2 py-1 rounded ${
                  isNext ? "bg-amber-500/10 text-amber-200" : s.done ? "text-zinc-500" : "text-zinc-600"
                }`}>
              <span className="w-4 shrink-0 text-center">
                {s.done ? <Check className="h-3 w-3 text-emerald-400" strokeWidth={3} />
                  : isNext && running ? <Loader2 className="h-3 w-3 animate-spin text-amber-400" />
                  : s.manual ? <Lock className="h-3 w-3" />
                  : <span className="text-zinc-700">{i + 1}</span>}
              </span>
              <span className={s.done ? "line-through decoration-zinc-700" : ""}>{s.label}</span>
              {s.detail && <span className="text-zinc-600">{s.detail}</span>}
              {s.manual && !s.done && (
                <span className="text-amber-500/80 ml-1">— your call, not automatic</span>
              )}
              {isNext && s.blocked && (
                <span className="text-zinc-500 ml-auto flex items-center gap-1">
                  <AlertTriangle className="h-3 w-3 text-amber-500" />{s.blocked}
                </span>
              )}
            </li>
          );
        })}
      </ol>

      {running && logLine && (
        <p className="text-[10px] font-mono text-zinc-400 truncate bg-zinc-950/60 px-2 py-1 rounded border border-zinc-900">
          {logLine}
        </p>
      )}

      <p className="text-[10px] text-zinc-500 leading-relaxed">
        Runs every step in order and skips whatever already exists, so it is safe
        to press again after a failure — it resumes rather than regenerating.
        It stops at the storyboard gate on purpose: approval is where render
        budget is allocated, and that stays a human decision.
      </p>
    </div>
  );
}
