"use client";

import React from "react";
import { ShieldAlert, X, Layers, RefreshCw, AlertTriangle } from "lucide-react";

interface LockedCoverageModalProps {
  isOpen: boolean;
  beatId: string;
  shotsCount: number;
  estimatedCost: number;
  onClose: () => void;
  onRegenerateCoverage: () => void;
  onReplaceWithSingleBeat: () => void;
}

export default function LockedCoverageModal({
  isOpen,
  beatId,
  shotsCount,
  estimatedCost,
  onClose,
  onRegenerateCoverage,
  onReplaceWithSingleBeat,
}: LockedCoverageModalProps) {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-md flex items-center justify-center p-6 animate-in fade-in duration-200">
      <div className="bg-zinc-950 border border-amber-500/40 rounded-2xl w-full max-w-lg overflow-hidden shadow-2xl neon-glow-amber">
        {/* Header */}
        <div className="p-5 border-b border-zinc-800 flex items-center justify-between bg-amber-500/10">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-amber-500/20 border border-amber-500/40 text-amber-400">
              <ShieldAlert className="w-6 h-6" />
            </div>
            <div>
              <h3 className="font-bold text-base text-zinc-100">
                Multi-Shot Coverage Protection
              </h3>
              <p className="text-xs text-amber-300 font-mono mt-0.5">
                Beat {beatId} • {shotsCount} Director Shots (${estimatedCost.toFixed(2)})
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content Body */}
        <div className="p-6 flex flex-col gap-4 text-zinc-300 font-sans text-sm leading-relaxed bg-zinc-950">
          <p>
            This beat uses <strong className="text-amber-400">locked Director coverage</strong>.
          </p>
          <p className="text-zinc-400 text-xs bg-zinc-900/70 border border-zinc-800 p-3 rounded-xl font-mono">
            Regenerating it as a standard single beat will replace the current multi-shot cinematic cut with one still plate.
          </p>
          <p className="text-xs text-zinc-400">
            How would you like to proceed?
          </p>
        </div>

        {/* Action Footer Buttons */}
        <div className="p-4 border-t border-zinc-800 bg-zinc-900/50 flex flex-col sm:flex-row gap-2.5 justify-end">
          <button
            onClick={onClose}
            className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs font-semibold rounded-lg transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              onReplaceWithSingleBeat();
              onClose();
            }}
            className="px-4 py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs font-semibold rounded-lg transition-colors"
          >
            Replace With Single Beat Render
          </button>
          <button
            onClick={() => {
              onRegenerateCoverage();
              onClose();
            }}
            className="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-bold font-mono rounded-lg transition-colors flex items-center justify-center gap-1.5 shadow-lg shadow-amber-500/20"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Regenerate Coverage
          </button>
        </div>
      </div>
    </div>
  );
}
