/**
 * Conversation transcript recorder for the real full-build run.
 *
 * The whole point: make the LLM↔customer dialogue OBSERVABLE and auditable.
 * It reconstructs each assistant turn from the WS `stream`/`message` frames the
 * WsTap collected, pairs it with the customer's reply, and writes both a
 * human-readable .md and a machine .json under test-results/transcripts/.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Frame } from './real';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, '../../../test-results/transcripts');

export interface Turn {
  index: number;
  /** What the assistant said this turn (joined stream chunks / message). */
  agent: string;
  /** What the simulated customer replied. */
  customer: string;
  /** Where the agent was when it replied. */
  phase?: string;
  /** Asset families visible so far. */
  assets?: string[];
  /** How the reply was produced: 'llm' | 'heuristic' | 'kickoff'. */
  via?: string;
}

export class Transcript {
  readonly turns: Turn[] = [];
  constructor(private name: string) {}

  add(t: Turn) {
    this.turns.push(t);
  }

  /**
   * Reconstruct the assistant's text for the most recent turn from received
   * frames produced AFTER `sinceFrameIndex`. Joins consecutive `stream` chunks
   * (grouped by message_id) and standalone `message` frames.
   */
  static agentTextSince(frames: Frame[], sinceFrameIndex: number): string {
    const parts: string[] = [];
    for (let i = sinceFrameIndex; i < frames.length; i++) {
      const f = frames[i];
      if (f.type === 'stream' && typeof f.content === 'string') parts.push(f.content);
      else if (f.type === 'message' && typeof f.content === 'string') parts.push('\n' + f.content);
    }
    return parts.join('').trim();
  }

  write(meta: Record<string, unknown> = {}) {
    fs.mkdirSync(OUT_DIR, { recursive: true });
    const base = path.join(OUT_DIR, this.name);
    fs.writeFileSync(`${base}.json`, JSON.stringify({ meta, turns: this.turns }, null, 2), 'utf-8');

    const md: string[] = [`# Full-build transcript — ${this.name}`, ''];
    for (const [k, v] of Object.entries(meta)) md.push(`- **${k}**: ${JSON.stringify(v)}`);
    md.push('');
    for (const t of this.turns) {
      md.push(`## Turn ${t.index}${t.phase ? ` _(phase: ${t.phase})_` : ''}`);
      if (t.assets?.length) md.push(`_assets so far: ${t.assets.join(', ')}_`, '');
      md.push('**🤖 Agent:**', '', '> ' + (t.agent || '_(no text — tool/asset activity only)_').replace(/\n/g, '\n> '), '');
      md.push(`**🧑 Customer** _(${t.via ?? 'reply'})_:`, '', '> ' + t.customer.replace(/\n/g, '\n> '), '');
    }
    fs.writeFileSync(`${base}.md`, md.join('\n'), 'utf-8');
    return { json: `${base}.json`, md: `${base}.md` };
  }
}
