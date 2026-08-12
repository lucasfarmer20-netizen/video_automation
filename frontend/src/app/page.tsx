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
import { MOCK_SCENES , setActiveProjectId } from "../lib/directorApi";

// Setup API URL mapping
const API_BASE = typeof window !== "undefined"
  ? (window.location.hostname === "localhost" ? "http://localhost:5000" : "")
  : "";

interface Project {
  name: string;
  rel: string;
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

export default function WorkspacePage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState<any | null>(null);
  const [activeChannel, setActiveChannel] = useState<"bestiary" | "calluses">("bestiary");
  
  // View states
  const [activeView, setActiveView] = useState<"grid" | "canvas">("grid");
  const [selectedSceneId, setSelectedSceneId] = useState<string>("s004");
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
  const [dismissedErrors, setDismissedErrors] = useState<Record<string, string>>({});
  const [chatHistory, setChatHistory] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [peaks, setPeaks] = useState<Record<string, any>>({});
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
    if (action === "build:draft1") { await post("/api/assemble/rough_cut"); return; }
    if (action === "export:master") { await post("/api/assemble/timeline"); return; }
  };

  const fetchActiveProject = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/project/active`, { headers: authHeaders() });
      if (isStaleReply(res)) return;
      const data = await res.json();
      if (data.ok) {
        // Adopt the server's id for this project; every later request names it
        // explicitly instead of relying on the shared active-project pointer.
        if (data.project_id) {
          projectIdRef.current = data.project_id;
          setActiveProjectId(data.project_id);  // director calls target it too
        }
        setActiveProject(data);
        setActiveChannel(data.project.channel);
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
    } catch (e) {
      console.error("Failed to load active project details", e);
    } finally {
      setLoading(false);
    }
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

  useEffect(() => {
    fetchProjects();
    fetchActiveProject();
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
      if (parsed.error || parsed.detail) errMsg = parsed.error || parsed.detail;
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
  const handleSelectProject = async (rel: string, projectId?: string) => {
    setLoading(true);
    // Retarget first. Any reply still in flight for the previous project now
    // fails the staleness check instead of repainting the studio.
    if (projectId) {
      projectIdRef.current = projectId;
      setActiveProjectId(projectId);
    }
    const data = await post("/api/project/select", { rel });
    if (data.ok) {
      await fetchActiveProject();
      await fetchProjects();
    } else {
      alert("Failed to load storyboard project");
      setLoading(false);
    }
  };

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
    if (res.ok) {
      const mb = (res.bytes / 1048576).toFixed(1);
      alert(`Deleted "${res.deleted}" (${mb} MB).
Moved to: ${res.moved_to}`);
      fetchProjects();
      fetchActiveProject();
    }
  };

  const handleCreateProject = async (name: string, channel: string) => {
    setLoading(true);
    const data = await post("/api/project/new", { name, channel });
    if (data.ok) {
      await fetchActiveProject();
      await fetchProjects();
    } else {
      alert("Failed to create project");
      setLoading(false);
    }
  };

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

  const handleGenerateVideo = async (sceneId: string, btn: HTMLButtonElement) => {
    if (!confirm("🎬 PAID: Video generation calls fal.ai. Continue?")) return;
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = "Generating...";
    const data = await post(`/api/shot/${sceneId}/generate_video`);
    btn.disabled = false;
    btn.textContent = oldText;
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
    const data = await post(`/api/assemble/${stage}`);
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
            onSelectProject={(rel) => {
              handleSelectProject(rel);
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
            onBuild={async () => { await post("/api/assemble/rough_cut"); }}
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
                  if (beatList.length > 0) setSelectedSceneId(beatList[0]);
                }}
              />
              <FilmOverviewPanel
                scenes={MOCK_SCENES}
                activeSceneId={selectedSceneId}
                onSelectScene={(scId) => setSelectedSceneId(scId)}
              />
              <DirectorWorkspace
                sceneId={selectedSceneId}
                activeProjectTitle={project.title || "Active"}
                mediaUrl={mediaUrl}
                onBackToStoryboard={() => setActiveStage("script")}
              />
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
