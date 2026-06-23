/**
 * FlowDiagram — interactive Amazon Connect Contact Flow visualization.
 *
 * Renders the flow as an interactive node/edge graph (pan / zoom / minimap)
 * using React Flow. The graph is DERIVED from the validated Connect JSON
 * (see lib/contactFlowGraph.ts), so it can never have a "syntax error" the way
 * hand-authored mermaid did, and it always matches the actual flow.
 */
import { useMemo, useEffect, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Panel,
  type Node,
  type Edge,
  type NodeProps,
  Handle,
  Position,
  type ColorMode,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { AlertCircle } from 'lucide-react';
import { deriveContactFlowGraph } from '../lib/contactFlowGraph';
import { cn } from '../lib/utils';

// Per-kind node styling (start / terminal / decision / integration / message).
const KIND_STYLE: Record<string, string> = {
  start: 'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/30 dark:border-emerald-400',
  terminal: 'border-rose-400 bg-rose-50 dark:bg-rose-900/30 dark:border-rose-500',
  decision: 'border-amber-400 bg-amber-50 dark:bg-amber-900/30 dark:border-amber-500',
  integration: 'border-sky-400 bg-sky-50 dark:bg-sky-900/30 dark:border-sky-500',
  message: 'border-violet-300 bg-violet-50 dark:bg-violet-900/30 dark:border-violet-500',
  default: 'border-surface-300 bg-white dark:bg-surface-800 dark:border-surface-600',
};

interface CfNodeData {
  label: string;
  identifier: string;
  type: string;
  subtitle: string;
  kind: string;
  direction?: 'TB' | 'LR';
  [key: string]: unknown;
}

function CfNode({ data }: NodeProps<Node<CfNodeData>>) {
  // Handles sit on top/bottom for top-down layout, left/right for left-to-right,
  // so edges connect cleanly in either orientation.
  const lr = data.direction === 'LR';
  return (
    <div
      className={cn(
        'rounded-lg border px-3 py-2 shadow-sm text-left w-[200px]',
        KIND_STYLE[data.kind] || KIND_STYLE.default
      )}
    >
      <Handle type="target" position={lr ? Position.Left : Position.Top} className="!bg-surface-400 !w-1.5 !h-1.5" />
      <div className="text-[11px] font-semibold text-surface-900 dark:text-surface-100 truncate">
        {data.label}
      </div>
      <div className="text-[9px] font-mono text-surface-400 dark:text-surface-500 truncate">
        {data.identifier}
      </div>
      {data.subtitle && (
        <div className="mt-0.5 text-[10px] text-surface-500 dark:text-surface-400 line-clamp-2">
          {data.subtitle}
        </div>
      )}
      <Handle type="source" position={lr ? Position.Right : Position.Bottom} className="!bg-surface-400 !w-1.5 !h-1.5" />
    </div>
  );
}

const nodeTypes = { cfNode: CfNode };

// Edge color by variant: condition (amber), error (rose), primary (slate).
function styleEdges(edges: Edge[], dark: boolean): Edge[] {
  return edges.map((e) => {
    const variant = (e.data as { variant?: string } | undefined)?.variant;
    const color = variant === 'error' ? '#f43f5e' : variant === 'condition' ? '#f59e0b' : (dark ? '#94a3b8' : '#64748b');
    return {
      ...e,
      animated: false,
      style: { stroke: color, strokeWidth: 1.5 },
      labelStyle: { fill: color, fontSize: 10, fontWeight: 600 },
      labelBgStyle: { fill: dark ? '#1e293b' : '#ffffff', fillOpacity: 0.85 },
      markerEnd: { type: 'arrowclosed' as const, color, width: 16, height: 16 },
    };
  });
}

interface FlowDiagramProps {
  /** The Connect flow JSON (string). */
  flowJson: string;
  language?: string;
  className?: string;
}

export function FlowDiagram({ flowJson, language = 'ko-KR', className }: FlowDiagramProps) {
  const ko = language === 'ko-KR';
  const [dark, setDark] = useState<boolean>(() =>
    typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
  );
  // Layout direction: top-down (default) or left-to-right. User-toggleable.
  const [direction, setDirection] = useState<'TB' | 'LR'>('TB');

  // Track app theme so the canvas + edges match light/dark.
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const root = document.documentElement;
    const obs = new MutationObserver(() => setDark(root.classList.contains('dark')));
    obs.observe(root, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);

  const graph = useMemo(() => {
    try {
      return deriveContactFlowGraph(flowJson, direction);
    } catch {
      return null;
    }
  }, [flowJson, direction]);

  const edges = useMemo(() => (graph ? styleEdges(graph.edges, dark) : []), [graph, dark]);

  // Graceful fallback — never break the screen if the JSON isn't a parseable flow.
  if (!graph || graph.nodes.length === 0) {
    return (
      <div className={cn('flex items-center justify-center p-8 text-center', className)}>
        <div className="flex flex-col items-center gap-2 text-surface-500 dark:text-surface-400">
          <AlertCircle className="w-6 h-6" />
          <p className="text-sm">
            {ko ? '다이어그램을 그릴 수 없습니다. JSON 탭에서 전체 내용을 확인하세요.' :
              "Couldn't render a diagram. See the full content in the JSON tab."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className={cn('w-full', className)} style={{ height: '70vh', minHeight: 320 }}>
      <ReactFlow
        nodes={graph.nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        colorMode={(dark ? 'dark' : 'light') as ColorMode}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.1}
        maxZoom={2}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable className="!bg-surface-100 dark:!bg-surface-800" />
        <Panel position="top-right">
          <div className="flex rounded-md overflow-hidden border border-surface-300 dark:border-surface-600 text-[11px] font-medium shadow-sm">
            <button
              onClick={() => setDirection('TB')}
              className={cn('px-2 py-1 transition-colors',
                direction === 'TB'
                  ? 'bg-primary-600 text-white'
                  : 'bg-white dark:bg-surface-800 text-surface-600 dark:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-700')}
              title={ko ? '위에서 아래로' : 'Top to bottom'}
            >
              {ko ? '↓ 세로' : '↓ Top-down'}
            </button>
            <button
              onClick={() => setDirection('LR')}
              className={cn('px-2 py-1 transition-colors border-l border-surface-300 dark:border-surface-600',
                direction === 'LR'
                  ? 'bg-primary-600 text-white'
                  : 'bg-white dark:bg-surface-800 text-surface-600 dark:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-700')}
              title={ko ? '왼쪽에서 오른쪽으로' : 'Left to right'}
            >
              {ko ? '→ 가로' : '→ Left-right'}
            </button>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}

export default FlowDiagram;
