"use client";

import React from "react";
import { Volume2, Tv, CheckCircle, Play, Sparkles } from "lucide-react";
import MixPanel, { MixConfig } from "./MixPanel";
import PreviewPlayer from "./PreviewPlayer";

interface Job { status: string; log: string }

interface AssemblyPanelProps {
  activeStep: 1 | 2 | 3 | 4 | 5;
  project: any;
  jobs: Record<string, Job>;
  canAssemble: boolean;
  missingStills: any[];
  mix: MixConfig | null;
  previewUrl: string | null;
  fcpxmlReady: boolean;
  epSlug: string;
  mediaUrl: (p: string) => string;
  onAssemble: (stage: string) => void;
  onGenerateAllStills: () => void;
  onSaveMix: (m: MixConfig) => Promise<any>;
  onUploadImage: (sceneId: string, f: File) => void;
  onUploadClip: (sceneId: string, f: File) => void;
}

export default function AssemblyPanel({
  activeStep, project, jobs, canAssemble, missingStills, mix,
  previewUrl, fcpxmlReady, epSlug, mediaUrl,
  onAssemble, onGenerateAllStills, onSaveMix, onUploadImage, onUploadClip,
}: AssemblyPanelProps) {
  return (
    <section className="rounded-2xl p-5 glass-panel border border-white/12 shadow-2xl flex flex-col gap-4">
        <h3 className={`font-bold text-sm mb-1 flex items-center gap-2 ${canAssemble ? "text-emerald-400" : "text-zinc-400"}`}>
          <span className={`w-2 h-2 rounded-full ${canAssemble ? "bg-emerald-400 animate-pulse shadow-[0_0_8px_#34d399]" : "bg-zinc-600"}`} />
          🎬 Assembling Timeline Proxy
        </h3>

        {!canAssemble && (
          <div className="bg-amber-950/30 backdrop-blur-md border border-amber-500/40 rounded-xl p-4 text-xs text-amber-200/90 font-mono leading-relaxed flex flex-col gap-3 shadow-md">
            <div>
              <span className="font-bold text-amber-400">Locked.</span>{" "}
              {!project.shots?.length
                ? <>This project has no beats yet. Draft a storyboard with Vesper, or pick another project in the left sidebar.</>
                : missingStills.length
                ? <><span className="text-amber-400 font-bold">{missingStills.length}</span> of {project.shots.length} beats have no draft image. Approval needs every beat illustrated — generate them in one pass below, then <span className="text-amber-400 font-bold">Approve →</span>.</>
                : <>All {project.shots.length} beats are illustrated. Use <span className="text-amber-400 font-bold">Approve →</span> in the header to unlock the assembly pipeline.</>}
            </div>

            {missingStills.length > 0 && (
              <div className="flex items-center justify-between gap-3 border-t border-amber-500/20 pt-3">
                <button
                  onClick={onGenerateAllStills}
                  disabled={jobs["drafts"]?.status === "running"}
                  className="bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500 hover:from-amber-300 hover:to-orange-400 text-zinc-950 px-4 py-2.5 rounded-xl text-xs font-extrabold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center gap-2 disabled:opacity-50 shrink-0 shadow-[0_0_12px_rgba(245,158,11,0.25)]"
                >
                  <Sparkles className="h-4 w-4" />
                  <span>0 · Generate {missingStills.length} draft still{missingStills.length === 1 ? "" : "s"}</span>
                </button>
                <span className={`text-xs font-mono font-bold ${
                  jobs["drafts"]?.status === "done" ? "text-emerald-400" : "text-zinc-500"
                }`}>
                  {jobs["drafts"]?.status || "Idle"}
                </span>
              </div>
            )}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          <div className="flex flex-col gap-3">
            {/* Voiceover — Step 2, Audio Studio */}
            {activeStep === 2 && (
            <div className="flex items-center justify-between bg-zinc-900/60 backdrop-blur-sm p-3.5 rounded-xl border border-zinc-800/80 shadow-sm">
              <button
                onClick={() => onAssemble("narration")}
                disabled={!canAssemble || jobs["narration"]?.status === "running"}
                className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700/80 text-zinc-100 px-4 py-2 rounded-xl text-xs font-bold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center gap-2 disabled:opacity-50 shadow-sm"
              >
                <Volume2 className="h-4 w-4 text-amber-400" />
                <span>1 · Generate voiceover</span>
              </button>
              <span className={`text-xs font-mono font-bold ${
                jobs["narration"]?.status === "done" ? "text-emerald-400" : "text-zinc-500"
              }`}>
                {jobs["narration"]?.status || "Idle"}
              </span>
            </div>
            )}

            {/* Build preview — Step 5, Final Review */}
            {activeStep === 5 && (
            <div className="flex items-center justify-between bg-zinc-900/60 backdrop-blur-sm p-3.5 rounded-xl border border-zinc-800/80 shadow-sm">
              <button
                onClick={() => onAssemble("preview")}
                disabled={!canAssemble || jobs["preview"]?.status === "running"}
                className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700/80 text-zinc-100 px-4 py-2 rounded-xl text-xs font-bold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center gap-2 disabled:opacity-50 shadow-sm"
              >
                <Tv className="h-4 w-4 text-amber-400" />
                <span>3 · Build preview (rough cut)</span>
              </button>
              <span className={`text-xs font-mono font-bold ${
                jobs["preview"]?.status === "done" ? "text-emerald-400" : "text-zinc-500"
              }`}>
                {jobs["preview"]?.status || "Idle"}
              </span>
            </div>
            )}

            {/* Resolve FCPXML export — Step 5, Final Review */}
            {activeStep === 5 && (
            <div className="flex items-center justify-between bg-zinc-900/60 backdrop-blur-sm p-3.5 rounded-xl border border-zinc-800/80 shadow-sm">
              <button
                onClick={() => onAssemble("timeline")}
                disabled={!canAssemble || jobs["timeline"]?.status === "running"}
                className="bg-zinc-900 hover:bg-zinc-800 border border-zinc-700/80 text-zinc-100 px-4 py-2 rounded-xl text-xs font-bold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center gap-2 disabled:opacity-50 shadow-sm"
              >
                <CheckCircle className="h-4 w-4 text-amber-400" />
                <span>4 · Export Resolve timeline</span>
              </button>
              <span className={`text-xs font-mono font-bold ${
                jobs["timeline"]?.status === "done" ? "text-emerald-400" : "text-zinc-500"
              }`}>
                {jobs["timeline"]?.status || "Idle"}
              </span>
            </div>
            )}

            {/* Track mixer — Step 2 */}
            {activeStep === 2 && (
              <MixPanel
                mix={mix}
                onSave={onSaveMix}
              />
            )}
          </div>

          {activeStep === 4 && (
          <div className="bg-zinc-900/50 backdrop-blur-sm p-4 border border-zinc-800/80 rounded-xl flex flex-col gap-3 shadow-sm">
            <div className="text-[10px] font-bold text-zinc-400 uppercase tracking-wider font-mono">
              Video Renders &amp; Ingest
            </div>
            
            <div className="flex items-center justify-between bg-zinc-950/70 p-3 rounded-xl border border-zinc-800">
              <button
                onClick={() => onAssemble("render")}
                disabled={!canAssemble || jobs["render"]?.status === "running"}
                className="bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500 hover:from-amber-300 hover:to-orange-400 text-zinc-950 px-4 py-2 rounded-xl text-xs font-extrabold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center gap-2 disabled:opacity-50 shadow-[0_0_12px_rgba(245,158,11,0.2)]"
              >
                <Play className="h-3.5 w-3.5 fill-current" />
                <span>Render pipeline via fal.ai</span>
              </button>
              <span className={`text-xs font-mono font-bold ${
                jobs["render"]?.status === "done" ? "text-emerald-400" : "text-zinc-500"
              }`}>
                {jobs["render"]?.status || "Idle"}
              </span>
            </div>

            <div className="border-t border-zinc-800/80 pt-3 flex flex-col gap-2">
              <span className="text-[9px] uppercase tracking-wider text-zinc-400 font-bold font-mono">
                Ingest local bypass (Option B)
              </span>
              <div className="flex gap-2">
                <select id="bypass_opt_select" className="bg-zinc-900 text-zinc-200 text-xs rounded-lg border border-zinc-800 px-2.5 py-1.5 flex-1 focus:border-amber-500 focus:outline-none">
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
                        if (file.type.startsWith("image/")) onUploadImage(sid, file);
                        else if (file.type.startsWith("video/")) onUploadClip(sid, file);
                      }
                    };
                    fileInput.click();
                  }}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-xs font-bold border border-zinc-700 px-3.5 py-1.5 rounded-lg transition-all hover:scale-[1.02] active:scale-[0.98]"
                >
                  Ingest File
                </button>
              </div>
            </div>
          </div>
          )}
        </div>

        {activeStep === 5 && (
        <PreviewPlayer
          previewUrl={previewUrl}
          fcpxmlReady={fcpxmlReady}
          epSlug={epSlug}
          mediaUrl={mediaUrl}
        />
        )}
    </section>
  );
}
