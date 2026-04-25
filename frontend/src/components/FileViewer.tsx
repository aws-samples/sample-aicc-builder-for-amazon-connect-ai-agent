/**
 * File Viewer Modal
 *
 * Displays workspace file content in a modal with:
 * - Syntax highlighting for code files
 * - Markdown rendering for .md files
 * - Table view for .csv / .xlsx files
 * - Extracted text for .docx / .pdf files
 */

import { useState, useEffect, useCallback, useMemo, lazy, Suspense } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  X,
  Copy,
  Check,
  Download,
  Loader2,
  FileCode,
  FileJson,
  FileText,
  FileSpreadsheet,
  File,
  Table,
  Code,
} from 'lucide-react';
import { cn } from '../lib/utils';
import { fetchWorkspaceFile } from '../services/workspaceApi';

// Lazy-load SyntaxHighlighter for performance (same pattern as AssetPreviewBubble)
const SyntaxHighlighter = lazy(() =>
  import('react-syntax-highlighter/dist/esm/prism-light').then(mod => ({ default: mod.default }))
);
let oneDarkStyle: any = null;
import('react-syntax-highlighter/dist/esm/styles/prism/one-dark').then(mod => { oneDarkStyle = mod.default; });

function getFileIcon(name: string) {
  if (name.endsWith('.py')) return <FileCode className="w-4 h-4 text-yellow-500" />;
  if (name.endsWith('.yaml') || name.endsWith('.yml')) return <FileCode className="w-4 h-4 text-blue-500" />;
  if (name.endsWith('.json')) return <FileJson className="w-4 h-4 text-green-500" />;
  if (name.endsWith('.md')) return <FileText className="w-4 h-4 text-surface-500" />;
  if (name.endsWith('.js') || name.endsWith('.ts') || name.endsWith('.tsx')) return <FileCode className="w-4 h-4 text-blue-400" />;
  if (name.endsWith('.csv') || name.endsWith('.xlsx') || name.endsWith('.tsv')) return <FileSpreadsheet className="w-4 h-4 text-emerald-500" />;
  if (name.endsWith('.pdf')) return <FileText className="w-4 h-4 text-red-500" />;
  if (name.endsWith('.docx') || name.endsWith('.doc')) return <FileText className="w-4 h-4 text-blue-600" />;
  return <File className="w-4 h-4 text-surface-400" />;
}

/** Parse CSV text into a 2D string array (handles quoted fields with commas) */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let current = '';
  let inQuotes = false;
  let row: string[] = [];

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    const next = text[i + 1];

    if (inQuotes) {
      if (ch === '"' && next === '"') {
        current += '"';
        i++; // skip escaped quote
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        current += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ',') {
        row.push(current.trim());
        current = '';
      } else if (ch === '\n' || (ch === '\r' && next === '\n')) {
        row.push(current.trim());
        if (row.some(c => c !== '')) rows.push(row);
        row = [];
        current = '';
        if (ch === '\r') i++; // skip \n after \r
      } else {
        current += ch;
      }
    }
  }
  // Last row
  if (current || row.length > 0) {
    row.push(current.trim());
    if (row.some(c => c !== '')) rows.push(row);
  }
  return rows;
}

