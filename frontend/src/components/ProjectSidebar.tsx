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
    <aside className="w-80 bg-zinc-950/80 border-r border-zinc-900 flex flex-col h-full shrink-0">
      {/* Top Channel Selector */}
      <div className="p-4 border-b border-zinc-900 flex flex-col gap-3">
        <label className="text-[10px] uppercase tracking-wider text-zinc-500 font-bold font-mono">
          Active Channel Scope
        </label>
        <div className="grid grid-cols-2 gap-2 bg-zinc-900/50 p-1 rounded-lg border border-zinc-800/80">
          <button
            onClick={() => setActiveChannel("bestiary")}
            className={`py-2 px-3 rounded-md text-xs font-semibold transition-all duration-300 ${
              activeChannel === "bestiary"
                ? "bg-amber-500 text-zinc-950 font-bold shadow-md shadow-amber-500/10"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            Bestiary
          </button>
          <button
            onClick={() => setActiveChannel("calluses")}
            className={`py-2 px-3 rounded-md text-xs font-semibold transition-all duration-300 ${
              activeChannel === "calluses"
                ? "bg-blue-500 text-white font-bold shadow-md shadow-blue-500/10"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            Calluses
          </button>
        </div>
        <button
          onClick={handleNewProject}
          className="w-full bg-zinc-900 hover:bg-zinc-800 text-zinc-200 py-2 px-4 rounded-lg text-xs font-bold transition flex items-center justify-center gap-1.5 border border-zinc-800"
        >
          <Plus className="h-3.5 w-3.5" />
          <span>New Storyboard</span>
        </button>
      </div>

      {/* Projects List */}
      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-4">
        
        {/* Bestiary Accordion */}
        <div className="flex flex-col">
          <button
            onClick={() => setBestiaryOpen(!bestiaryOpen)}
            className="flex items-center justify-between text-[10px] font-bold text-amber-500/90 uppercase tracking-wider py-2 px-2 hover:bg-zinc-900/20 rounded transition"
          >
            <span className="flex items-center gap-2">
              {bestiaryOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              The Illuminated Bestiary
            </span>
            <span className="bg-amber-500/10 text-amber-400 text-[9px] px-1.5 py-0.5 rounded font-mono border border-amber-500/20">
              {bestiaryProjects.length}
            </span>
          </button>
          
          {bestiaryOpen && (
            <div className="flex flex-col gap-1.5 mt-2 pl-2">
              {bestiaryProjects.map((p, idx) => (
                <div
                  key={idx}
                  onClick={() => onSelectProject(p.rel)}
                  className={`p-3 rounded-lg border cursor-pointer transition-all duration-300 ${
                    p.active
                      ? "bg-amber-500/5 border-amber-500/30 text-amber-400 font-medium shadow-[0_0_15px_rgba(245,158,11,0.03)]"
                      : "border-transparent text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/40"
                  }`}
                >
                  <div className="text-xs font-semibold truncate flex items-center justify-between gap-2">
                    <span className="truncate">{p.name}</span>
                    <div className="flex items-center gap-1 shrink-0">
                      {p.script_locked && <Lock className="h-3 w-3 text-emerald-400" />}
                      {p.storyboard_approved && <CheckCircle className="h-3 w-3 text-emerald-400" />}
                    </div>
                  </div>
                  <div className="text-[9px] text-zinc-550 truncate font-mono mt-1 flex items-center justify-between">
                    <span>{p.rel_display}</span>
                    <span className="text-amber-500/60 font-semibold">{p.beats_count || 0} beats</span>
                  </div>
                </div>
              ))}
              {bestiaryProjects.length === 0 && (
                <span className="text-zinc-650 text-xs italic p-2 pl-4">No storyboards</span>
              )}
            </div>
          )}
        </div>

        {/* Calluses Accordion */}
        <div className="flex flex-col">
          <button
            onClick={() => setCallusesOpen(!callusesOpen)}
            className="flex items-center justify-between text-[10px] font-bold text-blue-400/90 uppercase tracking-wider py-2 px-2 hover:bg-zinc-900/20 rounded transition"
          >
            <span className="flex items-center gap-2">
              {callusesOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              By the Calluses
            </span>
            <span className="bg-blue-500/10 text-blue-400 text-[9px] px-1.5 py-0.5 rounded font-mono border border-blue-500/20">
              {callusesProjects.length}
            </span>
          </button>
          
          {callusesOpen && (
            <div className="flex flex-col gap-1.5 mt-2 pl-2">
              {callusesProjects.map((p, idx) => (
                <div
                  key={idx}
                  onClick={() => onSelectProject(p.rel)}
                  className={`p-3 rounded-lg border cursor-pointer transition-all duration-300 ${
                    p.active
                      ? "bg-blue-500/5 border-blue-500/30 text-blue-400 font-medium shadow-[0_0_15px_rgba(59,130,246,0.03)]"
                      : "border-transparent text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/40"
                  }`}
                >
                  <div className="text-xs font-semibold truncate flex items-center justify-between gap-2">
                    <span className="truncate">{p.name}</span>
                    <div className="flex items-center gap-1 shrink-0">
                      {p.script_locked && <Lock className="h-3 w-3 text-emerald-400" />}
                      {p.storyboard_approved && <CheckCircle className="h-3 w-3 text-emerald-400" />}
                    </div>
                  </div>
                  <div className="text-[9px] text-zinc-550 truncate font-mono mt-1 flex items-center justify-between">
                    <span>{p.rel_display}</span>
                    <span className="text-blue-500/60 font-semibold">{p.beats_count || 0} beats</span>
                  </div>
                </div>
              ))}
              {callusesProjects.length === 0 && (
                <span className="text-zinc-650 text-xs italic p-2 pl-4">No storyboards</span>
              )}
            </div>
          )}
        </div>

      </div>
    </aside>
  );
}
