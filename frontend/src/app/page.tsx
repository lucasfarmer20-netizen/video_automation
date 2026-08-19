"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  Folder,
  Sliders,
  MessageSquare,
  LayoutGrid,
  GitBranch,
  Volume2,
  Clock,
  RotateCcw,
  Menu,
  X,
  Clapperboard,
  ArrowLeft,
} from "lucide-react";

// Components
import ProjectSidebar from "../components/ProjectSidebar";
import KnobsSidebar from "../components/KnobsSidebar";
import MotionPanel from "../components/MotionPanel";
import type { MixConfig } from "../components/MixPanel";
import AssemblyPanel from "../components/AssemblyPanel";
import StageHeader, { StageId, Stage } from "../components/StageHeader";
import JobBanners from "../components/JobBanners";
import VesperChat from "../components/VesperChat";
import BeatCard from "../components/BeatCard";
import FlowCanvas from "../components/FlowCanvas";
import MultitrackTimeline from "../components/MultitrackTimeline";
import Lightbox, { LightboxState } from "../components/Lightbox";
import RoughCutPanel from "../components/RoughCutPanel";
import GradePanel, { Grade } from "../components/GradePanel";
import MetadataPanel, { Metadata } from "../components/MetadataPanel";
import VoiceStudioModal from "../components/VoiceStudioModal";
import FilmOverviewPanel from "../components/FilmOverviewPanel";
import DirectorWorkspace from "../components/DirectorWorkspace";
import LockedCoverageModal from "../components/LockedCoverageModal";
import CoverageSurveyPanel from "../components/CoverageSurveyPanel";
import { setActiveProjectId, fetchBeatCoverageStates } from "../lib/directorApi";
import type { BeatCoverageState } from "../lib/directorApi";
import { filmCoverageView, defaultSelectedBeat } from "../lib/filmCoverage";
import { NO_SLOT_VIEW, viewForProject } from "../lib/slots";
import { stageFromHash, hashForStage, hashAlreadyNames } from "../lib/stageRoute";
import type { SlotTakes, SlotView, TimelineSlot } from "../lib/slots";

// Setup API URL mapping
const API_BASE = typeof window !== "undefined"
  ? (window.location.hostname === "localhost" ? "http://localhost:5000" : "")
  : "";

interface Project {
  name: string;
  rel: string;
  /** What the studio sends as `X-Project-Id` to target this project explicitly.
   *  `/api/projects` fills it for every entry, and it is required here so that
   *  losing it on the way to `handleSelectProject` cannot compile. */
  project_id: string;
  rel_display: string;
  active: boolean;
  channel: string;
  beats_count?: number;
  script_locked?: boolean;
  storyboard_approved?: boolean;
}

interface Shot {
  scene_id: string;
  narration: string;
  prompt: string;
  style_medium: string;
  motion_prompt: string;
  chosen_variation: number | null;
  chosen_video_variation?: number | null;
  motion_type: string;
  camera: { move: string; duration: number; speed: number };
  fx: string[];
  sfx: string;
  references: string[];
  video_model: string | null;
  video_audio: boolean | null;
  flow_hero: boolean;
  hero_clip: boolean;
  draft_variations: string[];
  draft_image: string | null;
  video_variations: string[];
  video_clip: string | null;
  approved: boolean;
  notes: string;
}

interface Job {
  status: string;
  log: string;
}

/**
 * The storage gate's own answer, or null.
 *
 * Keyed on the STATED cause in the body, and on nothing else. Inferring "the
 * store is down" from a status, or from the absence of a project, is precisely
 * the collapse the backend fix removes — two different causes rendered as one
 * answer — rebuilt on the client. A 503 on its own is not evidence: a proxy, a
 * cold start or the platform emits one while saying nothing about the durable
 * store, and equally a gateway that rewrites 503 to 502 must not be able to
 * silently switch this screen off. The server says which it is; this reads it.
 *
 * The message falls back rather than assuming `error` is present, so a reply
 * that names the gate but carries no sentence still produces a stated block
 * instead of throwing inside the caller's try and reaching no screen at all.
 */
type StorageGateReply = { detail?: { storage_gate?: string; error?: string } };

const storageGateBlock = (data: StorageGateReply | null | undefined): string | null => {
  if (data?.detail?.storage_gate !== "unavailable") return null;
  return String(data?.detail?.error || "The durable store could not be reached.");
};

