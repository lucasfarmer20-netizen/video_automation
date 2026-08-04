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
  /** null when the step is available; otherwise why it is not. */
  blocked: string | null;
  hint: string;
}

/** Steps mirror step2_audio_studio.jpg. Gates mirror the pipeline's real ones:
 *  nothing runs on an unapproved script, nothing renders on an unapproved
 *  storyboard. */
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
        : "The script is not locked yet. Narration runs on the locked script, so lock it in Step 1 first.",
      hint: c.beats ? `${c.narration}/${c.beats} voiced · ${c.sfx}/${c.beats} sfx` : "",
    },
    {
      id: 3, name: "Editing",
      blocked: c.narration > 0 ? null
        : "No narration generated yet. Beat durations are narration-led, so there is nothing to trim until Step 2 has run.",
      hint: c.narration ? `${c.narration} beat${c.narration === 1 ? "" : "s"} timed` : "",
    },
    {
      id: 4, name: "Visual FX",
      blocked: storyboardApproved ? null
        : "The storyboard is not approved. Nothing renders — free or paid — until every beat is approved in Step 1.",
      hint: c.beats ? `${c.rendered}/${c.beats} rendered` : "",
    },
    {
      id: 5, name: "Final Review",
      blocked: c.rendered > 0 ? null
        : "No beats have been rendered yet, so there is nothing to review. Render in Step 4.",
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
    <div className="flex flex-col gap-2">
      <nav className="flex items-stretch gap-1 overflow-x-auto pb-0.5" aria-label="Pipeline steps">
        {steps.map((s) => {
          const isActive = s.id === active;
          const isLocked = s.blocked !== null;
          // Locked steps stay clickable on purpose. Hiding or disabling a
          // control reads as "my controls vanished"; selecting it shows the
          // reason instead.
          return (
            <button
              key={s.id}
              onClick={() => onChange(s.id)}
              aria-current={isActive ? "step" : undefined}
              className={[
                "group relative flex-1 min-w-[8.5rem] text-left px-3 py-2 rounded-lg border transition",
                isActive
                  ? "bg-amber-500 border-amber-400 text-zinc-950 shadow-md shadow-amber-500/20"
                  : isLocked
                  ? "bg-zinc-950/60 border-zinc-900 text-zinc-600 hover:border-zinc-800"
                  : "bg-zinc-950/60 border-zinc-800 text-zinc-300 hover:border-zinc-700 hover:text-zinc-100",
              ].join(" ")}
            >
              <span className="flex items-center gap-1.5">
                <span className={`text-[10px] font-mono font-bold ${isActive ? "text-zinc-900" : "text-zinc-500"}`}>
                  {s.id}
                </span>
                <span className="text-xs font-bold truncate">{s.name}</span>
                {isLocked && !isActive && <Lock className="h-3 w-3 ml-auto shrink-0 text-zinc-700" />}
              </span>
              {s.hint && (
                <span className={`block text-[10px] font-mono mt-0.5 truncate ${
                  isActive ? "text-zinc-800" : "text-zinc-600"
                }`}>
                  {s.hint}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {current?.blocked && (
        <div className="bg-amber-950/25 border border-amber-500/30 rounded-lg px-3 py-2 text-xs text-amber-200/90 font-mono leading-relaxed flex gap-2">
          <Lock className="h-3.5 w-3.5 shrink-0 mt-0.5 text-amber-400" />
          <span><span className="font-bold text-amber-400">Step {current.id} is locked.</span> {current.blocked}</span>
        </div>
      )}
    </div>
  );
}
