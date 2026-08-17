"use client";

import React, { useState, useEffect } from "react";
import {
  DirectorCoveragePlan,
  DirectorShot,
  DirectorWarning,
  CreativePreferences,
  SceneSummary,
} from "../types/director";
import {
  fetchCoveragePlan,
  redirectSceneCoverage,
  setCoverageStatus,
  performShotAction,
  updateShot,
  waitForJob,
  critiqueCoverage,
  decideDirectorWarning,
  compileCoverage,
} from "../lib/directorApi";
import type { CompileRefusal } from "../lib/directorApi";

import DirectorShotCard from "./DirectorShotCard";
import ShotInspectorDrawer from "./ShotInspectorDrawer";
import ProblemQueueDrawer from "./ProblemQueueDrawer";
import TakeSelectorModal from "./TakeSelectorModal";
import CinemaScrubberPlayer from "./CinemaScrubberPlayer";
import CompactMontageMatrix from "./CompactMontageMatrix";
import CoverageRhythmBeatSheet from "./CoverageRhythmBeatSheet";

import {
  Clapperboard,
  Sparkles,
  Lock,
  Unlock,
  AlertTriangle,
  Send,
  Sliders,
  DollarSign,
  Layers,
  Clock,
  Film,
  CheckCircle2,
  ChevronRight,
  RotateCcw,
  LayoutGrid,
  Play,
  Grid,
  ArrowLeft,
  List,
} from "lucide-react";

interface DirectorWorkspaceProps {
  sceneId: string;
  activeProjectTitle?: string;
  mediaUrl: (path: string) => string;
  onBackToStoryboard?: () => void;
}

const QUICK_SHORTCUTS = [
  "+ More documentary",
  "+ More cinematic",
  "+ More environment",
  "+ More character",
  "+ Fewer cuts",
  "+ Slower pacing",
  "+ Less dramatic",
  "+ Reduce generation cost",
];

/**
 * Why a compile did not run — kept as distinct as the server keeps it.
 *
 * `POST /api/director/compile/{beat}` answers five different refusals, and each
 * of its messages tells the human a different next action:
 *
 *   draft               the plan was never locked -> lock it
 *   approval_drifted    it WAS locked, then edited -> review and lock again
 *   stale               the script line moved under it -> review and lock again
 *   unresolved_warnings a locked plan with undecided findings -> decide them
 *   busy                a compile for this beat is already running -> wait
 *   missing             there is no plan for this beat at all
 *   failed              the job ran and did not produce a beat clip, or the
 *                       request itself failed
 *
 * Collapsing those into "compile failed" would discard the only part of the
 * reply that is actionable, so the server's own sentence is rendered verbatim
 * and the kind only decides the framing around it.
 */
type CompileProblemKind =
  | "quote_changed"
  | "draft"
  | "approval_drifted"
  | "stale"
  | "unresolved_warnings"
  | "busy"
  | "missing"
  | "failed";

type CompileProblem = { kind: CompileProblemKind; message: string };

/**
 * Classify a refusal from the server's payload, never from its prose.
 *
 * `approval_drifted` is a boolean the route always sends on the draft 409, so
 * `false` is meaningful and must be distinguished from absent: it is what tells
 * "never locked" apart from "locked, then edited".
 *
 * `signature_mismatch` is checked FIRST, matching the route's own order and for
 * the route's own reason: it means the plan on the server is not the one the
 * human was quoted for, so nothing else about that plan is worth telling them —
 * they have not seen it.
 */
function classifyCompileRefusal(err: CompileRefusal | undefined): CompileProblem {
  const message = err?.message || "The compile request failed before it started.";
  if (err?.signatureMismatch) return { kind: "quote_changed", message };
  if (err?.approvalDrifted === true) return { kind: "approval_drifted", message };
  if (err?.approvalDrifted === false) return { kind: "draft", message };
  if (err?.stale) return { kind: "stale", message };
  if (err?.warnings) return { kind: "unresolved_warnings", message };
  if (err?.status === 404) return { kind: "missing", message };
  if (err?.status === 409) return { kind: "busy", message };
  return { kind: "failed", message };
}

/**
 * What a re-check of the critic actually produced.
 *
 * Exactly three states, and the third is the one that did not exist: a failure
 * used to leave the previous findings on screen and say nothing, which reads as
 * "the critic ran and found nothing new" — from there the human locks and spends.
 */
type CritiqueOutcome =
  | { kind: "found"; count: number }
  | { kind: "clear"; count: 0 }
  | { kind: "failed"; message: string };

/** Refusals the server raises before `start_job`, so nothing was bought. */
const SPENT_NOTHING: CompileProblemKind[] = [
  "quote_changed",
  "draft",
  "approval_drifted",
  "stale",
  "unresolved_warnings",
  "busy",
];

/**
 * What compiling this plan BUYS, as the server counts it.
 *
 * Falling back to the plan's own `ai_video` shots keeps the quote honest when
 * the scene summary is absent; it is a floor, never an overstatement. This
 * counts server-assigned `motion_type`s — the price itself is only ever
 * `estimated_cost`, computed on the server, never arithmetic done here.
 */
function paidShotsOf(plan: DirectorCoveragePlan): number {
  return (
    plan.paid_shots ??
    plan.coverage.filter((s) => s.motion_type === "ai_video").length
  );
}

/** `CoveragePlan.status` in `SceneSummary`'s vocabulary. Nothing is invented:
 *  every plan status has one reading, and there is no default branch to fall
 *  through into. */
const SUMMARY_STATUS: Record<DirectorCoveragePlan["status"], SceneSummary["status"]> = {
  draft: "draft",
  locked: "locked",
  compiling: "generating",
  compiled: "compiled",
  orphaned: "uncovered",
};

/**
 * This scene, summarised from the server's own numbers.
 *
 * The montage matrix used to be handed `MOCK_SCENES` — a fixture in
 * `directorApi.ts` whose own comment says it is "for unit testing / UI preview".
 * It is a whole invented film: "s004 — The Mountain Takes Its Toll", 11 shots,
 * `estimated_cost: 3.82`. In production the human opened the Director and was
 * shown that fabricated scene, and a fabricated $3.82, while their real locked
 * plan was not on screen at all. Worse, the fixture's `shots_count` then sliced
 * the REAL shot list (`allShots.slice(0, scene.shots_count)`), so the genuine
 * coverage was truncated to the shape of the mock.
 *
 * `src/lib/noMockData.test.ts` scans production sources so this cannot return
 * anywhere. It strips comments first, which is why the fixture can be named
 * here: the record of what went wrong is worth more than a scanner that is
 * marginally harder to talk around, and a comment cannot render anything.
 *
 * This is the same rule as the compile gate two sections up, and it would be an
 * odd thing to enforce for one number and not the next one along: a figure the
 * human might act on comes from the server's summary for the project actually
 * loaded, or it is not rendered. There is deliberately no fallback to the
 * fixture for missing data — a plan is always present by the time this renders,
 * and a mock offered as a fallback is exactly how this one survived.
 *
 * `estimated_cost` here is `summary.estimated_cost` from `/api/director/scene`,
 * computed by `planner.scene_summary` over the beats actually requested.
 */
