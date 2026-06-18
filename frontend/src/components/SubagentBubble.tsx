/**
 * SubagentBubble Component
 *
 * Displays Sub-Agent activities with:
 * - Collapsible thinking/reasoning section
 * - Tool calls (Brave search, webpage fetch, etc.)
 * - Progress status with visual indicators
 */

import { useState, memo } from 'react';
import { cn, formatDate } from '../lib/utils';
import type { Message, SubagentToolCall } from '../types';
import { useBuilderStore } from '../stores/builderStore';
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  CheckCircle2,
  XCircle,
  Brain,
  Search,
  BookOpen,
  Zap,
  FileText,
  MessageSquare,
  Phone,
  Building2,
  Mic,
  ClipboardCheck,
} from 'lucide-react';

// Sub-Agent display information (theme-aware: light + dark variants)
const SUBAGENT_INFO: Record<string, { name: string; nameKo: string; Icon: typeof Search; colorClass: string; bgClass: string; borderClass: string }> = {
  research_agent: {
    name: 'Research Agent',
    nameKo: '리서치 에이전트',
    Icon: Search,
    colorClass: 'text-cyan-700 dark:text-cyan-300',
    bgClass: 'bg-cyan-50 dark:bg-cyan-900/20',
    borderClass: 'border-cyan-200 dark:border-cyan-800',
  },
  faq_generator: {
    name: 'FAQ Generator',
    nameKo: 'FAQ 생성기',
    Icon: BookOpen,
    colorClass: 'text-emerald-700 dark:text-emerald-300',
    bgClass: 'bg-emerald-50 dark:bg-emerald-900/20',
    borderClass: 'border-emerald-200 dark:border-emerald-800',
  },
  lambda_generator: {
    name: 'Lambda Generator',
    nameKo: 'Lambda 생성기',
    Icon: Zap,
    colorClass: 'text-orange-700 dark:text-orange-300',
    bgClass: 'bg-orange-50 dark:bg-orange-900/20',
    borderClass: 'border-orange-200 dark:border-orange-800',
  },
  openapi_generator: {
    name: 'OpenAPI Generator',
    nameKo: 'OpenAPI 생성기',
    Icon: FileText,
    colorClass: 'text-blue-700 dark:text-blue-300',
    bgClass: 'bg-blue-50 dark:bg-blue-900/20',
    borderClass: 'border-blue-200 dark:border-blue-800',
  },
  prompt_generator: {
    name: 'Prompt Generator',
    nameKo: '프롬프트 생성기',
    Icon: MessageSquare,
    colorClass: 'text-purple-700 dark:text-purple-300',
    bgClass: 'bg-purple-50 dark:bg-purple-900/20',
    borderClass: 'border-purple-200 dark:border-purple-800',
  },
  contact_flow_generator: {
    name: 'Contact Flow Generator',
    nameKo: 'Contact Flow 생성기',
    Icon: Phone,
    colorClass: 'text-green-700 dark:text-green-300',
    bgClass: 'bg-green-50 dark:bg-green-900/20',
    borderClass: 'border-green-200 dark:border-green-800',
  },
  infrastructure_generator: {
    name: 'Infrastructure Generator',
    nameKo: '인프라 생성기',
    Icon: Building2,
    colorClass: 'text-slate-700 dark:text-surface-300',
    bgClass: 'bg-slate-50 dark:bg-surface-800',
    borderClass: 'border-slate-200 dark:border-surface-600',
  },
  interviewer: {
    name: 'Interviewer',
    nameKo: '인터뷰어',
    Icon: Mic,
    colorClass: 'text-pink-700 dark:text-pink-300',
    bgClass: 'bg-pink-50 dark:bg-pink-900/20',
    borderClass: 'border-pink-200 dark:border-pink-800',
  },
  reviewer_agent: {
    name: 'Reviewer Agent',
    nameKo: '리뷰어 에이전트',
    Icon: ClipboardCheck,
    colorClass: 'text-teal-700 dark:text-teal-300',
    bgClass: 'bg-teal-50 dark:bg-teal-900/20',
    borderClass: 'border-teal-200 dark:border-teal-800',
  },
};

// Default info for unknown subagents
const DEFAULT_SUBAGENT_INFO = {
  name: 'Sub-Agent',
  nameKo: '서브 에이전트',
  Icon: Zap,
  colorClass: 'text-surface-700 dark:text-surface-300',
  bgClass: 'bg-surface-50 dark:bg-surface-800',
  borderClass: 'border-surface-200 dark:border-surface-600',
};

interface SubagentBubbleProps {
  message: Message;
}

