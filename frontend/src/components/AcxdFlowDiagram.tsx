import { useEffect, useMemo, useState } from 'react';
import dagre from '@dagrejs/dagre';
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  useNodesState,
  type ColorMode,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { AlertCircle, Sparkles } from 'lucide-react';
import { cn } from '../lib/utils';

const GENERATIVE_TYPES = new Set([
  'knowledge_base',
  'generative_text',
  'generative_task',
  'generative_journey',
  'intent_capture',
]);

const NODE_WIDTH = 232;
const NODE_HEIGHT = 76;

type FlowDirection = 'TB' | 'LR';

interface AcxdNodeData {
  /** Human-readable headline (message text, data request id, branch names, …). */
  title: string;
  /** Secondary line — slot name, variable, target flow, … Empty when nothing useful. */
  detail: string;
  /** Localised node-type chip. */
  badge: string;
  identifier: string;
  nodeType: string;
  generative: boolean;
  direction: FlowDirection;
  [key: string]: unknown;
}

interface AcxdGraph {
  nodes: Node<AcxdNodeData>[];
  edges: Edge[];
}

interface EdgeCandidate {
  target: string;
  label?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function shortText(value: string, max = 56): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  return normalized.length > max ? `${normalized.slice(0, max - 1)}…` : normalized;
}

const NODE_TYPE_LABELS: Record<string, { ko: string; en: string }> = {
  start: { ko: '시작', en: 'Start' },
  end: { ko: '종료', en: 'End' },
  user_input: { ko: '입력 받기', en: 'User input' },
  data_request: { ko: '데이터 요청', en: 'Data request' },
  choice: { ko: '분기', en: 'Choice' },
  define: { ko: '변수 설정', en: 'Set variable' },
  basic: { ko: '메시지', en: 'Message' },
  escalate: { ko: '상담원 연결', en: 'Escalate' },
  redirect: { ko: '플로우 이동', en: 'Go to flow' },
  loop: { ko: '반복', en: 'Loop' },
  generative_text: { ko: '생성 응답', en: 'Generative text' },
  generative_task: { ko: '생성 작업', en: 'Generative task' },
  generative_journey: { ko: '생성 여정', en: 'Generative journey' },
  knowledge_base: { ko: '지식 베이스', en: 'Knowledge base' },
  intent_capture: { ko: '의도 파악', en: 'Intent capture' },
};

function typeBadge(nodeType: string, ko: boolean): string {
  const entry = NODE_TYPE_LABELS[nodeType];
  if (entry) return ko ? entry.ko : entry.en;
  return nodeType.replace(/_/g, ' ');
}

function firstMessageBody(...containers: unknown[]): string {
  for (const container of containers) {
    const messages = Array.isArray(container) ? container : isRecord(container) ? container.messages : undefined;
    if (!Array.isArray(messages)) continue;
    const first = messages.find(isRecord);
    const body = firstString(first?.body, first?.text, first?.content);
    if (body) return body;
  }
  return '';
}

function branchNames(node: Record<string, unknown>): string[] {
  const names: string[] = [];
  for (const field of ['childNodes', 'children', 'branches', 'transitions']) {
    const value = node[field];
    if (!Array.isArray(value)) continue;
    for (const entry of value) {
      if (!isRecord(entry)) continue;
      const name = firstString(entry.name, entry.label, entry.condition);
      if (name && !names.includes(name)) names.push(name);
    }
  }
  return names;
}

function constantText(value: unknown): string {
  if (isRecord(value)) {
    if ('value' in value && value.value !== undefined && value.value !== null) return String(value.value);
    if (typeof value.name === 'string') return value.name;
    return '';
  }
  if (value === undefined || value === null) return '';
  return String(value);
}

/**
 * Human-readable headline + detail for one flow node. The node ids are UUIDs
 * by contract, so they are never shown as text — the label always comes from
 * what the node does (message, slot, data request, branches, target flow).
 */
