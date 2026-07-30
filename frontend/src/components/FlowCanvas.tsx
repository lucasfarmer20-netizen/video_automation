"use client";

import React, { useEffect, useMemo } from "react";
import {
  ReactFlow,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  Handle,
  Position,
  NodeProps,
  Edge
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

interface Shot {
  scene_id: string;
  narration: string;
  motion_type: string;
  camera: { duration: number };
  draft_image: string | null;
  video_clip: string | null;
}

interface FlowCanvasProps {
  shots: Shot[];
  mediaUrl: (path: string) => string;
  onUpdateDuration?: (sceneId: string, duration: number) => void;
  onRegenerate?: (sceneId: string) => void;
  onGenerateSFX?: (sceneId: string) => void;
}

// Custom Node Component
const BeatNode = ({ data }: any) => {
  return (
    <div className="bg-zinc-950/90 border border-zinc-800 rounded-xl p-3.5 w-64 shadow-2xl glass-panel text-zinc-300 relative group">
      <Handle type="target" position={Position.Left} className="!w-2.5 !h-2.5 !bg-amber-500 !border-zinc-950" />
      
      <div className="flex items-center justify-between border-b border-zinc-900 pb-2 mb-2.5">
        <span className="text-[10px] font-bold text-amber-500 font-mono">{data.scene_id}</span>
        <div className="flex items-center gap-1">
          {data.onRegenerate && (
            <button
              onClick={() => data.onRegenerate(data.scene_id)}
              className="text-[9px] bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 px-1.5 py-0.5 rounded border border-amber-500/20 transition font-mono"
              title="Regenerate still"
            >
              🔄
            </button>
          )}
          {data.onGenerateSFX && (
            <button
              onClick={() => data.onGenerateSFX(data.scene_id)}
              className="text-[9px] bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 px-1.5 py-0.5 rounded border border-blue-500/20 transition font-mono"
              title="Generate SFX"
            >
              🔊
            </button>
          )}
          <span className="text-[9px] uppercase tracking-wider px-2 py-0.5 rounded bg-zinc-900 border border-zinc-800 font-mono font-semibold">
            {data.motion_type}
          </span>
        </div>
      </div>
      
      {data.thumbnail ? (
        <img
          src={data.thumbnail}
          className="w-full h-28 object-cover rounded-lg border border-zinc-900 mb-2.5"
          alt="node preview"
        />
      ) : (
        <div className="w-full h-28 bg-zinc-900/40 rounded-lg border border-zinc-900/80 mb-2.5 flex items-center justify-center text-[10px] text-zinc-600 italic">
          No image draft
        </div>
      )}
      
      <p className="text-[10px] text-zinc-400 line-clamp-2 leading-relaxed mb-2 font-medium">
        {data.narration || "No voiceover narration script..."}
      </p>
      
      <div className="flex items-center gap-1.5 mt-2 border-t border-zinc-900/60 pt-2 text-[9px] font-mono select-none">
        <span className={`w-1.5 h-1.5 rounded-full ${data.has_video ? 'bg-emerald-500 shadow-[0_0_8px_#10b981]' : 'bg-zinc-700'}`}></span>
        <span className={data.has_video ? 'text-zinc-300' : 'text-zinc-500'}>
          {data.has_video ? "Clip Rendered" : "No Video"}
        </span>
        
        {/* Inline Duration Editor */}
        <div className="ml-auto flex items-center gap-1 text-zinc-400">
          <span>⏱</span>
          <input
            type="number"
            step="0.1"
            min="1"
            max="60"
            value={data.duration}
            onChange={(e) => data.onUpdateDuration && data.onUpdateDuration(data.scene_id, parseFloat(e.target.value) || 6.0)}
            className="w-12 bg-zinc-900 text-amber-400 text-right px-1 py-0.5 rounded border border-zinc-800 text-[9px] font-mono focus:outline-none focus:border-amber-400"
          />
          <span>s</span>
        </div>
      </div>
      
      <Handle type="source" position={Position.Right} className="!w-2.5 !h-2.5 !bg-amber-500 !border-zinc-950" />
    </div>
  );
};

export default function FlowCanvas({ shots, mediaUrl, onUpdateDuration, onRegenerate, onGenerateSFX }: FlowCanvasProps) {
  const nodeTypes = useMemo(() => ({ beatNode: BeatNode }), []);
  const [nodes, setNodes, onNodesChange] = useNodesState<any>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<any>([]);

  useEffect(() => {
    // Generate nodes
    const generatedNodes = shots.map((s, idx) => {
      return {
        id: s.scene_id,
        type: "beatNode",
        position: { x: idx * 300 + 50, y: 150 },
        data: {
          scene_id: s.scene_id,
          motion_type: s.motion_type,
          narration: s.narration,
          thumbnail: s.draft_image ? mediaUrl(s.draft_image) : null,
          has_video: !!s.video_clip,
          duration: s.camera.duration,
          onUpdateDuration,
          onRegenerate,
          onGenerateSFX
        }
      };
    });

    // Generate edges
    const generatedEdges: Edge[] = [];
    for (let i = 0; i < shots.length - 1; i++) {
      generatedEdges.push({
        id: `e-${shots[i].scene_id}-${shots[i + 1].scene_id}`,
        source: shots[i].scene_id,
        target: shots[i + 1].scene_id,
        type: "smoothstep",
        animated: true,
        style: { stroke: "#f59e0b", strokeWidth: 1.5 }
      });
    }

    setNodes(generatedNodes);
    setEdges(generatedEdges);
  }, [shots, mediaUrl, setNodes, setEdges]);

  return (
    <div className="w-full h-full bg-zinc-950/20 border border-zinc-900 rounded-xl overflow-hidden shadow-inner">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        className="text-zinc-200"
      >
        <Controls className="!bg-zinc-900 !border-zinc-800 !text-zinc-200" />
        <Background color="#18181b" gap={20} size={1.2} />
      </ReactFlow>
    </div>
  );
}