// Memoized to prevent unnecessary re-renders
export const SubagentBubble = memo(function SubagentBubble({ message }: SubagentBubbleProps) {
  // Rules of Hooks: all hooks must run before any early return.
  const [isExpanded, setIsExpanded] = useState(false);
  const language = useBuilderStore(s => s.language);
  const isKo = language === 'ko-KR';

  const activity = message.subagentActivity;
  if (!activity) return null;

  const info = SUBAGENT_INFO[activity.subagent] || DEFAULT_SUBAGENT_INFO;
  const Icon = info.Icon;

  const isStarted = activity.status === 'started';
  const isRunning = activity.status === 'running';
  const isCompleted = activity.status === 'completed';
  // Used in status badge and icon display
  const hasError = activity.status === 'error';

  const hasThinking = !!(activity.thinking && activity.thinking.length > 0);
  const hasToolCalls = activity.toolCalls.length > 0;
  const hasExpandableContent = hasThinking || hasToolCalls;

  // Auto-show live feed while agent is running with content
  const isLive = (isRunning || isStarted) && (hasThinking || hasToolCalls);

  return (
    <div className="flex justify-start animate-fade-in">
      <div
        className={cn(
          'max-w-[85%] rounded-2xl px-4 py-3 border',
          info.bgClass,
          info.borderClass
        )}
      >
        {/* Header */}
        <div
          className={cn(
            'flex items-center gap-2',
            hasExpandableContent && 'cursor-pointer'
          )}
          onClick={() => hasExpandableContent && setIsExpanded(!isExpanded)}
        >
          {/* Status icon */}
          <div
            className={cn(
              'w-8 h-8 rounded-full flex items-center justify-center',
              isRunning || isStarted ? 'bg-white/50 dark:bg-white/10' : isCompleted ? 'bg-white/70 dark:bg-white/10' : hasError ? 'bg-red-100 dark:bg-red-900/40' : 'bg-surface-100 dark:bg-surface-700'
            )}
          >
            {isRunning || isStarted ? (
              <Loader2 className={cn('w-4 h-4 animate-spin', info.colorClass)} />
            ) : isCompleted ? (
              <CheckCircle2 className="w-4 h-4 text-green-600 dark:text-green-400" />
            ) : hasError ? (
              <XCircle className="w-4 h-4 text-red-600 dark:text-red-400" />
            ) : (
              <Loader2 className={cn('w-4 h-4', info.colorClass)} />
            )}
          </div>

          {/* Agent name and content */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <Icon className={cn('w-4 h-4', info.colorClass)} />
              <span className={cn('text-sm font-medium', info.colorClass)}>
                {isKo ? info.nameKo : info.name}
              </span>
              <span
                className={cn(
                  'text-xs px-1.5 py-0.5 rounded-full',
                  isRunning || isStarted
                    ? 'bg-yellow-100 dark:bg-yellow-900/40 text-yellow-700 dark:text-yellow-300'
                    : isCompleted
                    ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300'
                    : hasError
                    ? 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300'
                    : 'bg-surface-100 dark:bg-surface-700 text-surface-700 dark:text-surface-300'
                )}
              >
                {isStarted ? (isKo ? '시작' : 'Started') : isRunning ? (isKo ? '실행 중' : 'Running') : isCompleted ? (isKo ? '완료' : 'Done') : hasError ? (isKo ? '오류' : 'Error') : (isKo ? '대기' : 'Pending')}
              </span>
            </div>
            {activity.content && (
              <p className="text-sm text-surface-600 dark:text-surface-400 mt-0.5 truncate">
                {activity.content}
              </p>
            )}
          </div>

          {/* Expand/collapse button */}
          {hasExpandableContent && (
            <button
              aria-expanded={isExpanded || isLive}
              aria-label={isKo ? '세부 정보 토글' : 'Toggle details'}
              className="p-1 hover:bg-white/50 dark:hover:bg-white/10 rounded transition-colors"
            >
              {(isExpanded || isLive) ? (
                <ChevronDown className={cn('w-4 h-4', info.colorClass)} />
              ) : (
                <ChevronRight className={cn('w-4 h-4', info.colorClass)} />
              )}
            </button>
          )}
        </div>

        {/* Live feed: auto-shown while running OR completed with thinking */}
        {((isLive || (isCompleted && hasThinking)) && !isExpanded) && (
          <div className="mt-3 pt-3 border-t border-surface-300/30 dark:border-surface-600/30 space-y-2">
            {/* Thinking - live feed while running, static when completed */}
            {hasThinking && (
              (isRunning || isStarted)
                ? <LiveThinkingFeed thinking={activity.thinking!} colorClass={info.colorClass} />
                : <ThinkingSection thinking={activity.thinking!} colorClass={info.colorClass} />
            )}
            {/* Tool calls as inline feed */}
            {hasToolCalls && (
              <div className="space-y-1.5">
                {activity.toolCalls.slice(-5).map((tc, idx) => (
                  <LiveToolCallItem key={idx} toolCall={tc} />
                ))}
              </div>
            )}
          </div>
        )}

        {/* Tool calls summary (collapsed, not running) */}
        {hasToolCalls && !isExpanded && !isLive && (
          <div className="mt-2 text-xs text-surface-500 dark:text-surface-400">
            {(() => {
              const counts: Record<string, number> = {};
              activity.toolCalls.forEach(tc => {
                const name = tc.displayName || tc.tool;
                counts[name] = (counts[name] || 0) + 1;
              });
              return Object.entries(counts).map(([name, count]) => `${name} ×${count}`).join('  ·  ');
            })()}
          </div>
        )}

        {/* Expanded content (manual expand) */}
        {isExpanded && hasExpandableContent && (
          <div className="mt-3 pt-3 border-t border-surface-300/50 dark:border-surface-600/50 space-y-3">
            {/* Thinking section */}
            {hasThinking && (
              <ThinkingSection thinking={activity.thinking!} colorClass={info.colorClass} />
            )}

            {/* Tool calls section */}
            {hasToolCalls && (
              <ToolCallsSection toolCalls={activity.toolCalls} />
            )}
          </div>
        )}

        {/* Timestamp */}
        <div className="text-xs text-surface-400 dark:text-surface-500 mt-2">
          {formatDate(activity.timestamp)}
        </div>
      </div>
    </div>
  );
});

/**
 * Live thinking feed: shows last few lines of thinking in real-time
 */
function LiveThinkingFeed({ thinking, colorClass }: { thinking: string; colorClass: string }) {
  const tail = thinking.length > 500 ? '...' + thinking.slice(-500) : thinking;

  return (
    <div className="rounded-lg bg-white/40 dark:bg-white/5 p-2">
      <div className="flex items-center gap-1.5 mb-1">
        <Brain className={cn('w-3 h-3', colorClass)} />
        <span className={cn('text-xs font-medium', colorClass)}>Thinking...</span>
        <Loader2 className="w-3 h-3 animate-spin text-surface-400 dark:text-surface-500" />
      </div>
      <pre className="text-xs text-surface-600 dark:text-surface-300 whitespace-pre-wrap font-sans leading-relaxed max-h-40 overflow-y-auto">
        {tail}
      </pre>
    </div>
  );
}

/**
 * Live tool call item: compact inline display for running feed
 */
function LiveToolCallItem({ toolCall }: { toolCall: SubagentToolCall }) {
  const isRunning = toolCall.status === 'running';
  const isCompleted = toolCall.status === 'completed';
  const searchQuery = toolCall.input?.query as string | undefined;

  return (
    <div className={cn(
      'flex items-center gap-2 text-xs px-2 py-1.5 rounded-lg',
      isRunning ? 'bg-blue-50/70 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300' : isCompleted ? 'bg-green-50/70 dark:bg-green-900/30 text-green-700 dark:text-green-300' : 'bg-red-50/70 dark:bg-red-900/30 text-red-700 dark:text-red-300'
    )}>
      {isRunning && <Loader2 className="w-3 h-3 animate-spin flex-shrink-0" />}
      {isCompleted && <CheckCircle2 className="w-3 h-3 flex-shrink-0" />}
      <span className="font-medium flex-shrink-0">{toolCall.displayName}</span>
      {searchQuery && (
        <code className="bg-white/60 dark:bg-white/10 px-1.5 py-0.5 rounded truncate max-w-[250px]">
          {searchQuery}
        </code>
      )}
    </div>
  );
}

/**
 * Collapsible thinking/reasoning section
 */
function ThinkingSection({ thinking, colorClass }: { thinking: string; colorClass: string }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const isKo = useBuilderStore(s => s.language) === 'ko-KR';

  // Truncate long thinking content
  const maxLength = 500;
  const isTruncated = thinking.length > maxLength;
  const displayContent = isExpanded ? thinking : thinking.slice(0, maxLength);

  return (
    <div className="rounded-lg bg-white/50 dark:bg-white/5 p-3">
      <div
        className="flex items-center gap-2 cursor-pointer"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <Brain className={cn('w-4 h-4', colorClass)} />
        <span className={cn('text-xs font-medium', colorClass)}>Thinking</span>
        <button
          aria-expanded={isExpanded}
          aria-label={isKo ? '사고 과정 토글' : 'Toggle thinking'}
          className="p-0.5 hover:bg-surface-200/50 dark:hover:bg-surface-600/50 rounded transition-colors"
        >
          {isExpanded ? (
            <ChevronDown className="w-3 h-3 text-surface-500 dark:text-surface-400" />
          ) : (
            <ChevronRight className="w-3 h-3 text-surface-500 dark:text-surface-400" />
          )}
        </button>
      </div>
      {isExpanded && (
        <div className="mt-2">
          <pre className="text-xs text-surface-600 dark:text-surface-300 whitespace-pre-wrap font-sans">
            {displayContent}
            {isTruncated && !isExpanded && '...'}
          </pre>
          {isTruncated && (
            <button
              className="text-xs text-blue-600 dark:text-blue-400 hover:underline mt-1"
              onClick={(e) => {
                e.stopPropagation();
                setIsExpanded(!isExpanded);
              }}
            >
              {isExpanded ? (isKo ? '접기' : 'Collapse') : (isKo ? '더 보기' : 'Show more')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Tool calls list section
 */
function ToolCallsSection({ toolCalls }: { toolCalls: SubagentToolCall[] }) {
  const isKo = useBuilderStore(s => s.language) === 'ko-KR';
  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-surface-500 dark:text-surface-400">{isKo ? '도구 호출' : 'Tool Calls'}</div>
      {toolCalls.map((tc, idx) => (
        <ToolCallItem key={idx} toolCall={tc} />
      ))}
    </div>
  );
}

/**
 * Individual tool call item
 */
function ToolCallItem({ toolCall }: { toolCall: SubagentToolCall }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const isKo = useBuilderStore(s => s.language) === 'ko-KR';
  const isRunning = toolCall.status === 'running';
  const isCompleted = toolCall.status === 'completed';
  const isError = toolCall.status === 'error';

  const hasInput = !!(toolCall.input && Object.keys(toolCall.input).length > 0);
  const hasResult = !!toolCall.result;
  const hasExpandableContent = hasInput || hasResult;

  // Extract search query for display
  const searchQuery = toolCall.input?.query as string | undefined;

  return (
    <div
      className={cn(
        'rounded-lg p-2 border',
        isRunning && 'bg-blue-50/50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800',
        isCompleted && 'bg-green-50/50 dark:bg-green-900/20 border-green-200 dark:border-green-800',
        isError && 'bg-red-50/50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
      )}
    >
      <div
        className={cn(
          'flex items-center gap-2',
          hasExpandableContent && 'cursor-pointer'
        )}
        onClick={() => hasExpandableContent && setIsExpanded(!isExpanded)}
      >
        {/* Status indicator */}
        {isRunning && <Loader2 className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400 animate-spin" />}
        {isCompleted && <CheckCircle2 className="w-3.5 h-3.5 text-green-600 dark:text-green-400" />}
        {isError && <XCircle className="w-3.5 h-3.5 text-red-600 dark:text-red-400" />}

        {/* Tool name */}
        <span className="text-xs font-medium text-surface-700 dark:text-surface-300">
          {toolCall.displayName}
        </span>

        {/* Search query preview */}
        {searchQuery && (
          <code className="text-xs bg-surface-100 dark:bg-surface-700 px-1.5 py-0.5 rounded text-surface-600 dark:text-surface-300 truncate max-w-[200px]">
            "{searchQuery}"
          </code>
        )}

        {/* Expand button */}
        {hasExpandableContent && (
          <button
            aria-expanded={isExpanded}
            aria-label={isKo ? '도구 세부 정보 토글' : 'Toggle tool details'}
            className="ml-auto p-0.5 hover:bg-surface-200/50 dark:hover:bg-surface-600/50 rounded transition-colors"
          >
            {isExpanded ? (
              <ChevronDown className="w-3 h-3 text-surface-500 dark:text-surface-400" />
            ) : (
              <ChevronRight className="w-3 h-3 text-surface-500 dark:text-surface-400" />
            )}
          </button>
        )}
      </div>

      {/* Expanded details */}
      {isExpanded && hasExpandableContent && (
        <div className="mt-2 pt-2 border-t border-surface-300/50 dark:border-surface-600/50 space-y-2">
          {hasInput && (
            <div>
              <div className="text-xs font-medium text-surface-500 dark:text-surface-400 mb-1">{isKo ? '입력' : 'Input'}</div>
              <pre className="text-xs bg-white/50 dark:bg-black/30 text-surface-700 dark:text-surface-300 p-2 rounded overflow-x-auto max-h-32 overflow-y-auto">
                {JSON.stringify(toolCall.input, null, 2)}
              </pre>
            </div>
          )}
          {hasResult && (
            <div>
              <div className="text-xs font-medium text-surface-500 dark:text-surface-400 mb-1">{isKo ? '결과' : 'Result'}</div>
              <pre className="text-xs bg-white/50 dark:bg-black/30 text-surface-700 dark:text-surface-300 p-2 rounded overflow-x-auto max-h-32 overflow-y-auto">
                {typeof toolCall.result === 'string'
                  ? toolCall.result.slice(0, 500) + (toolCall.result.length > 500 ? '...' : '')
                  : JSON.stringify(toolCall.result, null, 2).slice(0, 500)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
