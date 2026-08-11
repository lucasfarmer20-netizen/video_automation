"use client";

import React, { useState, useEffect, useRef } from "react";
import { DirectorShot } from "../types/director";
import { Play, Pause, FastForward, RotateCcw, Compass, Sparkles, Layers, Image as ImageIcon } from "lucide-react";

interface CinemaScrubberPlayerProps {
  shots: DirectorShot[];
  onSelectShot: (shot: DirectorShot) => void;
  onQuickAction: (action: string, shot: DirectorShot) => void;
  mediaUrl: (path: string) => string;
}

export default function CinemaScrubberPlayer({
  shots,
  onSelectShot,
  onQuickAction,
  mediaUrl,
}: CinemaScrubberPlayerProps) {
  const [isPlaying, setIsPlaying] = useState<boolean>(false);
  const [currentIndex, setCurrentIndex] = useState<number>(0);
  const [playbackSpeed, setPlaybackSpeed] = useState<number>(1.0);
  const [elapsedInShot, setElapsedInShot] = useState<number>(0);

  const activeShot = shots[currentIndex] || shots[0];

  useEffect(() => {
    let timer: any;
    if (isPlaying && activeShot) {
      timer = setInterval(() => {
        setElapsedInShot((prev) => {
          const next = prev + 0.1 * playbackSpeed;
          if (next >= activeShot.camera.duration) {
            if (currentIndex < shots.length - 1) {
              setCurrentIndex((idx) => idx + 1);
              return 0;
            } else {
              setIsPlaying(false);
              return 0;
            }
          }
          return next;
        });
      }, 100);
    }
    return () => clearInterval(timer);
  }, [isPlaying, currentIndex, activeShot, playbackSpeed, shots.length]);

  const togglePlay = () => setIsPlaying(!isPlaying);

  const handleSeek = (index: number) => {
    setCurrentIndex(index);
    setElapsedInShot(0);
  };

  const getThumbnailSrc = (shot: DirectorShot) => {
    if (shot.chosen_variation && shot.draft_variations[shot.chosen_variation - 1]) {
      return mediaUrl(shot.draft_variations[shot.chosen_variation - 1]);
    }
    if (shot.thumbnail_url) return mediaUrl(shot.thumbnail_url);
    if (shot.clip) return mediaUrl(shot.clip);
    return null;
  };

  if (!activeShot) return null;
  const thumbnail = getThumbnailSrc(activeShot);

  return (
    <div className="w-full glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800">
      {/* Player Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="p-2 rounded-lg bg-amber-500/15 text-amber-400 font-bold text-xs font-mono">
            CINEMA SCRUBBER
          </span>
          <h3 className="font-bold text-sm text-zinc-100">
            Rough-Cut Storyboard Review
          </h3>
        </div>

        {/* Speed Controls */}
        <div className="flex items-center gap-1.5 font-mono text-xs">
          {[1.0, 1.5, 2.0].map((spd) => (
            <button
              key={spd}
              onClick={() => setPlaybackSpeed(spd)}
              className={`px-2.5 py-1 rounded border transition-colors ${
                playbackSpeed === spd
                  ? "bg-amber-500/20 border-amber-500 text-amber-300 font-bold"
                  : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:bg-zinc-800"
              }`}
            >
              {spd}x
            </button>
          ))}
        </div>
      </div>

      {/* Screen Frame */}
      <div className="relative w-full aspect-video max-h-[420px] bg-zinc-950 rounded-xl overflow-hidden border border-zinc-800 flex items-center justify-center shadow-2xl">
        {thumbnail ? (
          <img src={thumbnail} alt={activeShot.subject} className="w-full h-full object-cover" />
        ) : (
          <div className="flex flex-col items-center gap-2 text-zinc-600">
            <ImageIcon className="w-10 h-10 text-zinc-700 animate-pulse" />
            <span className="text-xs font-mono">Storyframe Preview ({activeShot.shot_number || activeShot.id})</span>
          </div>
        )}

        {/* Active Shot Overlay Badge */}
        <div className="absolute top-3 left-3 flex items-center gap-2">
          <span className="text-xs font-mono font-bold text-amber-400 bg-zinc-950/85 backdrop-blur px-3 py-1 rounded border border-amber-500/40">
            {activeShot.shot_number || activeShot.id} • {activeShot.shot_size} ({activeShot.angle})
          </span>
          <span className="text-xs font-mono font-bold text-zinc-300 bg-zinc-950/85 backdrop-blur px-3 py-1 rounded border border-zinc-800">
            {elapsedInShot.toFixed(1)}s / {activeShot.camera.duration.toFixed(1)}s
          </span>
        </div>

        {/* Pause Action HUD */}
        {!isPlaying && (
          <div className="absolute inset-0 bg-zinc-950/70 backdrop-blur-sm flex flex-col items-center justify-center gap-3 animate-in fade-in duration-150">
            <p className="text-xs font-mono text-zinc-300 font-semibold">
              Paused on Shot {activeShot.shot_number || activeShot.id} ({activeShot.purpose})
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => onQuickAction("swap_angle", activeShot)}
                className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <Compass className="w-3.5 h-3.5 text-blue-400" />
                Swap Angle
              </button>
              <button
                onClick={() => onQuickAction("extend_1s", activeShot)}
                className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <FastForward className="w-3.5 h-3.5 text-amber-400" />
                Extend +1s
              </button>
              <button
                onClick={() => onQuickAction("downgrade_parallax", activeShot)}
                className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5"
              >
                <Layers className="w-3.5 h-3.5 text-emerald-400" />
                Set Parallax ($0)
              </button>
              <button
                onClick={() => onSelectShot(activeShot)}
                className="px-3 py-1.5 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-lg transition-colors flex items-center gap-1.5 shadow"
              >
                <Sparkles className="w-3.5 h-3.5" />
                Inspect Shot
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Controls & Sub-Shot Scrubber Bar */}
      <div className="flex items-center gap-3">
        <button
          onClick={togglePlay}
          className="p-2.5 rounded-xl bg-amber-500 hover:bg-amber-400 text-zinc-950 font-bold transition shadow"
        >
          {isPlaying ? <Pause className="w-5 h-5 fill-current" /> : <Play className="w-5 h-5 fill-current ml-0.5" />}
        </button>

        {/* Sub-shot Scrubber Track */}
        <div className="flex-1 flex items-center gap-1 bg-zinc-950 p-1.5 rounded-xl border border-zinc-800 overflow-x-auto">
          {shots.map((shot, idx) => {
            const isActive = idx === currentIndex;
            return (
              <button
                key={shot.id}
                onClick={() => handleSeek(idx)}
                style={{ flexGrow: shot.camera.duration }}
                className={`h-7 px-2 rounded font-mono text-[10px] font-bold truncate transition-all ${
                  isActive
                    ? "bg-amber-500 text-zinc-950 neon-glow-amber"
                    : "bg-zinc-900 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
                }`}
              >
                {shot.shot_number || shot.id} ({shot.camera.duration.toFixed(1)}s)
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
