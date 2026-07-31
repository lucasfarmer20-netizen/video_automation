"use client";

import React, { useState } from "react";
import { Mic, Sliders, Sparkles, X, Volume2, Check } from "lucide-react";

interface VoiceStudioModalProps {
  isOpen: boolean;
  onClose: () => void;
  post: (url: string, body?: any) => Promise<any>;
  mediaUrl?: (path: string) => string;
}

export default function VoiceStudioModal({ isOpen, onClose, post, mediaUrl }: VoiceStudioModalProps) {
  const [gender, setGender] = useState("male");
  const [age, setAge] = useState("middle_aged");
  const [accent, setAccent] = useState("american");
  const [description, setDescription] = useState(
    "A low-pitched raspy documentary investigator with deep vocal fry and slow deliberate cadence"
  );
  
  const [stability, setStability] = useState(0.35);
  const [styleExaggeration, setStyleExaggeration] = useState(0.35);
  
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [generatedVoiceId, setGeneratedVoiceId] = useState<string | null>(null);
  const [sampleAudioUrl, setSampleAudioUrl] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleGenerateVoice = async () => {
    setLoading(true);
    setMessage("");
    setSampleAudioUrl(null);
    try {
      const res = await post("/api/voice/design", {
        gender,
        age,
        accent,
        description
      });
      
      const voiceObj = res.voice || res;
      if (res.ok && voiceObj) {
        const vId = voiceObj.generated_voice_id || voiceObj.voice_id || res.generated_voice_id || res.voice_id || null;
        if (vId) setGeneratedVoiceId(vId);
        
        let audioSrc = 
          voiceObj.sample_audio_base64 || 
          voiceObj.audio_base_64 || 
          voiceObj.audio_base64 || 
          voiceObj.sample_audio_url || 
          voiceObj.audio_url || 
          voiceObj.preview_url || 
          res.sample_audio_base64 || 
          res.sample_audio_url || 
          res.audio_base_64 || 
          res.audio_url || null;
          
        if (audioSrc) {
          if (audioSrc.startsWith("data:") || audioSrc.startsWith("http://") || audioSrc.startsWith("https://")) {
            setSampleAudioUrl(audioSrc);
          } else if (typeof mediaUrl === "function") {
            setSampleAudioUrl(mediaUrl(audioSrc));
          } else {
            setSampleAudioUrl(audioSrc);
          }
          setMessage("Unique voice sample generated! Listen to preview below.");
        } else {
          setMessage("Voice design generated, but audio sample was empty. Check ELEVENLABS_API_KEY.");
        }
      } else {
        setMessage(`Voice generation error: ${res.error || "Unknown API error"}`);
      }
    } catch (e: any) {
      setMessage(`Error: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleSaveSettings = async () => {
    setLoading(true);
    try {
      const payload: any = { stability, style_exaggeration: styleExaggeration };
      if (generatedVoiceId) payload.voice_id = generatedVoiceId;
      
      const res = await post("/api/voice/settings", payload);
      if (res.ok) {
        setMessage("ElevenLabs Voice parameters updated!");
        setTimeout(() => onClose(), 1000);
      }
    } catch (e: any) {
      setMessage(`Save failed: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur-md flex items-center justify-center p-4">
      <div className="bg-zinc-900 border border-zinc-800 rounded-2xl w-full max-w-xl shadow-2xl overflow-hidden text-zinc-100 flex flex-col max-h-[90vh]">
        
        {/* Header */}
        <div className="px-6 py-4 border-b border-zinc-800 flex items-center justify-between bg-zinc-950/40">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-500">
              <Mic className="w-4 h-4" />
            </div>
            <div>
              <h3 className="text-sm font-bold tracking-wide">ElevenLabs AI Voice Studio</h3>
              <p className="text-[11px] text-zinc-400">Design 100% unique documentary voices & tune vocal cadence</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-zinc-800 text-zinc-400 hover:text-zinc-100 transition">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 overflow-y-auto space-y-6 flex-1">
          
          {/* AI Voice Designer Section */}
          <div className="bg-zinc-950/60 p-4 rounded-xl border border-zinc-800 space-y-4">
            <div className="flex items-center gap-2 text-amber-400 text-xs font-bold font-mono uppercase tracking-wider">
              <Sparkles className="w-3.5 h-3.5" />
              <span>Voice Design Generator (Zero Uploads)</span>
            </div>

            <div className="grid grid-cols-3 gap-3 text-xs">
              <div>
                <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Gender</label>
                <select
                  value={gender}
                  onChange={(e) => setGender(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-zinc-200 focus:border-amber-400 focus:outline-none"
                >
                  <option value="male">Male</option>
                  <option value="female">Female</option>
                </select>
              </div>

              <div>
                <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Age</label>
                <select
                  value={age}
                  onChange={(e) => setAge(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-zinc-200 focus:border-amber-400 focus:outline-none"
                >
                  <option value="middle_aged">Middle Aged</option>
                  <option value="old">Old</option>
                  <option value="young">Young</option>
                </select>
              </div>

              <div>
                <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Accent</label>
                <select
                  value={accent}
                  onChange={(e) => setAccent(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-2.5 py-1.5 text-zinc-200 focus:border-amber-400 focus:outline-none"
                >
                  <option value="american">American</option>
                  <option value="british">British</option>
                  <option value="irish">Irish</option>
                  <option value="scottish">Scottish</option>
                  <option value="australian">Australian</option>
                </select>
              </div>
            </div>

            <div>
              <label className="block text-[10px] text-zinc-400 mb-1 font-mono">Voice Description Prompt</label>
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                className="w-full bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-xs text-zinc-200 focus:border-amber-400 focus:outline-none"
                placeholder="Describe tone, cadence, raspy pitch, vocal fry..."
              />
            </div>

            <button
              onClick={handleGenerateVoice}
              disabled={loading}
              className="w-full py-2 bg-amber-500 hover:bg-amber-400 text-zinc-950 font-bold text-xs rounded-lg shadow-md transition flex items-center justify-center gap-2 disabled:opacity-50"
            >
              <Sparkles className="w-3.5 h-3.5" />
              <span>{loading ? "Generating Unique Voice..." : "Generate Unique AI Voice"}</span>
            </button>

            {sampleAudioUrl && (
              <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl p-3.5 space-y-2 mt-3 animate-fade-in">
                <div className="flex items-center justify-between text-xs text-amber-400 font-mono font-bold">
                  <span className="flex items-center gap-1.5">
                    <Volume2 className="w-4 h-4 text-amber-500 animate-pulse" />
                    <span>Generated Voice Preview Sample</span>
                  </span>
                  {generatedVoiceId && (
                    <span className="text-[10px] text-zinc-400 font-normal">ID: {generatedVoiceId}</span>
                  )}
                </div>
                <audio
                  controls
                  autoPlay
                  src={sampleAudioUrl}
                  className="w-full h-8 rounded border border-amber-500/20 bg-zinc-950 accent-amber-500"
                />
              </div>
            )}
          </div>

          {/* Voice Tuning Sliders Section */}
          <div className="bg-zinc-950/60 p-4 rounded-xl border border-zinc-800 space-y-4">
            <div className="flex items-center gap-2 text-zinc-300 text-xs font-bold font-mono uppercase tracking-wider">
              <Sliders className="w-3.5 h-3.5 text-amber-500" />
              <span>Vocal Cadence & Resonance Tuning</span>
            </div>

            {/* Stability Slider */}
            <div className="space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-zinc-400 font-mono">Stability (Vocal Variation)</span>
                <span className="text-amber-400 font-mono font-bold">{stability.toFixed(2)}</span>
              </div>
              <input
                type="range"
                min="0.1"
                max="1.0"
                step="0.05"
                value={stability}
                onChange={(e) => setStability(parseFloat(e.target.value))}
                className="w-full accent-amber-500"
              />
              <p className="text-[10px] text-zinc-500">Lower stability (0.30 - 0.40) introduces emotional pitch drops and dramatic human breathing.</p>
            </div>

            {/* Style Exaggeration Slider */}
            <div className="space-y-1">
              <div className="flex justify-between text-xs">
                <span className="text-zinc-400 font-mono">Style Exaggeration</span>
                <span className="text-amber-400 font-mono font-bold">{styleExaggeration.toFixed(2)}</span>
              </div>
              <input
                type="range"
                min="0.0"
                max="1.0"
                step="0.05"
                value={styleExaggeration}
                onChange={(e) => setStyleExaggeration(parseFloat(e.target.value))}
                className="w-full accent-amber-500"
              />
              <p className="text-[10px] text-zinc-500">Higher style exaggeration amplifies heavy documentary delivery and vocal weight.</p>
            </div>
          </div>

          {message && (
            <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs font-mono">
              {message}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-zinc-800 bg-zinc-950/40 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-xs font-semibold bg-zinc-800 hover:bg-zinc-700 text-zinc-300 transition"
          >
            Cancel
          </button>
          <button
            onClick={handleSaveSettings}
            disabled={loading}
            className="px-4 py-2 rounded-lg text-xs font-bold bg-emerald-500 hover:bg-emerald-400 text-zinc-950 shadow-md transition flex items-center gap-1.5 disabled:opacity-50"
          >
            <Check className="w-4 h-4" />
            <span>Apply Voice Parameters</span>
          </button>
        </div>

      </div>
    </div>
  );
}
