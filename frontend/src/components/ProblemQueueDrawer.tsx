"use client";

import React from "react";
import { DirectorShot, SceneFinding } from "../types/director";
import { AlertTriangle, ShieldAlert, X, CheckCircle2, ArrowRight, Wrench, Check } from "lucide-react";

/**
 * The queue works at the scene's scope, because the lock does.
 *
 * `POST /api/director/lock_scene` walks every beat in `beats[]` and refuses on
 * any one of them holding a finding nobody decided. A queue that showed only the
 * open beat's findings was therefore missing decisions the human had to make
 * before the gate in front of them would open — and the count it produced was
 * then used to say "No finding in this scene is awaiting a decision" while four
 * findings on two other beats of that scene were blocking the lock.
 *
 * Showing all the beats' findings together, rather than reporting per-beat and
 * naming what is elsewhere, is the choice that makes the cross-beat finding
 * land somewhere sensible: "s004.06 and s006.01 are the same subject at the same
 * shot size" is a fact about the scene, and it only exists because the beats
 * were planned together. In a per-beat queue it has no correct home. Here it has
 * one, and the beat chips say which beats it spans.
 */
interface ProblemQueueDrawerProps {
  /** Every finding in the scene, each tagged with the beats that carry it. */
  findings: SceneFinding[];
  /** The OPEN beat's coverage — the only shots this screen can jump to. */
  shots: DirectorShot[];
  /** Which beat's plan is on screen behind the drawer. */
  openBeat: string;
  /** Every beat this scene locks as one unit. */
  sceneBeats: string[];
  isOpen: boolean;
  onClose: () => void;
  onSelectShot: (shotId: string) => void;
  /** Records a durable decision about a finding. "resolved" = the plan was
   *  changed to answer it; "accepted" = understood and deliberately kept.
   *  Takes the finding, not just its id, because the decision has to be written
   *  to every beat carrying it — see `SceneFinding`. */
  onResolveWarning: (finding: SceneFinding, decision?: "resolved" | "accepted" | "") => void;
}

export default function ProblemQueueDrawer({
  findings,
  shots,
  openBeat,
  sceneBeats,
  isOpen,
  onClose,
  onSelectShot,
  onResolveWarning,
}: ProblemQueueDrawerProps) {
  if (!isOpen) return null;

  const isMultiBeat = sceneBeats.length > 1;
  const undecided = findings.filter((f) => !f.decision);
  const elsewhere = undecided.filter((f) => !f.beats.includes(openBeat)).length;

  const warningMap: Record<string, SceneFinding[]> = {
    identity: findings.filter((f) => f.warning.type === "identity"),
    generation: findings.filter((f) => f.warning.type === "generation"),
    timing: findings.filter((f) => f.warning.type === "timing"),
    continuity: findings.filter((f) => f.warning.type === "continuity"),
    coverage: findings.filter((f) => f.warning.type === "coverage"),
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
              <p className="text-xs text-zinc-400">
                {findings.length} items require director attention
                {isMultiBeat ? ` across ${sceneBeats.join(", ")}` : ""}
              </p>
              {/* The sentence the old queue could not say, because it had not
                  looked: these are the findings that refuse the lock from a beat
                  the human is not currently viewing. */}
              {elsewhere > 0 && (
                <p data-testid="findings-elsewhere" className="text-[11px] font-mono text-amber-400 mt-0.5">
                  {elsewhere} of them {elsewhere === 1 ? "is" : "are"} on other beats in this scene.
                </p>
              )}
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
          {findings.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-64 text-center text-zinc-500 gap-2">
              <CheckCircle2 className="w-10 h-10 text-emerald-500/50" />
              <p className="text-sm font-semibold text-zinc-300">All Coverage Checks Passed</p>
              <p className="text-xs">No technical compromises or timing issues detected.</p>
            </div>
          ) : (
            findings.map((f, index) => {
              const w = f.warning;
              const relatedShot = shots.find((s) => s.id === w.shot_id);
              const decided = f.decision ? { decision: f.decision, note: f.note } : undefined;
              // A finding carried by more than one beat is one the critic wrote
              // about the scene: it is stored on each of them under a single id,
              // and it is what a scene planned as one unit produces.
              const spansBeats = f.beats.length > 1;
              return (
                <div
                  key={f.id || `${f.beats.join("-")}#${index}`}
                  data-testid="queue-finding"
                  data-beats={f.beats.join(",")}
                  className="p-3.5 rounded-xl border bg-zinc-900/60 border-zinc-800 flex flex-col gap-2 hover:border-amber-500/50 transition-colors"
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <span className="flex items-center gap-1.5">
                      <span className="text-xs font-mono font-bold text-amber-400 bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 rounded capitalize">
                        {String(w.kind || w.type || "warning").replace(/_/g, " ")}
                      </span>
                      {isMultiBeat && (
                        <span
                          className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-zinc-700 bg-zinc-950 text-zinc-400"
                          title={
                            spansBeats
                              ? "The critic filed this against the scene, so it is recorded on every one of these beats and a decision is written to all of them."
                              : "The beat whose plan carries this finding."
                          }
                        >
                          {spansBeats ? `${f.beats.join(" + ")} (scene)` : f.beats.join(", ")}
                        </span>
                      )}
                    </span>
                    {relatedShot ? (
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
                    ) : (
                      w.shot_id && (
                        // No card for it on this screen, so no link — saying
                        // where it is beats offering a jump that goes nowhere.
                        <span
                          className="text-xs font-mono text-zinc-500"
                          title="This shot belongs to another beat of the scene; open that beat to edit it."
                        >
                          Shot {w.shot_id}
                        </span>
                      )
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
                          onClick={() => onResolveWarning(f, "")}
                          className="ml-1 text-emerald-400/70 hover:text-emerald-200 underline"
                          title="Undo this decision — the finding will block locking again"
                        >
                          undo
                        </button>
                      </span>
                    ) : (
                      <span className="flex items-center gap-1.5">
                        <button
                          onClick={() => onResolveWarning(f, "resolved")}
                          disabled={!f.id}
                          className="px-2.5 py-1 bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-300 text-[11px] font-mono rounded transition-colors disabled:opacity-40"
                          title="The plan has been changed to answer this finding"
                        >
                          Mark resolved
                        </button>
                        <button
                          onClick={() => onResolveWarning(f, "accepted")}
                          disabled={!f.id}
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
