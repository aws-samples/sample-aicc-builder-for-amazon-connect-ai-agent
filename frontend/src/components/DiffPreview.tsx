/**
 * DiffPreview Component
 *
 * Renders unified diff text with line-by-line color coding.
 * No external diff library needed — parses standard unified diff format.
 *
 * Line types:
 *   +  → addition (green background)
 *   -  → deletion (red background)
 *   @@ → hunk header (blue)
 *   other → context (default)
 */

import { memo } from 'react';

interface DiffPreviewProps {
  diffContent: string;
}

export const DiffPreview = memo(function DiffPreview({ diffContent }: DiffPreviewProps) {
  if (!diffContent) return null;

  const lines = diffContent.split('\n');

  return (
    <pre className="p-4 text-xs font-mono leading-5 overflow-auto" style={{ margin: 0, background: 'rgba(0,0,0,0.9)' }}>
      {lines.map((line, i) => {
        let className = 'text-gray-300'; // context
        let bg = '';

        if (line.startsWith('+++') || line.startsWith('---')) {
          className = 'text-gray-400 font-bold';
        } else if (line.startsWith('+')) {
          className = 'text-green-300';
          bg = 'rgba(34, 197, 94, 0.15)';
        } else if (line.startsWith('-')) {
          className = 'text-red-300';
          bg = 'rgba(239, 68, 68, 0.15)';
        } else if (line.startsWith('@@')) {
          className = 'text-blue-300';
          bg = 'rgba(59, 130, 246, 0.1)';
        }

        return (
          <div
            key={i}
            className={className}
            style={{ background: bg, paddingLeft: '0.5rem', paddingRight: '0.5rem', minHeight: '1.25rem' }}
          >
            {line || '\u00A0'}
          </div>
        );
      })}
    </pre>
  );
});

export default DiffPreview;
