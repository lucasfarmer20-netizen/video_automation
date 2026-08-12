"use client";

import React from "react";
import { Lock, Check, ArrowRight } from "lucide-react";

/**
 * The six-stage FilmCraft spine.
 *
 * Everything shown here is computed by the server (`GET /api/stages`) and
 * rendered verbatim. This component holds no gating rules of its own — the
 * previous step header derived its own idea of what was blocked from counts the
 * client had assembled, which is exactly the "no false success" failure the
 * contract forbids: a browser that computes its own notion of "approved" can
 * disagree with the server about whether money may be spent.
 *
 * If a status looks wrong, fix `backend/stages.py`. There is deliberately
 * nowhere in this file to patch it.
 */

export type StageId =
  | "script" | "direct" | "generate" | "roughcut" | "refine" | "export";

export const STAGE_ORDER: StageId[] = [
  "script", "direct", "generate", "roughcut", "refine", "export",
];

export interface Stage {
  id: StageId;
  name: string;
  status: "complete" | "current" | "available" | "blocked";
  blocked_reason: string;
  hint: string;
  cta: string;
  cta_action: string;
  owns: string;
}

export interface StagePayload {
  stages: Stage[];
  counts: Record<string, unknown>;
  current: StageId | null;
  next_action: { stage: StageId | null; label: string; action: string; blocked_reason: string };
}

interface StageHeaderProps {
  stages: Stage[];
  active: StageId;
  onChange: (s: StageId) => void;
  /** Runs a stage's primary action. `goto:<stage>` is handled here. */
  onPrimaryAction?: (action: string) => void;
  busy?: boolean;
}

export default function StageHeader({
  stages, active, onChange, onPrimaryAction, busy = false,
}: StageHeaderProps) {
  // A payload that has not arrived yet must not be faked into a default spine;
  // an empty header is honest, a guessed one is not.
  if (!stages?.length) {
    return (
      <div className="h-[4.5rem] rounded-2xl bg-zinc-950/85 border border-zinc-800/80 animate-pulse" />
    );
  }

  const current = stages.find((s) => s.id === active);

  const runPrimary = () => {
    if (!current?.cta_action) return;
    if (current.cta_action.startsWith("goto:")) {
      onChange(current.cta_action.slice(5) as StageId);
      return;
    }
    onPrimaryAction?.(current.cta_action);
  };

  return (
    <div className="flex flex-col gap-2.5">
      <nav
        className="flex items-stretch gap-2 overflow-x-auto pb-1 p-1.5 rounded-2xl bg-zinc-950/85 backdrop-blur-md border border-zinc-800/80 shadow-xl"
        aria-label="Film stages"
      >
        {stages.map((s, i) => {
          const isActive = s.id === active;
          const isBlocked = s.status === "blocked";
          const isComplete = s.status === "complete";
          return (
            <button
              key={s.id}
              onClick={() => onChange(s.id)}
              aria-current={isActive ? "step" : undefined}
              className={[
                "group relative flex-1 min-w-[9.5rem] text-left px-3.5 py-2.5 rounded-xl border transition-all duration-200 hover:scale-[1.01] active:scale-[0.99]",
                isActive
                  ? "bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500 border-amber-300 text-zinc-950 font-bold shadow-[0_0_16px_rgba(245,158,11,0.35)]"
                  : isBlocked
                  ? "bg-zinc-900/40 border-zinc-900/80 text-zinc-500 hover:border-zinc-800 hover:text-zinc-400"
                  : isComplete
                  ? "bg-zinc-900/60 border-emerald-800/50 text-zinc-300 hover:border-emerald-600/60 hover:text-white"
                  : "bg-zinc-900/60 border-zinc-800/80 text-zinc-300 hover:border-amber-400/50 hover:text-white hover:shadow-md",
              ].join(" ")}
              title={isBlocked ? s.blocked_reason : s.owns}
            >
              <span className="flex items-center gap-2">
                <span
                  className={`text-[10px] font-mono font-extrabold px-1.5 py-0.5 rounded-md ${
                    isActive
                      ? "bg-zinc-950/20 text-zinc-950"
                      : isComplete
                      ? "bg-emerald-950/60 text-emerald-400"
                      : "bg-zinc-800/80 text-zinc-400"
                  }`}
                >
                  {isComplete && !isActive ? <Check className="h-2.5 w-2.5" /> : `0${i + 1}`}
                </span>
                <span className="text-xs font-extrabold truncate tracking-tight">{s.name}</span>
                {isBlocked && !isActive && (
                  <Lock className="h-3 w-3 ml-auto shrink-0 text-zinc-600" />
                )}
              </span>
              {s.hint && (
                <span
                  className={`block text-[10px] font-mono mt-1 truncate ${
                    isActive ? "text-zinc-900/90 font-semibold" : "text-zinc-500"
                  }`}
                >
                  {s.hint}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {current?.status === "blocked" ? (
        <div className="bg-amber-950/30 backdrop-blur-md border border-amber-500/40 rounded-xl px-4 py-2.5 text-xs text-amber-200/90 font-mono leading-relaxed flex items-center gap-2.5 shadow-md">
          <Lock className="h-4 w-4 shrink-0 text-amber-400" />
          <span>
            <span className="font-bold text-amber-400">{current.name} is locked.</span>{" "}
            {current.blocked_reason}
          </span>
        </div>
      ) : current?.cta ? (
        <div className="bg-zinc-950/70 backdrop-blur-md border border-zinc-800/80 rounded-xl px-4 py-2.5 flex items-center justify-between gap-3 shadow-md">
          <span className="text-[11px] font-mono text-zinc-500 truncate">
            <span className="text-zinc-400 font-bold">{current.name}</span> owns {current.owns}
          </span>
          <button
            onClick={runPrimary}
            disabled={busy}
            className="shrink-0 bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500 hover:from-amber-300 hover:to-orange-400 text-zinc-950 px-3.5 py-1.5 rounded-lg text-xs font-extrabold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center gap-1.5 disabled:opacity-50"
          >
            <span>{current.cta}</span>
            <ArrowRight className="h-3.5 w-3.5" />
          </button>
        </div>
      ) : null}
    </div>
  );
}
