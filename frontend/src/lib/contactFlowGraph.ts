/**
 * Derive a React Flow node/edge graph from a validated Amazon Connect Contact
 * Flow JSON.
 *
 * The Connect JSON IS the graph: every Action has an Identifier (node), a Type,
 * and Transitions (NextAction / Conditions[] / Errors[]) which are the edges.
 * Deriving the diagram from the JSON — rather than asking the LLM to hand-author
 * mermaid — means the diagram is always valid, always matches the actual flow,
 * and costs no extra tokens. dagre lays it out top-down.
 */
import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';

export interface ContactFlowGraph {
  nodes: Node[];
  edges: Edge[];
  startAction: string | null;
}

// Friendly, short labels per Connect action Type (fallback: the Type itself).
const TYPE_LABEL: Record<string, string> = {
  MessageParticipant: 'Play Message',
  MessageParticipantIteratively: 'Play Message (loop)',
  GetParticipantInput: 'Get Input',
  StoreUserInput: 'Store Input',
  CheckHoursOfOperation: 'Check Hours',
  CheckAttribute: 'Check Attribute',
  CheckContactAttributes: 'Check Attribute',
  Compare: 'Compare',
  ConnectParticipantWithLexBot: 'Lex Bot',
  InvokeLambdaFunction: 'Invoke Lambda',
  UpdateContactTargetQueue: 'Set Queue',
  TransferContactToQueue: 'Transfer to Queue',
  TransferToFlow: 'Transfer to Flow',
  UpdateContactAttributes: 'Set Attributes',
  UpdateContactRecordingBehavior: 'Set Recording',
  UpdateContactTextToSpeechVoice: 'Set Voice',
  UpdateFlowLoggingBehavior: 'Set Logging',
  UpdateContactCallbackNumber: 'Set Callback',
  Wait: 'Wait',
  Loop: 'Loop',
  DisconnectParticipant: 'Disconnect',
  EndFlowModuleExecution: 'End Module',
  InvokeFlowModule: 'Invoke Module',
  CreatePersistentContactAssociation: 'Persist Contact',
};

// Node categories drive color/shape. Terminal + decision blocks stand out.
type NodeKind = 'start' | 'terminal' | 'decision' | 'integration' | 'message' | 'default';

function kindForType(type: string, isStart: boolean): NodeKind {
  if (isStart) return 'start';
  if (type === 'DisconnectParticipant' || type === 'EndFlowModuleExecution') return 'terminal';
  if (type === 'GetParticipantInput' || type === 'CheckHoursOfOperation' ||
      type === 'Compare' || type === 'CheckContactAttributes' || type === 'CheckAttribute' ||
      type === 'Loop') return 'decision';
  if (type === 'InvokeLambdaFunction' || type === 'ConnectParticipantWithLexBot' ||
      type === 'TransferContactToQueue' || type === 'TransferToFlow') return 'integration';
  if (type === 'MessageParticipant' || type === 'MessageParticipantIteratively') return 'message';
  return 'default';
}

function shortText(s: unknown, max = 48): string {
  if (typeof s !== 'string') return '';
  const oneLine = s.replace(/\s+/g, ' ').trim();
  return oneLine.length > max ? oneLine.slice(0, max - 1) + '…' : oneLine;
}

const NODE_W = 200;
const NODE_H = 56;

export type FlowDirection = 'TB' | 'LR';

/**
 * Parse a Connect flow JSON string into a laid-out React Flow graph.
 * @param direction dagre rankdir — 'TB' (top-down, default) or 'LR' (left-to-right).
 * Returns null if the content isn't a parseable flow (caller falls back to JSON view).
 */
export function deriveContactFlowGraph(flowJson: string, direction: FlowDirection = 'TB'): ContactFlowGraph | null {
  let doc: Record<string, unknown>;
  try {
    doc = JSON.parse(flowJson);
  } catch {
    return null;
  }
  const actions = (doc.Actions || doc.actions) as Array<Record<string, unknown>> | undefined;
  if (!Array.isArray(actions) || actions.length === 0) return null;

  const startAction = (doc.StartAction || doc.startAction || null) as string | null;
  const ids = new Set(actions.map((a) => String(a.Identifier || a.identifier || '')));

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: direction, nodesep: 40, ranksep: 56, marginx: 16, marginy: 16 });
  g.setDefaultEdgeLabel(() => ({}));

  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const edgeSeen = new Set<string>();

  const addEdge = (source: string, target: string, label: string, variant: 'next' | 'condition' | 'error') => {
    if (!target || !ids.has(target)) return; // skip dangling targets
    const id = `${source}->${target}:${label}`;
    if (edgeSeen.has(id)) return;
    edgeSeen.add(id);
    edges.push({
      id,
      source,
      target,
      label: label || undefined,
      data: { variant },
      type: 'smoothstep',
    });
    g.setEdge(source, target);
  };

  for (const a of actions) {
    const id = String(a.Identifier || a.identifier || '');
    if (!id) continue;
    const type = String(a.Type || a.type || 'Action');
    const isStart = id === startAction;
    const params = (a.Parameters || a.parameters || {}) as Record<string, unknown>;
    const subtitle = shortText(params.Text || params.SSML || params.QueueId || params.LambdaFunctionARN || '');

    g.setNode(id, { width: NODE_W, height: NODE_H });
    nodes.push({
      id,
      position: { x: 0, y: 0 }, // dagre fills this in below
      data: {
        label: TYPE_LABEL[type] || type,
        identifier: id,
        type,
        subtitle,
        kind: kindForType(type, isStart),
        direction,
      },
      type: 'cfNode',
    });

    const tr = (a.Transitions || a.transitions || {}) as Record<string, unknown>;
    // Primary path
    addEdge(id, String(tr.NextAction || tr.nextAction || ''), '', 'next');
    // Condition branches (labeled by the matched operand, e.g. "1", "True")
    const conds = (tr.Conditions || tr.conditions || []) as Array<Record<string, unknown>>;
    for (const c of conds) {
      const cond = (c.Condition || c.condition || {}) as Record<string, unknown>;
      const operands = (cond.Operands || cond.operands || []) as unknown[];
      const label = operands.length ? String(operands[0]) : 'match';
      addEdge(id, String(c.NextAction || c.nextAction || ''), label, 'condition');
    }
    // Error branches (labeled by error type)
    const errs = (tr.Errors || tr.errors || []) as Array<Record<string, unknown>>;
    for (const e of errs) {
      const label = String(e.ErrorType || e.errorType || 'error');
      addEdge(id, String(e.NextAction || e.nextAction || ''), label, 'error');
    }
  }

  if (nodes.length === 0) return null;

  // Lay out with dagre, then map back to React Flow positions (top-left origin).
  dagre.layout(g);
  for (const n of nodes) {
    const pos = g.node(n.id);
    if (pos) {
      n.position = { x: pos.x - NODE_W / 2, y: pos.y - NODE_H / 2 };
    }
  }

  return { nodes, edges, startAction };
}
