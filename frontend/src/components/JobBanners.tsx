"use client";

import React from "react";

interface Job {
  status: string;
  log: string;
}

interface JobBannersProps {
  jobs: Record<string, Job>;
  /** Jobs in an error state that the user has not dismissed. */
  erroredJobs: [string, Job][];
  onDismissErrors: (entries: [string, Job][]) => void;
}

/** Running-job progress banner and the failure/stack-trace banner.
 *  Presentational only — all job state stays in page.tsx. */
export default function JobBanners({ jobs, erroredJobs, onDismissErrors }: JobBannersProps) {
  const anyRunning = Object.values(jobs).some((j) => j.status === "running");

  return (
    <>
      {anyRunning && (
        <div className="bg-amber-950/30 border border-amber-500/30 rounded-xl p-4 mb-6 shadow-xl backdrop-blur-md relative overflow-hidden animate-pulse">
          <div className="flex items-center justify-between gap-4 mb-2">
            <div className="flex items-center gap-3">
              <div className="w-4 h-4 border-2 border-amber-500 border-t-transparent rounded-full animate-spin shrink-0"></div>
              <span className="text-sm font-bold text-amber-400 font-mono tracking-wide">
                {jobs.script_draft?.status === "running"
                  ? "✨ Vesper AI is drafting your documentary storyboard..."
                  : jobs.drafts?.status === "running"
                  ? "🖼 Generating draft stills for every beat..."
                  : jobs.render?.status === "running"
                  ? "🎨 Rendering stills & motion clips in background..."
                  : jobs.motion_preview?.status === "running"
                  ? "🎥 Re-rendering a single beat with the current motion settings..."
                  : jobs.narration?.status === "running"
                  ? "🎙 Synthesizing AI narration audio tracks..."
                  : jobs.preview?.status === "running"
                  ? "🎞 Building the preview cut (video + narration + sfx + music)..."
                  : "⚙ Processing pipeline background job..."}
              </span>
            </div>
            <span className="text-[10px] font-mono uppercase tracking-widest bg-amber-500/10 text-amber-400 border border-amber-500/20 px-2.5 py-1 rounded-full">
              Active Process
            </span>
          </div>

          <div className="w-full bg-zinc-900 rounded-full h-2 overflow-hidden border border-amber-500/20 mb-2">
            <div className="bg-gradient-to-r from-amber-500 via-amber-400 to-amber-600 h-full rounded-full animate-pulse w-full"></div>
          </div>

          {Object.entries(jobs).map(([stage, job]) => job.status === "running" && job.log && (
            <div key={stage} className="text-[11px] font-mono text-zinc-400 truncate bg-zinc-950/60 px-3 py-1.5 rounded-lg border border-zinc-900">
              <span className="text-amber-500 font-bold">[{stage.toUpperCase()}]</span> {job.log.trim().split("\n").pop()}
            </div>
          ))}
        </div>
      )}

      {erroredJobs.length > 0 && (
        <div className="bg-red-950/50 border border-red-500/50 rounded-xl p-4 mb-6 shadow-2xl backdrop-blur-md relative overflow-hidden">
          <div className="flex items-center justify-between gap-4 mb-3">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-full bg-red-500/20 border border-red-500/40 text-red-400 font-bold flex items-center justify-center text-sm shrink-0">
                ⚠️
              </div>
              <div>
                <h4 className="text-sm font-bold text-red-400 font-mono tracking-wide">
                  Pipeline Execution Warning / Failure Log
                </h4>
                <p className="text-xs text-red-300/80 font-mono">
                  One or more background pipeline tasks encountered an error. Review details below.
                </p>
              </div>
            </div>
            <button
              onClick={() => onDismissErrors(erroredJobs)}
              className="text-xs font-mono uppercase tracking-widest bg-red-500/10 hover:bg-red-500/20 text-red-300 border border-red-500/30 px-3 py-1.5 rounded-lg transition"
            >
              Dismiss Error
            </button>
          </div>

          {erroredJobs.map(([stage, job]) => (
            <div key={stage} className="mt-2 bg-zinc-950 p-3 rounded-lg border border-red-900/40 text-[11px] font-mono text-red-300 space-y-1 overflow-x-auto max-h-60 leading-relaxed shadow-inner">
              <div className="flex items-center justify-between border-b border-red-900/40 pb-1 mb-1 font-bold">
                <span className="text-red-400 uppercase tracking-widest">[{stage.toUpperCase()} STAGE FAILED]</span>
              </div>
              <pre className="whitespace-pre-wrap text-zinc-300 select-text font-mono">
                {job.log || "Stage execution failed with an unhandled exception."}
              </pre>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