export default function WorkspacePage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState<any | null>(null);
  const [activeChannel, setActiveChannel] = useState<"bestiary" | "calluses">("bestiary");
  
  // View states
  const [activeView, setActiveView] = useState<"grid" | "canvas">("grid");
  // Empty, not "s004". That literal was MOCK_SCENES' scene_id, so the Director
  // opened by asking for a plan for a beat that does not exist in the user's
  // film and correctly answered "No coverage plan found" -- the default was
  // chosen to match the fixture rather than the film. It is filled in from the
  // loaded project below; "" renders as "no beat selected", never as a guess.
  const [selectedSceneId, setSelectedSceneId] = useState<string>("");
  const [sceneCoverage, setSceneCoverage] = useState<Record<string, BeatCoverageState>>({});
  const [sceneCoverageError, setSceneCoverageError] = useState<string | null>(null);

  const [lockedModalBeat, setLockedModalBeat] = useState<string | null>(null);
  // Which FilmCraft stage is showing. The spine is SCRIPT -> DIRECT -> GENERATE
  // -> ROUGH CUT -> REFINE -> EXPORT; panel content still lives where it always
  // did, remapped onto the stage that owns it (contract §2.2). Status, blocking
  // and the primary CTA all come from the server -- see backend/stages.py.
  const [activeStage, setActiveStage] = useState<StageId>("script");
  const [stages, setStages] = useState<Stage[]>([]);
  // Which project every request targets, and the yardstick for discarding
  // replies that arrive after a switch (contract §11.3). Held in a ref because
  // the staleness check runs when a response lands, not when a render closed
  // over it -- a state variable would compare against the value that was
  // current when the request was *sent*, which is precisely the wrong one.
  const projectIdRef = useRef<string>("");
  const [rightPanel, setRightPanel] = useState<"vesper" | "knobs">("vesper");
  
  // Background task state. `dismissedErrors` is tracked separately because
  // pollJobs replaces `jobs` wholesale every 3s from the server, which would
  // otherwise resurrect a banner the user just dismissed.
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  // Last known script_draft status, readable from the polling interval (which
  // closes over the initial `jobs` value and so cannot see current state).
  const scriptDraftStatus = useRef<string | undefined>(undefined);
  // Consecutive polls in which a running script_draft went missing.
  const draftMisses = useRef(0);
  /** The Director workspace, so a finished plan can bring the user to it. */
  const directorWorkspaceRef = useRef<HTMLDivElement | null>(null);
  /**
   * Whether the URL has been read yet.
   *
   * The write-back effect must not run before the read effect, or the first
   * commit would replace `#direct` with `#script` — the initial state value —
   * and the reload would land on Script anyway, which is the bug.
   */
  const stageReadFromUrl = useRef(false);
  const [dismissedErrors, setDismissedErrors] = useState<Record<string, string>>({});
  const [chatHistory, setChatHistory] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [peaks, setPeaks] = useState<Record<string, any>>({});
  // The cut, as slots (§7.1), stamped with the project it was read for.
  // `slots: null` means "not read yet", which is not the same claim as "there
  // are no clips" — see MultitrackTimeline's V1 track. The stamp matters
  // because slot ids are `beat_id::shot_id`, unique within a film and not
  // across them: held unstamped, film A's takes are served for film B's
  // identically-named slot until the new read lands. `viewForProject` is what
  // makes that impossible; see lib/slots.ts.
  const [slotView, setSlotView] = useState<SlotView>(NO_SLOT_VIEW);
  // The durable store said it could not answer (HTTP 503, `storage_gate:
  // "unavailable"` — see storage_gate_unavailable in backend/main.py). Held
  // separately from `activeProject` because "there is no project" and "I could
  // not reach the store" are different facts and must not render as the same
  // screen: the no-project screen offers to INITIALISE A FRESH WORKSPACE, which
  // over a project that is merely unreachable is the worst available action.
  const [storageBlock, setStorageBlock] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<LightboxState | null>(null);
  const [voiceStudioOpen, setVoiceStudioOpen] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [mobileRightPanelOpen, setMobileRightPanelOpen] = useState(false);

  // Helper: media url resolver.
  // The backend returns paths relative to a media root (e.g. "assets/s001/x.png",
  // "render/<slug>/s001.mp4"), never route-prefixed — so /media/ is added exactly
  // once here. Anything already carrying it is passed through rather than
  // double-prefixed into /media/media/... which 404s.
  const mediaUrl = useCallback((path: string) => {
    if (!path) return "";
    if (path.startsWith("http://") || path.startsWith("https://")) return path;
    const clean = path.replace(/\\/g, "/").replace(/^\/+/, "");
    if (clean.startsWith("media/")) return `${API_BASE}/${clean}`;
    return `${API_BASE}/media/${clean}`;
  }, []);

  // Fetch projects list
  const fetchProjects = async (ch?: string) => {
    try {
      const url = ch ? `${API_BASE}/api/projects?channel=${ch}` : `${API_BASE}/api/projects`;
      const res = await fetch(url);
      const data = await res.json();
      if (data.ok) {
        setProjects(data.projects);
      }
    } catch (e) {
      console.error("Failed to load projects", e);
    }
  };

  // Fetch active project data
  // Routes a stage's primary CTA. The labels and action strings are the
  // server's (contract §2.1); this only decides which existing call each one
  // maps to, so there is never a second, competing primary flow.
  const handleStagePrimary = async (action: string) => {
    if (action === "approve:coverage") { setActiveStage("direct"); return; }
    // Through startPaidRender, not a bare post: this action builds the rough
    // cut, and the rough cut buys Tier-C video.
    if (action === "build:draft1") { await startPaidRender("/api/assemble/rough_cut"); return; }
    if (action === "export:master") { await post("/api/assemble/timeline"); return; }
  };

  /**
   * Load the active project and install it.
   *
   * Returns whether the authoritative load actually **installed** this
   * project's identity and data — not whether the request was made. Callers
   * that switch project gate on this, because "the server accepted the switch"
   * and "the studio can safely display it" are different conditions, and using
   * the first to answer the second renders one film while addressing another.
   *
   * This does not touch the loading screen. Callers that raise it own lowering
   * it — see `whileLoading` — because a loader that sometimes lowers a screen it
   * never raised is precisely how one caller came to depend on a side effect
   * that later changed underneath it.
   */
  const fetchActiveProject = async (): Promise<boolean> => {
    let installed = false;
    try {
      const res = await fetch(`${API_BASE}/api/project/active`, { headers: authHeaders() });
      if (isStaleReply(res)) return false;
      const data = await res.json();
      // Checked before `data.ok`, and before anything is installed. The server
      // refuses rather than answering with local state it cannot vouch for, so
      // there is nothing to render here — what there is to do is SAY SO. Read
      // from the stated cause, never inferred from the absence of a project.
      const block = storageGateBlock(data);
      setStorageBlock(block);
      if (block) return false;
      if (data.ok) {
        // Adopt the server's id for this project; every later request names it
        // explicitly instead of relying on the shared active-project pointer.
        if (data.project_id) {
          projectIdRef.current = data.project_id;
          setActiveProjectId(data.project_id);  // director calls target it too
        }
        setActiveProject(data);
        setActiveChannel(data.project.channel);
        // Identity and data are both in place from here; everything below is
        // best-effort detail that a switch does not need to have succeeded.
        installed = true;
        if (data.project?.shots && data.project.shots.length > 0) {
          const firstBeatId = data.project.shots[0].scene_id;
          if (firstBeatId) {
            setSelectedSceneId((prev) => (prev.startsWith("scene_") ? firstBeatId : prev));
          }
        }
      }
      // The stage spine. Fetched every time project state changes, because
      // status/blocking are derived from that state server-side and a stale
      // spine would let the UI offer an action the server will refuse.
      try {
        const sd = await getJson("/api/stages");
        if (sd?.ok) setStages(sd.stages || []);
      } catch { /* leave the spine as-is rather than inventing one */ }
      // Metadata is a sidecar file, not part of the manifest, so it is fetched
      // separately and may simply not exist yet.
      try {
        const md = await getJson("/api/metadata");
        // getJson returns null for a stale reply; keep what is on screen rather
        // than clearing this film's metadata because another film answered.
        if (md) setMetadata(md.ok ? md.metadata : null);
      } catch { setMetadata(null); }
      // Waveform envelopes for the timeline. Cached server-side per clip, so
      // this is cheap after the first call.
      try {
        const pj = await getJson("/api/audio/peaks");
        if (pj) setPeaks(pj.ok ? (pj.peaks || {}) : {});
      } catch { setPeaks({}); }
      // The cut. Rebuilt from the plan and folded onto the saved edit server-
      // side, so this is the only thing the timeline's V1 track is drawn from.
      await fetchSlots();
    } catch (e) {
      console.error("Failed to load active project details", e);
    }
    return installed;
  };

  /**
   * Run something that replaces the workspace with the loading screen.
   *
   * Raising the screen and lowering it are ONE responsibility, and it lives
   * here. Splitting them is what broke `handleCreateProject`: it raised the
   * screen and relied on `fetchActiveProject` to lower it, which silently
   * stopped being true when that lowering became conditional, and a create
   * whose follow-up load failed sat on the spinner until someone reloaded.
   *
   * `fetchActiveProject` no longer touches `loading` at all — it loads and says
   * whether it installed, which is all it should ever have done. The screen
   * comes down here on every exit: success, failure, or throw. A caller that
   * wants it to stay up after success does not exist, because success means the
   * film is installed and ready to show.
   */
  const whileLoading = async <T,>(run: () => Promise<T>): Promise<T> => {
    setLoading(true);
    try {
      return await run();
    } finally {
      setLoading(false);
    }
  };

  /**
   * Re-read the cut from `GET /api/timeline/slots`.
   *
   * Called after anything that can change what is in a slot. The client never
   * writes slot state of its own: a take swap is a write to the server followed
   * by this read, which is what keeps the UI from claiming media the server has
   * not reported (§11.4) — and why the slot keeps its id, index and trims, since
   * the server reconciles them rather than the client rebuilding them.
   */
  const fetchSlots = async () => {
    // Whose cut this read is for, captured before the request goes out so the
    // reply is stamped with the project that asked, not the one on screen when
    // it lands.
    const forProject = projectIdRef.current;
    try {
      const res = await fetch(`${API_BASE}/api/timeline/slots`, { headers: authHeaders() });
      // Another film answered a request we issued before switching. Keep what is
      // on screen rather than repainting this cut with someone else's.
      if (isStaleReply(res)) return;
      const data = await res.json().catch(() => null);
      if (res.ok && data?.ok) {
        setSlotView((prev) => ({
          projectId: forProject,
          slots: data.slots || [],
          coverage: data.coverage || null,
          error: null,
          // Takes already read for this project survive the refresh; a read for
          // a different one starts from nothing rather than inheriting them.
          takes: prev.projectId === forProject ? prev.takes : {},
        }));
        return;
      }
      // Say the cut could not be read. An empty list here would read as "this
      // film has no clips", which is a different and false statement.
      setSlotView({
        projectId: forProject, slots: null, coverage: null,
        error: data?.error || `server error (${res.status})`, takes: {},
      });
    } catch (e) {
      setSlotView({
        projectId: forProject, slots: null, coverage: null,
        error: e instanceof Error ? e.message : "the cut could not be read", takes: {},
      });
    }
  };

  /** As much of a DirectorShot as the takes strip needs. */
  interface DirectorShotTakes {
    id: string;
    draft_variations?: string[];
    chosen_variation?: number | null;
  }

  /** Load the takes for a coverage slot's DirectorShot, on selection (§7.2). */
  const handleSelectSlot = async (slot: TimelineSlot | null) => {
    if (!slot?.shot_id) return;
    const forProject = projectIdRef.current;
    try {
      const data = await getJson(`/api/director/plan/${slot.beat_id}`);
      const coverage: DirectorShotTakes[] | undefined = data?.plan?.coverage;
      if (!Array.isArray(coverage)) return;
      setSlotView((prev) => {
        // The plan we asked for belongs to the project we asked from. If the
        // studio has moved on, these takes are not this film's and are dropped.
        if (prev.projectId !== forProject) return prev;
        const takes = { ...prev.takes };
        coverage.forEach((ds) => {
          takes[`${slot.beat_id}::${ds.id}`] = {
            variations: ds.draft_variations || [],
            chosen: typeof ds.chosen_variation === "number" ? ds.chosen_variation : null,
          };
        });
        return { ...prev, takes };
      });
    } catch { /* the takes strip says "no takes" rather than inventing any */ }
  };

  /**
   * Select a different take for a slot's shot.
   *
   * Writes through the endpoint that owns take selection — the client does not
   * touch the slot — then re-reads the cut. Whatever media the server then
   * reports for that slot is what is drawn, in the slot that was already there.
   */
  const handleSelectTake = async (slot: TimelineSlot, index: number) => {
    const url = slot.shot_id
      ? `/api/director/shot/${slot.shot_id}`
      : `/api/shot/${slot.beat_id}`;
    const res = await post(url, { chosen_variation: index });
    if (!res.ok) {
      alert("Take not selected: " + (res.error || "unknown error"));
      return;
    }
    await handleSelectSlot(slot);
    await fetchSlots();
    if (!slot.shot_id) fetchActiveProject();
  };

  /**
   * §6.2's primary action.
   *
   * Deliberately not `handleAssemble`, whose alert says the process started and
   * stops there. `/api/assemble/rough_cut` runs to the storyboard gate and halts
   * if Gate 1 is uncleared — true and invisible, since only the job log says so.
   * The button stays available either way; what changes is that the reply is
   * reported for what it is.
   */
  /**
   * Start a whole-episode render, after quoting the paid video it will buy.
   *
   * The batch render is the largest paid action in the studio and it was the one
   * with no number anywhere: the button said "render", the server called fal
   * once per Tier-C beat, and nobody was ever shown a total. A run that buys no
   * paid video needs no confirmation and gets none — the server still refuses
   * any beat that turns out to buy, so "quoted as free" cannot become a blank
   * cheque.
   */
  const startPaidRender = async (path: string) => {
    const quote = await getJson("/api/render/quote");
    if (!quote?.ok) {
      alert("Could not price this render, so nothing was started. Try again.");
      return null;
    }
    let url = path;
    if (quote.paid_beats > 0) {
      const lines = (quote.beats || []).map((b: any) =>
        `  ${b.scene_id}: ${b.generate_seconds}s on ${b.video_model}` +
        `${b.generate_audio ? " with audio" : ""} — $${b.estimated_cost.toFixed(2)}`
      ).join("\n");
      if (!confirm(
        `🎬 PAID: this render buys video for ${quote.paid_beats} beat(s) on ` +
        `fal.ai.\n\n${lines}\n\nTOTAL $${quote.estimated_cost.toFixed(2)}\n\n` +
        `Beats that already have a paid clip are kept, not re-bought.\n\nContinue?`
      )) return null;
      url = `${path}?accepted_cost=${quote.estimated_cost}`;
    }
    const r = await post(url);
    if (r?.cost_unconfirmed) {
      alert("Nothing was started and nothing was charged: " +
            (r.error || "the price changed. Try again to see the new one."));
      return null;
    }
    return r;
  };

  const handleBuildDraft1 = async () => {
    const r = await startPaidRender("/api/assemble/rough_cut");
    if (!r) return;
    if (!r.ok) {
      alert("Draft 1 not started: " + (r.error || "unknown error"));
      return;
    }
    alert(activeProject?.project?.storyboard_approved
      ? "Building Draft 1 in the background. Placeholders hold their slots; "
        + "watch the job banner for progress."
      : "Started — but the storyboard gate is not cleared, so this run will "
        + "stop there rather than produce a draft. Approve the storyboard to "
        + "let it through.");
  };

  /** Trim one slot. The trim belongs to the slot, so it outlives its media. */
  const handleTrimSlot = async (
    slot: TimelineSlot,
    trim: { trim_in: number; trim_out: number },
  ) => {
    const res = await post(`/api/timeline/slot/${encodeURIComponent(slot.id)}/trim`, trim);
    if (!res.ok) alert("Trim rejected: " + (res.error || "unknown error"));
    await fetchSlots();
  };

  // Poll assembly job status
  const pollJobs = async () => {
    try {
      // Identity-scoped: the server filters the registry to this project and
      // stamps the reply, and getJson drops it if we have since switched. Both
      // halves matter -- an unscoped poll installed another film's job banners
      // and let its completion read as this film's.
      const data = await getJson("/api/assemble/status");
      if (!data?.ok) return;

      const serverJobs = data.jobs || {};
      const prevStatus = scriptDraftStatus.current;

      // The job registry lives in the server process's memory, so a Cloud Run
      // cold start wipes it. Previously the running job just disappeared from
      // the UI with no error, which is indistinguishable from "the script I
      // generated never saved" — say so instead of silently dropping it.
      // A running draft vanishing from the registry usually means the server
      // restarted and lost it. But a single failed or slow poll looks identical,
      // and calling it dead on the first miss produced an error popup for a job
      // that was still running happily. Require several consecutive misses.
      if (prevStatus === "running" && !serverJobs.script_draft) draftMisses.current += 1;
      else draftMisses.current = 0;

      const nextJobs =
        draftMisses.current >= 4
          ? {
              ...serverJobs,
              script_draft: {
                status: "error",
                log:
                  "The server restarted while drafting, so this draft was lost " +
                  "before it could be saved. Re-run it."
              }
            }
          : serverJobs;

      scriptDraftStatus.current = nextJobs.script_draft?.status;
      setJobs(nextJobs);

      // Side effects stay outside the state updater — React may run an updater
      // more than once, which would fire these twice.
      if (prevStatus === "running") {
        if (nextJobs.script_draft?.status === "done") {
          fetchActiveProject();
          fetchProjects();
        }
        // No alert on failure: JobBanners already shows the error with its full
        // traceback, and a modal that says "look at the banner" is worse than the
        // banner -- it blocks the page and, when it misfired, appeared with no
        // corresponding error to look at.
      }
    } catch (e) {
      console.error("Failed to poll background jobs", e);
    }
  };

  /**
   * What the server actually holds for THIS film's beats.
   *
   * The film coverage overview used to be handed `MOCK_SCENES`, a test fixture,
   * so it quoted a cost of $3.82 for an 11-shot scene on a film the user was not
   * working on. Everything it shows now comes from here, and a beat the server
   * does not report is a beat the panel does not draw.
   */
  useEffect(() => {
    const beats = (activeProject?.project?.shots || []) as Shot[];
    if (beats.length === 0) {
      setSceneCoverage({});
      setSceneCoverageError(null);
      return;
    }
    let live = true;
    fetchBeatCoverageStates(beats.map((b) => b.scene_id))
      .then((states) => {
        if (!live) return;
        setSceneCoverage(states);
        setSceneCoverageError(null);
      })
      .catch((e) => {
        if (!live) return;
        // Say the read failed. An empty overview would claim this film has no
        // coverage, which is a different and possibly false statement.
        setSceneCoverage({});
        setSceneCoverageError(e instanceof Error ? e.message : "coverage could not be read");
      });
    return () => { live = false; };
  }, [activeProject]);

  /**
   * The stage lives in the URL, so a reload comes back to it.
   *
   * Read on mount rather than in the `useState` initialiser: this is a client
   * component that Next still prerenders, and reading `window` in the
   * initialiser is a hydration mismatch. One frame on Script is the cost.
   *
   * `hashchange` is here because Back and Forward are the two ways a user moves
   * between stages without touching the nav, and a URL that changed while the
   * screen did not would be worse than no routing at all.
   */
  useEffect(() => {
    const applyHash = () => {
      const fromUrl = stageFromHash(window.location.hash);
      if (fromUrl) setActiveStage(fromUrl);
      stageReadFromUrl.current = true;
    };
    applyHash();
    window.addEventListener("hashchange", applyHash);
    return () => window.removeEventListener("hashchange", applyHash);
  }, []);

  useEffect(() => {
    if (!stageReadFromUrl.current) return;
    // replaceState, not pushState: moving between stages is not a page
    // navigation, and one history entry per stage click would turn Back into
    // "undo one tab press" rather than "leave the studio".
    if (hashAlreadyNames(window.location.hash, activeStage)) return;
    window.history.replaceState(null, "", hashForStage(activeStage));
  }, [activeStage]);

  useEffect(() => {
    fetchProjects();
    // The first render raises the screen via useState(true), so this owns
    // lowering it — on BOTH outcomes, or a failed first fetch sits on the
    // spinner instead of reaching the "no active project" recovery below.
    // (Not whileLoading: the screen is already up before this effect runs.)
    fetchActiveProject().finally(() => setLoading(false));
    pollJobs();

    // Setup polling intervals
    const jobInterval = setInterval(pollJobs, 3000);
    return () => clearInterval(jobInterval);
  }, []);

  // Studio key. Only needed when the backend has STUDIO_API_KEY set (which
  // gates every mutating request, since Cloud Run runs --allow-unauthenticated).
  // Unset backend => header is absent and ignored, so this costs nothing locally.
  const studioKey = () =>
    (typeof window !== "undefined" && window.localStorage.getItem("studioKey")) || "";

  const authHeaders = (base: Record<string, string> = {}) => {
    const key = studioKey();
    const withProject = projectIdRef.current
      ? { ...base, "X-Project-Id": projectIdRef.current }
      : base;
    return key ? { ...withProject, "X-Studio-Key": key } : withProject;
  };

  /**
   * True when a reply is about a project we are no longer looking at.
   *
   * The server stamps every response with X-Project-Id. Without this check a
   * slow request issued against the previous project can land after a switch
   * and repaint the studio with the wrong film's state — the read-side twin of
   * the write-side bug that sent background renders into the wrong directory.
   */
  const isStaleReply = (res: Response) => {
    const said = res.headers.get("X-Project-Id");
    return Boolean(said && projectIdRef.current && said !== projectIdRef.current);
  };

  /** GET that carries project identity and drops stale replies. */
  const getJson = async (url: string) => {
    const res = await fetch(`${API_BASE}${url}`, { headers: authHeaders() });
    if (!res.ok || isStaleReply(res)) return null;
    return await res.json();
  };

  const promptForStudioKey = () => {
    if (typeof window === "undefined") return false;
    const entered = window.prompt(
      "This studio requires an access key (STUDIO_API_KEY). Enter it to continue:"
    );
    if (!entered?.trim()) return false;
    window.localStorage.setItem("studioKey", entered.trim());
    return true;
  };

  const parseError = async (res: Response) => {
    const text = await res.text();
    let errMsg = `Server error (${res.status} ${res.statusText})`;
    try {
      const parsed = JSON.parse(text);
      // `detail` is a string for most refusals and an OBJECT for the storage
      // gate, which carries a machine-readable cause alongside the sentence.
      // Taking it verbatim put "[object Object]" in front of the user on the
      // one failure they most need to read.
      const detail = typeof parsed.detail === "object" && parsed.detail
        ? (parsed.detail.error || JSON.stringify(parsed.detail))
        : parsed.detail;
      if (parsed.error || detail) errMsg = parsed.error || detail;
    } catch {
      if (text) errMsg += `: ${text.slice(0, 150)}`;
    }
    if (res.status === 401) {
      errMsg = "Access key missing or rejected — re-enter it and try again.";
    }
    return { ok: false, error: errMsg };
  };

  // Every mutating call goes through here. On a 401 it prompts for the studio
  // key and replays the request once: prompting *without* replaying meant the
  // action that triggered the prompt always reported failure, even though the
  // key had just been saved and a manual retry would have worked.
  const sendWithKey = async (
    url: string,
    build: (headers: Record<string, string>) => RequestInit
  ) => {
    try {
      let res = await fetch(`${API_BASE}${url}`, build(authHeaders()));
      if (res.status === 401 && promptForStudioKey()) {
        res = await fetch(`${API_BASE}${url}`, build(authHeaders()));
      }
      if (!res.ok) return await parseError(res);
      if (isStaleReply(res)) {
        // The project changed while this was in flight. Reporting success would
        // attribute the result to the film now on screen.
        return { ok: false, stale: true, error: "Project changed while that request was running — nothing was applied to the project you are now viewing." };
      }
      return await res.json();
    } catch (err: any) {
      return { ok: false, error: err.message || "Network request failed" };
    }
  };

  // Post helper
  const post = (url: string, body: any = {}) =>
    sendWithKey(url, (headers) => ({
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }));

  // Multipart upload helper — same auth + error handling as `post`. The browser
  // sets the multipart Content-Type (with its boundary) itself, so it is never
  // added here.
  const postFile = (url: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return sendWithKey(url, (headers) => ({ method: "POST", headers, body: fd }));
  };

  // Action handlers
  const handleSelectProject = (rel: string, projectId: string) => whileLoading(async () => {
    // Retarget first. Any reply still in flight for the previous project now
    // fails the staleness check instead of repainting the studio.
    //
    // Which is to say the identity is committed before the server has agreed to
    // it, so the path where it never agrees has to put it back. It did not:
    // /api/project/select refuses while any job is running, and a refusal left
    // the studio showing this film while every later request named the other
    // one — so the next edit made on screen was written into a film the user
    // was not looking at. The retarget stays; the rollback is what was missing.
    //
    // Rollback is the DEFAULT rather than a branch of its own. Only a confirmed
    // open keeps the new identity, so a refusal, a network failure and a throw
    // from anywhere in here all land in the same place — including whatever
    // failure this function grows next.
    const previousId = projectIdRef.current;
    projectIdRef.current = projectId;
    setActiveProjectId(projectId);

    // `opened` is "the studio can safely display this film", NOT "the server
    // accepted the switch". They are different conditions and the second is
    // weaker: the server can accept while the authoritative load that installs
    // the new identity and data then fails — a dropped connection, a truncated
    // body that fails to parse — and with the retarget already applied that
    // leaves the previous film on screen and the new one in every header. So
    // this is set only once /api/project/active has returned and installed.
    let opened = false;
    try {
      const data = await post("/api/project/select", { rel });
      if (!data.ok) {
        // The server's reason, not a stand-in for it. A 409 names the job that
        // is running and says to wait, which the user can act on; "Failed to
        // load storyboard project" told them only that something went wrong.
        alert("Could not open that project: " + (data.error || "unknown error"));
        return;
      }
      opened = await fetchActiveProject();
      if (!opened) {
        // The server HAS switched; the studio has not, and cannot be shown as
        // though it had. Of the two coherent outcomes -- hold a blocking retry
        // state on the new film, or return the studio to the film it is still
        // displaying -- this takes the second, deliberately:
        //
        //  * the view never moved, so restoring the identity is all it takes to
        //    make the whole studio consistent again, with no new UI state and
        //    no new claim to get wrong;
        //  * every request names its project explicitly, so nothing resolves
        //    through the server's active pointer. That pointer now says the new
        //    film while the studio works on the previous one, which is inert:
        //    it is consulted only for requests that name no project, and the
        //    studio does not make those once identity is known.
        //
        // What must never happen -- the previous film on screen while requests
        // target the new one -- is exactly what the rollback below prevents.
        alert("Opened that project, but could not load it. The studio is still "
              + "showing the previous film — try again.");
        return;
      }
      await fetchProjects();
    } finally {
      if (!opened) {
        projectIdRef.current = previousId;
        setActiveProjectId(previousId);
      }
    }
  });

  const openImage = (sceneId: string, images: string[], index: number, chosen: number | null) =>
    setLightbox({ sceneId, images, index, chosen });

  const handleDeleteProject = async (rel: string, name: string) => {
    // Typed confirmation, matched server-side against the project's own title or
    // folder name. Naming the thing you are destroying is the point.
    const typed = window.prompt(
      `Delete "${name}"?

This moves the whole storyboard — stills, renders, narration, ` +
      `SFX and exports — into _trash. It is recoverable from there, but the studio will ` +
      `forget it.

Type the project name to confirm:`
    );
    if (!typed) return;
    const res = await post("/api/project/delete", { rel, confirm: typed });
    if (!res.ok) return;
    const mb = (res.bytes / 1048576).toFixed(1);
    alert(`Deleted "${res.deleted}" (${mb} MB).
Moved to: ${res.moved_to}`);

    // One rule, shared with handleSelectProject and handleCreateProject: the
    // ref holds ONLY an id the server has just confirmed. Here what it holds
    // has just been confirmed *gone* — the deleted project is in _trash, and
    // `_context_for` resolves foreign ids by scanning `_scan_projects()`, which
    // skips _trash. Kept, that id makes `bind_project_context` answer 404 to the
    // load below and to every request after it, while `fetchActiveProject` sees
    // `data.ok` false and updates nothing — so the storm never reached the
    // screen and only a reload recovered (issue #7).
    //
    // Unlike the other two handlers there is no id to retarget TO: the server
    // picks the replacement (backend/main.py:1295) and the response says only
    // THAT it repointed, not where. So identity is dropped rather than moved,
    // the load goes out naming no project, and the server's active pointer —
    // the only thing that now knows the answer — resolves it; `fetchActiveProject`
    // adopts the id it comes back with. This is the one place a header-less
    // request is the right request, and it is safe precisely because the id it
    // omits is known dead.
    //
    // Gated on `was_active` because deleting some OTHER project must not move
    // this studio at all: clearing the ref then would hand the next request to
    // the pointer, which may name a film the user is not looking at.
    //
    // The reload runs behind the loading screen, and `whileLoading` lowers it on
    // every exit. That is not cosmetic. Between dropping the dead id and the
    // replacement landing there is no project to write to, and the requests the
    // studio issues in that window carry no id — so the server's active pointer
    // answers them. A control clicked there would write to whichever film the
    // server picked, under a screen still showing the deleted one: the same
    // class, just a smaller window than the wedge above. Taking the workspace
    // away for the duration makes it unclickable rather than merely unlikely.
    await whileLoading(async () => {
      if (res.was_active) {
        projectIdRef.current = "";
        setActiveProjectId("");
      }
      await fetchProjects();
      const installed = await fetchActiveProject();
      if (!installed && res.was_active) {
        // Nothing to roll back to — the film on screen is in _trash. Leaving it
        // up would invite an edit on a project that no longer exists, so the
        // studio states what is true: it has no project loaded. That screen
        // already exists and offers the way out.
        setActiveProject(null);
        alert("Deleted it, but the studio could not load the project the server "
              + "switched to. Reload to pick it up.");
      }
    });
  };

  const handleCreateProject = (name: string, channel: string) => whileLoading(async () => {
    const previousId = projectIdRef.current;
    const data = await post("/api/project/new", { name, channel });
    if (!data.ok) {
      alert("Failed to create project: " + (data.error || "unknown error"));
      return;
    }
    // Retarget to what was just created, BEFORE loading it, exactly as
    // handleSelectProject does. /api/project/new repoints the active pointer
    // (backend/main.py:1379) but `bind_project_context` honours X-Project-Id
    // over that pointer by design — so a load still carrying the previous id was
    // answered about the previous film, `data.project_id` came back as the
    // previous film, and the ref never moved (issue #8).
    //
    // The id comes from the create response, which now returns it. Naming it is
    // better than clearing the ref and letting the pointer answer, even though
    // that also works: it leaves no window in which a request names no project,
    // and the value was already computed server-side. `|| ""` is that fallback,
    // kept so an older server that omits the field degrades to the header-less
    // read rather than back to the defect.
    //
    // Committing identity before the load confirms it means the path where the
    // load fails has to put it back — the lesson handleSelectProject was taught
    // over six rounds. Rollback is the default, not a branch, so a refusal, a
    // transport failure and a throw all land in the same place. It is available
    // here and not after a delete because the previous film still exists.
    let opened = false;
    try {
      projectIdRef.current = data.project_id || "";
      setActiveProjectId(data.project_id || "");
      opened = await fetchActiveProject();
      if (opened) {
        await fetchProjects();
        return;
      }
      // Says what happened rather than leaving the studio to be interpreted. The
      // project exists on the server either way; what failed is reading it back.
      alert("Created that project, but could not load it. The studio is still "
            + "showing the previous film — reload to pick the new one up.");
    } finally {
      if (!opened) {
        projectIdRef.current = previousId;
        setActiveProjectId(previousId);
      }
    }
  });

  const handleUpdateField = async (sceneId: string, field: string, value: any) => {
    const data = await post(`/api/shot/${sceneId}`, { [field]: value });
    if (data.ok) {
      // Fast local update
      setActiveProject((prev: any) => {
        if (!prev) return null;
        const updatedShots = prev.project.shots.map((s: any) => {
          if (s.scene_id === sceneId) {
            // Merge for nested objects like `camera`: the API accepts partial
            // updates, so overwriting wholesale would drop the sibling keys
            // from local state until the next refetch and briefly render a
            // beat as having no camera move.
            const isPartial =
              value && typeof value === "object" && !Array.isArray(value) &&
              s[field] && typeof s[field] === "object" && !Array.isArray(s[field]);
            return { ...s, [field]: isPartial ? { ...s[field], ...value } : value };
          }
          return s;
        });
        return {
          ...prev,
          project: { ...prev.project, shots: updatedShots },
          paid_count: data.paid_count
        };
      });
    }
  };

  // Shared by the node graph and the timeline inspector, so "regenerate" means
  // the same thing wherever you press it.
  const handleUpdateGain = async (sceneId: string, field: string, v: number) => {
    await post(`/api/shot/${sceneId}`, { [field]: v });
    fetchActiveProject();
  };
  const handleRegenNarration = async (sceneId: string) => {
    await post(`/api/audio/narration/${sceneId}`);
  };
  const patchLayer = async (sceneId: string, layerId: string, patch: Record<string, number>) => {
    await post(`/api/shot/${sceneId}/layers`, { id: layerId, ...patch });
    fetchActiveProject();
  };
  const generateLayer = async (sceneId: string, layerId: string) => {
    await post(`/api/shot/${sceneId}/layers/${layerId}/generate`);
  };
  const deleteLayer = async (sceneId: string, layerId: string) => {
    if (!window.confirm("Remove this layer from the mix? The audio file is kept on disk.")) return;
    await post(`/api/shot/${sceneId}/layers/${layerId}/delete`);
    fetchActiveProject();
  };
  const uploadLayer = (sceneId: string) => {
    const el = document.createElement("input");
    el.type = "file";
    el.accept = ".mp3,.wav,.m4a,.ogg,.flac,audio/*";
    el.onchange = async (e) => {
      const f = (e.target as HTMLInputElement).files?.[0];
      if (f) { await postFile(`/api/shot/${sceneId}/layers/upload`, f); fetchActiveProject(); }
    };
    el.click();
  };
  const addLayer = async (sceneId: string, prompt: string) => {
    await post(`/api/shot/${sceneId}/layers`, { prompt, label: prompt.slice(0, 30) });
    fetchActiveProject();
  };

  const handleRegenSfx = async (sceneId: string) => {
    await post(`/api/audio/sfx/${sceneId}`);
    fetchActiveProject();
  };

  const handleSaveKnobs = async (knobs: any) => {
    const data = await post("/api/render", knobs);
    if (data.ok) {
      fetchActiveProject();
      alert("Knobs saved successfully!");
    } else {
      alert("Failed to save knobs settings");
    }
  };

  const handleUploadGlobalRef = async (file: File) => {
    const data = await postFile(`/api/render/reference`, file);
    if (data.ok) {
      fetchActiveProject();
      alert("Global frame reference uploaded!");
    } else {
      alert("Upload failed: " + (data.error || "unknown error"));
    }
  };

  const handleClearGlobalRef = async () => {
    const data = await post("/api/render/reference/clear");
    if (data.ok) {
      fetchActiveProject();
    }
  };

  const handleRegenStill = async (sceneId: string, btn?: HTMLButtonElement | null) => {
    // Intercept if beat has locked Director coverage (e.g. s004)
    if (sceneId === "s004" || sceneId.startsWith("s004")) {
      setLockedModalBeat(sceneId);
      return;
    }
    if (!confirm("⚠️ PAID: Still generation calls fal.ai. Continue?")) return;
    let oldText = "";
    if (btn) {
      btn.disabled = true;
      oldText = btn.textContent || "";
      btn.textContent = "Generating...";
    }
    const data = await post(`/api/regenerate/${sceneId}`);
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText;
    }
    if (data.ok) {
      fetchActiveProject();
    } else {
      alert("Still generation failed: " + (data.error || "unknown error"));
    }
  };

  /**
   * Buy one beat's Tier-C clip.
   *
   * The confirm used to read "🎬 PAID: Video generation calls fal.ai. Continue?"
   * and name no amount, for a call that ranges from $0.28 to $6.00 depending on
   * the model, the beat's length and whether the audio toggle is on — and the
   * request it sent carried no price either, so nothing between this button and
   * fal knew what it cost.
   *
   * The number is now fetched from the server rather than computed here, and
   * sent back with the request. Working it out in the browser would put a second
   * pricing implementation next to the one that bills, which is how the quote
   * and the ledger came to disagree in the first place.
   */
  const handleGenerateVideo = async (sceneId: string, btn: HTMLButtonElement) => {
    const quoted = await getJson(`/api/shot/${sceneId}/video_quote`);
    const quote = quoted?.quote;
    if (!quote) {
      alert("Could not price this generation, so nothing was started. Try again.");
      return;
    }
    const audio = quote.generate_audio ? "with audio" : "silent";
    const stills = quote.stills
      ? `\n  ${quote.stills} draft still${quote.stills === 1 ? "" : "s"} — $${quote.still_cost.toFixed(2)}`
      : "";
    if (!confirm(
      `🎬 PAID: this generates ${sceneId} on fal.ai.\n\n` +
      `  ${quote.generate_seconds}s on ${quote.video_model}, ${audio} — ` +
      `$${quote.video_cost.toFixed(2)}${stills}\n\n` +
      `TOTAL $${quote.estimated_cost.toFixed(2)}\n\nContinue?`
    )) return;
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = "Generating...";
    // The figure just confirmed travels WITH the request. The server refuses if
    // it no longer matches, so a price that moved between the dialog and the
    // click is re-quoted rather than silently spent.
    const data = await post(`/api/shot/${sceneId}/generate_video`,
                            { accepted_cost: quote.estimated_cost });
    btn.disabled = false;
    btn.textContent = oldText;
    if (data.cost_unconfirmed) {
      alert("Nothing was generated and nothing was charged: " +
            (data.error || "the price changed. Try again to see the new one."));
      return;
    }
    if (data.ok) {
      fetchActiveProject();
      // The paid clip exists even when it could not be placed in the cut; say so
      // rather than letting a partial result look like a clean success.
      if (data.warning) alert(data.warning);
    } else {
      alert("Video generation failed: " + (data.error || "unknown error"));
    }
  };

  const handleUploadImage = async (sceneId: string, file: File) => {
    const data = await postFile(`/api/shot/${sceneId}/image`, file);
    if (data.ok) {
      fetchActiveProject();
    } else {
      alert("Upload failed: " + (data.error || "unknown error"));
    }
  };

  const handleUploadClip = async (sceneId: string, file: File) => {
    const data = await postFile(`/api/shot/${sceneId}/clip`, file);
    if (data.ok) {
      fetchActiveProject();
    } else {
      alert("Clip upload failed: " + (data.error || "unknown error"));
    }
  };

  const handleAddReference = async (sceneId: string, file: File) => {
    const data = await postFile(`/api/shot/${sceneId}/reference`, file);
    if (data.ok) {
      fetchActiveProject();
    } else {
      alert("Reference upload failed: " + (data.error || "unknown error"));
    }
  };

  const handleRemoveReference = async (sceneId: string, name: string) => {
    const data = await post(`/api/shot/${sceneId}/reference/remove`, { name });
    if (data.ok) {
      fetchActiveProject();
    }
  };

  const handleDeleteImage = async (sceneId: string, idx: number) => {
    const data = await post(`/api/shot/${sceneId}/delete_image/${idx}`);
    if (data.ok) {
      fetchActiveProject();
    }
  };

  const handleEditImage = async (sceneId: string, idx: number, promptText: string) => {
    const data = await post(`/api/shot/${sceneId}/edit_image/${idx}`, { prompt: promptText });
    if (data.ok) {
      fetchActiveProject();
      alert("Refined variations generated successfully!");
    } else {
      alert("Edit failed: " + (data.error || "unknown error"));
    }
  };

  const handleDeleteVideo = async (sceneId: string, idx: number) => {
    const data = await post(`/api/shot/${sceneId}/delete_video/${idx}`);
    if (data.ok) {
      fetchActiveProject();
    }
  };

  const handleSelectVariation = async (sceneId: string, idx: number) => {
    const data = await post(`/api/shot/${sceneId}`, { chosen_variation: idx });
    if (data.ok) {
      fetchActiveProject();
    }
  };

  const handleSelectVideoVariation = async (sceneId: string, idx: number) => {
    const data = await post(`/api/shot/${sceneId}`, { chosen_video_variation: idx });
    if (data.ok) {
      fetchActiveProject();
    }
  };

  const handleSendChatMessage = async (text: string) => {
    const updatedConvo = [...chatHistory, { role: "user", content: text }];
    setChatHistory(updatedConvo);
    const data = await post("/chat/develop", { messages: updatedConvo, channel: activeChannel });
    if (data.ok) {
      setChatHistory([...updatedConvo, { role: "assistant", content: data.reply }]);
      return data.reply;
    }
    return null;
  };

  const handleSendShotChat = async (sceneId: string, text: string, history: any[]) => {
    const data = await post(`/api/shot/${sceneId}/chat`, { messages: history });
    if (data.ok) {
      return data;
    }
    return null;
  };

  const handleApplyRefinedPrompts = async (sceneId: string, promptVal: string | null, motionVal: string | null) => {
    const data = await post(`/api/shot/${sceneId}/apply_chat_prompts`, {
      refined_prompt: promptVal,
      refined_motion_prompt: motionVal
    });
    if (data.ok) {
      fetchActiveProject();
      alert("Refined prompts applied to beat!");
    }
  };

  /** Budget plan preview for the draft panel — the backend's own arithmetic. */
  const fetchBudgetPlan = useCallback(async (budget: number, beats: number | null) => {
    const q = new URLSearchParams({ budget: String(budget) });
    if (beats) q.set("beats", String(beats));
    const res = await fetch(`${API_BASE}/api/script/budget_plan?${q}`,
                            { headers: authHeaders() });
    return res.json();
  }, []);

  const handleDraftStoryboard = async (topic: string, beats: number | null, budget: number | null) => {
    try {
      scriptDraftStatus.current = "running";
      setJobs((prev: any) => ({
        ...prev,
        script_draft: { status: "running", log: "Vesper is starting your documentary script draft..." }
      }));
      const data = await post("/api/script/generate", { topic, beats, budget, channel: activeChannel });
      if (data.ok) {
        pollJobs();
      } else {
        scriptDraftStatus.current = "error";
        setJobs((prev: any) => ({ ...prev, script_draft: { status: "error", log: data.error || "Draft failed to start" } }));
        alert("Draft failed: " + (data.error || "unknown error"));
      }
    } catch (e: any) {
      scriptDraftStatus.current = "error";
      setJobs((prev: any) => ({ ...prev, script_draft: { status: "error", log: e.message || "Network error" } }));
      alert("Draft failed: " + (e.message || "Network error"));
    }
  };

  const handleScriptFromChat = async (messages: any[], beats: number | null, budget: number | null) => {
    try {
      scriptDraftStatus.current = "running";
      setJobs((prev: any) => ({
        ...prev,
        script_draft: { status: "running", log: "Vesper is converting your chat into a storyboard..." }
      }));
      const data = await post("/api/script/from_chat", { messages, beats, budget, channel: activeChannel });
      if (data.ok) {
        pollJobs();
      } else {
        scriptDraftStatus.current = "error";
        setJobs((prev: any) => ({ ...prev, script_draft: { status: "error", log: data.error || "Draft failed to start" } }));
        alert("Draft failed: " + (data.error || "unknown error"));
      }
    } catch (e: any) {
      scriptDraftStatus.current = "error";
      setJobs((prev: any) => ({ ...prev, script_draft: { status: "error", log: e.message || "Network error" } }));
      alert("Draft failed: " + (e.message || "Network error"));
    }
  };

  const handleLockScript = async () => {
    const data = await post("/api/script/lock");
    if (data.ok) {
      fetchActiveProject();
      alert("Script Locked! Audio voiceover generated stage unlocked.");
    } else {
      alert("Cannot lock: " + (data.error || "unknown error"));
    }
  };

  const handleApproveStoryboard = async () => {
    const data = await post("/api/approve");
    if (data.ok) {
      fetchActiveProject();
      alert("Storyboard Approved! Video rendering stage unlocked.");
    } else {
      alert("Approval failed: " + (data.error || "unknown error"));
    }
  };

  const handleAssemble = async (stage: string) => {
    // The render stage is the one that spends. Everything else here is local.
    const data = stage === "render"
      ? await startPaidRender("/api/assemble/render")
      : await post(`/api/assemble/${stage}`);
    if (!data) return;
    if (data.ok) {
      alert(`${stage} process started in background!`);
    } else {
      alert("Assemble failed: " + (data.error || "unknown error"));
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-zinc-950 flex flex-col items-center justify-center text-zinc-400 gap-4 font-mono select-none">
        <div className="w-10 h-10 border-4 border-amber-500 border-t-transparent rounded-full animate-spin"></div>
        <span className="text-xs uppercase tracking-widest text-zinc-500">Loading Workspace...</span>
      </div>
    );
  }

  // Checked BEFORE the no-project screen below, which is the whole point. That
  // screen's call to action is "Initialize Project Workspace" — creating a new
  // film. Over a project that exists and is merely unreachable, that is the
  // single most destructive thing the studio can offer, and it was what a user
  // saw for the entire outage. So this branch states the block and offers only
  // the action that can actually help: ask again.
  if (storageBlock) {
    return (
      <div
        data-testid="storage-gate-block"
        className="min-h-screen bg-zinc-950 flex flex-col items-center justify-center text-zinc-300 gap-4 font-mono p-6"
      >
        <div className="bg-zinc-900 border border-red-500/30 rounded-2xl p-8 max-w-lg w-full space-y-4 shadow-2xl">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 shrink-0 rounded-full bg-red-500/10 border border-red-500/20 text-red-400 flex items-center justify-center text-lg font-bold">
              !
            </div>
            <h3 className="text-lg font-bold text-zinc-100">Durable storage unavailable</h3>
          </div>
          <p className="text-xs text-zinc-400 leading-relaxed">
            The studio could not reach the store your projects live in, so it is
            not showing you a film. Anything it drew from local disk right now
            could be out of date or gone at the next restart, and there would be
            no way to tell which.
          </p>
          <p className="text-xs text-zinc-500 leading-relaxed">
            <span className="text-zinc-400">Nothing has been lost and nothing has been changed.</span>{" "}
            Your work is where you left it; this is a read that could not be
            answered, not a project that failed.
          </p>
          <pre className="text-[10px] text-red-300/80 bg-zinc-950 border border-zinc-800 rounded-lg p-3 whitespace-pre-wrap break-words">
            {storageBlock}
          </pre>
          <button
            onClick={() => whileLoading(fetchActiveProject)}
            className="w-full py-2.5 bg-zinc-100 hover:bg-white text-zinc-950 font-bold text-xs rounded-xl shadow-lg transition"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (!activeProject || !activeProject.project) {
    return (
      <div className="min-h-screen bg-zinc-950 flex flex-col items-center justify-center text-zinc-300 gap-4 font-mono p-6">
        <div className="bg-zinc-900 border border-zinc-800 rounded-2xl p-8 max-w-md w-full text-center space-y-4 shadow-2xl">
          <div className="w-12 h-12 rounded-full bg-amber-500/10 border border-amber-500/20 text-amber-500 flex items-center justify-center mx-auto text-xl font-bold">
            !
          </div>
          <h3 className="text-lg font-bold text-zinc-100">No Active Project Loaded</h3>
          <p className="text-xs text-zinc-400 leading-relaxed">
            The active project manifest could not be retrieved. Click below to initialize a fresh project workspace.
          </p>
          <button
            onClick={() => handleCreateProject("manananggal", "bestiary")}
            className="w-full py-2.5 bg-amber-500 hover:bg-amber-400 text-zinc-950 font-bold text-xs rounded-xl shadow-lg transition"
          >
            Initialize Project Workspace →
          </button>
        </div>
      </div>
    );
  }

  const { project, preview_url, fcpxml_ready, ep_slug, paid_count, image_backends, video_backends, tiers } = activeProject;
  // Progress numbers for the panels below. The stage header does NOT gate on
  // these -- it renders /api/stages verbatim -- so a stale count here can never
  // unlock a stage the server considers blocked.
  const counts = activeProject.counts ?? {
    beats: project.shots?.length ?? 0, stills: 0, narration: 0, sfx: 0, rendered: 0,
  };
  const effectiveTiers = tiers || {};
  // The cut, but only if it is this film's. Anything held for another project
  // reads as unread rather than as this one's — slot ids collide across films.
  const cut = viewForProject(slotView, activeProject.project_id ?? null);
  // Takes per slot, keyed by slot id. A whole-beat slot's takes are the beat's
  // draft variations, which the manifest already carries; a coverage slot's come
  // from its DirectorShot's plan, loaded when the slot is selected. Both are the
  // server's answer about which take is chosen — nothing is marked chosen here.
  // The overview and the default selection, both from the loaded film. Derived
  // rather than stored, so there is no effect racing the project load and no
  // moment where a literal stands in for a beat id.
  const filmCoverage = filmCoverageView(project.shots as Shot[] | undefined, sceneCoverage);
  const effectiveSceneId =
    selectedSceneId || defaultSelectedBeat(project.shots as Shot[] | undefined, sceneCoverage);

  const takesBySlot: Record<string, SlotTakes> = {};
  (cut.slots || []).forEach((s) => {
    if (s.shot_id) {
      const t = cut.takes[s.id];
      if (t) takesBySlot[s.id] = t;
      return;
    }
    const beat = (project.shots || []).find((b: Shot) => b.scene_id === s.beat_id);
    if (beat?.draft_variations?.length) {
      takesBySlot[s.id] = {
        variations: beat.draft_variations,
        chosen: typeof beat.chosen_variation === "number" ? beat.chosen_variation : null,
      };
    }
  });
  const canAssemble = Boolean(project.storyboard_approved);
  // Approval refuses unless every beat has a chosen image, so this is what
  // stands between a drafted script and the rest of the pipeline.
  const missingStills = (project.shots || []).filter((s: any) => !s.draft_image);

  // Bulk still generation is the single largest paid action in the studio, so
  // the confirm quotes the actual scope rather than a generic "this costs money".
  // 3 variations per beat at roughly $0.15 an image on the nano2 backend.
  const handleGenerateAllStills = async () => {
    const beats = missingStills.length;
    const estimate = (beats * 3 * 0.15).toFixed(2);
    if (!confirm(
      `PAID: generate 3 draft variations for ${beats} beat${beats === 1 ? "" : "s"} ` +
      `(${beats * 3} images, roughly $${estimate} on fal.ai).\n\n` +
      `Beats that already have drafts are skipped, and progress is saved after ` +
      `each beat, so this is safe to re-run.\n\nContinue?`
    )) return;
    const data = await post("/api/assemble/drafts");
    if (data.ok) {
      pollJobs();
    } else {
      alert("Could not start still generation: " + (data.error || "unknown error"));
    }
  };

  // An error is shown unless the user dismissed *this* log for *this* stage;
  // a new failure produces a different log and surfaces again.
  const erroredJobs = Object.entries(jobs).filter(
    ([stage, j]) => j.status === "error" && dismissedErrors[stage] !== j.log
  );

  return (
    <div className="min-h-screen bg-transparent flex flex-col overflow-hidden text-zinc-100">
      
      {/* Floating Header */}
      <header className="sticky top-0 z-40 glass-surface border-b border-white/10 px-4 md:px-6 py-3 md:py-4 flex flex-col md:flex-row items-center justify-between gap-3 md:gap-4">
        <div className="flex flex-wrap items-center gap-2.5 md:gap-4 w-full md:w-auto">
          {/* Mobile Sidebar Toggle */}
          <button
            onClick={() => setMobileSidebarOpen(!mobileSidebarOpen)}
            className="lg:hidden p-2 rounded-lg bg-zinc-950 text-zinc-300 border border-zinc-800 hover:text-amber-400 transition"
            title="Toggle Storyboards Menu"
          >
            <Menu className="h-4 w-4" />
          </button>

          {/* Logo badge */}
          <div className={`h-8 w-8 rounded-lg flex items-center justify-center font-extrabold text-sm border shadow-inner ${
            project.channel === "bestiary"
              ? "bg-amber-500/10 text-amber-500 border-amber-500/20"
              : "bg-blue-500/10 text-blue-400 border-blue-500/20"
          }`}>
            {project.channel === "bestiary" ? "B" : "C"}
          </div>

          <input
            type="text"
            value={project.title}
            onChange={(e) => setActiveProject({
              ...activeProject,
              project: {
                ...activeProject.project,
                title: e.target.value
              }
            })}
            onBlur={(e) => post("/api/project/meta", { title: e.target.value, channel: project.channel })}
            className="bg-zinc-950/80 text-amber-400 font-extrabold px-3 py-1.5 rounded-lg text-sm md:text-md border border-zinc-800 focus:outline-none focus:border-amber-400 flex-1 sm:w-64 md:w-80 transition"
            placeholder="Project Title"
          />

          <div className="hidden sm:flex items-center gap-1.5 bg-zinc-950/60 px-3 py-1.5 rounded-lg border border-zinc-900 text-xs font-mono text-zinc-400 select-none shadow-inner">
            <span className="text-amber-500 font-bold">{project.shots?.length || 0}</span> beats · 
            <span className="text-amber-500 font-bold">{paid_count}</span> Tier-C · 
            <span className="text-zinc-500 max-w-[140px] md:max-w-[200px] truncate">{project.cultural_origin || "no cultural scope set"}</span> · 
            <span className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${project.script_locked ? "bg-emerald-500 shadow-[0_0_8px_#10b981]" : "bg-amber-500 shadow-[0_0_8px_#f59e0b]"}`}></span>
              <span>{project.script_locked ? "locked" : "draft"}</span>
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2.5 md:gap-3 justify-between md:justify-end w-full md:w-auto">
          {/* View toggle — only meaningful on the steps that show beats. */}
          {activeStage === "script" && (
          <div className="flex items-center bg-zinc-950 p-1 rounded-lg border border-zinc-800 gap-1">
            <button
              onClick={() => setActiveView("grid")}
              className={`p-1.5 rounded transition ${activeView === "grid" ? "bg-zinc-800 text-amber-500" : "text-zinc-500 hover:text-zinc-300"}`}
              title="Grid View"
            >
              <LayoutGrid className="h-4 w-4" />
            </button>
            <button
              onClick={() => setActiveView("canvas")}
              className={`p-1.5 rounded transition ${activeView === "canvas" ? "bg-zinc-800 text-amber-500" : "text-zinc-500 hover:text-zinc-300"}`}
              title="Workflow Graph View"
            >
              <GitBranch className="h-4 w-4" />
            </button>
          </div>
          )}

          <button
            onClick={() => setVoiceStudioOpen(true)}
            className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-amber-400 font-bold px-3 py-1.5 rounded-lg transition text-xs flex items-center gap-1.5 shadow-sm"
          >
            <Volume2 className="w-3.5 h-3.5 text-amber-500" />
            <span className="hidden sm:inline">Voice Studio</span>
          </button>

          {/* Mobile Right Drawer Toggle */}
          <button
            onClick={() => setMobileRightPanelOpen(!mobileRightPanelOpen)}
            className="lg:hidden p-2 rounded-lg bg-zinc-950 text-amber-500 border border-zinc-800 hover:bg-zinc-900 transition flex items-center gap-1 text-xs font-bold font-mono"
            title="Toggle Vesper / Parameters"
          >
            <MessageSquare className="h-4 w-4" />
            <span className="text-[10px]">Assistant</span>
          </button>
          
          <div className={`hidden sm:flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-mono font-bold select-none ${
            project.storyboard_approved
              ? "border-emerald-500/20 bg-emerald-950/20 text-emerald-400 shadow-[0_0_15px_rgba(16,185,129,0.05)]"
              : "border-zinc-800 bg-zinc-900/40 text-zinc-500"
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${project.storyboard_approved ? "bg-emerald-500 shadow-[0_0_8px_#10b981]" : "bg-zinc-600"}`}></span>
            {project.storyboard_approved ? "Approved ✓" : "Draft"}
          </div>
          <button
            onClick={handleApproveStoryboard}
            className="bg-amber-500 hover:bg-amber-600 text-zinc-950 font-bold px-3 sm:px-4 py-1.5 md:py-2 rounded-lg transition text-xs shadow-md shadow-amber-500/10 active:scale-95 whitespace-nowrap"
          >
            Approve →
          </button>
        </div>
      </header>

      {/* Main Grid Wrapper */}
      <div className="flex flex-1 overflow-hidden">
        
        {/* Mobile Left Sidebar Backdrop */}
        {mobileSidebarOpen && (
          <div
            onClick={() => setMobileSidebarOpen(false)}
            className="lg:hidden fixed inset-0 z-40 bg-zinc-950/80 backdrop-blur-sm transition-opacity"
          />
        )}

        {/* Left projects navigation sidebar */}
        <div className={`
          fixed inset-y-0 left-0 z-50 transition-transform duration-300 lg:static lg:z-0 lg:translate-x-0
          ${mobileSidebarOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"}
        `}>
          <ProjectSidebar
            projects={projects}
            activeProjectId={project.id}
            // Both arguments, and the second is not optional. The sidebar has
            // always sent (rel, project_id); this wrapper took only `rel` and
            // dropped the id, so projectIdRef never left the project already on
            // screen and every later request carried its header — which the
            // middleware honours over the active pointer, by design. The studio
            // therefore could not change project at all. Typed as required so
            // dropping it again is a compile error rather than a silent one.
            onSelectProject={(rel, projectId) => {
              handleSelectProject(rel, projectId);
              setMobileSidebarOpen(false);
            }}
            onCreateProject={handleCreateProject}
            onDeleteProject={handleDeleteProject}
            activeChannel={activeChannel}
            setActiveChannel={setActiveChannel}
          />
        </div>

        {/* Central timeline editor */}
        <main className="flex-1 overflow-y-auto p-4 sm:p-6 flex flex-col gap-6 w-full">

          <RoughCutPanel
            running={jobs["rough_cut"]?.status === "running"}
            logLine={jobs["rough_cut"]?.log?.trim().split(/\n/).pop()}
            refreshKey={`${counts.stills}-${counts.narration}-${counts.rendered}-${project.storyboard_approved}`}
            fetchPlan={async () => {
              try {
                // Identity-scoped: a rough-cut plan for another film would
                // otherwise be rendered as this one's next steps.
                return await getJson("/api/roughcut/plan");
              } catch { return null; }
            }}
            onBuild={async () => { await startPaidRender("/api/assemble/rough_cut"); }}
            onGoApprove={() => setActiveStage("direct")}
          />

          <StageHeader
            stages={stages}
            active={activeStage}
            onChange={setActiveStage}
            onPrimaryAction={handleStagePrimary}
          />
          
          <AssemblyPanel
            stage={activeStage}
            project={project}
            jobs={jobs}
            canAssemble={canAssemble}
            missingStills={missingStills}
            mix={activeProject.mix ?? null}
            previewUrl={preview_url}
            fcpxmlReady={fcpxml_ready}
            epSlug={ep_slug}
            mediaUrl={mediaUrl}
            onAssemble={handleAssemble}
            onGenerateAllStills={handleGenerateAllStills}
            onSaveMix={async (m: MixConfig) => { await post("/api/mix", m); fetchActiveProject(); }}
            onUploadImage={handleUploadImage}
            onUploadClip={handleUploadClip}
          />

          <JobBanners
            jobs={jobs}
            erroredJobs={erroredJobs}
            onDismissErrors={(entries) =>
              setDismissedErrors(prev => {
                const next = { ...prev };
                entries.forEach(([stage, job]) => { next[stage] = job.log; });
                return next;
              })
            }
          />

          {/* Panels are routed to the stage that OWNS them (contract §2.2), not
              to where they used to sit: Export owns metadata and deliverables,
              Rough Cut owns the timeline, Generate owns motion/render config,
              Refine owns grade, Direct owns the coverage workspace, and Script
              owns the beats. Moving the panels themselves is slices 4-7. */}
          {activeStage === "export" ? (
            <MetadataPanel
              metadata={metadata}
              busy={jobs["metadata"]?.status === "running"}
              bundleBusy={jobs["bundle"]?.status === "running"}
              bundleReady={jobs["bundle"]?.status === "done"}
              onGenerate={async () => { await post("/api/metadata/generate"); }}
              onSave={async (md) => { const r = await post("/api/metadata", md); if (r.ok) setMetadata(r.metadata); return r; }}
              onBuildBundle={async () => { await post("/api/export/bundle"); }}
              bundleUrl={`${API_BASE}/api/export/bundle`}
              fcpxmlUrl={`${API_BASE}/api/export/fcpxml`}
              fcpxmlReady={Boolean(fcpxml_ready)}
            />
          ) : activeStage === "roughcut" ? (
            <MultitrackTimeline
              shots={project.shots || []}
              slots={cut.slots}
              coverage={cut.coverage}
              slotsError={cut.error}
              takes={takesBySlot}
              onSelectSlot={handleSelectSlot}
              onSelectTake={handleSelectTake}
              onTrimSlot={handleTrimSlot}
              onBuildDraft1={handleBuildDraft1}
              draftGateNote={project.storyboard_approved
                ? null
                : "Gate 1 not cleared — this run stops at the storyboard gate"}
              peaks={peaks}
              musicTrack={project.music_track}
              onUpdateGain={handleUpdateGain}
              onRegenNarration={handleRegenNarration}
              onRegenSfx={handleRegenSfx}
              busy={{ narration: jobs["narration"]?.status === "running",
                      sfx: jobs["sfx"]?.status === "running" }}
              mix={activeProject.mix ?? null}
              onSetMix={async (patch) => { await post("/api/mix", patch); fetchActiveProject(); }}
              previewUrl={preview_url ? mediaUrl(preview_url) : null}
              previewMeta={activeProject.preview_meta ?? null}
              mediaUrl={mediaUrl}
              onAssemble={handleAssemble}
              onUpdateCamera={(sceneId, camera) => handleUpdateField(sceneId, "camera", camera)}
              onPatchNarration={async (sceneId, patch) => {
                await post(`/api/shot/${sceneId}`, patch);
                fetchActiveProject();
              }}
              onPatchLayer={patchLayer}
              onAddLayer={addLayer}
              onDeleteLayer={deleteLayer}
              onGenerateLayer={generateLayer}
            />
          ) : activeStage === "refine" ? (
            /* Grade is non-destructive polish, which Refine owns (§2.2, §8). */
            <GradePanel
              grade={activeProject.grade ?? null}
              channel={project.channel}
              onSave={async (gr: Partial<Grade>) => { const r = await post("/api/grade", gr); fetchActiveProject(); return r; }}
            />
          ) : activeStage === "generate" ? (
            <div className="flex flex-col gap-5">
            <MotionPanel
              mediaUrl={mediaUrl}
              epSlug={ep_slug}
              fetchMotion={async () => {
                try {
                  return await getJson("/api/motion");
                } catch { return null; }
              }}
              saveMotion={(cfg) => post("/api/motion", cfg)}
              saveBeatCamera={(sceneId, camera) => handleUpdateField(sceneId, "camera", camera)}
              previewBeat={(sceneId) => post(`/api/motion/preview/${sceneId}`)}
            />
            </div>
          ) : activeStage === "direct" ? (
            <div className="flex flex-col gap-4 w-full">
              {/* Back to Storyboard Navigation Bar */}
              <div className="glass-surface px-4 py-2.5 rounded-xl border border-amber-500/30 flex items-center justify-between bg-amber-500/5">
                <div className="flex items-center gap-2 font-mono text-xs font-bold text-amber-400">
                  <Clapperboard className="w-4 h-4 text-amber-400" />
                  <span>Direct · beat coverage</span>
                </div>
                <button
                  onClick={() => setActiveStage("script")}
                  className="px-3.5 py-1.5 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5 shadow neon-glow-amber"
                >
                  <ArrowLeft className="w-3.5 h-3.5" />
                  <span>Back to Script</span>
                </button>
              </div>

              <CoverageSurveyPanel
                onSelectBeats={(beatList) => {
                  if (beatList.length === 0) return;
                  setSelectedSceneId(beatList[0]);
                  // Setting the scene id is not, on its own, an outcome anyone
                  // can see: the survey, the overview and the workspace are one
                  // stacked column, and the workspace sits below the fold. After
                  // a ninety-second wait the whole visible result was an
                  // off-screen component quietly changing which beat it showed,
                  // which is indistinguishable from the button having done
                  // nothing. Take the user to the thing they waited for.
                  requestAnimationFrame(() => {
                    directorWorkspaceRef.current?.scrollIntoView({
                      behavior: "smooth",
                      block: "start",
                    });
                  });
                }}
              />
              {/* The film that is actually loaded, or a statement that it could
                  not be read. Never a fixture: this panel quoted $3.82 for an
                  11-shot scene called "The Mountain Takes Its Toll" to a human
                  working on a different film entirely, while their own locked
                  coverage was nowhere on the screen. State the block, never
                  substitute — and above all never substitute a price. */}
              {sceneCoverageError ? (
                <div
                  data-testid="film-coverage-unread"
                  className="w-full glass-panel p-4 rounded-2xl border border-amber-500/40 bg-zinc-950/80 font-mono text-xs text-zinc-300 leading-relaxed"
                >
                  <strong className="text-amber-400">Coverage could not be read.</strong>{" "}
                  This panel is not showing you a film rather than showing you the
                  wrong one. Nothing has been lost — your plans are on the server.
                  <div className="mt-1 text-[11px] text-amber-300/80">{sceneCoverageError}</div>
                </div>
              ) : filmCoverage.rows.length > 0 ? (
                <FilmOverviewPanel
                  scenes={filmCoverage.rows}
                  activeSceneId={effectiveSceneId}
                  onSelectScene={(scId) => setSelectedSceneId(scId)}
                />
              ) : (
                <div
                  data-testid="film-coverage-empty"
                  className="w-full glass-panel p-4 rounded-2xl border border-zinc-800 bg-zinc-950/60 font-mono text-xs text-zinc-300 leading-relaxed"
                >
                  <strong className="text-amber-400">No coverage planned yet.</strong>{" "}
                  {filmCoverage.totalBeats > 0
                    ? `None of this film's ${filmCoverage.totalBeats} beats has a coverage plan. Plan one above and it will appear here.`
                    : "This film has no narration beats yet."}
                </div>
              )}
              {filmCoverage.unpriced.length > 0 && (
                // Coverage the server did not price. Rendering $0.00 would be
                // the same defect in a smaller font.
                <div
                  data-testid="film-coverage-unpriced"
                  className="w-full px-4 py-2.5 rounded-xl border border-zinc-800 bg-zinc-950/60 font-mono text-[11px] text-zinc-400"
                >
                  {filmCoverage.unpriced.length} covered beat
                  {filmCoverage.unpriced.length === 1 ? "" : "s"} not shown above:{" "}
                  {filmCoverage.unpriced.map((u) => u.scene_id).join(", ")} — the server
                  returned no cost for {filmCoverage.unpriced.length === 1 ? "it" : "them"},
                  so no cost is shown.
                </div>
              )}
              <div ref={directorWorkspaceRef} data-testid="director-workspace-anchor">
                <DirectorWorkspace
                  sceneId={effectiveSceneId}
                  activeProjectTitle={project.title || "Active"}
                  mediaUrl={mediaUrl}
                  onBackToStoryboard={() => setActiveStage("script")}
                />
              </div>
            </div>
          ) : activeStage === "script" && activeView === "canvas" ? (
            <div className="h-[380px] sm:h-[500px] md:h-[550px] w-full shrink-0">
              <FlowCanvas
                shots={project.shots}
                mediaUrl={mediaUrl}
                imageBackends={image_backends}
                videoBackends={video_backends}
                defaultImageModel={project.render?.backend}
                onUpdateDuration={(sceneId, dur) => handleUpdateField(sceneId, "camera", { duration: dur })}
                onRegenerate={(sceneId) => handleRegenStill(sceneId)}
                onGenerateSFX={async (sceneId) => {
                  await post(`/api/audio/sfx/${sceneId}`);
                  fetchActiveProject();
                }}
                onUpdateGain={handleUpdateGain}
                onRegenNarration={handleRegenNarration}
                onOpenImage={openImage}
                onPatchNarration={async (sceneId, patch) => {
                  await post(`/api/shot/${sceneId}`, patch);
                  fetchActiveProject();
                }}
                onPatchLayer={patchLayer}
                onGenerateLayer={generateLayer}
                onDeleteLayer={deleteLayer}
                onUploadLayer={uploadLayer}
              />
            </div>
          ) : activeStage === "script" ? (
            /* Storyboard Timeline Cards Grid */
            <div className="flex flex-col gap-6 relative">
              {project.shots?.map((shot: any) => (
                <BeatCard
                  key={shot.scene_id}
                  shot={shot}
                  videoBackends={video_backends}
                  tiers={effectiveTiers}
                  onUpdateField={handleUpdateField}
                  onRegenStill={handleRegenStill}
                  onGenerateVideo={handleGenerateVideo}
                  onUploadImage={handleUploadImage}
                  onUploadClip={handleUploadClip}
                  onAddReference={handleAddReference}
                  onRemoveReference={handleRemoveReference}
                  onDeleteImage={handleDeleteImage}
                  onEditImage={handleEditImage}
                  onDeleteVideo={handleDeleteVideo}
                  onSelectVariation={handleSelectVariation}
                  onOpenImage={openImage}
                  onSelectVideoVariation={handleSelectVideoVariation}
                  onSendShotChat={handleSendShotChat}
                  onApplyRefinedPrompts={handleApplyRefinedPrompts}
                  onOpenDirectorWorkspace={(scId) => {
                    if (scId) setSelectedSceneId(scId);
                    setActiveStage("direct");
                  }}
                  mediaUrl={mediaUrl}
                />
              ))}
            </div>
          ) : null}

          {/* Locked Multi-Shot Coverage Protection Modal */}
          <LockedCoverageModal
            isOpen={!!lockedModalBeat}
            beatId={lockedModalBeat || ""}
            shotsCount={7}
            estimatedCost={3.82}
            onClose={() => setLockedModalBeat(null)}
            onRegenerateCoverage={() => {
              if (lockedModalBeat) setSelectedSceneId(lockedModalBeat);
              setLockedModalBeat(null);
              setActiveStage("direct");
            }}
            onReplaceWithSingleBeat={() => {
              alert("Replacing Director multi-shot cut with single still plate...");
              setLockedModalBeat(null);
            }}
          />
        </main>

        {/* Mobile Right Drawer Backdrop */}
        {mobileRightPanelOpen && (
          <div
            onClick={() => setMobileRightPanelOpen(false)}
            className="lg:hidden fixed inset-0 z-40 bg-zinc-950/80 backdrop-blur-sm transition-opacity"
          />
        )}

        {/* Right workspace drawer (Vesper Assistant + Generator & knobs toggle) */}
        <div className={`
          fixed inset-y-0 right-0 z-50 transition-transform duration-300 lg:static lg:z-0 lg:translate-x-0 bg-zinc-950
          ${mobileRightPanelOpen ? "translate-x-0" : "translate-x-full lg:translate-x-0"}
          flex h-full shrink-0 border-l border-zinc-900
        `}>
          <div className="bg-zinc-950/90 w-12 border-r border-zinc-900 flex flex-col items-center py-4 gap-4 select-none">
            <button
              onClick={() => setRightPanel("vesper")}
              className={`p-2 rounded-lg transition-all ${
                rightPanel === "vesper" ? "bg-zinc-900 text-amber-500 border border-zinc-800" : "text-zinc-600 hover:text-zinc-400"
              }`}
              title="Claude Vesper Chat"
            >
              <MessageSquare className="h-5 w-5" />
            </button>
            <button
              onClick={() => setRightPanel("knobs")}
              className={`p-2 rounded-lg transition-all ${
                rightPanel === "knobs" ? "bg-zinc-900 text-amber-500 border border-zinc-800" : "text-zinc-600 hover:text-zinc-400"
              }`}
              title="Render Parameters"
            >
              <Sliders className="h-5 w-5" />
            </button>
            <button
              onClick={() => setMobileRightPanelOpen(false)}
              className="lg:hidden p-2 rounded-lg text-zinc-500 hover:text-zinc-200 mt-auto"
              title="Close Assistant"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          <div className="flex h-full w-[300px] sm:w-[360px]">
            {rightPanel === "vesper" ? (
              <VesperChat
                channel={project.channel}
                onDraftStoryboard={handleDraftStoryboard}
                onScriptFromChat={handleScriptFromChat}
                fetchBudgetPlan={fetchBudgetPlan}
                onLockScript={handleLockScript}
                chatHistory={chatHistory}
                onSendChatMessage={handleSendChatMessage}
                scriptLocked={project.script_locked}
              />
            ) : (
              <KnobsSidebar
                config={project.render}
                imageBackends={image_backends}
                videoBackends={video_backends}
                onSave={handleSaveKnobs}
                onUploadGlobalRef={handleUploadGlobalRef}
                onClearGlobalRef={handleClearGlobalRef}
                mediaUrl={mediaUrl}
              />
            )}
          </div>
        </div>

      </div>
      {/* Voice Studio Modal */}
      <Lightbox
        state={lightbox}
        mediaUrl={mediaUrl}
        onClose={() => setLightbox(null)}
        onIndex={(i) => setLightbox((s) => (s ? { ...s, index: i } : s))}
        onChoose={async (sceneId, index) => {
          await handleSelectVariation(sceneId, index);
          // Reflect the new choice without closing — you often want to keep
          // comparing after picking.
          setLightbox((s) => (s ? { ...s, chosen: index } : s));
        }}
        onDelete={(sceneId, index) => handleDeleteImage(sceneId, index)}
      />

      <VoiceStudioModal
        isOpen={voiceStudioOpen}
        onClose={() => setVoiceStudioOpen(false)}
        post={post}
        mediaUrl={mediaUrl}
      />
    </div>
  );
}
