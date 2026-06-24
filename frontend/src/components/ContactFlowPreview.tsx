/**
 * Contact Flow Preview Component
 *
 * Displays Contact Flow with two views:
 * 1. Visual Diagram (Mermaid) - default view
 * 2. JSON Code - for technical details
 *
 * PERFORMANCE: No typewriter animation. Uses <pre> during streaming.
 * SyntaxHighlighter only for completed JSON view.
 */

import { useState, useRef, useMemo, useEffect, lazy, Suspense } from 'react';
import * as Tabs from '@radix-ui/react-tabs';
import {
  Workflow,
  Copy,
  Check,
  CheckCircle2,
  Loader2,
  Eye,
  Code,
  Download,
  Maximize2,
} from 'lucide-react';
import { cn } from '../lib/utils';
import { FlowDiagram } from './FlowDiagram';
import { useBuilderStore } from '../stores/builderStore';
import type { AssetPreview } from '../types';

// Lazy-load SyntaxHighlighter
const SyntaxHighlighter = lazy(() =>
  import('react-syntax-highlighter/dist/esm/prism-light').then(mod => ({ default: mod.default }))
);
let oneDarkStyle: any = null;
import('react-syntax-highlighter/dist/esm/styles/prism/one-dark').then(mod => { oneDarkStyle = mod.default; });

interface ContactFlowPreviewProps {
  preview: AssetPreview;
  language?: string;
}

interface ParsedContent {
  json: string | null;
  mermaid: string | null;
  rawContent: string;
}

function parseContactFlowContent(content: string): ParsedContent {
  const result: ParsedContent = { json: null, mermaid: null, rawContent: content };

  const mermaidMatch = content.match(/```\s*mermaid\s*[\r\n]+([\s\S]*?)[\r\n]+\s*```/i);
  if (mermaidMatch) result.mermaid = mermaidMatch[1].trim();

  const jsonMatch = content.match(/```\s*json\s*[\r\n]+([\s\S]*?)[\r\n]+\s*```/i);
  if (jsonMatch) result.json = jsonMatch[1].trim();

  if (!result.json && !result.mermaid) {
    const trimmed = content.trim();
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
      try { JSON.parse(trimmed); result.json = trimmed; } catch { /* not JSON */ }
    }
  }

  return result;
}

