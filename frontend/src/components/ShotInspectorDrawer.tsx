"use client";

import React, { useState } from "react";
import { DirectorShot } from "../types/director";
import {
  X,
  Sparkles,
  ShieldAlert,
  UserCheck,
  RotateCcw,
  Sliders,
  Trash2,
  Plus,
  Compass,
  Video,
  Layers,
  Image as ImageIcon,
  DollarSign,
  HelpCircle,
  CheckCircle2,
} from "lucide-react";

interface ShotInspectorDrawerProps {
  shot: DirectorShot | null;
  onClose: () => void;
  onUpdateShot: (shotId: string, updates: Partial<DirectorShot>) => void;
  onOpenTakeSelector?: (shot: DirectorShot) => void;
  onAction: (action: string, shot: DirectorShot) => void;
  mediaUrl: (path: string) => string;
}

export default function ShotInspectorDrawer({
  shot,
  onClose,
  onUpdateShot,
  onOpenTakeSelector,
  onAction,
  mediaUrl,
}: ShotInspectorDrawerProps) {
  if (!shot) return null;

  const [duration, setDuration] = useState(shot.camera.duration);
  const [framing, setFraming] = useState(shot.shot_size);
  const [angle, setAngle] = useState(shot.angle);
  const [motionType, setMotionType] = useState(shot.motion_type);

  const handleSaveDuration = (val: number) => {
    setDuration(val);
    onUpdateShot(shot.id, {
      camera: { ...shot.camera, duration: val },
    });
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
    <aside className="fixed inset-y-0 right-0 w-[420px] bg-zinc-950/95 border-l border-zinc-800/80 backdrop-blur-2xl z-50 flex flex-col shadow-2xl overflow-y-auto animate-in slide-in-from-right duration-200">
      {/* Drawer Header */}
      <div className="p-4 border-b border-zinc-800/80 flex items-center justify-between sticky top-0 bg-zinc-950/90 backdrop-blur z-10">
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono font-bold text-amber-400 bg-amber-500/10 border border-amber-500/30 px-2 py-0.5 rounded">
            Shot {shot.shot_number || shot.id}
          </span>
          <h3 className="text-zinc-100 font-bold text-sm truncate max-w-[200px]">
            {shot.subject}
          </h3>
        </div>
        <button
          onClick={onClose}
          className="text-zinc-400 hover:text-white p-1 rounded-lg hover:bg-zinc-900 transition-colors"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      <div className="p-5 flex flex-col gap-5 flex-1">
        {/* Frame Preview */}
        <div className="relative w-full aspect-video bg-zinc-900 rounded-xl overflow-hidden border border-zinc-800 flex items-center justify-center">
          {thumbnail ? (
            <img src={thumbnail} alt={shot.subject} className="w-full h-full object-cover" />
          ) : (
            <div className="flex flex-col items-center gap-2 text-zinc-600">
              <ImageIcon className="w-8 h-8 text-zinc-700" />
              <span className="text-xs font-mono">No frame rendered</span>
            </div>
          )}
          <div className="absolute bottom-2 left-2 right-2 flex justify-between items-center text-[10px] font-mono bg-zinc-950/80 backdrop-blur px-2.5 py-1 rounded border border-zinc-800">
            <span className="text-zinc-300">Beat {shot.beat_id}</span>
            <span className="text-emerald-400 font-bold">${shot.estimated_cost.toFixed(2)}</span>
          </div>
        </div>

        {/* SECTION 1: Why This Shot Exists (Director Planner Rationale) */}
        {(shot.reason || shot.purpose) && (
          <div className="bg-amber-500/10 border border-amber-500/25 rounded-xl p-3.5 flex flex-col gap-1.5">
            <div className="flex items-center gap-1.5 text-xs font-bold text-amber-400">
              <HelpCircle className="w-4 h-4 text-amber-400" />
              <span>Why did the AI Director choose this shot?</span>
            </div>
            <p className="text-xs text-amber-200/90 leading-relaxed font-sans font-medium">
              "{shot.reason || shot.purpose}"
            </p>
          </div>
        )}

        {/* SECTION 2: Technical Constraint Visibility */}
        {shot.constrained_by && (Array.isArray(shot.constrained_by) ? shot.constrained_by.length > 0 : Boolean(shot.constrained_by)) ? (
          <div className="bg-amber-950/40 border border-amber-500/40 rounded-xl p-3.5 flex flex-col gap-1.5">
            <div className="flex items-center gap-1.5 text-xs font-bold text-amber-300">
              <ShieldAlert className="w-4 h-4 text-amber-400" />
              <span>Technical Constraint Badges</span>
            </div>
            <div className="flex flex-wrap gap-1.5 mt-0.5">
              {(Array.isArray(shot.constrained_by) ? shot.constrained_by : [shot.constrained_by]).map((c, i) => (
                <span key={i} className="text-[10px] font-mono font-bold text-amber-300 bg-amber-500/20 border border-amber-500/40 px-2 py-0.5 rounded">
                  🏷️ {String(c).replace(/_/g, " ")}
                </span>
              ))}
            </div>
            <p className="text-[11px] text-amber-200/80 leading-relaxed font-sans mt-1">
              Technical limits (e.g. model duration quantization or identity reliability ceilings) influenced this coverage choice.
            </p>
          </div>
        ) : (
          <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-xl p-3 flex items-center justify-between text-xs text-emerald-400 font-semibold">
            <span className="flex items-center gap-1.5">
              <CheckCircle2 className="w-4 h-4 text-emerald-400" />
              Pure Creative Choice (Unconstrained)
            </span>
          </div>
        )}

        {/* SECTION 3: Identity-Critical Take Selection Action */}
        {shot.identity_critical && (
          <div className="bg-purple-500/10 border border-purple-500/30 rounded-xl p-3.5 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5 text-xs font-bold text-purple-300">
                <UserCheck className="w-4 h-4 text-purple-400" />
                <span>Identity-Critical Protagonist Shot</span>
              </div>
              <span className="text-[10px] font-mono text-purple-400 bg-purple-500/20 px-2 py-0.5 rounded">
                4 Takes Available
              </span>
            </div>
            <p className="text-xs text-zinc-400">
              This shot requires human verification for key character facial consistency.
            </p>
            {onOpenTakeSelector && (
              <button
                onClick={() => onOpenTakeSelector(shot)}
                className="w-full py-2 bg-purple-600 hover:bg-purple-500 text-white rounded-lg text-xs font-bold font-mono transition-colors flex items-center justify-center gap-1.5 shadow-lg shadow-purple-900/30 mt-1"
              >
                <Sparkles className="w-3.5 h-3.5" />
                Review 4 Takes Side-by-Side
              </button>
            )}
          </div>
        )}

        {/* SECTION 4: Directing Parameters Form */}
        <div className="flex flex-col gap-3 pt-2 border-t border-zinc-800/80">
          <h4 className="text-xs font-mono font-bold text-zinc-400 uppercase tracking-wider">
            Cinematography Controls
          </h4>

          {/* Duration Slider */}
          <div className="flex flex-col gap-1">
            <div className="flex justify-between text-xs text-zinc-300 font-semibold">
              <span>Duration</span>
              <span className="font-mono text-amber-400">{duration.toFixed(1)} sec</span>
            </div>
            <input
              type="range"
              min="1.0"
              max="10.0"
              step="0.1"
              value={duration}
              onChange={(e) => handleSaveDuration(parseFloat(e.target.value))}
              className="w-full accent-amber-500 bg-zinc-800 h-1.5 rounded-lg cursor-pointer"
            />
          </div>

          {/* Framing Selector */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-zinc-300 font-semibold">Framing / Size</label>
            <div className="grid grid-cols-4 gap-1.5">
              {["WS", "MCU", "CU", "Insert"].map((sz) => (
                <button
                  key={sz}
                  onClick={() => {
                    setFraming(sz);
                    onUpdateShot(shot.id, { shot_size: sz });
                  }}
                  className={`py-1.5 text-xs font-mono rounded border transition-colors ${
                    framing === sz
                      ? "bg-amber-500/20 border-amber-500 text-amber-300 font-bold"
                      : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:bg-zinc-800"
                  }`}
                >
                  {sz}
                </button>
              ))}
            </div>
          </div>

          {/* Angle Selector */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-zinc-300 font-semibold">Camera Angle</label>
            <select
              value={angle}
              onChange={(e) => {
                setAngle(e.target.value);
                onUpdateShot(shot.id, { angle: e.target.value });
              }}
              className="w-full bg-zinc-900 border border-zinc-800 rounded-lg p-2 text-xs text-zinc-200 focus:border-amber-500 outline-none font-mono"
            >
              <option value="Three-quarter">Three-quarter</option>
              <option value="Eye-level">Eye-level</option>
              <option value="Low angle">Low angle</option>
              <option value="Macro">Macro</option>
              <option value="Top-down">Top-down (Overhead)</option>
              <option value="Over-the-shoulder">Over-the-shoulder (OTS)</option>
            </select>
          </div>

          {/* Motion Tier Selector */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-zinc-300 font-semibold">Motion Technique</label>
            <div className="grid grid-cols-3 gap-1.5">
              {[
                { type: "static", label: "Static ($0)" },
                { type: "parallax", label: "Parallax ($0)" },
                { type: "ai_video", label: "AI Video ($1+)" },
              ].map((t) => (
                <button
                  key={t.type}
                  onClick={() => {
                    setMotionType(t.type as any);
                    onUpdateShot(shot.id, {
                      motion_type: t.type as any,
                      estimated_cost: t.type === "ai_video" ? 1.25 : 0.0,
                    });
                  }}
                  className={`py-1.5 text-[11px] font-mono rounded border transition-colors ${
                    motionType === t.type
                      ? "bg-amber-500/20 border-amber-500 text-amber-300 font-bold"
                      : "bg-zinc-900 border-zinc-800 text-zinc-400 hover:bg-zinc-800"
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* SECTION 5: Actions Toolbar */}
        <div className="flex flex-col gap-2 pt-3 border-t border-zinc-800/80 mt-auto">
          <h4 className="text-xs font-mono font-bold text-zinc-400 uppercase tracking-wider">
            Shot Actions
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => onAction("regenerate", shot)}
              className="py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
            >
              <RotateCcw className="w-3.5 h-3.5 text-amber-400" />
              Regenerate Take
            </button>
            <button
              onClick={() => onAction("alternate_angle", shot)}
              className="py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
            >
              <Compass className="w-3.5 h-3.5 text-blue-400" />
              Alternate Angle
            </button>
            <button
              onClick={() => onAction("replace", shot)}
              className="py-2 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-200 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
            >
              <Sparkles className="w-3.5 h-3.5 text-purple-400" />
              Replace Shot
            </button>
            <button
              onClick={() => onAction("delete", shot)}
              className="py-2 bg-red-950/40 hover:bg-red-900/60 border border-red-800/50 text-red-300 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors"
            >
              <Trash2 className="w-3.5 h-3.5 text-red-400" />
              Delete Shot
            </button>
          </div>
        </div>
      </div>
    </aside>
  );
}
