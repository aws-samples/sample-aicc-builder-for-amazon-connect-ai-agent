/**
 * ACXD Flow Preview (chat / centre panel)
 *
 * Mirrors ContactFlowPreview: while the flow is still streaming the JSON is
 * shown as it arrives; once the asset is complete and parses, the diagram
 * (derived deterministically from the JSON by AcxdFlowDiagram) becomes the
 * default view, with the JSON one tab away.
 */

import { useState, useMemo, useEffect, lazy, Suspense } from 'react';
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
import { AcxdFlowDiagram } from './AcxdFlowDiagram';
import { useBuilderStore } from '../stores/builderStore';
import type { AssetPreview } from '../types';

const SyntaxHighlighter = lazy(() =>
  import('react-syntax-highlighter/dist/esm/prism-light').then((mod) => ({ default: mod.default })),
);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let oneDarkStyle: any = null;
import('react-syntax-highlighter/dist/esm/styles/prism/one-dark').then((mod) => { oneDarkStyle = mod.default; });

interface AcxdFlowPreviewProps {
  preview: AssetPreview;
  language?: string;
}

/** The flow JSON, unwrapped from a ```json fence when the stream carries one. */
function extractFlowJson(content: string): string | null {
  const fenced = content.match(/```\s*json\s*[\r\n]+([\s\S]*?)[\r\n]+\s*```/i)?.[1]?.trim() ?? content.trim();
  if (!fenced.startsWith('{')) return null;
  try {
    const parsed: unknown = JSON.parse(fenced);
    return parsed && typeof parsed === 'object' && 'nodes' in (parsed as Record<string, unknown>) ? fenced : null;
  } catch {
    return null;
  }
}

function flowTitle(json: string | null, fallback: string | undefined): string {
  if (json) {
    try {
      const doc = JSON.parse(json) as Record<string, unknown>;
      const id = doc.flowId ?? doc.name;
      if (typeof id === 'string' && id.trim()) return id.trim();
    } catch { /* fall through */ }
  }
  return fallback?.replace(/\.json$/i, '') || '';
}