export function ContactFlowPreview({ preview, language = 'ko-KR' }: ContactFlowPreviewProps) {
  const [copied, setCopied] = useState(false);
  // Default to the diagram tab, but fall back to JSON when there's no mermaid
  // (e.g. a rehydrated or imported flow that is pure JSON). Otherwise the body
  // renders nothing — the diagram tab/content is gated on parsedContent.mermaid,
  // so an activeTab of 'diagram' with no diagram shows an empty card.
  const [activeTab, setActiveTab] = useState<string>('diagram');
  const contentRef = useRef<HTMLDivElement>(null);
  const setFullscreenAssetKey = useBuilderStore((s) => s.setFullscreenAssetKey);
  const ko = language === 'ko-KR';

  // Detect lazy-loading state: s3Key exists but content not yet loaded from S3
  const isLazyLoading = !!(preview.s3Key && !preview.content);

  const parsedContent = useMemo(() => parseContactFlowContent(preview.content), [preview.content]);

  // The diagram is DERIVED from the validated Connect JSON (see FlowDiagram /
  // lib/contactFlowGraph). No dependency on hand-authored mermaid — the JSON is
  // the single source of truth, so the diagram is always valid and matches the
  // flow. We can render a diagram whenever we have JSON content (only while the
  // flow is still streaming and not yet valid JSON do we fall back to JSON-only).
  const flowJson = parsedContent.json;
  const canShowDiagram = !!flowJson && preview.isComplete;

  // If the active tab is 'diagram' but we can't draw one yet (still streaming /
  // not valid JSON), show the JSON tab so content is always visible.
  useEffect(() => {
    if (activeTab === 'diagram' && !canShowDiagram) {
      setActiveTab('json');
    }
  }, [activeTab, canShowDiagram]);

  // Use content directly - no typewriter animation
  const content = preview.content;
  const lines = content.split('\n');
  const isStreaming = !preview.isComplete;

  const handleCopy = async () => {
    const contentToCopy = parsedContent.json || content;
    await navigator.clipboard.writeText(contentToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // Download the Contact Flow JSON directly (client-side blob).
  const handleDownloadJson = () => {
    const json = parsedContent.json || content;
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = preview.fileName || `${preview.operationId || 'contact_flow'}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // Find this preview's key in the store to open it fullscreen.
  const handleFullscreen = () => {
    const previews = useBuilderStore.getState().assetPreviews;
    const key = Object.keys(previews).find((k) => previews[k] === preview)
      || Object.keys(previews).find((k) => previews[k].content === preview.content && previews[k].assetType === 'contact_flow');
    if (key) setFullscreenAssetKey(key);
  };

  const jsonDisplayContent = useMemo(() => {
    const parsed = parseContactFlowContent(content);
    return parsed.json || content;
  }, [content]);

  return (
    <div className="animate-fade-in my-3">
      <div className="w-full rounded-2xl border overflow-hidden text-green-600 dark:text-green-300 bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-green-200 dark:border-green-800">
          <div className="flex items-center gap-2">
            <div className={cn('w-8 h-8 rounded-full flex items-center justify-center', preview.isComplete ? 'bg-white/80 dark:bg-white/10' : 'bg-white/50 dark:bg-white/5')}>
              {!preview.isComplete ? <Loader2 className="w-4 h-4 animate-spin" /> : <Workflow className="w-4 h-4" />}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm">Contact Flow</span>
                {preview.operationId && <span className="text-xs opacity-70 font-mono">({preview.operationId})</span>}
              </div>
              {preview.fileName && <span className="text-xs opacity-70 font-mono">{preview.fileName}</span>}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={handleCopy} className="p-1.5 rounded-md transition-colors hover:bg-white/50 dark:hover:bg-white/10 text-current opacity-70 hover:opacity-100" title={ko ? '복사' : 'Copy'} aria-label={ko ? '복사' : 'Copy'}>
              {copied ? <Check className="w-4 h-4 text-green-600 dark:text-green-400" /> : <Copy className="w-4 h-4" />}
            </button>
            {preview.isComplete && (parsedContent.json || content) && (
              <button onClick={handleDownloadJson} className="p-1.5 rounded-md transition-colors hover:bg-white/50 dark:hover:bg-white/10 text-current opacity-70 hover:opacity-100" title={ko ? 'JSON 다운로드' : 'Download JSON'} aria-label={ko ? 'JSON 다운로드' : 'Download JSON'}>
                <Download className="w-4 h-4" />
              </button>
            )}
            {preview.isComplete && (
              <button onClick={handleFullscreen} className="p-1.5 rounded-md transition-colors hover:bg-white/50 dark:hover:bg-white/10 text-current opacity-70 hover:opacity-100" title={ko ? '전체 화면' : 'Fullscreen'} aria-label={ko ? '전체 화면' : 'Fullscreen'}>
                <Maximize2 className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>

        {/* Tabs */}
        {isLazyLoading ? (
          <div className="flex flex-col items-center justify-center py-12 text-surface-500 dark:text-surface-400">
            <Loader2 className="w-8 h-8 animate-spin mb-3" />
            <span className="text-sm">{language === 'ko-KR' ? 'Contact Flow 로딩 중...' : 'Loading Contact Flow...'}</span>
          </div>
        ) : (
        <Tabs.Root value={activeTab} onValueChange={setActiveTab}>
          <Tabs.List className="flex border-b border-green-200 dark:border-green-800 bg-green-100/50 dark:bg-green-900/30">
            {canShowDiagram && (
              <Tabs.Trigger value="diagram" className={cn('flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px', activeTab === 'diagram' ? 'border-green-600 text-green-700 dark:text-green-300 bg-white dark:bg-surface-850' : 'border-transparent text-green-600 dark:text-green-400 hover:text-green-700 dark:hover:text-green-300 hover:bg-green-100 dark:hover:bg-green-900/40')}>
                <Eye className="w-4 h-4" />{language === 'ko-KR' ? '다이어그램' : 'Diagram'}
              </Tabs.Trigger>
            )}
            <Tabs.Trigger value="json" className={cn('flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px', activeTab === 'json' ? 'border-green-600 text-green-700 dark:text-green-300 bg-white dark:bg-surface-850' : 'border-transparent text-green-600 dark:text-green-400 hover:text-green-700 dark:hover:text-green-300 hover:bg-green-100 dark:hover:bg-green-900/40')}>
              <Code className="w-4 h-4" />{language === 'ko-KR' ? 'JSON 코드' : 'JSON Code'}
            </Tabs.Trigger>
          </Tabs.List>

          {canShowDiagram && flowJson && (
            <Tabs.Content value="diagram" className="relative">
              <div className="bg-white dark:bg-surface-900">
                <FlowDiagram flowJson={flowJson} language={language} />
              </div>
            </Tabs.Content>
          )}

          <Tabs.Content value="json" className="relative">
            <div ref={contentRef} className="relative overflow-auto max-h-[70vh]">
              {isStreaming && (
                <div className="absolute top-2 right-2 z-10">
                  <div className="flex items-center gap-2 text-xs bg-white/90 px-2 py-1 rounded-full shadow-sm">
                    <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
                    <span className="text-gray-600">{language === 'ko-KR' ? '생성 중...' : 'Generating...'}</span>
                  </div>
                </div>
              )}
              {preview.isComplete ? (
                <Suspense fallback={<pre className="p-4 text-sm font-mono text-gray-100 whitespace-pre-wrap" style={{ background: 'rgba(0,0,0,0.85)' }}>{jsonDisplayContent}</pre>}>
                  <SyntaxHighlighter language="json" style={oneDarkStyle || {}} customStyle={{ margin: 0, borderRadius: 0, fontSize: '0.8rem', background: 'rgba(0,0,0,0.85)' }} showLineNumbers wrapLines>
                    {jsonDisplayContent}
                  </SyntaxHighlighter>
                </Suspense>
              ) : (
                <pre className="p-4 text-sm font-mono text-gray-100 whitespace-pre-wrap break-words" style={{ margin: 0, background: 'rgba(0,0,0,0.85)', minHeight: '100px' }}>
                  {jsonDisplayContent}
                  <span className="animate-pulse">▊</span>
                </pre>
              )}
            </div>
          </Tabs.Content>
        </Tabs.Root>
        )}

        {/* Footer */}
        <div className="px-4 py-2 bg-white/30 dark:bg-white/5 border-t border-green-200 dark:border-green-800 flex items-center justify-between text-xs">
          <span className="opacity-70">
            {activeTab === 'diagram' ? (language === 'ko-KR' ? 'Contact Flow 시각화' : 'Contact Flow Visualization') : `${lines.length} ${language === 'ko-KR' ? '줄' : 'lines'}`}
          </span>
          {preview.isComplete && (
            <span className="flex items-center gap-1 text-green-600 dark:text-green-400">
              <CheckCircle2 className="w-3 h-3" />{language === 'ko-KR' ? '완료' : 'Complete'}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default ContactFlowPreview;
