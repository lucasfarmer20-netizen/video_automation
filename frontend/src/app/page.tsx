"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  Folder,
  Sliders,
  MessageSquare,
  LayoutGrid,
  GitBranch,
  Volume2,
  Tv,
  CheckCircle,
  Clock,
  Play,
  RotateCcw,
  Sparkles
} from "lucide-react";

// Components
import ProjectSidebar from "../components/ProjectSidebar";
import KnobsSidebar from "../components/KnobsSidebar";
import VesperChat from "../components/VesperChat";
import BeatCard from "../components/BeatCard";
import FlowCanvas from "../components/FlowCanvas";
import VoiceStudioModal from "../components/VoiceStudioModal";

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
  const [rightPanel, setRightPanel] = useState<"vesper" | "knobs">("vesper");
  
  // Background task state
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [chatHistory, setChatHistory] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [voiceStudioOpen, setVoiceStudioOpen] = useState(false);

  // Helper: media url resolver
  const mediaUrl = useCallback((path: string) => {
    if (!path) return "";
    if (path.startsWith("http://") || path.startsWith("https://")) return path;
    const clean = path.replace("\\", "/").replace(/^\/+/, "");
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
  const fetchActiveProject = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/project/active`);
      const data = await res.json();
      if (data.ok) {
        setActiveProject(data);
        setActiveChannel(data.project.channel);
      }
    } catch (e) {
      console.error("Failed to load active project details", e);
    } finally {
      setLoading(false);
    }
  };

  // Poll assembly job status
  const pollJobs = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/assemble/status`);
      const data = await res.json();
      if (data.ok) {
        setJobs(data.jobs);
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

  // Post helper
  const post = async (url: string, body: any = {}) => {
    const res = await fetch(`${API_BASE}${url}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    return res.json();
  };

  // Action handlers
  const handleSelectProject = async (rel: string) => {
    setLoading(true);
    const data = await post("/api/project/select", { rel });
    if (data.ok) {
      await fetchActiveProject();
      await fetchProjects();
    } else {
      alert("Failed to load storyboard project");
      setLoading(false);
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
            return { ...s, [field]: value };
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
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${API_BASE}/api/render/reference`, { method: "POST", body: fd });
    const data = await res.json();
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
    } else {
      alert("Video generation failed: " + (data.error || "unknown error"));
    }
  };

  const handleUploadImage = async (sceneId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${API_BASE}/api/shot/${sceneId}/image`, { method: "POST", body: fd });
    const data = await res.json();
    if (data.ok) {
      fetchActiveProject();
    } else {
      alert("Upload failed: " + (data.error || "unknown error"));
    }
  };

  const handleUploadClip = async (sceneId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${API_BASE}/api/shot/${sceneId}/clip`, { method: "POST", body: fd });
    const data = await res.json();
    if (data.ok) {
      fetchActiveProject();
    } else {
      alert("Clip upload failed: " + (data.error || "unknown error"));
    }
  };

  const handleAddReference = async (sceneId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${API_BASE}/api/shot/${sceneId}/reference`, { method: "POST", body: fd });
    const data = await res.json();
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
    const res = await fetch(`${API_BASE}/api/shot/${sceneId}/delete_image/${idx}`, { method: "POST" });
    const data = await res.json();
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
    const res = await fetch(`${API_BASE}/api/shot/${sceneId}/delete_video/${idx}`, { method: "POST" });
    const data = await res.json();
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

  const handleDraftStoryboard = async (topic: string, beats: number | null) => {
    setLoading(true);
    const data = await post("/api/script/generate", { topic, beats, channel: activeChannel });
    if (data.ok) {
      await fetchActiveProject();
    } else {
      alert("Draft failed: " + (data.error || "unknown error"));
      setLoading(false);
    }
  };

  const handleScriptFromChat = async (messages: any[], beats: number | null) => {
    setLoading(true);
    const data = await post("/api/script/from_chat", { messages, beats, channel: activeChannel });
    if (data.ok) {
      await fetchActiveProject();
    } else {
      alert("Scripting failed: " + (data.error || "unknown error"));
      setLoading(false);
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

  if (loading || !activeProject) {
    return (
      <div className="min-h-screen bg-zinc-950 flex flex-col items-center justify-center text-zinc-400 gap-4 font-mono select-none">
        <div className="w-10 h-10 border-4 border-amber-500 border-t-transparent rounded-full animate-spin"></div>
        <span className="text-xs uppercase tracking-widest text-zinc-500">Loading Workspace...</span>
      </div>
    );
  }

  const { project, preview_url, fcpxml_ready, ep_slug, paid_count, image_backends, video_backends, Tiers } = activeProject;

  return (
    <div className="min-h-screen bg-zinc-950 flex flex-col overflow-hidden text-zinc-100">
      
      {/* Floating Header */}
      <header className="sticky top-0 z-40 bg-zinc-900/60 backdrop-blur-xl border-b border-zinc-900 px-6 py-4 flex flex-col md:flex-row items-center justify-between gap-4">
        <div className="flex flex-wrap items-center gap-4 w-full md:w-auto">
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
            className="bg-zinc-950/80 text-amber-400 font-extrabold px-3 py-1.5 rounded-lg text-md border border-zinc-850 focus:outline-none focus:border-amber-400 w-full sm:w-64 md:w-80 transition"
            placeholder="Project Title"
          />

          <div className="flex items-center gap-1.5 bg-zinc-950/60 px-3 py-1.5 rounded-lg border border-zinc-900 text-xs font-mono text-zinc-400 select-none shadow-inner">
            <span className="text-amber-500 font-bold">{project.shots?.length || 0}</span> beats · 
            <span className="text-amber-500 font-bold">{paid_count}</span> Tier-C · 
            <span className="text-zinc-500 max-w-[200px] truncate">{project.cultural_origin || "no cultural scope set"}</span> · 
            <span className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${project.script_locked ? "bg-emerald-500 shadow-[0_0_8px_#10b981]" : "bg-amber-500 shadow-[0_0_8px_#f59e0b]"}`}></span>
              <span>{project.script_locked ? "locked" : "draft"}</span>
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3 justify-end w-full md:w-auto">
          {/* View toggle */}
          <div className="flex items-center bg-zinc-950 p-1 rounded-lg border border-zinc-850">
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

          <button
            onClick={() => setVoiceStudioOpen(true)}
            className="bg-zinc-900 hover:bg-zinc-850 border border-zinc-800 text-amber-400 font-bold px-3 py-1.5 rounded-lg transition text-xs flex items-center gap-1.5 shadow-sm"
          >
            <Volume2 className="w-3.5 h-3.5 text-amber-500" />
            <span>Voice Studio</span>
          </button>
          
          <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-mono font-bold select-none ${
            project.storyboard_approved
              ? "border-emerald-500/20 bg-emerald-950/20 text-emerald-400 shadow-[0_0_15px_rgba(16,185,129,0.05)]"
              : "border-zinc-850 bg-zinc-900/40 text-zinc-550"
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${project.storyboard_approved ? "bg-emerald-500 shadow-[0_0_8px_#10b981]" : "bg-zinc-650"}`}></span>
            {project.storyboard_approved ? "Approved ✓" : "Draft"}
          </div>
          <button
            onClick={handleApproveStoryboard}
            className="bg-amber-500 hover:bg-amber-600 text-zinc-950 font-bold px-4 py-2 rounded-lg transition text-xs shadow-md shadow-amber-500/10 active:scale-95"
          >
            Approve storyboard →
          </button>
        </div>
      </header>

      {/* Main Grid Wrapper */}
      <div className="flex flex-1 overflow-hidden">
        
        {/* Left projects navigation sidebar */}
        <ProjectSidebar
          projects={projects}
          activeProjectId={project.id}
          onSelectProject={handleSelectProject}
          onCreateProject={handleCreateProject}
          activeChannel={activeChannel}
          setActiveChannel={setActiveChannel}
        />

        {/* Central timeline editor */}
        <main className="flex-1 overflow-y-auto p-6 flex flex-col gap-6">
          
          {/* Assemble control board */}
          {project.storyboard_approved && (
            <section className="glass-panel rounded-xl p-5 border border-zinc-900 flex flex-col gap-4">
              <h3 className="text-emerald-400 font-bold text-sm mb-1">🎬 Assembling Timeline Proxy</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                <div className="flex flex-col gap-3">
                  {/* Step 1: Voiceover */}
                  <div className="flex items-center justify-between bg-zinc-950/50 p-3 rounded-lg border border-zinc-900">
                    <button
                      onClick={() => handleAssemble("narration")}
                      disabled={jobs["narration"]?.status === "running"}
                      className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-200 px-3.5 py-2 rounded-lg text-xs font-bold transition flex items-center gap-1.5 disabled:opacity-50"
                    >
                      <Volume2 className="h-4 w-4 text-amber-500" />
                      <span>1 · Generate voiceover</span>
                    </button>
                    <span className={`text-xs font-mono font-semibold ${
                      jobs["narration"]?.status === "done" ? "text-emerald-500" : "text-zinc-550"
                    }`}>
                      {jobs["narration"]?.status || "Idle"}
                    </span>
                  </div>

                  {/* Step 3: Preview */}
                  <div className="flex items-center justify-between bg-zinc-950/50 p-3 rounded-lg border border-zinc-900">
                    <button
                      onClick={() => handleAssemble("preview")}
                      disabled={jobs["preview"]?.status === "running"}
                      className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-200 px-3.5 py-2 rounded-lg text-xs font-bold transition flex items-center gap-1.5 disabled:opacity-50"
                    >
                      <Tv className="h-4 w-4 text-amber-500" />
                      <span>3 · Build preview (rough cut)</span>
                    </button>
                    <span className={`text-xs font-mono font-semibold ${
                      jobs["preview"]?.status === "done" ? "text-emerald-500" : "text-zinc-550"
                    }`}>
                      {jobs["preview"]?.status || "Idle"}
                    </span>
                  </div>

                  {/* Step 4: Resolve xml */}
                  <div className="flex items-center justify-between bg-zinc-950/50 p-3 rounded-lg border border-zinc-900">
                    <button
                      onClick={() => handleAssemble("timeline")}
                      disabled={jobs["timeline"]?.status === "running"}
                      className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 text-zinc-200 px-3.5 py-2 rounded-lg text-xs font-bold transition flex items-center gap-1.5 disabled:opacity-50"
                    >
                      <CheckCircle className="h-4 w-4 text-amber-500" />
                      <span>4 · Export Resolve timeline</span>
                    </button>
                    <span className={`text-xs font-mono font-semibold ${
                      jobs["timeline"]?.status === "done" ? "text-emerald-500" : "text-zinc-550"
                    }`}>
                      {jobs["timeline"]?.status || "Idle"}
                    </span>
                  </div>
                </div>

                <div className="bg-zinc-950/40 p-4 border border-zinc-900 rounded-lg flex flex-col gap-3">
                  <div className="text-[10px] font-bold text-zinc-550 uppercase tracking-wider font-mono">
                    2 · Video Renders &amp; Ingest
                  </div>
                  
                  <div className="flex items-center justify-between bg-zinc-950/70 p-2.5 rounded-lg border border-zinc-900">
                    <button
                      onClick={() => handleAssemble("render")}
                      disabled={jobs["render"]?.status === "running"}
                      className="bg-amber-500 hover:bg-amber-600 text-zinc-950 px-3.5 py-2 rounded-lg text-xs font-bold transition flex items-center gap-1.5 disabled:opacity-50"
                    >
                      <Play className="h-3.5 w-3.5 fill-current" />
                      <span>Render pipeline via fal.ai</span>
                    </button>
                    <span className={`text-xs font-mono font-semibold ${
                      jobs["render"]?.status === "done" ? "text-emerald-500" : "text-zinc-550"
                    }`}>
                      {jobs["render"]?.status || "Idle"}
                    </span>
                  </div>

                  <div className="border-t border-zinc-900 pt-3 flex flex-col gap-2">
                    <span className="text-[9px] uppercase tracking-wider text-zinc-500 font-bold font-mono">
                      Ingest local bypass (Option B)
                    </span>
                    <div className="flex gap-2">
                      <select id="bypass_opt_select" className="bg-zinc-900 text-zinc-300 text-xs rounded border border-zinc-800 px-2 py-1 flex-1">
                        {project.shots?.map((s: any) => (
                          <option key={s.scene_id} value={s.scene_id}>{s.scene_id}</option>
                        ))}
                      </select>
                      <button
                        onClick={() => {
                          const sid = (document.getElementById("bypass_opt_select") as HTMLSelectElement)?.value;
                          const fileInput = document.createElement("input");
                          fileInput.type = "file";
                          fileInput.accept = "image/*,video/*";
                          fileInput.onchange = (e) => {
                            const file = (e.target as HTMLInputElement).files?.[0];
                            if (file && sid) {
                              if (file.type.startsWith("image/")) handleUploadImage(sid, file);
                              else if (file.type.startsWith("video/")) handleUploadClip(sid, file);
                            }
                          };
                          fileInput.click();
                        }}
                        className="bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs font-bold border border-zinc-750 px-3 py-1.5 rounded"
                      >
                        Ingest File
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              {/* Preview player */}
              {preview_url && (
                <div className="border-t border-zinc-900 pt-4 mt-2">
                  <video controls className="w-full max-h-80 bg-black border border-zinc-900 rounded-lg shadow-2xl" src={mediaUrl(preview_url)} />
                  <div className="text-[10px] text-zinc-550 font-mono mt-2 flex justify-between select-none">
                    <span>Narration + Music synced Proxy track preview</span>
                    {fcpxml_ready && (
                      <span className="text-amber-500 font-semibold animate-pulse">
                        ▶ Timeline compiled! Import {ep_slug}.fcpxml into DaVinci Resolve
                      </span>
                    )}
                  </div>
                </div>
              )}
            </section>
          )}

          {/* Workflow Graph View (React Flow) */}
          {activeView === "canvas" ? (
            <div className="h-[550px] w-full shrink-0">
              <FlowCanvas
                shots={project.shots}
                mediaUrl={mediaUrl}
                onUpdateDuration={(sceneId, dur) => handleUpdateField(sceneId, "camera", { move: "push_in", duration: dur, speed: 1.0 })}
                onRegenerate={(sceneId) => handleRegenStill(sceneId)}
                onGenerateSFX={async (sceneId) => {
                  await post(`/api/audio/sfx/${sceneId}`);
                  fetchActiveProject();
                }}
              />
            </div>
          ) : (
            /* Storyboard Timeline Cards Grid */
            <div className="flex flex-col gap-6 relative">
              {project.shots?.map((shot: any) => (
                <BeatCard
                  key={shot.scene_id}
                  shot={shot}
                  videoBackends={video_backends}
                  tiers={activeProject.tiers}
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
                  onSelectVideoVariation={handleSelectVideoVariation}
                  onSendShotChat={handleSendShotChat}
                  onApplyRefinedPrompts={handleApplyRefinedPrompts}
                  mediaUrl={mediaUrl}
                />
              ))}
            </div>
          )}
        </main>

        {/* Right workspace drawer (Vesper Assistant + Generator & knobs toggle) */}
        <div className="flex h-full shrink-0 border-l border-zinc-900">
          <div className="bg-zinc-950/90 w-12 border-r border-zinc-900 flex flex-col items-center py-4 gap-4 select-none">
            <button
              onClick={() => setRightPanel("vesper")}
              className={`p-2 rounded-lg transition-all ${
                rightPanel === "vesper" ? "bg-zinc-900 text-amber-500 border border-zinc-800" : "text-zinc-650 hover:text-zinc-400"
              }`}
              title="Claude Vesper Chat"
            >
              <MessageSquare className="h-5 w-5" />
            </button>
            <button
              onClick={() => setRightPanel("knobs")}
              className={`p-2 rounded-lg transition-all ${
                rightPanel === "knobs" ? "bg-zinc-900 text-amber-500 border border-zinc-800" : "text-zinc-650 hover:text-zinc-400"
              }`}
              title="Render Parameters"
            >
              <Sliders className="h-5 w-5" />
            </button>
          </div>

          <div className="flex h-full">
            {rightPanel === "vesper" ? (
              <VesperChat
                channel={project.channel}
                onDraftStoryboard={handleDraftStoryboard}
                onScriptFromChat={handleScriptFromChat}
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
      <VoiceStudioModal
        isOpen={voiceStudioOpen}
        onClose={() => setVoiceStudioOpen(false)}
        post={post}
      />
    </div>
  );
}
