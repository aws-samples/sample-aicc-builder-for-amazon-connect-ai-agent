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

import { useState, useRef, useMemo, lazy, Suspense } from 'react';
import * as Tabs from '@radix-ui/react-tabs';
import {
  Workflow,
  Copy,
  Check,
  CheckCircle2,
  Loader2,
  Eye,
  Code,
} from 'lucide-react';
import { cn } from '../lib/utils';
import { MermaidDiagram } from './MermaidDiagram';
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
  const [activeTab, setActiveTab] = useState<string>('diagram');
  const contentRef = useRef<HTMLDivElement>(null);

  // Detect lazy-loading state: s3Key exists but content not yet loaded from S3
  const isLazyLoading = !!(preview.s3Key && !preview.content);

  const parsedContent = useMemo(() => parseContactFlowContent(preview.content), [preview.content]);

  // Use content directly - no typewriter animation
  const content = preview.content;
  const lines = content.split('\n');
  const isStreaming = !preview.isComplete;

  const handleCopy = async () => {
    const contentToCopy = activeTab === 'json' ? (parsedContent.json || content) : (parsedContent.mermaid || content);
    await navigator.clipboard.writeText(contentToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const jsonDisplayContent = useMemo(() => {
    const parsed = parseContactFlowContent(content);
    return parsed.json || content;
  }, [content]);

  return (
    <div className="animate-fade-in my-3">
      <div className="w-full rounded-2xl border overflow-hidden text-green-600 bg-green-50 border-green-200">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-green-200">
          <div className="flex items-center gap-2">
            <div className={cn('w-8 h-8 rounded-full flex items-center justify-center', preview.isComplete ? 'bg-white/80' : 'bg-white/50')}>
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
          <button onClick={handleCopy} className="p-1.5 rounded-md transition-colors hover:bg-white/50 text-current opacity-70 hover:opacity-100" title={language === 'ko-KR' ? '복사' : 'Copy'}>
            {copied ? <Check className="w-4 h-4 text-green-600" /> : <Copy className="w-4 h-4" />}
          </button>
        </div>

        {/* Tabs */}
        {isLazyLoading ? (
          <div className="flex flex-col items-center justify-center py-12 text-gray-500">
            <Loader2 className="w-8 h-8 animate-spin mb-3" />
            <span className="text-sm">{language === 'ko-KR' ? 'Contact Flow 로딩 중...' : 'Loading Contact Flow...'}</span>
          </div>
        ) : (
        <Tabs.Root value={activeTab} onValueChange={setActiveTab}>
          <Tabs.List className="flex border-b border-green-200 bg-green-100/50">
            {parsedContent.mermaid && (
              <Tabs.Trigger value="diagram" className={cn('flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px', activeTab === 'diagram' ? 'border-green-600 text-green-700 bg-white' : 'border-transparent text-green-600 hover:text-green-700 hover:bg-green-100')}>
                <Eye className="w-4 h-4" />{language === 'ko-KR' ? '다이어그램' : 'Diagram'}
              </Tabs.Trigger>
            )}
            <Tabs.Trigger value="json" className={cn('flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px', activeTab === 'json' ? 'border-green-600 text-green-700 bg-white' : 'border-transparent text-green-600 hover:text-green-700 hover:bg-green-100')}>
              <Code className="w-4 h-4" />{language === 'ko-KR' ? 'JSON 코드' : 'JSON Code'}
            </Tabs.Trigger>
          </Tabs.List>

          {parsedContent.mermaid && (
            <Tabs.Content value="diagram" className="relative">
              {isStreaming && (
                <div className="absolute top-2 right-2 z-10">
                  <div className="flex items-center gap-2 text-xs bg-white/90 px-2 py-1 rounded-full shadow-sm">
                    <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
                    <span className="text-gray-600">{language === 'ko-KR' ? '생성 중...' : 'Generating...'}</span>
                  </div>
                </div>
              )}
              <div className="max-h-[70vh] overflow-auto bg-white">
                <MermaidDiagram chart={parsedContent.mermaid} language={language} className="min-h-[300px]" />
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
        <div className="px-4 py-2 bg-white/30 border-t border-green-200 flex items-center justify-between text-xs">
          <span className="opacity-70">
            {activeTab === 'diagram' ? (language === 'ko-KR' ? 'Contact Flow 시각화' : 'Contact Flow Visualization') : `${lines.length} ${language === 'ko-KR' ? '줄' : 'lines'}`}
          </span>
          {preview.isComplete && (
            <span className="flex items-center gap-1 text-green-600">
              <CheckCircle2 className="w-3 h-3" />{language === 'ko-KR' ? '완료' : 'Complete'}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default ContactFlowPreview;
