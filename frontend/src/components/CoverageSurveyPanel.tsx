"use client";

import React, { useState, useEffect } from "react";
import { CoverageSurvey, SurveyBeat } from "../types/director";
import { fetchCoverageSurvey, redirectSceneCoverage } from "../lib/directorApi";
import { Compass, Sparkles, AlertTriangle, Play, ChevronRight, CheckCircle2, RefreshCw } from "lucide-react";

interface CoverageSurveyPanelProps {
  onSelectBeats: (beats: string[]) => void;
}

export default function CoverageSurveyPanel({ onSelectBeats }: CoverageSurveyPanelProps) {
  const [survey, setSurvey] = useState<CoverageSurvey | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [planningBeats, setPlanningBeats] = useState<string | null>(null);

  const loadSurvey = () => {
    setLoading(true);
    setError(null);
    fetchCoverageSurvey()
      .then((data) => {
        setSurvey(data);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || "Failed to load coverage survey from GET /api/director/survey");
        setLoading(false);
      });
  };

  useEffect(() => {
    loadSurvey();
  }, []);

  const handlePlanBeats = async (beatList: string[]) => {
    const key = beatList.join(",");
    setPlanningBeats(key);
    try {
      await redirectSceneCoverage(beatList, "Initial scene coverage planning");
      onSelectBeats(beatList);
    } catch (e: any) {
      console.log("Started planning or opening workspace:", e.message);
      onSelectBeats(beatList);
    } finally {
      setPlanningBeats(null);
    }
  };

  if (loading) {
    return (
      <div className="w-full glass-panel p-6 rounded-2xl border border-zinc-800 flex items-center justify-center text-amber-500 gap-3">
        <Sparkles className="w-5 h-5 animate-spin" />
        <span className="font-mono text-xs font-bold">Querying GET /api/director/survey...</span>
      </div>
    );
  }

  if (error || !survey) {
    return (
      <div className="w-full glass-panel p-6 rounded-2xl border border-amber-500/40 bg-zinc-950/80 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-amber-500/15 text-amber-400">
            <AlertTriangle className="w-5 h-5" />
          </div>
          <div>
            <h4 className="font-bold text-xs text-zinc-100 font-mono">Coverage Survey (GET /api/director/survey)</h4>
            <p className="text-[11px] text-zinc-400 font-mono mt-0.5">{error || "Survey endpoint not responding"}</p>
          </div>
        </div>
        <button
          onClick={loadSurvey}
          className="px-3 py-1.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-700 text-zinc-300 text-xs font-mono font-bold rounded-lg transition flex items-center gap-1.5"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry Survey
        </button>
      </div>
    );
  }

  return (
    <div className="w-full glass-panel p-5 rounded-2xl flex flex-col gap-4 border border-zinc-800 animate-in fade-in duration-200">
      {/* Header Banner */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-zinc-800/80">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-amber-500/15 text-amber-400 border border-amber-500/30">
            <Compass className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-bold text-sm text-zinc-100 flex items-center gap-2 font-mono">
              <span>EPISODE COVERAGE SURVEY</span>
              <span className="text-[10px] font-normal text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded border border-amber-500/30">
                Entry Point
              </span>
            </h3>
            <p className="text-xs text-zinc-400 mt-0.5">
              Deterministic pre-planning score identifying which beats need Director coverage most
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3 text-xs font-mono">
          <span className="text-zinc-300 font-bold">{survey.episode_seconds.toFixed(0)}s Episode</span>
          <span className="text-zinc-600">•</span>
          <span className="text-amber-400 font-bold bg-amber-500/10 border border-amber-500/30 px-2.5 py-1 rounded-lg">
            ⚠️ {survey.frozen_if_nothing_covered.toFixed(0)}s frozen if untouched ({survey.frozen_pct}%)
          </span>
        </div>
      </div>

      {/* Recommended Scenes & Beats List */}
      <div className="flex flex-col gap-2">
        {survey.beats && survey.beats.map((beat) => {
          const isHigh = beat.recommend === 3;
          const isMed = beat.recommend === 2;
          const isPlanning = planningBeats === beat.beat_id;

          const dots = isHigh ? "●●●" : isMed ? "●●" : "○";
          const dotColor = isHigh ? "text-red-400" : isMed ? "text-amber-400" : "text-zinc-500";

          return (
            <div
              key={beat.beat_id}
              className={`p-3.5 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3 transition-colors ${
                isHigh
                  ? "bg-amber-500/10 border-amber-500/40 hover:border-amber-500"
                  : "bg-zinc-950/60 border-zinc-800 hover:border-zinc-700"
              }`}
            >
              {/* Left Column: Priority dots & Beat info */}
              <div className="flex items-start gap-3">
                <span className={`text-xs font-mono font-bold ${dotColor} pt-0.5`}>{dots}</span>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono font-bold text-amber-400 bg-zinc-900 border border-zinc-800 px-2 py-0.5 rounded">
                      Beat {beat.beat_id}
                    </span>
                    <span className="text-xs font-mono text-zinc-300 font-bold">{beat.seconds.toFixed(1)}s</span>
                    <span className="text-[10px] font-mono text-zinc-500 uppercase px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800">
                      {beat.motion_type}
                    </span>
                  </div>
                  <p className="text-xs text-zinc-300 font-mono mt-1 font-semibold">{beat.reason}</p>
                </div>
              </div>

              {/* Action Button */}
              <button
                onClick={() => handlePlanBeats([beat.beat_id])}
                disabled={isPlanning}
                className="px-4 py-2 bg-amber-500 hover:bg-amber-400 text-zinc-950 text-xs font-mono font-bold rounded-lg transition flex items-center gap-1.5 shrink-0 shadow disabled:opacity-50"
              >
                {isPlanning ? (
                  <Sparkles className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Play className="w-3.5 h-3.5 fill-current" />
                )}
                <span>PLAN SCENE ({beat.beat_id})</span>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
