"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import { CoverageSurvey, SurveyBeat } from "../types/director";
import {
  fetchBeatCoverageStates,
  fetchCoverageSurvey,
  fetchRunningPlanJobs,
  redirectSceneCoverage,
  waitForJob,
} from "../lib/directorApi";
import type { BeatCoverageState } from "../lib/directorApi";
import { Compass, Sparkles, AlertTriangle, Play, ChevronRight, CheckCircle2, RefreshCw, Clock } from "lucide-react";

interface CoverageSurveyPanelProps {
  onSelectBeats: (beats: string[]) => void;
}

/**
 * Why the plan did not open the workspace.
 *
 * `busy` is a 409: another plan is already running. Nothing has gone wrong and
 * the user did not cause it, so it must not be dressed as a failure. `failed`
 * is a plan that ran and did not produce one, or a POST that never started.
 */
type PlanProblem = { key: string; kind: "busy" | "failed"; message: string };

export default function CoverageSurveyPanel({ onSelectBeats }: CoverageSurveyPanelProps) {
  const [survey, setSurvey] = useState<CoverageSurvey | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [planningBeats, setPlanningBeats] = useState<string | null>(null);
  const [planLog, setPlanLog] = useState<string>("");
  const [planProblem, setPlanProblem] = useState<PlanProblem | null>(null);
  // Which beat this panel last carried through to a finished plan. The workspace
  // it opens sits far below the fold, so without this the entire visible outcome
  // of a ninety-second wait was a component off-screen quietly changing scene --
  // indistinguishable, from where the user sits, from the button doing nothing.
  const [planned, setPlanned] = useState<string | null>(null);
  // What the server already holds for these beats. The survey itself is pure
  // arithmetic over narration and never reads a plan, so without this the panel
  // offers PLAN SCENE on locked coverage and walks the user into a refusal.
  const [coverage, setCoverage] = useState<Record<string, BeatCoverageState>>({});
  // The beat whose re-plan has been asked for but not yet confirmed.
  const [confirmReplan, setConfirmReplan] = useState<string | null>(null);

  /** Re-read which beats already have coverage. Never blocks the survey. */
  const loadCoverage = useCallback((beatIds: string[]) => {
    if (beatIds.length === 0) return;
    fetchBeatCoverageStates(beatIds)
      .then(setCoverage)
      .catch(() => { /* the offer stays as it was rather than guessing */ });
  }, []);

  const loadSurvey = () => {
    setLoading(true);
    setError(null);
    fetchCoverageSurvey()
      .then((data) => {
        setSurvey(data);
        setLoading(false);
        loadCoverage((data.beats || []).map((b) => b.beat_id));
      })
      .catch((err) => {
        setError(err.message || "Failed to load coverage survey from GET /api/director/survey");
        setLoading(false);
      });
  };

  // Held in a ref, not closed over. The parent passes an inline arrow, so
  // `onSelectBeats` is a new function every render; in a dependency array it
  // would re-run the re-attach effect on every render, polling
  // /api/assemble/status forever and re-entering a job already being watched.
  const onSelectBeatsRef = useRef(onSelectBeats);
  // Same reasoning as `onSelectBeatsRef`: `followPlanJob` is deliberately stable
  // (mount-only re-attach depends on it), so anything it needs that changes with
  // render is reached through a ref rather than a dependency.
  const loadCoverageRef = useRef(loadCoverage);
  const surveyBeatIdsRef = useRef<string[]>([]);
  useEffect(() => {
    onSelectBeatsRef.current = onSelectBeats;
    loadCoverageRef.current = loadCoverage;
    surveyBeatIdsRef.current = (survey?.beats || []).map((b) => b.beat_id);
  });

  /**
   * Watch a plan job through to its outcome, and present that outcome.
   *
   * Shared by the button and by the re-attach on mount, deliberately: the two
   * must not be able to disagree about what "done" looks like. Returns nothing;
   * everything it has to say it says through state.
   */
  const followPlanJob = useCallback(async (beatKey: string, jobKey: string) => {
    setPlanningBeats(beatKey);
    setPlanLog("");
    setPlanProblem(null);
    try {
      const done = await waitForJob(jobKey, { onLog: setPlanLog });
      if (!done.ok) {
        setPlanProblem({
          key: beatKey,
          kind: "failed",
          message:
            done.status === "timeout"
              ? `The planner for ${beatKey} is still running — it has not finished, so there ` +
                `is no plan to open yet. Leave it and reopen the beat shortly.`
              : `Planning ${beatKey} failed, so no coverage plan was written. ` +
                `${(done.log || "").slice(-300)}`.trim(),
        });
        return;
      }
      setPlanned(beatKey);
      // The offer must now match what the server holds: this beat has coverage.
      loadCoverageRef.current(surveyBeatIdsRef.current);
      onSelectBeatsRef.current(beatKey.split(","));
    } finally {
      setPlanningBeats(null);
      setPlanLog("");
    }
  }, []);

  useEffect(() => {
    loadSurvey();
  }, []);

  /**
   * Re-attach to a plan this browser already asked for and then forgot.
   *
   * The job lives on the server, so a remount — a stage round trip, a re-render
   * that drops this panel, or a plain page reload — loses the React state that
   * was watching it while the job itself carries on. Asking the server what is
   * running is the only thing that survives all three; lifting the state into a
   * parent would survive only the first.
   *
   * Deliberately fire-and-forget on failure: a status endpoint that cannot be
   * read is not a reason to refuse to show the survey.
   */
  useEffect(() => {
    let live = true;
    fetchRunningPlanJobs()
      .then((running) => {
        const beat = Object.keys(running)[0];
        if (!live || !beat) return;
        return followPlanJob(beat, running[beat]);
      })
      .catch(() => { /* the survey stands on its own; nothing to re-attach to */ });
    return () => { live = false; };
    // Mount only: re-attaching is exactly what a fresh mount has to do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Plan a beat, and open the workspace only once the plan is on disk.
   *
   * POST /api/director/plan answers the instant `start_job` spawns a thread; the
   * plan itself is a plan -> critic -> re-plan cycle of Anthropic calls, roughly
   * ninety seconds. This used to call `onSelectBeats` about one second into that,
   * so the workspace mounted, fetched a plan that did not exist yet, showed
   * "404 / Unplanned" and — since it only refetches when `sceneId` changes —
   * never looked again. The plan landed a minute later into a screen that had
   * already given up. Worse, the catch branch was identical to the success
   * branch, so a 409 ("a plan for s003 is already running") and an outright
   * failure both opened the workspace exactly as success did.
   *
   * `waitForJob` is the pattern already used by DirectorWorkspace for this same
   * endpoint, and its own comment documents this exact failure.
   *
   * The direction of error is deliberate: an unfinished or failed plan is never
   * presented as a finished one. Saying "still planning" about a job that has
   * finished costs a refresh; opening a finished-looking workspace over a job
   * that never ran cost a walkthrough.
   */
  const handlePlanBeats = async (beatList: string[], replan = false) => {
    const key = beatList.join(",");
    // Only one plan job runs at a time server-side, so starting a second from
    // this panel could only earn a 409.
    if (planningBeats) return;
    setPlanningBeats(key);
    setPlanLog("");
    setPlanProblem(null);
    setPlanned(null);
    setConfirmReplan(null);
    try {
      const res = await redirectSceneCoverage(
        beatList,
        replan ? "Re-planning scene coverage over existing plan" : "Initial scene coverage planning",
        [],
        undefined,
        undefined,
        // Only ever true because the user confirmed a prompt that named what it
        // discards. Never a retry, never a default.
        replan
      );
      if (!res.job) {
        // Started, but unnamed: there is no job to watch, so this panel cannot
        // know when it finishes. It says so rather than guessing "ready".
        setPlanProblem({
          key,
          kind: "failed",
          message:
            `Planning ${key} started, but the server did not name the job, so this ` +
            `panel cannot tell when it finishes. Watch the job log and reopen the beat.`,
        });
        return;
      }
      // The same watcher the re-attach uses, so the two cannot disagree about
      // what finishing looks like. It owns clearing `planningBeats`.
      await followPlanJob(key, res.job);
      return;
    } catch (e: any) {
      const status = e?.status;
      setPlanProblem(
        status === 409
          ? {
              key,
              kind: "busy",
              message:
                `${e?.message || `A plan for ${key} is already running`} — nothing is wrong ` +
                `and nothing was lost; wait for it to finish rather than starting another.`,
            }
          : {
              key,
              kind: "failed",
              message:
                e?.message || `Could not start planning for ${key}, so no plan was requested.`,
            }
      );
    } finally {
      setPlanningBeats(null);
      setPlanLog("");
    }
  };

  if (loading) {
    return (
      <div className="w-full glass-panel p-6 rounded-2xl border border-zinc-800 flex items-center justify-center text-amber-500 gap-3">
        <Sparkles className="w-5 h-5 animate-spin" />
        <span className="font-mono text-xs font-bold">Querying GET /api/director/survey...</span>
      </div>
    );
  }

  if (error || !survey) {
    return (
      <div className="w-full glass-panel p-6 rounded-2xl border border-amber-500/40 bg-zinc-950/80 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-amber-500/15 text-amber-400">
            <AlertTriangle className="w-5 h-5" />
          </div>
          <div>
            <h4 className="font-bold text-xs text-zinc-100 font-mono">Coverage Survey (GET /api/director/survey)</h4>
            <p className="text-[11px] text-zinc-400 font-mono mt-0.5">{error || "Survey endpoint not responding"}</p>
          </div>
        </div>
        <button
          onClick={loadSurvey}
          className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs font-mono font-bold rounded-lg transition flex items-center gap-1.5"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry Survey
        </button>
      </div>
    );
  }

  return (
    <div className="w-full glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800 animate-in fade-in duration-200">
      {/* Header Banner */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-zinc-800/80">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-amber-500/15 text-amber-400 border border-amber-500/30">
            <Compass className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-zinc-100 flex items-center gap-2 font-mono">
              <span>EPISODE COVERAGE SURVEY</span>
              <span className="text-[10px] font-normal text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded border border-amber-500/30">
                Entry Point
              </span>
            </h3>
            <p className="text-xs text-zinc-400 mt-0.5">
              Deterministic pre-planning score identifying which beats need Director coverage most
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3 text-xs font-mono">
          <span className="text-zinc-300 font-bold">{survey.episode_seconds.toFixed(0)}s Episode</span>
          <span className="text-zinc-600">•</span>
          <span className="text-amber-400 font-bold bg-amber-500/10 border border-amber-500/30 px-2.5 py-1 rounded-lg">
            ⚠️ {survey.frozen_if_nothing_covered.toFixed(0)}s frozen if untouched ({survey.frozen_pct}%)
          </span>
        </div>
      </div>

      {/* The outcome of the last plan, stated where the button was pressed.
          The workspace this opens is stacked below the fold, so on its own it
          is not an outcome the user can see. */}
      {planned && (
        <div
          data-testid="plan-done-banner"
          className="flex items-center gap-2.5 px-3.5 py-2.5 rounded-xl border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 font-mono text-xs"
        >
          <CheckCircle2 className="w-4 h-4 shrink-0" />
          <span>
            <strong>Beat {planned} is planned.</strong> Its coverage is open in the Director
            workspace below — scroll down, or use the beat card, to review the shots.
          </span>
        </div>
      )}

      {/* Recommended Scenes & Beats List */}
      <div className="flex flex-col gap-2">
        {(!survey.beats || survey.beats.length === 0) && (
          // An empty list rendered as an empty list is why a Direct stage with
          // no beats read as a broken Direct stage: no rows, no error, nothing
          // in the console, and no way to tell "nothing to cover" from "this
          // screen failed". Say which.
          <div
            data-testid="survey-empty"
            className="px-3.5 py-3 rounded-xl border border-zinc-700 bg-zinc-950/60 text-zinc-300 font-mono text-xs leading-relaxed"
          >
            <strong className="text-amber-400">No beats to survey.</strong> The survey
            answered, and the active project has no narration beats in it — so there is
            nothing to plan coverage for yet. Draft or load a script first; this panel is
            not broken and no request failed.
          </div>
        )}
        {survey.beats && survey.beats.map((beat) => {
          const isHigh = beat.recommend === 3;
          const isMed = beat.recommend === 2;
          const isPlanning = planningBeats === beat.beat_id;
          const problem = planProblem && planProblem.key === beat.beat_id ? planProblem : null;
          const cover = coverage[beat.beat_id];
          const isConfirming = confirmReplan === beat.beat_id;

          const dots = isHigh ? "●●●" : isMed ? "●●" : "○";
          const dotColor = isHigh ? "text-red-400" : isMed ? "text-amber-400" : "text-zinc-500";

          return (
            <div
              key={beat.beat_id}
              className={`p-3.5 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3 transition-colors ${
                isHigh
                  ? "bg-amber-500/10 border-amber-500/40 hover:border-amber-500"
                  : "bg-zinc-950/60 border-zinc-800 hover:border-zinc-700"
              }`}
            >
              {/* Left Column: Priority dots & Beat info */}
              <div className="flex items-start gap-3">
                <span className={`text-xs font-mono font-bold ${dotColor} pt-0.5`}>{dots}</span>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono font-bold text-amber-400 bg-zinc-900 border border-zinc-800 px-2 py-0.5 rounded">
                      Beat {beat.beat_id}
                    </span>
                    <span className="text-xs font-mono text-zinc-300 font-bold">{beat.seconds.toFixed(1)}s</span>
                    <span className="text-[10px] font-mono text-zinc-500 uppercase px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800">
                      {beat.motion_type}
                    </span>
                    {/* What the server already holds. The survey score alone
                        described a beat as if nothing had ever been planned for
                        it, which is how a locked beat kept being offered as an
                        unplanned one — and why the shots on planned beats never
                        surfaced anywhere the user was looking. */}
                    {cover && (
                      <span
                        data-testid={`beat-coverage-${beat.beat_id}`}
                        data-locked={cover.locked ? "true" : "false"}
                        className={`text-[10px] font-mono font-bold uppercase px-1.5 py-0.5 rounded border ${
                          cover.locked
                            ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/40"
                            : "bg-sky-500/10 text-sky-300 border-sky-500/40"
                        }`}
                      >
                        {cover.locked ? "Locked" : "Planned"} · {cover.shots} shot
                        {cover.shots === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-zinc-300 font-mono mt-1 font-semibold">{beat.reason}</p>

                  {/* The job, while it is running. Ninety seconds of silence is
                      what made "did that button do anything?" the only reading. */}
                  {isPlanning && (
                    <p
                      data-testid={`plan-running-${beat.beat_id}`}
                      className="text-[11px] text-amber-300/90 font-mono mt-1.5 leading-relaxed"
                    >
                      Planning {beat.beat_id} — this takes about a minute and a half. The
                      workspace opens when the plan is written.
                      {planLog ? ` ${planLog}` : ""}
                    </p>
                  )}

                  {problem && (
                    <p
                      data-testid={`plan-problem-${beat.beat_id}`}
                      data-kind={problem.kind}
                      className={`text-[11px] font-mono mt-1.5 leading-relaxed ${
                        problem.kind === "busy" ? "text-zinc-300" : "text-red-300"
                      }`}
                    >
                      {problem.kind === "busy" ? (
                        <Clock className="w-3 h-3 inline-block mr-1 -mt-0.5" />
                      ) : (
                        <AlertTriangle className="w-3 h-3 inline-block mr-1 -mt-0.5" />
                      )}
                      {problem.message}
                    </p>
                  )}

                  {/* Re-planning throws away work that was reviewed, had its
                      warnings resolved, and was locked on purpose. It is not a
                      retry of the button next to it, so it says what it costs
                      and asks once. */}
                  {isConfirming && cover && (
                    <div
                      data-testid={`replan-confirm-${beat.beat_id}`}
                      className="mt-2 p-2.5 rounded-lg border border-red-500/40 bg-red-500/10 text-[11px] font-mono text-red-200 leading-relaxed"
                    >
                      <p>
                        Re-planning {beat.beat_id} <strong>discards its {cover.shots} existing
                        shot{cover.shots === 1 ? "" : "s"}</strong> and any warnings resolved on
                        them, and writes a new plan in their place.
                        {cover.locked && " This coverage is locked; that lock is what is being overridden."}
                        {" "}It cannot be undone.
                      </p>
                      <div className="flex items-center gap-2 mt-2">
                        <button
                          data-testid={`replan-go-${beat.beat_id}`}
                          onClick={() => handlePlanBeats([beat.beat_id], true)}
                          className="px-2.5 py-1 rounded-md bg-red-500 hover:bg-red-400 text-zinc-950 font-bold transition"
                        >
                          Discard and re-plan
                        </button>
                        <button
                          data-testid={`replan-cancel-${beat.beat_id}`}
                          onClick={() => setConfirmReplan(null)}
                          className="px-2.5 py-1 rounded-md bg-zinc-800 hover:bg-zinc-700 text-zinc-200 font-bold transition"
                        >
                          Keep it
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* Action. A beat the server already has coverage for is not on
                  offer to plan: clicking it earned a refusal the user had been
                  invited into, dressed as a pipeline failure. */}
              <div className="flex items-center gap-2 shrink-0">
                {cover ? (
                  <>
                    <button
                      data-testid={`open-beat-${beat.beat_id}`}
                      onClick={() => onSelectBeats([beat.beat_id])}
                      className="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-lg transition flex items-center gap-1.5 shadow"
                    >
                      <ChevronRight className="w-3.5 h-3.5" />
                      <span>OPEN {cover.shots} SHOT{cover.shots === 1 ? "" : "S"} ({beat.beat_id})</span>
                    </button>
                    <button
                      data-testid={`replan-beat-${beat.beat_id}`}
                      onClick={() => setConfirmReplan(isConfirming ? null : beat.beat_id)}
                      disabled={Boolean(planningBeats)}
                      title="Throw away this coverage and plan the beat again"
                      className="px-3 py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs font-mono font-bold rounded-lg transition flex items-center gap-1.5 disabled:opacity-50"
                    >
                      {isPlanning ? (
                        <Sparkles className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <RefreshCw className="w-3.5 h-3.5" />
                      )}
                      <span>{isPlanning ? "RE-PLANNING…" : "RE-PLAN"}</span>
                    </button>
                  </>
                ) : (
                  <button
                    data-testid={`plan-beat-${beat.beat_id}`}
                    onClick={() => handlePlanBeats([beat.beat_id])}
                    disabled={Boolean(planningBeats)}
                    className="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-lg transition flex items-center gap-1.5 shadow disabled:opacity-50"
                  >
                    {isPlanning ? (
                      <Sparkles className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Play className="w-3.5 h-3.5 fill-current" />
                    )}
                    <span>{isPlanning ? `PLANNING (${beat.beat_id})…` : `PLAN SCENE (${beat.beat_id})`}</span>
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