/** CSV Table renderer */
function CsvTable({ content }: { content: string }) {
  const rows = useMemo(() => parseCsv(content), [content]);

  if (rows.length === 0) {
    return <div className="p-4 text-sm text-surface-400">Empty data</div>;
  }

  // Handle sheet headers (lines starting with "# Sheet:")
  const sections: Array<{ title?: string; rows: string[][] }> = [];
  let currentSection: { title?: string; rows: string[][] } = { rows: [] };

  for (const row of rows) {
    if (row.length === 1 && row[0].startsWith('# Sheet:')) {
      if (currentSection.rows.length > 0) sections.push(currentSection);
      currentSection = { title: row[0].replace('# Sheet: ', ''), rows: [] };
    } else {
      currentSection.rows.push(row);
    }
  }
  if (currentSection.rows.length > 0) sections.push(currentSection);

  return (
    <div className="p-2 space-y-4">
      {sections.map((section, si) => (
        <div key={si}>
          {section.title && (
            <div className="px-2 py-1 text-xs font-semibold text-surface-500 dark:text-surface-400 mb-1">
              {section.title}
            </div>
          )}
          <div className="overflow-x-auto border border-surface-200 dark:border-surface-700 rounded-lg">
            <table className="min-w-full text-xs">
              <thead>
                <tr className="bg-surface-100 dark:bg-surface-800">
                  {section.rows[0]?.map((cell, ci) => (
                    <th key={ci} className="px-3 py-2 text-left font-semibold text-surface-700 dark:text-surface-300 border-b border-surface-200 dark:border-surface-700 whitespace-nowrap">
                      {cell}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {section.rows.slice(1).map((row, ri) => (
                  <tr key={ri} className={ri % 2 === 0 ? 'bg-white dark:bg-surface-900' : 'bg-surface-50 dark:bg-surface-850'}>
                    {row.map((cell, ci) => (
                      <td key={ci} className="px-3 py-1.5 text-surface-600 dark:text-surface-400 border-b border-surface-100 dark:border-surface-800 whitespace-nowrap max-w-[300px] truncate" title={cell}>
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-[10px] text-surface-400 mt-1 px-1">
            {section.rows.length - 1} rows × {section.rows[0]?.length || 0} columns
          </div>
        </div>
      ))}
    </div>
  );
}

/** Markdown renderer */
function MarkdownView({ content }: { content: string }) {
  return (
    <div className="p-6 prose prose-sm dark:prose-invert max-w-none
      prose-headings:text-surface-800 dark:prose-headings:text-surface-200
      prose-p:text-surface-600 dark:prose-p:text-surface-400
      prose-a:text-primary-600 dark:prose-a:text-primary-400
      prose-code:text-pink-600 dark:prose-code:text-pink-400
      prose-code:bg-surface-100 dark:prose-code:bg-surface-800 prose-code:px-1 prose-code:rounded
      prose-pre:bg-surface-900 prose-pre:text-surface-200
      prose-table:border-surface-200 dark:prose-table:border-surface-700
      prose-th:border-surface-200 dark:prose-th:border-surface-700
      prose-td:border-surface-200 dark:prose-td:border-surface-700
    ">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {content}
      </ReactMarkdown>
    </div>
  );
}

interface FileViewerProps {
  sessionId: string;
  filePath: string;
  onClose: () => void;
}

export function FileViewer({ sessionId, filePath, onClose }: FileViewerProps) {
  const [content, setContent] = useState<string>('');
  const [language, setLanguage] = useState<string>('text');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // For markdown/csv: allow toggling between rendered and raw source view
  const [showRaw, setShowRaw] = useState(false);

  const fileName = filePath.split('/').pop() || filePath;
  const isCsv = language === 'csv';
  const isMarkdown = language === 'markdown';
  const hasToggle = isCsv || isMarkdown;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setShowRaw(false);

    fetchWorkspaceFile(sessionId, filePath).then(result => {
      if (cancelled) return;
      if (result) {
        let displayContent = result.content;
        // Pretty-print JSON files
        if ((result.language === 'json' || filePath.endsWith('.json')) && displayContent) {
          try {
            displayContent = JSON.stringify(JSON.parse(displayContent), null, 2);
          } catch {
            // Not valid JSON, show as-is
          }
        }
        setContent(displayContent);
        setLanguage(result.language || 'text');
      } else {
        setError('Failed to load file');
      }
      setLoading(false);
    });

    return () => { cancelled = true; };
  }, [sessionId, filePath]);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = content;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [content]);

  const handleDownload = useCallback(() => {
    const blob = new Blob([content], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [content, fileName]);

  const renderContent = () => {
    if (showRaw) {
      // Raw source view for markdown/csv
      return (
        <Suspense fallback={
          <pre className="p-4 text-xs font-mono text-surface-600 dark:text-surface-400 whitespace-pre-wrap">{content}</pre>
        }>
          {oneDarkStyle ? (
            <SyntaxHighlighter
              language={isCsv ? 'text' : 'markdown'}
              style={oneDarkStyle}
              showLineNumbers
              customStyle={{ margin: 0, borderRadius: 0, fontSize: '12px', minHeight: '100%' }}
              lineNumberStyle={{ minWidth: '3em', paddingRight: '1em', color: '#636d83' }}
            >
              {content}
            </SyntaxHighlighter>
          ) : (
            <pre className="p-4 text-xs font-mono text-surface-600 dark:text-surface-400 whitespace-pre-wrap">{content}</pre>
          )}
        </Suspense>
      );
    }

    if (isCsv) return <CsvTable content={content} />;
    if (isMarkdown) return <MarkdownView content={content} />;

    // Default: syntax highlighted code
    return (
      <Suspense fallback={
        <pre className="p-4 text-xs font-mono text-surface-600 dark:text-surface-400 whitespace-pre-wrap">{content}</pre>
      }>
        {oneDarkStyle ? (
          <SyntaxHighlighter
            language={language}
            style={oneDarkStyle}
            showLineNumbers
            customStyle={{ margin: 0, borderRadius: 0, fontSize: '12px', minHeight: '100%' }}
            lineNumberStyle={{ minWidth: '3em', paddingRight: '1em', color: '#636d83' }}
          >
            {content}
          </SyntaxHighlighter>
        ) : (
          <pre className="p-4 text-xs font-mono text-surface-600 dark:text-surface-400 whitespace-pre-wrap">{content}</pre>
        )}
      </Suspense>
    );
  };

  return (
    <Dialog.Root open={true} onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/50 z-50 animate-in fade-in" />
        <Dialog.Content
          className={cn(
            'fixed inset-4 lg:inset-12 z-50 flex flex-col overflow-hidden',
            'bg-white dark:bg-surface-900 rounded-xl shadow-2xl',
            'animate-in fade-in zoom-in-95'
          )}
          aria-describedby={undefined}
        >
          <Dialog.Title className="sr-only">{fileName}</Dialog.Title>
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-surface-200 dark:border-surface-700 flex-shrink-0">
            <div className="flex items-center gap-2 min-w-0">
              {getFileIcon(fileName)}
              <span className="text-sm font-mono text-surface-700 dark:text-surface-300 truncate">
                {filePath}
              </span>
              {(language === 'docx' || language === 'pdf') && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 flex-shrink-0">
                  Extracted text
                </span>
              )}
            </div>
            <div className="flex items-center gap-1 flex-shrink-0">
              {/* Toggle raw/rendered for markdown & csv */}
              {hasToggle && !loading && !error && (
                <button
                  onClick={() => setShowRaw(!showRaw)}
                  className={cn(
                    'p-1.5 transition-colors rounded flex items-center gap-1 text-xs',
                    showRaw
                      ? 'text-primary-600 dark:text-primary-400 bg-primary-50 dark:bg-primary-900/20'
                      : 'text-surface-400 hover:text-surface-600 dark:hover:text-surface-300'
                  )}
                  title={showRaw ? 'Show rendered' : 'Show source'}
                >
                  {showRaw ? <Table className="w-3.5 h-3.5" /> : <Code className="w-3.5 h-3.5" />}
                  <span>{showRaw ? (isCsv ? 'Table' : 'Preview') : 'Source'}</span>
                </button>
              )}
              <button
                onClick={handleCopy}
                className="p-1.5 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 transition-colors rounded"
                title="Copy"
              >
                {copied ? <Check className="w-4 h-4 text-green-500" /> : <Copy className="w-4 h-4" />}
              </button>
              <button
                onClick={handleDownload}
                className="p-1.5 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 transition-colors rounded"
                title="Download"
              >
                <Download className="w-4 h-4" />
              </button>
              <Dialog.Close asChild>
                <button className="p-1.5 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 transition-colors rounded">
                  <X className="w-4 h-4" />
                </button>
              </Dialog.Close>
            </div>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-auto">
            {loading ? (
              <div className="flex items-center justify-center h-full">
                <Loader2 className="w-6 h-6 animate-spin text-surface-400" />
              </div>
            ) : error ? (
              <div className="flex items-center justify-center h-full text-surface-400 text-sm">
                {error}
              </div>
            ) : (
              renderContent()
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
