"use client";

import React from "react";
import { SceneSummary, DirectorShot } from "../types/director";
import { Video, Layers, Image as ImageIcon, UserCheck, AlertTriangle } from "lucide-react";

interface CompactMontageMatrixProps {
  scenes: SceneSummary[];
  activeSceneId: string;
  allShots?: DirectorShot[];
  onSelectScene: (sceneId: string) => void;
  onSelectShot: (shot: DirectorShot) => void;
  mediaUrl: (path: string) => string;
}

export default function CompactMontageMatrix({
  scenes,
  activeSceneId,
  allShots = [],
  onSelectScene,
  onSelectShot,
  mediaUrl,
}: CompactMontageMatrixProps) {
  return (
    <div
      data-testid="montage-matrix"
      className="w-full glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800"
    >
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-bold text-sm text-zinc-100 flex items-center gap-2">
            <span>COMPACT MONTAGE MATRIX</span>
            {/* Counted, not asserted. This read "158-Shot Bird's-Eye View" as a
                hardcoded string, next to scene rows that were themselves a
                fixture — so the panel stated a shot count belonging to no film
                anyone had planned. It says what it is showing now. */}
            <span
              data-testid="matrix-shot-count"
              className="text-xs font-mono font-normal text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded border border-amber-500/30"
            >
              {allShots.length}-Shot Bird&apos;s-Eye View
            </span>
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Hover to inspect framing • Click micro-thumbnail to jump to shot • Color borders indicate tier
          </p>
        </div>

        {/* Legend */}
        <div className="flex items-center gap-3 text-[11px] font-mono text-zinc-400">
          <span className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />
            <span>Parallax/Static ($0)</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-full bg-purple-500" />
            <span>AI Video ($1+)</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-full bg-amber-500" />
            <span>Identity Review</span>
          </span>
        </div>
      </div>

      {/* Rows per scene */}
      <div className="flex flex-col gap-3">
        {scenes.map((scene) => {
          const isSelected = scene.scene_id === activeSceneId;
          return (
            <div
              key={scene.scene_id}
              className={`p-3 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3 transition-colors ${
                isSelected
                  ? "bg-amber-500/10 border-amber-500/50"
                  : "bg-zinc-950/60 border-zinc-900 hover:border-zinc-800"
              }`}
            >
              {/* Scene Info Label */}
              <div
                onClick={() => onSelectScene(scene.scene_id)}
                className="cursor-pointer min-w-[180px] shrink-0"
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold text-zinc-200">{scene.title}</span>
                </div>
                <div className="text-[10px] font-mono text-zinc-500 mt-0.5">
                  {scene.shots_count} shots • {scene.duration}s • ${scene.estimated_cost.toFixed(2)}
                </div>
              </div>

              {/* Micro Shot Thumbnails Grid */}
              <div className="flex flex-wrap items-center gap-1.5 flex-1">
                {allShots
                  .filter((s) => Boolean(s.beat_id))
                  .slice(0, scene.shots_count)
                  .map((shot) => {
                    const isAiVideo = shot.motion_type === "ai_video";
                    const isIdentity = shot.identity_critical;
                    const borderClass = isIdentity
                      ? "border-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.5)]"
                      : isAiVideo
                      ? "border-purple-500"
                      : "border-emerald-500/60";

                    return (
                      <div
                        key={shot.id}
                        onClick={() => {
                          onSelectScene(scene.scene_id);
                          onSelectShot(shot);
                        }}
                        title={`Shot ${shot.shot_number || shot.id} (${shot.shot_size} · ${shot.camera.duration}s) - ${shot.purpose}`}
                        className={`group relative w-12 h-8 rounded border overflow-hidden cursor-pointer transition-transform hover:scale-125 hover:z-20 bg-zinc-900 ${borderClass}`}
                      >
                        {shot.thumbnail_url || shot.clip ? (
                          <img
                            src={mediaUrl(shot.thumbnail_url || shot.clip || "")}
                            alt={shot.subject}
                            className="w-full h-full object-cover"
                            onError={(e) => {
                              (e.target as HTMLElement).style.display = "none";
                            }}
                          />
                        ) : null}

                        <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/40 text-[9px] font-mono font-bold text-white group-hover:bg-zinc-950/80">
                          {shot.shot_number ? shot.shot_number.split(".")[1] : shot.id.split(".")[1] || "01"}
                        </div>
                      </div>
                    );
                  })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