function sceneSummaryOf(plan: DirectorCoveragePlan, sceneId: string): SceneSummary {
  return {
    scene_id: plan.scene_id || sceneId,
    title: plan.scene_title || `Scene ${plan.scene_id || sceneId}`,
    duration: plan.beat_duration ?? plan.total_duration,
    beats_count: plan.scene_beats?.length || 1,
    shots_count: plan.coverage.length,
    estimated_cost: plan.estimated_cost,
    status: SUMMARY_STATUS[plan.status],
    warnings_count: plan.warnings.length,
  };
}

export default function DirectorWorkspace({
  sceneId,
  activeProjectTitle,
  mediaUrl,
  onBackToStoryboard,
}: DirectorWorkspaceProps) {
  const [coveragePlan, setCoveragePlan] = useState<DirectorCoveragePlan | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  // Directing Controls State
  const [redirectInput, setRedirectInput] = useState<string>("");
  const [selectedShortcuts, setSelectedShortcuts] = useState<string[]>([]);
  const [preferences, setPreferences] = useState<CreativePreferences>({
    documentaryVsCinematic: 60,
    restrainedVsDramatic: 35,
    economicalVsQuality: 75,
  });

  // Drawer, Modal & Review Mode State (Plan Review vs Take Review)
  const [reviewMode, setReviewMode] = useState<"rhythm" | "grid" | "scrubber" | "matrix">("rhythm");
  const [selectedShot, setSelectedShot] = useState<DirectorShot | null>(null);
  const [problemDrawerOpen, setProblemDrawerOpen] = useState<boolean>(false);
  const [takeModalShot, setTakeModalShot] = useState<DirectorShot | null>(null);
  const [redirecting, setRedirecting] = useState<boolean>(false);

  // Compile (the thing a locked plan exists for) — see handleCompileCoverage.
  const [compileGateOpen, setCompileGateOpen] = useState<boolean>(false);
  const [compilingBeat, setCompilingBeat] = useState<string | null>(null);
  const [compileLog, setCompileLog] = useState<string>("");
  const [compileProblem, setCompileProblem] = useState<CompileProblem | null>(null);
  const [compileDone, setCompileDone] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);

  // Load coverage plan on scene switch
  useEffect(() => {
    let isMounted = true;
    setLoading(true);
    setError(null);
    fetchCoveragePlan(sceneId)
      .then((plan) => {
        if (isMounted) {
          setCoveragePlan(plan);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (isMounted) {
          setError(err.message || "No director coverage plan found for this scene (404)");
          setCoveragePlan(null);
          setLoading(false);
        }
      });
    return () => {
      isMounted = false;
    };
  }, [sceneId]);

  const toggleShortcut = (shortcut: string) => {
    setSelectedShortcuts((prev) =>
      prev.includes(shortcut) ? prev.filter((s) => s !== shortcut) : [...prev, shortcut]
    );
  };

  const handleRedirectScene = async () => {
    if (redirecting) return;
    setRedirecting(true);
    try {
      const res = await redirectSceneCoverage(
        sceneId,
        redirectInput,
        selectedShortcuts,
        preferences
      );
      // The response means "a thread has started", not "the plan is ready".
      // Refetching here returned the plan the user was trying to replace.
      if (res.ok) {
        if (res.job) {
          const done = await waitForJob(res.job, { onLog: setRedirectLog });
          if (!done.ok) {
            setError(
              done.status === "timeout"
                ? "The planner is still running — reopen this scene shortly."
                : `Planning failed. ${(done.log || "").slice(-300)}`
            );
            return;
          }
        }
        const updatedPlan = await fetchCoveragePlan(coveragePlan?.scene_beats || sceneId);
        setCoveragePlan(updatedPlan);
        setError(null);
        setRedirectLog("");
      }
    } catch (err: any) {
      setError(err.message || "Failed to issue POST /api/director/plan request");
    } finally {
      setRedirecting(false);
    }
  };

  const handleToggleLock = async () => {
    if (!coveragePlan) return;
    const isCurrentlyLocked = coveragePlan.status === "locked" || coveragePlan.status === "compiled";
    const shouldLock = !isCurrentlyLocked;
    const beatsToLock = coveragePlan.scene_beats && coveragePlan.scene_beats.length > 0
      ? coveragePlan.scene_beats
      : sceneId;
    await setCoverageStatus(beatsToLock, shouldLock);
    setCoveragePlan({ ...coveragePlan, status: shouldLock ? "locked" : "draft" });
  };

  /**
   * Compile the locked plan — the control the studio did not have.
   *
   * `POST /api/director/compile/{beat}` has existed and been fully gated since
   * Spike B, and nothing in the frontend called it. A user could survey the
   * episode, plan a scene, decide its warnings and lock it, and then have no way
   * to turn any of that into assets: "I don't know if it is generating the new
   * assets, and if not, I don't know how to generate them."
   *
   * Three things this handler must get right:
   *
   * 1. **It is a long-running job.** The POST returns the moment `start_job`
   *    spawns a thread. Compiling renders every shot and, for `ai_video` ones,
   *    waits on a paid video model — minutes, not seconds. `waitForJob` is the
   *    pattern `handleRedirectScene` and `CoverageSurveyPanel.handlePlanBeats`
   *    both converged on after fire-and-forget cost this project two rounds.
   *
   * 2. **A refusal is not a failure and the refusals are not each other.** Each
   *    409 says precisely what to do next, so the server's sentence is what
   *    reaches the screen. Nothing is retried, and there is no force flag to
   *    offer: a client-side bypass of the draft check is exactly what the
   *    backend deleted.
   *
   * 3. **§11.4 — nothing claims "compiled" but the server.** A finished job is
   *    evidence the thread ended, not that a beat clip exists. The plan is
   *    refetched and the success line is written from the status that comes
   *    back; if the job ended and the plan does not say `compiled`, this says
   *    so instead of congratulating the user.
   *
   * The plan is locked per scene, so it compiles per scene: one job per beat in
   * `scene_beats`, in order, stopping at the first beat that refuses rather than
   * spending on later beats after an unexplained one.
   *
   * And the price the human agreed to binds the plan it was quoted for. The
   * gate above quotes THIS plan; the request carries `approved_signature`, the
   * server's identity for it, and the route refuses if what it is about to
   * dispatch is something else. Without that the request named only a beat, so
   * a plan replaced and re-locked in another tab between the gate opening and
   * the confirmation compiled at the newer price on consent given for the older
   * one. On that refusal the plan is refetched and the human is re-quoted;
   * nothing is retried, because a new price needs a new decision.
   */
  const handleCompileCoverage = async () => {
    if (!coveragePlan || compilingBeat) return;
    const beats =
      coveragePlan.scene_beats && coveragePlan.scene_beats.length > 0
        ? coveragePlan.scene_beats
        : [sceneId];

    setCompileGateOpen(false);
    setCompileProblem(null);
    setCompileDone(null);
    setCompileLog("");

    try {
      for (const beat of beats) {
        setCompilingBeat(beat);
        const res = await compileCoverage(beat, coveragePlan.approved_signature);
        if (!res.job) {
          // Started, but unnamed: there is nothing to watch, so this screen
          // cannot know when it ends — and must not imply that it has.
          setCompileProblem({
            kind: "failed",
            message:
              `The compile for ${beat} started, but the server did not name the job, ` +
              `so this screen cannot tell when it finishes or whether it succeeded. ` +
              `Watch the job log before starting another.`,
          });
          return;
        }
        const done = await waitForJob(res.job, { onLog: setCompileLog });
        if (!done.ok) {
          setCompileProblem({
            kind: "failed",
            message:
              done.status === "timeout"
                ? `The compile for ${beat} is still running — it has not finished, so ` +
                  `there is no beat clip yet. Leave it and reopen this scene shortly.`
                : `Compiling ${beat} failed, so the beat clip was not written. ` +
                  `${(done.log || "").slice(-300)}`.trim(),
          });
          return;
        }
      }

      // §11.4. The job ending is not the claim; the saved plan is.
      const fresh = await fetchCoveragePlan(beats);
      setCoveragePlan(fresh);
      if (fresh.status === "compiled") {
        setCompileDone(
          `${beats.join(", ")} compiled — the plan now reads "${fresh.status}". ` +
            `Open Cinema Scrubber or Montage Matrix to review the takes.`
        );
      } else {
        setCompileProblem({
          kind: "failed",
          message:
            `The compile job for ${beats.join(", ")} finished, but the saved plan still ` +
            `reads "${fresh.status}" — so there is no compiled coverage to review yet. ` +
            `Check the job log before spending again.`,
        });
      }
    } catch (e) {
      const problem = classifyCompileRefusal(e as CompileRefusal);
      setCompileProblem(problem);
      if (problem.kind === "quote_changed") {
        // Re-quote. The refusal on its own tells them the plan moved; it does
        // not tell them what it moved TO, and they cannot consent to a price
        // they have not been shown. The gate stays closed either way — a new
        // price is a new decision, so they click COMPILE COVERAGE again.
        try {
          const fresh = await fetchCoveragePlan(beats);
          setCoveragePlan(fresh);
          setCompileProblem({
            kind: "quote_changed",
            message:
              `${problem.message} The plan on screen has been refreshed: it is now ` +
              `${fresh.coverage.length} shot${fresh.coverage.length === 1 ? "" : "s"}, ` +
              `${paidShotsOf(fresh)} paid, $${fresh.estimated_cost.toFixed(2)}.`,
          });
        } catch {
          // The refusal itself stands and is already on screen. A failed
          // refresh must not overwrite it with a message about the refresh.
        }
      }
    } finally {
      setCompilingBeat(null);
      setCompileLog("");
    }
  };

  const [critiquing, setCritiquing] = useState<boolean>(false);
  const [critiqueOutcome, setCritiqueOutcome] = useState<CritiqueOutcome | null>(null);
  const [shotError, setShotError] = useState<string | null>(null);
  const [redirectLog, setRedirectLog] = useState<string>("");

  // Every edit round-trips. This used to be local React state and nothing else:
  // no call to POST /api/director/shot/{id}, the endpoint the backend documents
  // as "the only sanctioned way" to edit a shot. Duration drags, tier flips, take
  // selections and framing changes all evaporated on scene switch or refetch --
  // and LOCK then validated the UNEDITED plan, 400'd with "nothing was locked",
  // and handleToggleLock never caught it, so the button silently did nothing
  // while the header showed a green matched-coverage badge.
  //
  // The server is also the only place that can do this correctly: it recomputes
  // estimated_cost, re-routes the backend, and rebalances the sibling shots so
  // coverage still sums to the beat. Client-side cost arithmetic is gone with it.
  const handleUpdateShot = async (shotId: string, updates: Partial<DirectorShot>) => {
    if (!coveragePlan) return;
    const before = coveragePlan;

    // Optimistic, so dragging a duration still feels immediate...
    const optimistic = coveragePlan.coverage.map((s) =>
      s.id === shotId ? { ...s, ...updates } : s
    );
    // Invalidate existing warnings for this shot on hand edit until re-checked (Addendum 5.3)
    const invalidatedWarnings = coveragePlan.warnings.map((w) =>
      w.shot_id === shotId ? { ...w, stale: true } : w
    );
    setCoveragePlan({ ...coveragePlan, coverage: optimistic, warnings: invalidatedWarnings });
    if (selectedShot && selectedShot.id === shotId) {
      setSelectedShot({ ...selectedShot, ...updates });
    }

    try {
      const res = await updateShot(shotId, updates as Record<string, unknown>);
      // ...but the server's version wins, including the siblings it rebalanced
      // and the cost it recomputed. Refetching the plan is the only way to see
      // the sibling changes, which a merge of `res.shot` alone would miss.
      const fresh = await fetchCoveragePlan(coveragePlan.scene_beats || [sceneId]);
      setCoveragePlan({
        ...fresh,
        warnings: (fresh.warnings || []).map((w) =>
          w.shot_id === shotId ? { ...w, stale: true } : w
        ),
      });
      if (res.shot && selectedShot && selectedShot.id === shotId) {
        setSelectedShot(res.shot);
      }
      if (res.problems?.length) setShotError(res.problems.join("; "));
      else setShotError(null);
    } catch (e: any) {
      // A rejected edit must not linger on screen as though it took.
      setCoveragePlan(before);
      if (selectedShot && selectedShot.id === shotId) {
        setSelectedShot(before.coverage.find((s) => s.id === shotId) || null);
      }
      setShotError(e?.message || "That edit was rejected by the server.");
    }
  };

  /**
   * Re-run the critic over the whole scene, and say which of three things happened.
   *
   * The endpoint is synchronous, so unlike planning and compiling there is no job
   * to wait for. The defect was on the other side: the catch branch read
   *
   *     } catch (e: any) { console.log("Critique check complete"); }
   *
   * A failure was logged as "complete" and nothing reached the screen. The user
   * clicks Re-check, the request fails, the spinner stops, the warning list is
   * unchanged — and that is indistinguishable from "the critic ran and found
   * nothing new". Which is the most dangerous wrong answer available here,
   * because the next two things the human does are LOCK and then SPEND.
   *
   * The quiet path is the same defect without an exception: a reply whose `ok`
   * is falsy, or that carries no warning list at all, left the old findings on
   * screen and said nothing. An absent list has re-checked nothing; treating it
   * as an empty one clears a scene on the strength of a reply that never said so.
   *
   * So the outcome is now always exactly one of three legible states — findings,
   * genuinely none, or failed and here is why — and the failure says which
   * findings are on screen, because they are the ones from *before* this run.
   * "Nothing changed" must never be able to mean "it broke". Contract §11.4.
   */
  const handleRecheckCritique = async () => {
    if (!coveragePlan || critiquing) return;
    const beats =
      coveragePlan.scene_beats && coveragePlan.scene_beats.length > 0
        ? coveragePlan.scene_beats
        : [sceneId];
    setCritiquing(true);
    setCritiqueOutcome(null);
    try {
      const res = await critiqueCoverage(beats);
      if (!res.ok || !Array.isArray(res.warnings)) {
        setCritiqueOutcome({
          kind: "failed",
          message:
            `The critic replied about ${beats.join(", ")} without a warning list. ` +
            `Nothing on screen has been re-checked — any findings shown are the ` +
            `ones from before this run.`,
        });
        return;
      }
      const fresh = res.warnings;
      setCoveragePlan((prev) => (prev ? { ...prev, warnings: fresh } : prev));
      setCritiqueOutcome(
        fresh.length > 0
          ? { kind: "found", count: fresh.length }
          : { kind: "clear", count: 0 }
      );
    } catch (e) {
      setCritiqueOutcome({
        kind: "failed",
        message:
          `${(e as Error)?.message || `Re-checking ${beats.join(", ")} failed.`} ` +
          `Nothing on screen has been re-checked — any findings shown are the ` +
          `ones from before this run.`,
      });
    } finally {
      setCritiquing(false);
    }
  };

  // Wrapped. This was un-caught, so every 404 from the phantom /action route
  // became an unhandled rejection with no banner at all.
  const handleShotAction = async (action: string, shot: DirectorShot) => {
    try {
      const res = await performShotAction(shot.id, action, shot);
      if (!res.supported) {
        setShotError(res.error || `"${action}" is not available yet.`);
        return;
      }
      const fresh = await fetchCoveragePlan(coveragePlan?.scene_beats || [sceneId]);
      setCoveragePlan(fresh);
      setShotError(null);
    } catch (e: any) {
      setShotError(e?.message || `"${action}" failed.`);
    }
  };

  const handleSelectTake = (shotId: string, variationIndex: number) => {
    handleUpdateShot(shotId, { chosen_variation: variationIndex });
  };

  /**
   * Record a decision about a critic finding.
   *
   * This used to filter the warning out of local state and stop there, so the
   * header could read "0 issues" while the saved plan still held the finding --
   * and locking the scene silently approved it. The decision is persisted first
   * and the server's warning list replaces ours, so what is on screen is what
   * the backend will enforce at lock time.
   *
   * The finding itself is never removed. It stays visible with its decision
   * attached; that is the record of the review having happened.
   */
  const handleResolveWarning = async (
    warningId: string,
    // Empty string CLEARS the decision. It must not be defaulted away: 'undo'
    // sends it, and a default of "resolved" would silently re-resolve the very
    // finding the user was reopening.
    decision: "resolved" | "accepted" | "" = "resolved"
  ) => {
    if (!coveragePlan || !warningId) return;
    try {
      const res = await decideDirectorWarning(sceneId, warningId, decision);
      setCoveragePlan((prev) =>
        prev ? { ...prev, warnings: res.warnings,
                 warning_dispositions: res.warning_dispositions } : prev
      );
    } catch (e: any) {
      setError(e?.message || "Could not record that decision.");
    }
  };

  /** Findings with no recorded decision — the ones that block locking. */
  const unresolvedWarnings = (coveragePlan?.warnings || []).filter(
    (w) => !(w.id && coveragePlan?.warning_dispositions?.[w.id]?.decision)
  );

  if (loading) {
    return (
      <div className="w-full h-96 flex flex-col items-center justify-center text-amber-500 gap-3">
        <Sparkles className="w-8 h-8 animate-spin" />
        <span className="font-mono text-sm font-bold">Querying GET /api/director/scene?beats={sceneId}...</span>
      </div>
    );
  }

  if (error || !coveragePlan) {
    return (
      <div className="w-full max-w-[1200px] mx-auto p-8 my-10 glass-panel rounded-2xl border border-amber-500/40 bg-zinc-950 flex flex-col items-center justify-center text-center gap-4 animate-in fade-in duration-200">
        <div className="p-3 rounded-full bg-amber-500/15 border border-amber-500/30 text-amber-400">
          <AlertTriangle className="w-8 h-8" />
        </div>
        <div>
          <h3 className="text-lg font-bold text-zinc-100 font-mono">
            404 / Unplanned Beat Coverage ({sceneId})
          </h3>
          <p className="text-xs text-zinc-400 font-mono mt-1 max-w-lg leading-relaxed">
            {error || `No coverage plan exists for beat ${sceneId}. A plan must be requested before coverage can be reviewed.`}
          </p>
          {activeProjectTitle && (
            <div className="mt-2 inline-flex items-center gap-1.5 text-[11px] font-mono text-zinc-400 bg-zinc-900 border border-zinc-800 px-3 py-1 rounded-full">
              <span className="w-2 h-2 rounded-full bg-amber-500" />
              <span>Target Project: <strong>{activeProjectTitle}</strong></span>
            </div>
          )}
        </div>
        <button
          onClick={() => handleRedirectScene()}
          disabled={redirecting}
          className="px-5 py-2.5 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-xl transition-all flex items-center gap-2 shadow-lg neon-glow-amber"
        >
          {redirecting ? (
            <Sparkles className="w-4 h-4 animate-spin" />
          ) : (
            <Clapperboard className="w-4 h-4" />
          )}
          <span>POST /api/director/plan ({sceneId})</span>
        </button>
      </div>
    );
  }

  const isLocked = coveragePlan.status === "locked" || coveragePlan.status === "compiled";
  const paidShots = paidShotsOf(coveragePlan);
  const coverageTotalSum = coveragePlan.coverage.reduce((acc, s) => acc + s.camera.duration, 0);
  const targetDuration = coveragePlan.beat_duration || coveragePlan.total_duration;
  const isDurationMatched = Math.abs(coverageTotalSum - targetDuration) <= 0.1;
  const isSnapshotStale = Boolean(
    coveragePlan.live_beat_duration &&
      Math.abs(coveragePlan.live_beat_duration - targetDuration) > 0.1
  );

  return (
    <div className="w-full flex flex-col gap-4 p-4 max-w-[1600px] mx-auto animate-in fade-in duration-300">
      {/* Stale Plan Snapshot Divergence Alert */}
      {isSnapshotStale && (
        <div className="glass-panel p-3.5 rounded-xl border border-amber-500/50 bg-amber-500/10 flex items-center justify-between text-amber-300 font-mono text-xs">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
            <span>
              ⚠️ <strong>Stale Plan Snapshot:</strong> Live narration duration ({coveragePlan.live_beat_duration?.toFixed(1)}s) differs from plan snapshot ({targetDuration.toFixed(1)}s). Re-planning recommended.
            </span>
          </div>
          <button
            onClick={handleRedirectScene}
            className="px-3 py-1 bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-200 rounded text-[11px] font-bold transition-colors"
          >
            Re-plan Scene
          </button>
        </div>
      )}

      {/* SECTION 1: Scene Director Header Bar */}
      <div className="glass-surface p-4 rounded-2xl flex flex-wrap items-center justify-between gap-4 border border-zinc-800">
        <div className="flex items-center gap-3">
          <div className="p-3 rounded-xl bg-amber-500/15 border border-amber-500/30 text-amber-400">
            <Clapperboard className="w-6 h-6" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-bold text-zinc-100">{coveragePlan.scene_title}</h2>
              <span className="text-[10px] font-mono font-bold text-amber-300 bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 rounded capitalize">
                Profile: {coveragePlan.profile.replace("_", " ")}
              </span>
            </div>
            <div className="flex items-center gap-3 text-xs font-mono text-zinc-400 mt-1">
              <span>{targetDuration.toFixed(1)}s Narration</span>
              <span>•</span>
              <span>{coveragePlan.coverage.length} Director Shots</span>
              <span>•</span>
              <span
                className={`font-bold px-1.5 py-0.5 rounded border text-[11px] ${
                  isDurationMatched
                    ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
                    : "bg-amber-500/10 text-amber-400 border-amber-500/30"
                }`}
                title="Coverage total must match narration timing to lock"
              >
                Coverage: {coverageTotalSum.toFixed(1)}s / {targetDuration.toFixed(1)}s {isDurationMatched ? "✓" : "⚠️ Mismatch"}
              </span>
              <span>•</span>
              <span className="text-emerald-400 font-bold">
                Est. Cost: ${coveragePlan.estimated_cost.toFixed(2)}
              </span>
            </div>
          </div>
        </div>

        {/* Lock / Coverage Status Action + Active Project Safety Badge + Back to Storyboard */}
        <div className="flex flex-wrap items-center gap-3">
          {onBackToStoryboard && (
            <button
              onClick={onBackToStoryboard}
              className="px-3.5 py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-amber-400 hover:text-amber-300 text-xs font-mono font-bold rounded-xl transition-all flex items-center gap-2 shadow"
              title="Return to main Storyboard grid"
            >
              <ArrowLeft className="w-4 h-4" />
              <span>STORYBOARD GRID</span>
            </button>
          )}

          {activeProjectTitle && (
            <span className="text-[11px] font-mono text-zinc-400 bg-zinc-900 border border-zinc-800 px-2.5 py-1 rounded-lg flex items-center gap-1.5" title="Active Project Rule §5 Safety Badge">
              <span className="w-2 h-2 rounded-full bg-amber-500" />
              <span>Project: <strong>{activeProjectTitle}</strong></span>
            </span>
          )}

          <div className="flex items-center gap-2">
            <span
              className={`text-xs font-mono font-bold px-3 py-1 rounded-full border capitalize ${
                isLocked
                  ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/40"
                  : "bg-amber-500/15 text-amber-400 border-amber-500/40"
              }`}
            >
              Status: {coveragePlan.status}
            </span>
          </div>

          <button
            onClick={handleToggleLock}
            className={`px-5 py-2.5 rounded-xl font-mono text-xs font-bold transition-all duration-200 flex items-center gap-2 shadow-lg ${
              isLocked
                ? "bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700"
                : "bg-emerald-600 hover:bg-emerald-500 text-zinc-950 neon-glow-emerald"
            }`}
          >
            {isLocked ? (
              <>
                <Unlock className="w-4 h-4 text-amber-400" />
                <span>UNLOCK TO EDIT</span>
              </>
            ) : (
              <>
                <Lock className="w-4 h-4 text-zinc-950" />
                <span>LOCK & GENERATE COVERAGE ({activeProjectTitle || "Active"})</span>
              </>
            )}
          </button>

          {/* The control the locked plan exists for.
              Deliberately NOT disabled while the plan reads "draft". The draft
              check lives on the server, where it cannot be stepped over, and its
              refusal is the sentence that tells the user what to do; hiding the
              button behind a client-side guess would replace that sentence with
              a greyed-out control and no explanation — and would make the
              approval-drift refusal, which is the case where the client believes
              it IS locked, unreachable. */}
          <button
            data-testid="compile-open-gate"
            onClick={() => setCompileGateOpen((open) => !open)}
            disabled={Boolean(compilingBeat)}
            title="Render every shot in this plan and buy the paid video shots"
            className="px-5 py-2.5 rounded-xl font-mono text-xs font-bold transition-all duration-200 flex items-center gap-2 shadow-lg bg-purple-600 hover:bg-purple-500 text-zinc-50 border border-purple-400/40 disabled:opacity-50"
          >
            {compilingBeat ? (
              <Sparkles className="w-4 h-4 animate-spin" />
            ) : (
              <Film className="w-4 h-4" />
            )}
            <span>
              {compilingBeat ? `COMPILING (${compilingBeat})…` : "COMPILE COVERAGE"}
            </span>
          </button>
        </div>
      </div>

      {/* SECTION 1A: The spend gate.
          Gate 1's premise is that money is committed by a deliberate, informed
          human decision, so the price is on screen BEFORE the request is made,
          not in a receipt afterwards. Opening this panel calls nothing. */}
      {compileGateOpen && !compilingBeat && (
        <div
          data-testid="compile-cost-gate"
          className="glass-panel p-4 rounded-xl border border-purple-500/40 bg-purple-500/10 flex flex-col sm:flex-row sm:items-center justify-between gap-3"
        >
          <div className="flex items-start gap-3">
            <DollarSign className="w-5 h-5 text-purple-300 shrink-0 mt-0.5" />
            <div className="text-xs font-mono text-zinc-200 leading-relaxed">
              <strong className="text-purple-200">
                Compiling {(coveragePlan.scene_beats && coveragePlan.scene_beats.length > 0
                  ? coveragePlan.scene_beats
                  : [sceneId]
                ).join(", ")}{" "}
                spends money.
              </strong>
              <br />
              It generates {coveragePlan.coverage.length} shot
              {coveragePlan.coverage.length === 1 ? "" : "s"}, of which{" "}
              <strong className="text-purple-200">{paidShots} paid video shot
              {paidShots === 1 ? "" : "s"}</strong>{" "}
              are bought from a video model. Estimated cost{" "}
              <strong className="text-purple-200">
                ${coveragePlan.estimated_cost.toFixed(2)}
              </strong>
              . Spending is not reversible.
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              data-testid="compile-cancel"
              onClick={() => setCompileGateOpen(false)}
              className="px-3.5 py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs font-mono font-bold rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              data-testid="compile-confirm"
              onClick={handleCompileCoverage}
              className="px-4 py-2 bg-purple-600 hover:bg-purple-500 text-zinc-50 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5 shadow"
            >
              <Film className="w-3.5 h-3.5" />
              <span>COMPILE &amp; SPEND ${coveragePlan.estimated_cost.toFixed(2)}</span>
            </button>
          </div>
        </div>
      )}

      {/* The job, while it runs. Minutes of silence is what made "did that
          button do anything?" the only available reading. */}
      {compilingBeat && (
        <div
          data-testid="compile-running"
          className="glass-panel p-3.5 rounded-xl border border-purple-500/40 bg-purple-500/10 flex flex-col gap-2"
        >
          <div className="flex items-center gap-2 text-xs font-mono text-purple-200">
            <Sparkles className="w-4 h-4 animate-spin shrink-0" />
            <span>
              Compiling {compilingBeat} — rendering every shot and buying the paid ones.
              This screen updates when the server says the plan is compiled.
            </span>
          </div>
          {compileLog && (
            <pre className="max-h-24 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/80 px-3 py-2 text-[11px] font-mono text-zinc-400 whitespace-pre-wrap">
              {compileLog.slice(-600)}
            </pre>
          )}
        </div>
      )}

      {/* The refusal, in the server's own words. */}
      {compileProblem && (
        <div
          data-testid="compile-problem"
          data-kind={compileProblem.kind}
          className={`glass-panel p-3.5 rounded-xl border flex items-start justify-between gap-3 ${
            compileProblem.kind === "busy"
              ? "border-zinc-700 bg-zinc-900/60"
              : compileProblem.kind === "failed"
                ? "border-red-500/50 bg-red-950/40"
                : "border-amber-500/50 bg-amber-500/10"
          }`}
        >
          <div className="flex items-start gap-2.5">
            {compileProblem.kind === "busy" ? (
              <Clock className="w-4 h-4 text-zinc-300 shrink-0 mt-0.5" />
            ) : (
              <AlertTriangle
                className={`w-4 h-4 shrink-0 mt-0.5 ${
                  compileProblem.kind === "failed" ? "text-red-400" : "text-amber-400"
                }`}
              />
            )}
            <span
              className={`text-xs font-mono leading-relaxed ${
                compileProblem.kind === "busy"
                  ? "text-zinc-200"
                  : compileProblem.kind === "failed"
                    ? "text-red-200"
                    : "text-amber-200"
              }`}
            >
              {compileProblem.message}
              {SPENT_NOTHING.includes(compileProblem.kind) && (
                <span className="block mt-1 text-[11px] text-zinc-400">
                  This request did not start a compile, so nothing was generated and
                  nothing was charged for it.
                </span>
              )}
            </span>
          </div>
          <button
            onClick={() => setCompileProblem(null)}
            className="shrink-0 text-zinc-400 hover:text-zinc-200 text-xs font-mono font-bold"
            aria-label="Dismiss"
          >
            &#10005;
          </button>
        </div>
      )}

      {/* Success, written from the status the server sent back — never from the
          fact that a job ended. */}
      {compileDone && (
        <div
          data-testid="compile-done"
          className="glass-panel p-3.5 rounded-xl border border-emerald-500/40 bg-emerald-500/10 flex items-center gap-2.5"
        >
          <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
          <span className="text-xs font-mono text-emerald-200 leading-relaxed">
            {compileDone}
          </span>
        </div>
      )}

      {/* SECTION 1B: Visual Strategy & Blocking (if present) */}
      {coveragePlan.visual_strategy && (
        <div className="glass-panel p-4 rounded-xl border border-amber-500/30 bg-amber-500/5 flex flex-col gap-1.5">
          <div className="flex items-center gap-2 text-xs font-bold font-mono text-amber-400">
            <Sparkles className="w-3.5 h-3.5" />
            <span>DIRECTOR VISUAL STRATEGY</span>
          </div>
          <p className="text-xs text-zinc-200 leading-relaxed font-sans italic">
            "{coveragePlan.visual_strategy}"
          </p>
        </div>
      )}

      {/* SECTION 2: Scene-Level Natural Language Direction Bar */}
      <div className="glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800/80">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-bold text-amber-400">
            <Sparkles className="w-4 h-4 text-amber-400" />
            <span>Scene-Level Director Intent</span>
          </div>
          <span className="text-[11px] font-mono text-zinc-500">
            Redirect scene without editing individual shots
          </span>
        </div>

        {/* Command Input Box */}
        <div className="relative w-full">
          <textarea
            value={redirectInput}
            onChange={(e) => setRedirectInput(e.target.value)}
            disabled={isLocked}
            placeholder="e.g. This feels too dramatic. Show Heney less and spend more time on the workers, weather, and working conditions..."
            rows={2}
            className="w-full bg-zinc-950/80 border border-zinc-800 rounded-xl p-3.5 text-xs text-zinc-100 placeholder-zinc-600 focus:border-amber-500 outline-none resize-none font-sans leading-relaxed disabled:opacity-50"
          />
          <button
            onClick={handleRedirectScene}
            disabled={isLocked || redirecting}
            className="absolute bottom-3 right-3 px-4 py-1.5 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-bold font-mono rounded-lg transition-all flex items-center gap-1.5 shadow-lg disabled:opacity-50"
          >
            {redirecting ? (
              <Sparkles className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Send className="w-3.5 h-3.5" />
            )}
            <span>REDIRECT SCENE</span>
          </button>
        </div>

        {/* The planner is a background job of tens of seconds. Show its own log
            rather than a spinner with nothing behind it. */}
        {redirecting && redirectLog && (
          <pre className="max-h-24 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/80 px-3 py-2 text-[11px] font-mono text-zinc-400 whitespace-pre-wrap">
            {redirectLog.slice(-600)}
          </pre>
        )}

        {/* Quick Shortcuts Pills */}
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] font-mono text-zinc-400 mr-1">Quick Direction:</span>
          {QUICK_SHORTCUTS.map((pill) => {
            const isSelected = selectedShortcuts.includes(pill);
            return (
              <button
                key={pill}
                disabled={isLocked}
                onClick={() => toggleShortcut(pill)}
                className={`text-[11px] font-mono px-2.5 py-1 rounded-full border transition-all ${
                  isSelected
                    ? "bg-amber-500/20 border-amber-500 text-amber-300 font-bold"
                    : "bg-zinc-900/60 border-zinc-800 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
                }`}
              >
                {pill}
              </button>
            );
          })}
        </div>

        {/* High-Level Creative Sliders */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 pt-3 border-t border-zinc-800/60">
          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between text-[11px] font-mono text-zinc-400">
              <span>Documentary</span>
              <span>Cinematic</span>
            </div>
            <input
              type="range"
              min="0"
              max="100"
              disabled={isLocked}
              value={preferences.documentaryVsCinematic}
              onChange={(e) =>
                setPreferences({ ...preferences, documentaryVsCinematic: parseInt(e.target.value) })
              }
              className="w-full accent-amber-500 bg-zinc-800 h-1.5 rounded-lg cursor-pointer"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between text-[11px] font-mono text-zinc-400">
              <span>Restrained</span>
              <span>Dramatic</span>
            </div>
            <input
              type="range"
              min="0"
              max="100"
              disabled={isLocked}
              value={preferences.restrainedVsDramatic}
              onChange={(e) =>
                setPreferences({ ...preferences, restrainedVsDramatic: parseInt(e.target.value) })
              }
              className="w-full accent-amber-500 bg-zinc-800 h-1.5 rounded-lg cursor-pointer"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between text-[11px] font-mono text-zinc-400">
              <span>Economical</span>
              <span>Max Quality</span>
            </div>
            <input
              type="range"
              min="0"
              max="100"
              disabled={isLocked}
              value={preferences.economicalVsQuality}
              onChange={(e) =>
                setPreferences({ ...preferences, economicalVsQuality: parseInt(e.target.value) })
              }
              className="w-full accent-amber-500 bg-zinc-800 h-1.5 rounded-lg cursor-pointer"
            />
          </div>
        </div>
      </div>

      {/* SECTION 3: Problem Queue Alert Banner */}
      {shotError && (
        <div className="mb-3 flex items-start justify-between gap-3 rounded-lg border border-red-500/50 bg-red-950/40 px-3 py-2">
          <span className="text-xs font-mono text-red-300">
            <strong className="font-bold">Edit rejected:</strong> {shotError}
          </span>
          <button
            onClick={() => setShotError(null)}
            className="shrink-0 text-red-400 hover:text-red-200 text-xs font-mono font-bold"
            aria-label="Dismiss"
          >
            &#10005;
          </button>
        </div>
      )}
      {unresolvedWarnings.length > 0 && (
        <div className="glass-panel p-3.5 rounded-xl border border-amber-500/40 bg-amber-500/10 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0" />
            <span data-testid="unresolved-count" className="text-xs font-bold text-amber-300 font-mono">
              ⚠️ {unresolvedWarnings.length} coverage issue{unresolvedWarnings.length === 1 ? "" : "s"} awaiting a decision in this scene
              {unresolvedWarnings.some((w) => w.stale) && (
                <span className="ml-2 text-[10px] text-amber-400 font-normal bg-amber-500/20 px-1.5 py-0.5 rounded">
                  (Edits made — click Re-check)
                </span>
              )}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              data-testid="recheck-warnings"
              onClick={handleRecheckCritique}
              disabled={critiquing}
              className="px-3 py-1 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-amber-300 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-50"
              title="Re-run LLM critic over revised plan without re-planning"
            >
              {critiquing ? (
                <Sparkles className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RotateCcw className="w-3.5 h-3.5" />
              )}
              <span>Re-check Warnings</span>
            </button>
            <button
              onClick={() => setProblemDrawerOpen(true)}
              className="px-3 py-1 bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-300 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1"
            >
              <span>Review Problems</span>
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      )}

      {/* SECTION 3B: What the last re-check actually produced.
          Deliberately OUTSIDE the banner above, which unmounts the moment the
          findings are all decided — which is exactly when the human asks the
          critic to look again. Rendered there, "the critic ran and raised
          nothing" and "the re-check failed" would both have nowhere to appear. */}
      {(critiqueOutcome || unresolvedWarnings.length === 0) && (
        <div
          data-testid="critique-outcome"
          data-kind={critiqueOutcome?.kind || "idle"}
          className={`glass-panel p-3 rounded-xl border flex flex-wrap items-center justify-between gap-3 ${
            critiqueOutcome?.kind === "failed"
              ? "border-red-500/50 bg-red-950/40"
              : critiqueOutcome?.kind === "clear"
                ? "border-emerald-500/40 bg-emerald-500/10"
                : "border-zinc-800 bg-zinc-950/60"
          }`}
        >
          <div className="flex items-start gap-2.5">
            {critiqueOutcome?.kind === "failed" ? (
              <AlertTriangle className="w-4 h-4 text-red-400 shrink-0 mt-0.5" />
            ) : critiqueOutcome?.kind === "clear" ? (
              <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
            ) : (
              <RotateCcw className="w-4 h-4 text-zinc-400 shrink-0 mt-0.5" />
            )}
            <span
              className={`text-xs font-mono leading-relaxed ${
                critiqueOutcome?.kind === "failed"
                  ? "text-red-200"
                  : critiqueOutcome?.kind === "clear"
                    ? "text-emerald-200"
                    : "text-zinc-300"
              }`}
            >
              {critiqueOutcome?.kind === "failed"
                ? critiqueOutcome.message
                : critiqueOutcome?.kind === "clear"
                  ? "The critic re-checked this scene and raised nothing. That is a " +
                    "result, not a silence — it ran, and the scene came back clean."
                  : critiqueOutcome?.kind === "found"
                    ? `The critic re-checked this scene and raised ${critiqueOutcome.count} ` +
                      `finding${critiqueOutcome.count === 1 ? "" : "s"}, listed above.`
                    : "No finding in this scene is awaiting a decision. Re-check to ask " +
                      "the critic to look again before locking."}
            </span>
          </div>
          {/* The same re-check, reachable once every finding has been decided —
              which is precisely when the human wants to run it again. */}
          {unresolvedWarnings.length === 0 && (
            <button
              data-testid="recheck-warnings-idle"
              onClick={handleRecheckCritique}
              disabled={critiquing}
              className="px-3 py-1 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-amber-300 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5 disabled:opacity-50 shrink-0"
              title="Re-run the LLM critic over this scene without re-planning"
            >
              {critiquing ? (
                <Sparkles className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RotateCcw className="w-3.5 h-3.5" />
              )}
              <span>Re-check Warnings</span>
            </button>
          )}
        </div>
      )}

      {/* SECTION 4: Dual Review Surfaces (Plan Review vs Take Review) */}
      <div className="flex flex-col gap-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between text-xs font-mono font-bold text-zinc-400 gap-2 px-1">
          <div className="flex items-center gap-2">
            <span className="text-zinc-200">COVERAGE REVIEW SURFACE</span>
            <span className="text-[10px] text-zinc-500">({coveragePlan.coverage.length} Shots)</span>
          </div>

          {/* Mode Switcher Buttons Grouped by Phase */}
          <div className="flex flex-wrap items-center bg-zinc-950 p-1 rounded-lg border border-zinc-800 gap-1">
            <span className="text-[10px] font-mono text-zinc-500 px-1.5 uppercase font-bold">Plan Review (Pre-Gen):</span>
            <button
              onClick={() => setReviewMode("rhythm")}
              className={`px-2.5 py-1 rounded transition flex items-center gap-1.5 text-xs font-mono font-bold ${
                reviewMode === "rhythm"
                  ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              <List className="w-3.5 h-3.5" />
              <span>Rhythm & Beat Sheet</span>
            </button>
            <button
              onClick={() => setReviewMode("grid")}
              className={`px-2.5 py-1 rounded transition flex items-center gap-1.5 text-xs font-mono font-bold ${
                reviewMode === "grid"
                  ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              <LayoutGrid className="w-3.5 h-3.5" />
              <span>Storyboard Cards</span>
            </button>

            <span className="text-[10px] font-mono text-zinc-500 px-1.5 uppercase font-bold border-l border-zinc-800 pl-2">Take Review (Post-Compile):</span>
            <button
              onClick={() => setReviewMode("scrubber")}
              className={`px-2.5 py-1 rounded transition flex items-center gap-1.5 text-xs font-mono font-bold ${
                reviewMode === "scrubber"
                  ? "bg-purple-500/20 text-purple-300 border border-purple-500/40"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              <Play className="w-3.5 h-3.5 text-purple-400" />
              <span>Cinema Scrubber</span>
            </button>
            <button
              onClick={() => setReviewMode("matrix")}
              className={`px-2.5 py-1 rounded transition flex items-center gap-1.5 text-xs font-mono font-bold ${
                reviewMode === "matrix"
                  ? "bg-purple-500/20 text-purple-300 border border-purple-500/40"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
            >
              <Grid className="w-3.5 h-3.5 text-purple-400" />
              <span>Montage Matrix</span>
            </button>
          </div>
        </div>

        {/* View Mode Rendering */}
        {reviewMode === "rhythm" ? (
          <CoverageRhythmBeatSheet
            shots={coveragePlan.coverage}
            onSelectShot={(s) => setSelectedShot(s)}
          />
        ) : reviewMode === "scrubber" ? (
          <CinemaScrubberPlayer
            shots={coveragePlan.coverage}
            onSelectShot={(s) => setSelectedShot(s)}
            onQuickAction={(action, shot) => handleShotAction(action, shot)}
            mediaUrl={mediaUrl}
          />
        ) : reviewMode === "matrix" ? (
          <CompactMontageMatrix
            scenes={[sceneSummaryOf(coveragePlan, sceneId)]}
            activeSceneId={sceneId}
            allShots={coveragePlan.coverage}
            onSelectScene={() => {}}
            onSelectShot={(s) => setSelectedShot(s)}
            mediaUrl={mediaUrl}
          />
        ) : (
          /* Storyboard Rail Grid */
          <div className="flex items-stretch gap-3 overflow-x-auto pb-3 pt-1 scrollbar-thin">
            {coveragePlan.coverage.map((shot) => (
              <DirectorShotCard
                key={shot.id}
                shot={shot}
                warnings={coveragePlan.warnings.filter((w) => w.shot_id === shot.id || w.shot_id === shot.shot_number)}
                isSelected={selectedShot?.id === shot.id}
                onSelectShot={(s) => setSelectedShot(s)}
                mediaUrl={mediaUrl}
              />
            ))}
          </div>
        )}
      </div>

      {/* Slide-over Inspector Drawer */}
      {selectedShot && (
        <ShotInspectorDrawer
          shot={selectedShot}
          onClose={() => setSelectedShot(null)}
          onUpdateShot={handleUpdateShot}
          onOpenTakeSelector={(s) => setTakeModalShot(s)}
          onAction={handleShotAction}
          mediaUrl={mediaUrl}
        />
      )}

      {/* Problem Queue Drawer */}
      <ProblemQueueDrawer
        warnings={coveragePlan.warnings}
        shots={coveragePlan.coverage}
        isOpen={problemDrawerOpen}
        onClose={() => setProblemDrawerOpen(false)}
        onSelectShot={(shotId) => {
          const s = coveragePlan.coverage.find((c) => c.id === shotId);
          if (s) setSelectedShot(s);
        }}
        onResolveWarning={handleResolveWarning}
        dispositions={coveragePlan.warning_dispositions || {}}
      />

      {/* Take Selector Modal */}
      <TakeSelectorModal
        shot={takeModalShot}
        isOpen={!!takeModalShot}
        onClose={() => setTakeModalShot(null)}
        onSelectTake={handleSelectTake}
        mediaUrl={mediaUrl}
      />
    </div>
  );
}