function describeNode(node: Record<string, unknown>, nodeType: string, ko: boolean): { title: string; detail: string } {
  const metadata = isRecord(node.metadata) ? node.metadata : {};
  const explicitName = firstString(node.name, node.displayName, node.label, metadata.name, metadata.displayName);
  const branches = branchNames(node);
  const branchSummary = branches.join(' / ');

  switch (nodeType) {
    case 'start':
      return { title: explicitName || (ko ? '대화 시작' : 'Conversation starts'), detail: '' };
    case 'end':
      return { title: explicitName || (ko ? '대화 종료' : 'Conversation ends'), detail: '' };
    case 'user_input': {
      const userInput = isRecord(metadata.userInput) ? metadata.userInput : {};
      const prompt = firstMessageBody(userInput, node);
      const slot = firstString(userInput.slotName, userInput.slot);
      return {
        title: explicitName || prompt || (ko ? '고객 입력을 받습니다' : 'Collects customer input'),
        detail: slot ? (ko ? `슬롯: ${slot}` : `slot: ${slot}`) : '',
      };
    }
    case 'data_request': {
      const dataRequest = isRecord(metadata.dataRequest) ? metadata.dataRequest : {};
      const list = Array.isArray(node.dataRequests) ? node.dataRequests.filter((v) => typeof v === 'string') : [];
      const id = firstString(dataRequest.dataRequestId, dataRequest.name, list[0]);
      return { title: explicitName || id || (ko ? '백엔드 호출' : 'Backend call'), detail: branchSummary };
    }
    case 'choice':
      return { title: explicitName || branchSummary || (ko ? '조건 분기' : 'Conditional branch'), detail: explicitName ? branchSummary : '' };
    case 'define': {
      const define = isRecord(metadata.define) ? metadata.define : {};
      const variable = firstString(define.variableName, define.name);
      const value = constantText(define.value);
      const assignment = variable ? (value ? `${variable} = ${value}` : variable) : '';
      return { title: explicitName || assignment || (ko ? '변수 설정' : 'Set variable'), detail: branchSummary };
    }
    case 'basic': {
      const basic = isRecord(metadata.basic) ? metadata.basic : {};
      return { title: explicitName || firstMessageBody(basic, node) || (ko ? '안내 메시지' : 'Message'), detail: '' };
    }
    case 'escalate':
      return { title: explicitName || firstMessageBody(node, metadata) || (ko ? '상담원에게 연결' : 'Hand off to an agent'), detail: '' };
    case 'redirect': {
      const redirect = isRecord(metadata.redirect) ? metadata.redirect : {};
      const target = firstString(redirect.flowName, redirect.flowId, redirect.target);
      return { title: explicitName || (target ? `→ ${target}` : ko ? '다른 플로우로 이동' : 'Go to another flow'), detail: '' };
    }
    case 'loop': {
      const counter = firstString(metadata.counterVariable);
      const max = metadata.maxIterations;
      const summary = counter && max !== undefined ? `${counter} < ${String(max)}` : counter || (max !== undefined ? `max ${String(max)}` : '');
      return { title: explicitName || summary || (ko ? '재시도 반복' : 'Retry loop'), detail: branchSummary };
    }
    case 'knowledge_base': {
      const kb = isRecord(metadata.knowledgeBase) ? metadata.knowledgeBase : {};
      return { title: explicitName || firstString(kb.name, kb.knowledgeBaseId) || (ko ? '지식 베이스 답변' : 'Knowledge base answer'), detail: '' };
    }
    case 'generative_text':
    case 'generative_task':
    case 'generative_journey':
    case 'intent_capture': {
      const key = nodeType === 'generative_text' ? 'generativeText'
        : nodeType === 'generative_task' ? 'generativeTask'
          : nodeType === 'generative_journey' ? 'generativeJourney' : 'intentCapture';
      const config = isRecord(metadata[key]) ? (metadata[key] as Record<string, unknown>) : {};
      const prompt = firstString(config.prompt, config.instruction, config.description);
      return { title: explicitName || prompt || (ko ? 'AI가 응답을 생성합니다' : 'AI generates the response'), detail: branchSummary };
    }
    default:
      return { title: explicitName || firstMessageBody(node, metadata) || branchSummary, detail: '' };
  }
}