export function AcxdFlowPreview({ preview, language = 'ko-KR' }: AcxdFlowPreviewProps) {
  const ko = language === 'ko-KR';
  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<string>('diagram');
  const setFullscreenAssetKey = useBuilderStore((s) => s.setFullscreenAssetKey);

  const isLazyLoading = !!(preview.s3Key && !preview.content);
  const content = preview.content;
  const isStreaming = !preview.isComplete;

  const flowJson = useMemo(() => extractFlowJson(content), [content]);
  const canShowDiagram = !!flowJson && preview.isComplete;
  const title = useMemo(() => flowTitle(flowJson, preview.fileName), [flowJson, preview.fileName]);

  // JSON while the flow streams; the diagram the moment it completes and
  // parses. Only reacts to that flip, so a JSON tab the user picked afterwards
  // stays put.
  useEffect(() => {
    setActiveTab(canShowDiagram ? 'diagram' : 'json');
  }, [canShowDiagram]);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(flowJson || content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDownloadJson = () => {
    const blob = new Blob([flowJson || content], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = preview.fileName || `${title || 'flow'}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleFullscreen = () => {
    const previews = useBuilderStore.getState().assetPreviews;
    const key = Object.keys(previews).find((k) => previews[k] === preview)
      || Object.keys(previews).find((k) => previews[k].assetType === 'acxd_flow' && previews[k].content === preview.content);
    if (key) setFullscreenAssetKey(key);
  };

  const tabClass = (tab: string) => cn(
    'flex items-center gap-2 px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px',
    activeTab === tab
      ? 'border-fuchsia-600 text-fuchsia-700 dark:text-fuchsia-300 bg-white dark:bg-surface-850'
      : 'border-transparent text-fuchsia-600 dark:text-fuchsia-400 hover:text-fuchsia-700 dark:hover:text-fuchsia-300 hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/40',
  );

  return (
    <div className="animate-fade-in my-3">
      <div className="w-full rounded-2xl border overflow-hidden text-fuchsia-600 dark:text-fuchsia-300 bg-fuchsia-50 dark:bg-fuchsia-900/20 border-fuchsia-200 dark:border-fuchsia-800">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-fuchsia-200 dark:border-fuchsia-800">
          <div className="flex items-center gap-2">
            <div className={cn('w-8 h-8 rounded-full flex items-center justify-center', preview.isComplete ? 'bg-white/80 dark:bg-white/10' : 'bg-white/50 dark:bg-white/5')}>
              {!preview.isComplete ? <Loader2 className="w-4 h-4 animate-spin" /> : <Workflow className="w-4 h-4" />}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm">{ko ? 'ACXD 플로우' : 'ACXD Flow'}</span>
                {title && <span className="text-xs opacity-70 font-mono">({title})</span>}
              </div>
              {preview.fileName && <span className="text-xs opacity-70 font-mono">{preview.fileName}</span>}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button onClick={handleCopy} className="p-1.5 rounded-md transition-colors hover:bg-white/50 dark:hover:bg-white/10 text-current opacity-70 hover:opacity-100" title={ko ? '복사' : 'Copy'} aria-label={ko ? '복사' : 'Copy'}>
              {copied ? <Check className="w-4 h-4 text-green-600 dark:text-green-400" /> : <Copy className="w-4 h-4" />}
            </button>
            {preview.isComplete && content && (
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

        {isLazyLoading ? (
          <div className="flex flex-col items-center justify-center py-12 text-surface-500 dark:text-surface-400">
            <Loader2 className="w-8 h-8 animate-spin mb-3" />
            <span className="text-sm">{ko ? '플로우 로딩 중...' : 'Loading flow...'}</span>
          </div>
        ) : (
          <Tabs.Root value={activeTab} onValueChange={setActiveTab}>
            <Tabs.List className="flex border-b border-fuchsia-200 dark:border-fuchsia-800 bg-fuchsia-100/50 dark:bg-fuchsia-900/30">
              {canShowDiagram && (
                <Tabs.Trigger value="diagram" className={tabClass('diagram')}>
                  <Eye className="w-4 h-4" />{ko ? '다이어그램' : 'Diagram'}
                </Tabs.Trigger>
              )}
              <Tabs.Trigger value="json" className={tabClass('json')}>
                <Code className="w-4 h-4" />{ko ? 'JSON 코드' : 'JSON Code'}
              </Tabs.Trigger>
            </Tabs.List>

            {canShowDiagram && flowJson && (
              <Tabs.Content value="diagram" className="relative">
                <div className="bg-white dark:bg-surface-900">
                  <AcxdFlowDiagram flowJson={flowJson} language={language} height={460} />
                </div>
              </Tabs.Content>
            )}

            <Tabs.Content value="json" className="relative">
              <div className="relative overflow-auto max-h-[60vh]">
                {isStreaming && (
                  <div className="absolute top-2 right-2 z-10">
                    <div className="flex items-center gap-2 text-xs bg-white/90 px-2 py-1 rounded-full shadow-sm">
                      <span className="w-2 h-2 bg-fuchsia-500 rounded-full animate-pulse" />
                      <span className="text-gray-600">{ko ? '생성 중...' : 'Generating...'}</span>
                    </div>
                  </div>
                )}
                {preview.isComplete ? (
                  <Suspense fallback={<pre className="p-4 text-sm font-mono text-gray-100 whitespace-pre-wrap" style={{ background: 'rgba(0,0,0,0.85)' }}>{flowJson || content}</pre>}>
                    <SyntaxHighlighter language="json" style={oneDarkStyle || {}} customStyle={{ margin: 0, borderRadius: 0, fontSize: '0.8rem', background: 'rgba(0,0,0,0.85)' }} showLineNumbers wrapLines>
                      {flowJson || content}
                    </SyntaxHighlighter>
                  </Suspense>
                ) : (
                  <pre className="p-4 text-sm font-mono text-gray-100 whitespace-pre-wrap break-words" style={{ margin: 0, background: 'rgba(0,0,0,0.85)', minHeight: '100px' }}>
                    {content}
                    <span className="animate-pulse">▊</span>
                  </pre>
                )}
              </div>
            </Tabs.Content>
          </Tabs.Root>
        )}

        {/* Footer */}
        <div className="px-4 py-2 bg-white/30 dark:bg-white/5 border-t border-fuchsia-200 dark:border-fuchsia-800 flex items-center justify-between text-xs">
          <span className="opacity-70">
            {activeTab === 'diagram'
              ? (ko ? '대화 플로우 시각화 — 노드를 드래그하거나 확대할 수 있어요' : 'Conversation flow — drag nodes or zoom')
              : `${content.split('\n').length} ${ko ? '줄' : 'lines'}`}
          </span>
          {preview.isComplete && (
            <span className="flex items-center gap-1 text-fuchsia-600 dark:text-fuchsia-400">
              <CheckCircle2 className="w-3 h-3" />{ko ? '완료' : 'Complete'}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default AcxdFlowPreview;
