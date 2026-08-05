"use client";

import React from "react";
import { Lock } from "lucide-react";

export type StepId = 1 | 2 | 3 | 4 | 5;

export interface StepCounts {
  beats: number;
  stills: number;
  narration: number;
  sfx: number;
  rendered: number;
}

interface StepHeaderProps {
  active: StepId;
  onChange: (s: StepId) => void;
  counts: StepCounts;
  scriptLocked: boolean;
  storyboardApproved: boolean;
}

interface StepDef {
  id: StepId;
  name: string;
  blocked: string | null;
  hint: string;
}

export function buildSteps(
  counts: StepCounts, scriptLocked: boolean, storyboardApproved: boolean
): StepDef[] {
  const c = counts;
  return [
    {
      id: 1, name: "Pre-Production", blocked: null,
      hint: c.beats ? `${c.stills}/${c.beats} illustrated` : "no beats yet",
    },
    {
      id: 2, name: "Audio Studio",
      blocked: scriptLocked ? null
        : "The script is not locked yet. Lock it in Step 1 first.",
      hint: c.beats ? `${c.narration}/${c.beats} voiced · ${c.sfx}/${c.beats} sfx` : "",
    },
    {
      id: 3, name: "Editing",
      blocked: c.narration > 0 ? null
        : "No narration generated yet. Generate narration in Step 2 first.",
      hint: c.narration ? `${c.narration} beat${c.narration === 1 ? "" : "s"} timed` : "",
    },
    {
      id: 4, name: "Visual FX",
      blocked: storyboardApproved ? null
        : "The storyboard is not approved yet. Approve it in Step 1 first.",
      hint: c.beats ? `${c.rendered}/${c.beats} rendered` : "",
    },
    {
      id: 5, name: "Final Review",
      blocked: c.rendered > 0 ? null
        : "No beats rendered yet. Render in Step 4 first.",
      hint: c.rendered ? `${c.rendered} clip${c.rendered === 1 ? "" : "s"}` : "",
    },
  ];
}

export default function StepHeader({
  active, onChange, counts, scriptLocked, storyboardApproved
}: StepHeaderProps) {
  const steps = buildSteps(counts, scriptLocked, storyboardApproved);
  const current = steps.find((s) => s.id === active);

  return (
    <div className="flex flex-col gap-2.5">
      <nav className="flex items-stretch gap-2 overflow-x-auto pb-1 p-1.5 rounded-2xl bg-zinc-950/85 backdrop-blur-md border border-zinc-800/80 shadow-xl" aria-label="Pipeline steps">
        {steps.map((s) => {
          const isActive = s.id === active;
          const isLocked = s.blocked !== null;
          return (
            <button
              key={s.id}
              onClick={() => onChange(s.id)}
              aria-current={isActive ? "step" : undefined}
              className={[
                "group relative flex-1 min-w-[9.5rem] text-left px-3.5 py-2.5 rounded-xl border transition-all duration-200 hover:scale-[1.01] active:scale-[0.99]",
                isActive
                  ? "bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500 border-amber-300 text-zinc-950 font-bold shadow-[0_0_16px_rgba(245,158,11,0.35)]"
                  : isLocked
                  ? "bg-zinc-900/40 border-zinc-900/80 text-zinc-500 hover:border-zinc-800 hover:text-zinc-400"
                  : "bg-zinc-900/60 border-zinc-800/80 text-zinc-300 hover:border-amber-400/50 hover:text-white hover:shadow-md",
              ].join(" ")}
            >
              <span className="flex items-center gap-2">
                <span className={`text-[10px] font-mono font-extrabold px-1.5 py-0.5 rounded-md ${
                  isActive ? "bg-zinc-950/20 text-zinc-950" : "bg-zinc-800/80 text-zinc-400"
                }`}>
                  0{s.id}
                </span>
                <span className="text-xs font-extrabold truncate tracking-tight">{s.name}</span>
                {isLocked && !isActive && <Lock className="h-3 w-3 ml-auto shrink-0 text-zinc-600" />}
              </span>
              {s.hint && (
                <span className={`block text-[10px] font-mono mt-1 truncate ${
                  isActive ? "text-zinc-900/90 font-semibold" : "text-zinc-500"
                }`}>
                  {s.hint}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {current?.blocked && (
        <div className="bg-amber-950/30 backdrop-blur-md border border-amber-500/40 rounded-xl px-4 py-2.5 text-xs text-amber-200/90 font-mono leading-relaxed flex items-center gap-2.5 shadow-md">
          <Lock className="h-4 w-4 shrink-0 text-amber-400" />
          <span><span className="font-bold text-amber-400">Step {current.id} ({current.name}) is locked.</span> {current.blocked}</span>
        </div>
      )}
    </div>
  );
}