function edgeCandidates(value: unknown, inheritedLabel = ''): EdgeCandidate[] {
  if (typeof value === 'string') return [{ target: value, label: inheritedLabel }];
  if (Array.isArray(value)) return value.flatMap((entry) => edgeCandidates(entry, inheritedLabel));
  if (!isRecord(value)) return [];

  const target = firstString(
    value.nodeId,
    value.targetNodeId,
    value.targetId,
    value.target,
    value.to,
    value.nextNodeId,
    value.next,
    value.id,
  );
  if (target) {
    return [{ target, label: firstString(value.name, value.label, value.condition, inheritedLabel) }];
  }

  return Object.entries(value).flatMap(([label, entry]) =>
    edgeCandidates(entry, label === 'default' ? inheritedLabel : label),
  );
}

function parseFlow(content: string): Record<string, unknown> | null {
  const fenced = content.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1] || content;
  try {
    const parsed: unknown = JSON.parse(fenced);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function deriveAcxdGraph(flowJson: string, direction: FlowDirection, ko: boolean): AcxdGraph | null {
  const doc = parseFlow(flowJson);
  const rawNodes = doc?.nodes;
  if (!doc || !isRecord(rawNodes) || Object.keys(rawNodes).length === 0) return null;

  const orderedNodes = Object.entries(rawNodes)
    .filter(([, node]) => isRecord(node))
    .sort(([left], [right]) => left.localeCompare(right)) as Array<[string, Record<string, unknown>]>;
  if (orderedNodes.length === 0) return null;

  const idByReference = new Map<string, string>();
  for (const [key, node] of orderedNodes) {
    const id = firstString(node.nodeId, key);
    idByReference.set(key, id);
    idByReference.set(id, id);
  }

  const startNodeId = idByReference.get(firstString(doc.startNodeId, doc.start_node_id)) || '';
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: direction, nodesep: 40, ranksep: 56, marginx: 16, marginy: 16 });
  graph.setDefaultEdgeLabel(() => ({}));

  const nodes: Node<AcxdNodeData>[] = [];
  const edges: Edge[] = [];
  const seenEdges = new Set<string>();

  const addEdge = (source: string, candidate: EdgeCandidate) => {
    const target = idByReference.get(candidate.target);
    if (!target || source === target) return;
    const label = shortText(candidate.label || '', 28);
    const edgeId = `${source}->${target}:${label}`;
    if (seenEdges.has(edgeId)) return;
    seenEdges.add(edgeId);
    edges.push({
      id: edgeId,
      source,
      target,
      label: label || undefined,
      type: 'smoothstep',
      data: { variant: label.toLowerCase().includes('error') ? 'error' : 'default' },
    });
    graph.setEdge(source, target);
  };

  for (const [key, rawNode] of orderedNodes) {
    const id = idByReference.get(key)!;
    const nodeType = firstString(rawNode.type, 'node');
    const described = describeNode(rawNode, nodeType, ko);
    const generative = GENERATIVE_TYPES.has(nodeType);

    graph.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
    nodes.push({
      id,
      type: 'acxdNode',
      position: { x: 0, y: 0 },
      data: {
        title: shortText(described.title, 72),
        detail: shortText(described.detail, 48),
        badge: typeBadge(nodeType, ko),
        identifier: id,
        nodeType,
        generative,
        direction,
        isStart: id === startNodeId,
      },
    });

    for (const field of ['childNodes', 'children', 'edges', 'branches', 'transitions', 'nextNodeId', 'next']) {
      for (const candidate of edgeCandidates(rawNode[field])) addEdge(id, candidate);
    }
  }

  const rootEdges = doc.edges;
  if (Array.isArray(rootEdges)) {
    for (const edge of rootEdges.filter(isRecord)) {
      const source = idByReference.get(firstString(edge.sourceNodeId, edge.sourceId, edge.source, edge.from));
      if (source) addEdge(source, { target: firstString(edge.targetNodeId, edge.targetId, edge.target, edge.to), label: firstString(edge.name, edge.label) });
    }
  }

  dagre.layout(graph);
  for (const node of nodes) {
    const position = graph.node(node.id);
    if (position) node.position = { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 };
  }

  return { nodes, edges };
}

