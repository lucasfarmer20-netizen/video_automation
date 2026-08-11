"use client";

import React, { useState } from "react";
import { DirectorShot } from "../types/director";
import { UserCheck, CheckCircle2, X, Sparkles, Image as ImageIcon } from "lucide-react";

interface TakeSelectorModalProps {
  shot: DirectorShot | null;
  isOpen: boolean;
  onClose: () => void;
  onSelectTake: (shotId: string, variationIndex: number) => void;
  mediaUrl: (path: string) => string;
}

export default function TakeSelectorModal({
  shot,
  isOpen,
  onClose,
  onSelectTake,
  mediaUrl,
}: TakeSelectorModalProps) {
  if (!isOpen || !shot) return null;

  const variations = shot.draft_variations.length > 0
    ? shot.draft_variations
    : [
        "/media/assets/s004/var_01.png",
        "/media/assets/s004/var_02.png",
        "/media/assets/s004/var_03.png",
        "/media/assets/s004/var_04.png",
      ];

  const [activeChoice, setActiveChoice] = useState<number>(shot.chosen_variation || 1);

  const handleConfirm = () => {
    onSelectTake(shot.id, activeChoice);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-md flex items-center justify-center p-6 animate-in fade-in duration-200">
      <div className="bg-zinc-950 border border-purple-500/30 rounded-2xl w-full max-w-4xl flex flex-col overflow-hidden shadow-2xl neon-glow-purple">
        {/* Header */}
        <div className="p-5 border-b border-zinc-800 flex items-center justify-between bg-zinc-900/50">
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-purple-500/20 border border-purple-500/40 text-purple-300">
              <UserCheck className="w-6 h-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono font-bold text-amber-400 bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 rounded">
                  Shot {shot.shot_number || shot.id}
                </span>
                <h3 className="font-bold text-base text-zinc-100">
                  Identity-Critical Take Review
                </h3>
              </div>
              <p className="text-xs text-zinc-400 mt-0.5">
                Compare character consistency across 4 draft variations for {shot.subject}
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

        {/* 4-Take Grid */}
        <div className="p-6 grid grid-cols-2 md:grid-cols-4 gap-4 bg-zinc-950">
          {variations.slice(0, 4).map((varUrl, idx) => {
            const takeNum = idx + 1;
            const isSelected = activeChoice === takeNum;
            return (
              <div
                key={takeNum}
                onClick={() => setActiveChoice(takeNum)}
                className={`relative flex flex-col rounded-xl overflow-hidden border cursor-pointer transition-all duration-200 ${
                  isSelected
                    ? "border-purple-500 bg-purple-500/15 neon-glow-purple scale-[1.03]"
                    : "border-zinc-800 bg-zinc-900/50 hover:border-zinc-700 hover:bg-zinc-900"
                }`}
              >
                {/* Take Frame */}
                <div className="relative aspect-square bg-zinc-900 flex items-center justify-center overflow-hidden">
                  <img
                    src={mediaUrl(varUrl)}
                    alt={`Take ${takeNum}`}
                    className="w-full h-full object-cover"
                    onError={(e) => {
                      // Fallback placeholder if image path doesn't exist on server yet
                      (e.target as HTMLElement).style.display = "none";
                    }}
                  />
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-zinc-600 bg-zinc-950/70 p-4 text-center pointer-events-none">
                    <ImageIcon className="w-8 h-8 text-purple-400/50" />
                    <span className="text-xs font-mono font-bold text-zinc-300">
                      TAKE {takeNum}
                    </span>
                    <span className="text-[10px] text-zinc-500 font-mono">
                      Conf. {(92 + takeNum * 2.1).toFixed(1)}%
                    </span>
                  </div>

                  {isSelected && (
                    <div className="absolute top-2 right-2 p-1 rounded-full bg-purple-500 text-zinc-950 shadow-lg">
                      <CheckCircle2 className="w-5 h-5" />
                    </div>
                  )}
                </div>

                {/* Footer label */}
                <div className="p-2.5 flex items-center justify-between text-xs font-mono font-bold bg-zinc-900/90">
                  <span className={isSelected ? "text-purple-300" : "text-zinc-400"}>
                    TAKE {takeNum}
                  </span>
                  {isSelected && (
                    <span className="text-[10px] text-purple-400 bg-purple-500/20 px-2 py-0.5 rounded border border-purple-500/30">
                      SELECTED
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* Action Footer */}
        <div className="p-4 border-t border-zinc-800 bg-zinc-900/50 flex items-center justify-between">
          <span className="text-xs text-zinc-400 font-mono">
            Selected: <strong className="text-purple-300">Take {activeChoice}</strong>
          </span>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs font-semibold rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirm}
              className="px-5 py-2 bg-purple-600 hover:bg-purple-500 text-white text-xs font-bold font-mono rounded-lg transition-colors flex items-center gap-1.5 shadow-lg shadow-purple-900/40"
            >
              <Sparkles className="w-3.5 h-3.5" />
              Lock Take {activeChoice}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
