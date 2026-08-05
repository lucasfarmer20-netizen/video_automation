"use client";

import React, { useState } from "react";
import { Folder, Film, CheckCircle, Lock, Plus, ChevronDown, ChevronRight, Eye } from "lucide-react";

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

interface ProjectSidebarProps {
  projects: Project[];
  activeProjectId: string | null;
  onSelectProject: (rel: string) => void;
  onCreateProject: (name: string, channel: string) => void;
  activeChannel: "bestiary" | "calluses";
  setActiveChannel: (channel: "bestiary" | "calluses") => void;
}

export default function ProjectSidebar({
  projects,
  activeProjectId,
  onSelectProject,
  onCreateProject,
  activeChannel,
  setActiveChannel
}: ProjectSidebarProps) {
  const [bestiaryOpen, setBestiaryOpen] = useState(true);
  const [callusesOpen, setCallusesOpen] = useState(true);

  const bestiaryProjects = projects.filter(p => p.channel === "bestiary");
  const callusesProjects = projects.filter(p => p.channel === "calluses");

  const handleNewProject = () => {
    const name = prompt("Enter a name for the new storyboard / project:");
    if (!name) return;
    onCreateProject(name, activeChannel);
  };

  return (
    <aside className="w-80 bg-zinc-950/85 backdrop-blur-md border-r border-zinc-800/80 flex flex-col h-full shrink-0 shadow-2xl z-30">
      {/* Top Channel Selector */}
      <div className="p-4 border-b border-zinc-900/90 flex flex-col gap-3">
        <label className="text-[10px] uppercase tracking-wider text-zinc-400 font-bold font-mono">
          Active Channel Scope
        </label>
        <div className="grid grid-cols-2 gap-1.5 bg-zinc-900/80 p-1 rounded-xl border border-zinc-800 shadow-inner">
          <button
            onClick={() => setActiveChannel("bestiary")}
            className={`py-2 px-3 rounded-lg text-xs font-extrabold transition-all duration-200 ${
              activeChannel === "bestiary"
                ? "bg-gradient-to-r from-amber-400 via-amber-500 to-orange-500 text-zinc-950 shadow-[0_0_12px_rgba(245,158,11,0.3)]"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            Bestiary
          </button>
          <button
            onClick={() => setActiveChannel("calluses")}
            className={`py-2 px-3 rounded-lg text-xs font-extrabold transition-all duration-200 ${
              activeChannel === "calluses"
                ? "bg-gradient-to-r from-blue-500 to-indigo-600 text-white shadow-[0_0_12px_rgba(59,130,246,0.3)]"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            Calluses
          </button>
        </div>
        <button
          onClick={handleNewProject}
          className="w-full bg-zinc-900/90 hover:bg-zinc-800 text-zinc-200 hover:text-white py-2.5 px-4 rounded-xl text-xs font-bold transition-all hover:scale-[1.02] active:scale-[0.98] flex items-center justify-center gap-2 border border-zinc-800 shadow-sm"
        >
          <Plus className="h-4 w-4 text-amber-400" />
          <span>New Storyboard</span>
        </button>
      </div>

      {/* Projects List */}
      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
        
        {/* Bestiary Accordion */}
        <div className="flex flex-col">
          <button
            onClick={() => setBestiaryOpen(!bestiaryOpen)}
            className="flex items-center justify-between text-[10px] font-extrabold text-amber-400/90 uppercase tracking-wider py-2 px-2.5 hover:bg-zinc-900/40 rounded-lg transition-colors"
          >
            <span className="flex items-center gap-2">
              {bestiaryOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              The Illuminated Bestiary
            </span>
            <span className="bg-amber-500/15 text-amber-300 text-[9px] px-2 py-0.5 rounded-full font-mono border border-amber-500/30 font-bold">
              {bestiaryProjects.length}
            </span>
          </button>
          
          {bestiaryOpen && (
            <div className="flex flex-col gap-1.5 mt-2 pl-2">
              {bestiaryProjects.map((p, idx) => (
                <div
                  key={idx}
                  onClick={() => onSelectProject(p.rel)}
                  className={`p-3 rounded-xl border cursor-pointer transition-all duration-200 hover:scale-[1.01] ${
                    p.active
                      ? "bg-amber-500/10 border-amber-400/60 text-amber-300 font-semibold shadow-[0_0_15px_rgba(245,158,11,0.15)]"
                      : "border-zinc-900/60 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60 hover:border-zinc-800"
                  }`}
                >
                  <div className="text-xs font-bold truncate flex items-center justify-between gap-2">
                    <span className="truncate">{p.name}</span>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {p.script_locked && <Lock className="h-3 w-3 text-emerald-400 drop-shadow" />}
                      {p.storyboard_approved && <CheckCircle className="h-3 w-3 text-emerald-400 drop-shadow" />}
                    </div>
                  </div>
                  <div className="text-[9px] text-zinc-500 truncate font-mono mt-1.5 flex items-center justify-between">
                    <span className="truncate pr-2">{p.rel_display}</span>
                    <span className="text-amber-400/80 font-bold shrink-0">{p.beats_count || 0} beats</span>
                  </div>
                </div>
              ))}
              {bestiaryProjects.length === 0 && (
                <span className="text-zinc-600 text-xs italic p-2 pl-4">No storyboards</span>
              )}
            </div>
          )}
        </div>

        {/* Calluses Accordion */}
        <div className="flex flex-col">
          <button
            onClick={() => setCallusesOpen(!callusesOpen)}
            className="flex items-center justify-between text-[10px] font-extrabold text-blue-400/90 uppercase tracking-wider py-2 px-2.5 hover:bg-zinc-900/40 rounded-lg transition-colors"
          >
            <span className="flex items-center gap-2">
              {callusesOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              By the Calluses
            </span>
            <span className="bg-blue-500/15 text-blue-300 text-[9px] px-2 py-0.5 rounded-full font-mono border border-blue-500/30 font-bold">
              {callusesProjects.length}
            </span>
          </button>
          
          {callusesOpen && (
            <div className="flex flex-col gap-1.5 mt-2 pl-2">
              {callusesProjects.map((p, idx) => (
                <div
                  key={idx}
                  onClick={() => onSelectProject(p.rel)}
                  className={`p-3 rounded-xl border cursor-pointer transition-all duration-200 hover:scale-[1.01] ${
                    p.active
                      ? "bg-blue-500/10 border-blue-400/60 text-blue-300 font-semibold shadow-[0_0_15px_rgba(59,130,246,0.15)]"
                      : "border-zinc-900/60 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60 hover:border-zinc-800"
                  }`}
                >
                  <div className="text-xs font-bold truncate flex items-center justify-between gap-2">
                    <span className="truncate">{p.name}</span>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {p.script_locked && <Lock className="h-3 w-3 text-emerald-400 drop-shadow" />}
                      {p.storyboard_approved && <CheckCircle className="h-3 w-3 text-emerald-400 drop-shadow" />}
                    </div>
                  </div>
                  <div className="text-[9px] text-zinc-500 truncate font-mono mt-1.5 flex items-center justify-between">
                    <span className="truncate pr-2">{p.rel_display}</span>
                    <span className="text-blue-400/80 font-bold shrink-0">{p.beats_count || 0} beats</span>
                  </div>
                </div>
              ))}
              {callusesProjects.length === 0 && (
                <span className="text-zinc-600 text-xs italic p-2 pl-4">No storyboards</span>
              )}
            </div>
          )}
        </div>

      </div>
    </aside>
  );
}