function AcxdNode({ data }: NodeProps<Node<AcxdNodeData>>) {
  const leftToRight = data.direction === 'LR';
  const isStart = data.isStart === true || data.nodeType === 'start';
  const isTerminal = data.nodeType === 'end' || data.nodeType === 'escalate';

  return (
    <div
      className={cn(
        'relative rounded-lg border px-3 py-2 shadow-sm text-left w-[232px]',
        data.generative
          ? 'border-fuchsia-400 bg-fuchsia-50 dark:bg-fuchsia-950/35 dark:border-fuchsia-400'
          : isStart
            ? 'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/30 dark:border-emerald-400'
            : isTerminal
              ? 'border-rose-400 bg-rose-50 dark:bg-rose-900/30 dark:border-rose-500'
              : 'border-surface-300 bg-white dark:bg-surface-800 dark:border-surface-600',
      )}
    >
      <Handle type="target" position={leftToRight ? Position.Left : Position.Top} className="!bg-surface-400 !w-1.5 !h-1.5" />
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            'inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide',
            data.generative
              ? 'bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/60 dark:text-fuchsia-200'
              : isStart
                ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-200'
                : isTerminal
                  ? 'bg-rose-100 text-rose-700 dark:bg-rose-900/60 dark:text-rose-200'
                  : 'bg-surface-100 text-surface-600 dark:bg-surface-700 dark:text-surface-300',
          )}
        >
          {data.badge}
        </span>
        {data.generative && (
          <span className="inline-flex shrink-0 items-center gap-0.5 text-[9px] font-semibold text-fuchsia-600 dark:text-fuchsia-300">
            <Sparkles className="h-2.5 w-2.5" />
            generative
          </span>
        )}
      </div>
      <div className="mt-1 text-[11px] font-medium leading-snug text-surface-900 dark:text-surface-100 break-words" title={data.identifier}>
        {data.title}
      </div>
      {data.detail && (
        <div className="mt-0.5 text-[9.5px] text-surface-500 dark:text-surface-400 truncate">{data.detail}</div>
      )}
      <Handle type="source" position={leftToRight ? Position.Right : Position.Bottom} className="!bg-surface-400 !w-1.5 !h-1.5" />
    </div>
  );
}

const nodeTypes = { acxdNode: AcxdNode };

function styledEdges(edges: Edge[], dark: boolean): Edge[] {
  return edges.map((edge) => {
    const error = (edge.data as { variant?: string } | undefined)?.variant === 'error';
    const color = error ? '#f43f5e' : dark ? '#94a3b8' : '#64748b';
    return {
      ...edge,
      style: { stroke: color, strokeWidth: 1.5 },
      labelStyle: { fill: color, fontSize: 10, fontWeight: 600 },
      labelBgStyle: { fill: dark ? '#1e293b' : '#ffffff', fillOpacity: 0.85 },
      markerEnd: { type: 'arrowclosed' as const, color, width: 16, height: 16 },
    };
  });
}

interface AcxdFlowDiagramProps {
  flowJson: string;
  language?: string;
  className?: string;
  /**
   * 'fill' (default) stretches to the parent — the workspace pane and the
   * fullscreen modal give it a definite height, so no white band is left
   * under the canvas. Pass a pixel height where the parent has none (chat).
   */
  height?: 'fill' | number;
}

