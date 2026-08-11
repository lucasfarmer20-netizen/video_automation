"use client";

import React from "react";
import { DirectorShot } from "../types/director";
import { UserCheck, ShieldAlert, Sparkles, Video, Image as ImageIcon, Layers, DollarSign, HelpCircle } from "lucide-react";

interface DirectorShotCardProps {
  shot: DirectorShot;
  isSelected?: boolean;
  onSelectShot: (shot: DirectorShot) => void;
  mediaUrl: (path: string) => string;
}

export default function DirectorShotCard({
  shot,
  isSelected = false,
  onSelectShot,
  mediaUrl,
}: DirectorShotCardProps) {
  const getMotionTierBadge = () => {
    switch (shot.motion_type) {
      case "ai_video":
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-bold text-purple-400 bg-purple-500/15 border border-purple-500/30 px-1.5 py-0.5 rounded">
            <Video className="w-3 h-3 text-purple-400" />
            <span>AI Video</span>
          </span>
        );
      case "parallax":
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-bold text-blue-400 bg-blue-500/15 border border-blue-500/30 px-1.5 py-0.5 rounded">
            <Layers className="w-3 h-3 text-blue-400" />
            <span>2.5D Parallax</span>
          </span>
        );
      case "static":
      default:
        return (
          <span className="flex items-center gap-1 text-[10px] font-mono font-bold text-zinc-400 bg-zinc-800 border border-zinc-700 px-1.5 py-0.5 rounded">
            <ImageIcon className="w-3 h-3 text-zinc-400" />
            <span>Static FX</span>
          </span>
        );
    }
  };

  const getThumbnailSrc = () => {
    if (shot.chosen_variation && shot.draft_variations[shot.chosen_variation - 1]) {
      return mediaUrl(shot.draft_variations[shot.chosen_variation - 1]);
    }
    if (shot.thumbnail_url) return mediaUrl(shot.thumbnail_url);
    if (shot.clip) return mediaUrl(shot.clip);
    return null;
  };

  const thumbnail = getThumbnailSrc();

  return (
    <div
      onClick={() => onSelectShot(shot)}
      className={`group relative flex flex-col rounded-xl border overflow-hidden cursor-pointer transition-all duration-200 min-w-[200px] max-w-[220px] shrink-0 glass-panel ${
        isSelected
          ? "border-amber-500/80 bg-amber-500/10 neon-glow-amber scale-[1.02]"
          : "border-zinc-800/80 hover:border-zinc-600 bg-zinc-900/60 hover:bg-zinc-900"
      }`}
    >
      {/* Frame Preview Header */}
      <div className="relative w-full aspect-video bg-zinc-950 flex items-center justify-center overflow-hidden border-b border-zinc-800/60">
        {thumbnail ? (
          <img
            src={thumbnail}
            alt={shot.subject}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
          />
        ) : (
          <div className="flex flex-col items-center justify-center gap-1 text-zinc-600 p-2 text-center">
            <ImageIcon className="w-6 h-6 text-zinc-700" />
            <span className="text-[10px] font-mono">Storyframe Draft</span>
          </div>
        )}

        {/* Top Overlay Badges */}
        <div className="absolute top-1.5 left-1.5 right-1.5 flex items-center justify-between pointer-events-none">
          <span className="text-[11px] font-mono font-bold text-amber-400 bg-zinc-950/85 backdrop-blur px-2 py-0.5 rounded border border-amber-500/30">
            {shot.shot_number || shot.id}
          </span>
          <span className="text-[11px] font-mono font-bold text-zinc-300 bg-zinc-950/85 backdrop-blur px-2 py-0.5 rounded border border-zinc-800">
            {shot.camera.duration.toFixed(1)}s
          </span>
        </div>

        {/* Identity / Constraint Badges on Thumbnail */}
        <div className="absolute bottom-1.5 left-1.5 flex flex-wrap gap-1">
          {shot.identity_critical && (
            <span className="text-[9px] font-mono font-bold text-amber-300 bg-amber-500/80 backdrop-blur px-1.5 py-0.5 rounded text-zinc-950 flex items-center gap-1">
              <UserCheck className="w-2.5 h-2.5" />
              IDENTITY CRITICAL
            </span>
          )}
          {shot.constrained_by && (
            <span className="text-[9px] font-mono font-bold text-amber-400 bg-amber-950/90 border border-amber-500/50 px-1.5 py-0.5 rounded flex items-center gap-1">
              <ShieldAlert className="w-2.5 h-2.5 text-amber-400" />
              CONSTRAINED
            </span>
          )}
        </div>
      </div>

      {/* Shot Info Footer */}
      <div className="p-3 flex flex-col gap-1.5 text-left flex-1 justify-between">
        <div>
          {/* Framing & Angle */}
          <div className="flex items-center justify-between text-xs font-bold text-zinc-200">
            <span>
              {shot.shot_size} · {shot.angle}
            </span>
            <span className="text-[10px] font-mono text-emerald-400 font-bold">
              ${shot.estimated_cost.toFixed(2)}
            </span>
          </div>

          {/* Subject / Purpose */}
          <p className="text-xs font-semibold text-amber-400/90 truncate mt-0.5">
            {shot.subject}
          </p>
          <p className="text-[11px] text-zinc-400 line-clamp-2 mt-1 leading-snug font-sans">
            {shot.purpose}
          </p>
        </div>

        {/* Bottom Tier & Action Pill */}
        <div className="pt-2 border-t border-zinc-800/60 flex items-center justify-between mt-1">
          {getMotionTierBadge()}
          <span className="text-[10px] font-mono text-zinc-500 group-hover:text-amber-400 transition-colors">
            Inspect →
          </span>
        </div>
      </div>
    </div>
  );
}