/**
 * Deterministically derives a React Flow graph from the persisted ACXD flow
 * JSON. Invalid or incomplete streamed JSON remains visible as source text.
 */
export function AcxdFlowDiagram({ flowJson, language = 'ko-KR', className, height = 'fill' }: AcxdFlowDiagramProps) {
  const ko = language === 'ko-KR';
  const [dark, setDark] = useState(() =>
    typeof document !== 'undefined' && document.documentElement.classList.contains('dark'),
  );
  const [direction, setDirection] = useState<FlowDirection>('TB');

  useEffect(() => {
    if (typeof document === 'undefined') return;
    const root = document.documentElement;
    const observer = new MutationObserver(() => setDark(root.classList.contains('dark')));
    observer.observe(root, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);

  const graph = useMemo(() => deriveAcxdGraph(flowJson, direction, ko), [flowJson, direction, ko]);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<AcxdNodeData>>(graph?.nodes ?? []);

  useEffect(() => {
    setNodes(graph?.nodes ?? []);
  }, [graph, setNodes]);

  const edges = useMemo(() => styledEdges(graph?.edges ?? [], dark), [graph, dark]);

  if (!graph || graph.nodes.length === 0) {
    const hasContent = flowJson.trim().length > 0;
    return (
      <div className={cn('rounded-lg border border-surface-200 dark:border-surface-700 bg-surface-50 dark:bg-surface-900/40 p-4', className)}>
        <div className="flex items-center gap-2 text-surface-500 dark:text-surface-400">
          <AlertCircle className="h-5 w-5 shrink-0" />
          <p className="text-sm">
            {hasContent
              ? (ko ? '유효한 ACXD 플로우 JSON이 수신되면 다이어그램을 표시합니다.' : 'The diagram appears when valid ACXD flow JSON is available.')
              : (ko ? 'ACXD 플로우를 생성하는 중입니다…' : 'Generating ACXD flow…')}
          </p>
        </div>
        {hasContent && (
          <pre className="mt-3 max-h-64 overflow-auto rounded-md bg-surface-950 p-3 text-xs text-surface-100 whitespace-pre-wrap break-words">
            {flowJson}
          </pre>
        )}
      </div>
    );
  }

  return (
    <div
      className={cn('w-full', height === 'fill' && 'h-full', className)}
      style={height === 'fill' ? { minHeight: 340 } : { height, minHeight: 280 }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        nodeTypes={nodeTypes}
        colorMode={(dark ? 'dark' : 'light') as ColorMode}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.1}
        maxZoom={2}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
        {/* Top-left: flows grow downward, so the default bottom-right corner sat on the last nodes. */}
        <MiniMap pannable zoomable position="top-left" className="!bg-surface-100 dark:!bg-surface-800" />
        <Panel position="top-right">
          <div className="flex overflow-hidden rounded-md border border-surface-300 text-[11px] font-medium shadow-sm dark:border-surface-600">
            <button
              onClick={() => setDirection('TB')}
              className={cn(
                'px-2 py-1 transition-colors',
                direction === 'TB'
                  ? 'bg-primary-600 text-white'
                  : 'bg-white text-surface-600 hover:bg-surface-100 dark:bg-surface-800 dark:text-surface-300 dark:hover:bg-surface-700',
              )}
              title={ko ? '위에서 아래로' : 'Top to bottom'}
            >
              {ko ? '↓ 세로' : '↓ Top-down'}
            </button>
            <button
              onClick={() => setDirection('LR')}
              className={cn(
                'border-l border-surface-300 px-2 py-1 transition-colors dark:border-surface-600',
                direction === 'LR'
                  ? 'bg-primary-600 text-white'
                  : 'bg-white text-surface-600 hover:bg-surface-100 dark:bg-surface-800 dark:text-surface-300 dark:hover:bg-surface-700',
              )}
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

export default AcxdFlowDiagram;
