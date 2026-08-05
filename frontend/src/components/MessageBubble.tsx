/**
 * Message Bubble Component
 *
 * Renders individual chat messages with markdown support
 * Supports: user, assistant, system, tool, and thinking message types
 */

import { useState, useEffect, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { cn, formatDate } from '../lib/utils';
import type { Message, BuilderPhase } from '../types';
import { PHASE_LABELS, PHASE_ICONS } from '../types';
import { Copy, Check, Wrench, Loader2, CheckCircle2, XCircle, Brain, ChevronDown, ChevronRight, FileCode, FileText, Workflow, Server, BookOpen, Package, PanelRight } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { AssetPreviewBubble } from './AssetPreviewBubble';
import { SubagentBubble } from './SubagentBubble';
import { AttachmentChips } from './AttachmentPreview';

// Tool display names (bilingual)
const TOOL_DISPLAY_NAMES: Record<string, { name: string; nameEn: string; description: string; descriptionEn: string }> = {
  introspect_database: { name: '데이터베이스 분석', nameEn: 'Database Analysis', description: '데이터베이스 스키마를 분석하고 있습니다', descriptionEn: 'Analyzing database schema' },
  save_operation_spec: { name: '작업 사양 저장', nameEn: 'Save Operation Spec', description: '작업 사양을 저장하고 있습니다', descriptionEn: 'Saving operation spec' },
  get_operation_spec: { name: '작업 사양 조회', nameEn: 'Get Operation Spec', description: '작업 사양을 조회하고 있습니다', descriptionEn: 'Retrieving operation spec' },
  list_operations: { name: '작업 목록 조회', nameEn: 'List Operations', description: '정의된 작업 목록을 조회하고 있습니다', descriptionEn: 'Listing defined operations' },
  generate_lambda_function: { name: 'Lambda 함수 생성', nameEn: 'Generate Lambda', description: 'Lambda 함수 코드를 생성하고 있습니다', descriptionEn: 'Generating Lambda function code' },
  generate_ai_prompt: { name: 'AI 프롬프트 생성', nameEn: 'Generate AI Prompt', description: 'AI 프롬프트를 생성하고 있습니다', descriptionEn: 'Generating AI prompt' },
  generate_openapi_spec: { name: 'OpenAPI 스펙 생성', nameEn: 'Generate OpenAPI Spec', description: 'OpenAPI 스펙을 생성하고 있습니다', descriptionEn: 'Generating OpenAPI spec' },
  generate_contact_flow: { name: 'Contact Flow 생성', nameEn: 'Generate Contact Flow', description: 'Contact Flow를 생성하고 있습니다', descriptionEn: 'Generating Contact Flow' },
  generate_cdk_infrastructure: { name: '인프라 생성', nameEn: 'Generate Infrastructure', description: '인프라 코드를 생성하고 있습니다', descriptionEn: 'Generating infrastructure code' },
  infrastructure_generator_agent: { name: '인프라 생성', nameEn: 'Infrastructure Generator', description: 'CloudFormation 인프라 템플릿을 생성하고 있습니다', descriptionEn: 'Generating CloudFormation templates' },
  merge_infrastructure_fragments: { name: '인프라 병합', nameEn: 'Merge Infrastructure', description: '생성된 인프라 조각들을 병합하고 있습니다', descriptionEn: 'Merging infrastructure fragments' },
  lambda_generator_agent: { name: 'Lambda 함수 생성', nameEn: 'Lambda Generator', description: 'Lambda 함수 코드를 생성하고 있습니다', descriptionEn: 'Generating Lambda function code' },
  openapi_generator_agent: { name: 'OpenAPI 스펙 생성', nameEn: 'OpenAPI Generator', description: 'OpenAPI 스펙을 생성하고 있습니다', descriptionEn: 'Generating OpenAPI spec' },
  prompt_generator_agent: { name: 'AI 프롬프트 생성', nameEn: 'AI Prompt Generator', description: 'AI 프롬프트를 생성하고 있습니다', descriptionEn: 'Generating AI prompt' },
  contact_flow_generator_agent: { name: 'Contact Flow 생성', nameEn: 'Contact Flow Generator', description: 'Contact Flow를 생성하고 있습니다', descriptionEn: 'Generating Contact Flow' },
  package_and_upload_assets: { name: '에셋 패키징', nameEn: 'Package Assets', description: '생성된 에셋을 패키징하고 있습니다', descriptionEn: 'Packaging generated assets' },
  update_progress: { name: '진행 상황 업데이트', nameEn: 'Update Progress', description: '진행 상황을 업데이트하고 있습니다', descriptionEn: 'Updating progress' },
  reviewer_agent: { name: '검증 에이전트', nameEn: 'Reviewer Agent', description: '생성된 자산을 검증하고 있습니다', descriptionEn: 'Reviewing generated assets' },
  research_agent: { name: '리서치 에이전트', nameEn: 'Research Agent', description: '웹에서 정보를 조사하고 있습니다', descriptionEn: 'Researching information from the web' },
  faq_generator_agent: { name: 'FAQ 생성', nameEn: 'FAQ Generator', description: 'FAQ 문서를 생성하고 있습니다', descriptionEn: 'Generating FAQ documents' },
  read_workspace_file: { name: '파일 읽기', nameEn: 'Read File', description: '워크스페이스 파일을 읽고 있습니다', descriptionEn: 'Reading workspace file' },
  write_workspace_file: { name: '파일 쓰기', nameEn: 'Write File', description: '워크스페이스에 파일을 작성하고 있습니다', descriptionEn: 'Writing file to workspace' },
  patch_workspace_file: { name: '파일 수정', nameEn: 'Patch File', description: '파일 내용을 수정하고 있습니다', descriptionEn: 'Patching file content' },
  list_workspace_dir: { name: '디렉토리 조회', nameEn: 'List Directory', description: '디렉토리 내용을 조회하고 있습니다', descriptionEn: 'Listing directory contents' },
  append_workspace_file: { name: '파일 추가 작성', nameEn: 'Append to File', description: '파일에 내용을 추가하고 있습니다', descriptionEn: 'Appending content to file' },
  save_requirement_document: { name: '요구사항 저장', nameEn: 'Save Requirements', description: '요구사항 문서를 저장하고 있습니다', descriptionEn: 'Saving requirements document' },
  load_requirement_document: { name: '요구사항 로드', nameEn: 'Load Requirements', description: '요구사항 문서를 불러오고 있습니다', descriptionEn: 'Loading requirements document' },
  save_session_flow_config: { name: '세션 설정 저장', nameEn: 'Save Session Config', description: '세션 흐름 설정을 저장하고 있습니다', descriptionEn: 'Saving session flow config' },
  get_session_flow_config_tool: { name: '세션 설정 조회', nameEn: 'Get Session Config', description: '세션 흐름 설정을 조회하고 있습니다', descriptionEn: 'Getting session flow config' },
  format_operation_summary: { name: '작업 요약', nameEn: 'Format Summary', description: '작업 요약을 생성하고 있습니다', descriptionEn: 'Formatting operation summary' },
  infer_missing_tools: { name: '도구 검증', nameEn: 'Verify Tools', description: '누락된 도구를 검증하고 있습니다', descriptionEn: 'Verifying missing tools' },
  validate_parameter_consistency: { name: '파라미터 검증', nameEn: 'Validate Parameters', description: '파라미터 일관성을 검증하고 있습니다', descriptionEn: 'Validating parameter consistency' },
  update_operation_spec: { name: '작업 사양 수정', nameEn: 'Update Spec', description: '작업 사양을 수정하고 있습니다', descriptionEn: 'Updating operation spec' },
  get_all_tool_ids: { name: '도구 목록 조회', nameEn: 'Get Tool IDs', description: '도구 목록을 조회하고 있습니다', descriptionEn: 'Getting tool IDs' },
  get_all_operation_ids: { name: '작업 목록 조회', nameEn: 'Get Operation IDs', description: '작업 목록을 조회하고 있습니다', descriptionEn: 'Getting operation IDs' },
  asset_lookup: { name: '에셋 조회', nameEn: 'Asset Lookup', description: '생성된 에셋을 조회하고 있습니다', descriptionEn: 'Looking up generated asset' },
  get_assets_for_review: { name: '에셋 리뷰 조회', nameEn: 'Assets for Review', description: '리뷰할 에셋을 조회하고 있습니다', descriptionEn: 'Getting assets for review' },
  replace_asset_field: { name: '에셋 필드 교체', nameEn: 'Replace Asset Field', description: '에셋 필드를 교체하고 있습니다', descriptionEn: 'Replacing asset field' },
  find_workspace_files: { name: '파일 검색', nameEn: 'Find Files', description: '워크스페이스에서 파일을 검색하고 있습니다', descriptionEn: 'Finding workspace files' },
  grep_workspace: { name: '텍스트 검색', nameEn: 'Search Text', description: '워크스페이스에서 텍스트를 검색하고 있습니다', descriptionEn: 'Searching text in workspace' },
  merge_openapi_fragments: { name: 'OpenAPI 병합', nameEn: 'Merge OpenAPI', description: 'OpenAPI 조각들을 병합하고 있습니다', descriptionEn: 'Merging OpenAPI fragments' },
  update_contact_flow_greeting: { name: '인사말 업데이트', nameEn: 'Update Greeting', description: 'Contact Flow 인사말을 업데이트하고 있습니다', descriptionEn: 'Updating Contact Flow greeting' },
  generate_flow_mermaid_only: { name: '흐름도 생성', nameEn: 'Generate Flow Diagram', description: '흐름도를 생성하고 있습니다', descriptionEn: 'Generating flow diagram' },
  stream_fallback_asset: { name: '에셋 스트리밍', nameEn: 'Stream Asset', description: '에셋을 스트리밍하고 있습니다', descriptionEn: 'Streaming asset fallback' },
  save_interview_notes: { name: '인터뷰 메모 저장', nameEn: 'Save Interview Notes', description: '인터뷰 메모를 저장하고 있습니다', descriptionEn: 'Saving interview notes' },
  interviewer_agent: { name: '인터뷰 에이전트', nameEn: 'Interviewer Agent', description: '요구사항 인터뷰를 진행하고 있습니다', descriptionEn: 'Conducting requirements interview' },
};

/**
 * Extract a summary label from tool input/result for better visibility
 */
function getToolSummary(toolName: string, input?: Record<string, unknown>, _result?: unknown): string | null {
  const operationId = (input?.operation_id ?? input?.operationId) as string | undefined;

  if (toolName === 'save_operation_spec') {
    const summary = input?.summary as string | undefined;
    if (summary) {
      return operationId ? `${summary} (${operationId})` : summary;
    }
  }
  if (toolName === 'generate_lambda_function' || toolName === 'lambda_generator_agent') {
    if (operationId) return operationId;
  }
  if (toolName === 'infrastructure_generator_agent') {
    const mode = input?.mode as string | undefined;
    if (mode === 'base') return '기본 인프라 (DDB, IAM, API GW)';
    if (mode === 'operation') {
      try {
        const ops = typeof input?.operations === 'string' ? JSON.parse(input.operations as string) : input?.operations;
        const opId = Array.isArray(ops) ? ops[0]?.operation_id : ops?.operation_id;
        return opId ? `Operation: ${opId}` : 'Operation fragment';
      } catch { return 'Operation fragment'; }
    }
    return null;
  }
  if (toolName === 'merge_infrastructure_fragments') {
    return '인프라 조각 병합';
  }
  // Sub-agent tools with operation_id
  if (['openapi_generator_agent', 'prompt_generator_agent', 'contact_flow_generator_agent',
       'generate_openapi_spec', 'generate_ai_prompt', 'generate_contact_flow'].includes(toolName)) {
    if (operationId) return operationId;
  }
  // Requirement document tools
  if (toolName === 'save_requirement_document' || toolName === 'load_requirement_document') {
    const docType = input?.doc_type as string | undefined;
    if (docType) return docType;
  }
  if (toolName === 'save_session_flow_config') {
    return null; // No extra summary needed beyond tool name
  }
  if (toolName === 'format_operation_summary') {
    return null;
  }
  // Update operation spec
  if (toolName === 'update_operation_spec') {
    if (operationId) return operationId;
  }
  if (toolName === 'get_operation_spec') {
    if (operationId) return operationId;
  }
  // Introspect database
  if (toolName === 'introspect_database') {
    const tableName = (input?.table_name ?? input?.tableName) as string | undefined;
    if (tableName) return tableName;
  }
  // Workspace file tools — patch gets extra search hint
  if (toolName === 'patch_workspace_file') {
    const filePath = (input?.file_path ?? input?.filePath ?? input?.path) as string | undefined;
    const search = input?.search as string | undefined;
    const fileName = filePath?.split('/').pop() || filePath;
    if (search && fileName) {
      return `${fileName} — "${search.slice(0, 40)}${search.length > 40 ? '...' : ''}"`;
    }
    return fileName || null;
  }
  if (['read_workspace_file', 'write_workspace_file', 'append_workspace_file'].includes(toolName)) {
    const filePath = (input?.file_path ?? input?.filePath ?? input?.path) as string | undefined;
    if (filePath) {
      // Show just the filename, not the full path
      const parts = filePath.split('/');
      return parts[parts.length - 1] || filePath;
    }
  }
  if (toolName === 'find_workspace_files') {
    const pattern = input?.pattern as string | undefined;
    if (pattern) return pattern;
  }
  if (toolName === 'grep_workspace') {
    const query = (input?.query ?? input?.pattern) as string | undefined;
    if (query) return query;
  }
  // Asset tools
  if (toolName === 'asset_lookup') {
    const assetType = (input?.asset_type ?? input?.assetType) as string | undefined;
    if (assetType && operationId) return `${assetType}: ${operationId}`;
    if (assetType) return assetType;
  }
  if (toolName === 'replace_asset_field') {
    const assetType = (input?.asset_type ?? input?.assetType) as string | undefined;
    if (assetType) return assetType;
  }
  if (toolName === 'merge_openapi_fragments') {
    return null;
  }
  return null;
}

/**
 * Get a meaningful completed status text based on the tool result
 */
function getCompletedStatusText(toolCall: { tool: string; result?: unknown; status?: string }, isKo: boolean): string {
  if (!toolCall.result || toolCall.result === '(completed)' || toolCall.result === '(completed - cleanup)') {
    return isKo ? '완료' : 'Done';
  }

  // Try to parse result as JSON if it's a string
  let parsed: unknown = toolCall.result;
  if (typeof parsed === 'string') {
    const raw = parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // Not JSON, use as-is
      // If the string is short enough, show it
      if (raw.length <= 50) return raw;
      return isKo ? '완료' : 'Done';
    }
  }

  if (typeof parsed === 'object' && parsed !== null) {
    const obj = parsed as Record<string, unknown>;
    if (obj.success === false) return isKo ? '실패' : 'Failed';
    // Prefer summary field if available and short enough
    if (typeof obj.summary === 'string' && obj.summary.length <= 80) return obj.summary;
    if (obj.success === true) {
      if (typeof obj.count === 'number') return isKo ? `${obj.count}개 결과` : `${obj.count} results`;
      if (typeof obj.message === 'string' && obj.message.length <= 60) return obj.message;
      return isKo ? '완료' : 'Done';
    }
    if (typeof obj.count === 'number') return isKo ? `${obj.count}개 결과` : `${obj.count} results`;
  }

  return isKo ? '완료' : 'Done';
}

// Module-scope memoized ReactMarkdown components factories
// These are created once and reused across all MessageBubble instances
function createMarkdownComponents(isUser: boolean) {
  return {
    code({ className, children, ...props }: any) {
      const inline = !className;
      const match = /language-(\w+)/.exec(className || '');
      const language = match ? match[1] : '';
      const codeString = String(children).replace(/\n$/, '');

      if (!inline && language) {
        return <CodeBlock language={language} code={codeString} />;
      }

      return (
        <code
          className={cn(
            'px-1.5 py-0.5 rounded text-sm font-mono',
            isUser ? 'bg-primary-700 text-primary-100' : 'bg-surface-200 dark:bg-surface-700 text-surface-800 dark:text-surface-200'
          )}
          {...props}
        >
          {children}
        </code>
      );
    },
    p({ children }: any) { return <p className="mb-2 last:mb-0">{children}</p>; },
    ul({ children }: any) { return <ul className="list-disc list-inside mb-2 space-y-1">{children}</ul>; },
    ol({ children }: any) { return <ol className="list-decimal list-inside mb-2 space-y-1">{children}</ol>; },
    strong({ children }: any) {
      return (
        <strong className={cn('font-semibold', isUser ? 'text-white' : 'text-surface-900 dark:text-surface-100')}>
          {children}
        </strong>
      );
    },
    table({ children }: any) {
      return (
        <div className="overflow-x-auto my-3 -mx-2">
          <table className="min-w-full border-collapse text-sm">{children}</table>
        </div>
      );
    },
    thead({ children }: any) {
      return <thead className={cn(isUser ? 'bg-primary-700' : 'bg-surface-200 dark:bg-surface-700')}>{children}</thead>;
    },
    th({ children }: any) {
      return (
        <th className={cn('px-3 py-2 text-left font-semibold border', isUser ? 'border-primary-500 text-white' : 'border-surface-300 dark:border-surface-600 text-surface-900 dark:text-surface-100')}>
          {children}
        </th>
      );
    },
    td({ children }: any) {
      return (
        <td className={cn('px-3 py-2 border', isUser ? 'border-primary-500 text-primary-100' : 'border-surface-300 dark:border-surface-600 text-surface-700 dark:text-surface-300')}>
          {children}
        </td>
      );
    },
    tr({ children }: any) {
      return <tr className={cn('even:bg-opacity-50', isUser ? 'even:bg-primary-700' : 'even:bg-surface-100 dark:even:bg-surface-800/50')}>{children}</tr>;
    },
  };
}

// Pre-create the two variants at module scope
const userMarkdownComponents = createMarkdownComponents(true);
const assistantMarkdownComponents = createMarkdownComponents(false);

interface MessageBubbleProps {
  message: Message;
}

// Memoized to prevent unnecessary re-renders when parent state changes
export const MessageBubble = memo(function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isSystem = message.role === 'system';
  const isTool = message.role === 'tool';
  const isThinking = message.role === 'thinking';
  const isAsset = message.role === 'asset';
  const isSubagent = message.role === 'subagent';

  // Render phase divider for phase transition markers
  if (isSystem && message.content.startsWith('phase_divider:')) {
    const phase = message.content.replace('phase_divider:', '') as BuilderPhase;
    const language = useBuilderStore.getState().language;
    const label = PHASE_LABELS[phase]?.[language] || phase;
    return (
      <div className="flex items-center gap-3 py-3 px-4">
        <div className="flex-1 border-t border-surface-300 dark:border-surface-600" />
        <span className="text-xs font-medium text-surface-500 dark:text-surface-400 whitespace-nowrap">
          {PHASE_ICONS[phase]} {label}
        </span>
        <div className="flex-1 border-t border-surface-300 dark:border-surface-600" />
      </div>
    );
  }

  // Render subagent activity message
  if (isSubagent && message.subagentActivity) {
    return <SubagentBubble message={message} />;
  }

  // Render tool call message
  if (isTool && message.toolCall) {
    return <ToolCallBubble message={message} />;
  }

  // Render thinking message
  if (isThinking) {
    return <ThinkingBubble message={message} />;
  }

  // Render asset reference message
  if (isAsset && message.assetRef) {
    return <AssetBubble message={message} />;
  }

  return (
    <div
      className={cn(
        'flex animate-fade-in',
        isUser ? 'justify-end' : 'justify-start'
      )}
      data-testid="chat-message"
      data-role={message.role}
    >
      <div
        className={cn(
          'max-w-[80%] rounded-2xl px-4 py-3',
          isUser
            ? 'bg-primary-600 text-white'
            : isSystem
            ? 'bg-amber-50 dark:bg-amber-900/20 text-amber-900 dark:text-amber-200 border border-amber-200 dark:border-amber-800'
            : 'bg-surface-100 dark:bg-surface-800 text-surface-900 dark:text-surface-100'
        )}
      >
        <div className={cn('prose prose-sm max-w-none dark:prose-invert', isUser && 'prose-invert')}>
          <ReactMarkdown
            components={isUser ? userMarkdownComponents : assistantMarkdownComponents}
            remarkPlugins={[remarkGfm]}
          >
            {message.content}
          </ReactMarkdown>
        </div>

        {/* Display attachments for user messages */}
        {isUser && message.attachments && message.attachments.length > 0 && (
          <AttachmentChips
            attachments={message.attachments}
            className={cn(
              isUser ? '[&_div]:bg-primary-700/50 [&_div]:text-primary-100' : ''
            )}
          />
        )}

        <div
          className={cn(
            'text-xs mt-2',
            isUser ? 'text-primary-200' : 'text-surface-500 dark:text-surface-400'
          )}
        >
          {formatDate(message.timestamp)}
        </div>
      </div>
    </div>
  );
});

interface CodeBlockProps {
  language: string;
  code: string;
}

// Memoized to prevent expensive SyntaxHighlighter re-renders
const CodeBlock = memo(function CodeBlock({ language, code }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative group my-3 -mx-2">
      <div className="absolute right-2 top-2 z-10">
        <button
          onClick={handleCopy}
          aria-label={copied ? 'Copied' : 'Copy code'}
          className={cn(
            'p-1.5 rounded-md transition-all',
            'bg-gray-700 hover:bg-gray-600',
            // E7: keyboard/touch reachable — visible on hover OR focus, and on
            // touch (no hover) it stays reachable via the always-tappable target.
            'opacity-60 group-hover:opacity-100 focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-primary-500',
            'text-gray-300 hover:text-white'
          )}
          title="Copy code"
        >
          {copied ? (
            <Check className="w-4 h-4 text-green-400" />
          ) : (
            <Copy className="w-4 h-4" />
          )}
        </button>
      </div>
      <div className="text-xs text-gray-400 px-3 py-1 bg-gray-800 rounded-t-lg font-mono">
        {language}
      </div>
      <SyntaxHighlighter
        language={language}
        style={oneDark}
        customStyle={{
          margin: 0,
          borderTopLeftRadius: 0,
          borderTopRightRadius: 0,
          fontSize: '0.875rem',
        }}
        showLineNumbers
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
});

/**
 * Tool-specific result renderer components
 */

/** Patch result: red/green diff view */
function PatchResultView({ search, replace, count, changedLines }: {
  search?: string; replace?: string; count?: number; changedLines?: number[];
}) {
  return (
    <div className="text-xs font-mono bg-surface-50 dark:bg-surface-800 rounded p-2 space-y-1 overflow-x-auto">
      {search && (
        <div className="text-red-600 dark:text-red-400 whitespace-pre-wrap">
          <span className="select-none text-red-400">- </span>{typeof search === 'string' ? search.slice(0, 500) : ''}
        </div>
      )}
      {replace && (
        <div className="text-green-600 dark:text-green-400 whitespace-pre-wrap">
          <span className="select-none text-green-400">+ </span>{typeof replace === 'string' ? replace.slice(0, 500) : ''}
        </div>
      )}
      {(count !== undefined || (changedLines && changedLines.length > 0)) && (
        <div className="text-surface-500 dark:text-surface-400 pt-1 border-t border-surface-200 dark:border-surface-700 mt-1">
          {count !== undefined && <span>{count} replacement(s)</span>}
          {changedLines && changedLines.length > 0 && (
            <span className="ml-2">Lines: {changedLines.join(', ')}</span>
          )}
        </div>
      )}
    </div>
  );
}

/** Search results: formatted list */
function SearchResultsView({ results, pattern }: {
  results: Array<{ path: string; line_number: number; line: string }>;
  pattern?: string;
}) {
  const maxShow = 10;
  const shown = results.slice(0, maxShow);
  return (
    <div className="text-xs space-y-0.5 max-h-60 overflow-y-auto">
      {shown.map((r, i) => (
        <div key={i} className="flex gap-2 py-0.5 hover:bg-surface-50 dark:hover:bg-surface-800 rounded">
          <span className="text-blue-600 dark:text-blue-400 font-mono whitespace-nowrap shrink-0">
            {r.path.split('/').pop()}:{r.line_number}
          </span>
          <span className="text-surface-700 dark:text-surface-300 truncate">{r.line}</span>
        </div>
      ))}
      {results.length > maxShow && (
        <div className="text-surface-400 dark:text-surface-500 pt-1">({results.length} total results{pattern ? ` for "${pattern}"` : ''})</div>
      )}
    </div>
  );
}

/** Spec update: field list */
function SpecUpdateView({ operationId, fields }: {
  operationId?: string; fields: string[];
}) {
  return (
    <div className="text-xs space-y-1">
      {operationId && (
        <div className="font-medium text-green-700 dark:text-green-400">{operationId} updated:</div>
      )}
      <ul className="list-disc list-inside text-surface-700 dark:text-surface-300 space-y-0.5">
        {fields.map((f, i) => <li key={i}>{f}</li>)}
      </ul>
    </div>
  );
}

/** Content preview: first few lines + size */
function ContentPreviewView({ content, size }: { content: string; size?: number }) {
  const lines = content.split('\n');
  const previewLines = lines.slice(0, 5);
  const hasMore = lines.length > 5;
  return (
    <div className="text-xs font-mono bg-surface-50 dark:bg-surface-800 rounded p-2 space-y-1 overflow-x-auto max-h-40 overflow-y-auto">
      {previewLines.map((line, i) => (
        <div key={i} className="text-surface-700 dark:text-surface-300 whitespace-pre-wrap">{line}</div>
      ))}
      {hasMore && (
        <div className="text-surface-400 dark:text-surface-500 pt-1 border-t border-surface-200 dark:border-surface-700 mt-1">
          ... ({size ? `${size.toLocaleString()} bytes` : `${lines.length} lines`} total)
        </div>
      )}
    </div>
  );
}

/**
 * Render tool result with tool-specific formatting
 */
function renderToolResult(toolName: string, result: unknown, input?: Record<string, unknown>) {
  // Parse result if string
  let obj: Record<string, unknown> | null = null;
  if (typeof result === 'object' && result !== null) {
    obj = result as Record<string, unknown>;
  } else if (typeof result === 'string') {
    try { obj = JSON.parse(result); } catch { /* not JSON */ }
  }

  // patch_workspace_file: diff view
  if (toolName === 'patch_workspace_file' && obj?.success) {
    return (
      <PatchResultView
        search={(obj.search as string) || (input?.search as string)}
        replace={(obj.replace as string) || (input?.replace as string)}
        count={obj.replacements_made as number | undefined}
        changedLines={obj.changed_lines as number[] | undefined}
      />
    );
  }

  // grep_workspace: formatted search results
  if (toolName === 'grep_workspace' && obj?.results && Array.isArray(obj.results)) {
    return (
      <SearchResultsView
        results={obj.results as Array<{ path: string; line_number: number; line: string }>}
        pattern={input?.pattern as string | undefined}
      />
    );
  }

  // update_operation_spec: field change summary
  if (toolName === 'update_operation_spec' && obj?.updated_fields && Array.isArray(obj.updated_fields)) {
    return (
      <SpecUpdateView
        operationId={obj.operation_id as string | undefined}
        fields={obj.updated_fields as string[]}
      />
    );
  }

  // read_workspace_file / load_requirement_document: content preview
  if ((toolName === 'read_workspace_file' || toolName === 'load_requirement_document') && obj?.content) {
    return (
      <ContentPreviewView
        content={obj.content as string}
        size={(obj.size ?? obj.char_count) as number | undefined}
      />
    );
  }

  // Default: JSON
  return (
    <pre className="text-xs bg-surface-100 dark:bg-surface-800 text-surface-800 dark:text-surface-200 p-2 rounded overflow-x-auto max-h-60 overflow-y-auto">
      {typeof result === 'string' ? result : JSON.stringify(result, null, 2)}
    </pre>
  );
}

/**
 * Tool Call Bubble Component
 *
 * Displays tool invocations with status, input, and result
 */
function ToolCallBubble({ message }: MessageBubbleProps) {
  const toolCall = message.toolCall!;
  const toolEntry = TOOL_DISPLAY_NAMES[toolCall.tool];
  const language = useBuilderStore(state => state.language);
  const isKo = language === 'ko-KR';
  const toolInfo = toolEntry
    ? { name: isKo ? toolEntry.name : toolEntry.nameEn, description: isKo ? toolEntry.description : toolEntry.descriptionEn }
    : { name: toolCall.tool, description: '' };

  const isRunning = toolCall.status === 'running';
  const isCompleted = toolCall.status === 'completed';
  const isError = toolCall.status === 'error';
  const hasInput = !!(toolCall.input && Object.keys(toolCall.input).length > 0);
  const hasResult = !!toolCall.result;

  // E3: Default collapsed (one-line summary). Only auto-expand on error.
  const [isExpanded, setIsExpanded] = useState(isError);

  useEffect(() => {
    if (isError) setIsExpanded(true);
  }, [isError]);

  // Get tool-specific summary for better visibility
  const toolSummary = getToolSummary(toolCall.tool, toolCall.input, toolCall.result);

  return (
    <div className="flex justify-start animate-fade-in" data-testid="tool-message">
      <div
        className={cn(
          'max-w-[80%] rounded-2xl px-4 py-3 border',
          isRunning && 'bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800',
          isCompleted && 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800',
          isError && 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800'
        )}
      >
        {/* Tool header */}
        <div className="flex items-center gap-2">
          <div
            className={cn(
              'w-8 h-8 rounded-full flex items-center justify-center',
              isRunning && 'bg-blue-100 dark:bg-blue-800/50',
              isCompleted && 'bg-green-100 dark:bg-green-800/50',
              isError && 'bg-red-100 dark:bg-red-800/50'
            )}
          >
            {isRunning ? (
              <Loader2 className="w-4 h-4 text-blue-600 dark:text-blue-400 animate-spin" />
            ) : isCompleted ? (
              <CheckCircle2 className="w-4 h-4 text-green-600 dark:text-green-400" />
            ) : (
              <XCircle className="w-4 h-4 text-red-600 dark:text-red-400" />
            )}
          </div>
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <Wrench className="w-3.5 h-3.5 text-surface-500 dark:text-surface-400" />
              <span
                className={cn(
                  'text-sm font-medium',
                  isRunning && 'text-blue-700 dark:text-blue-300',
                  isCompleted && 'text-green-700 dark:text-green-300',
                  isError && 'text-red-700 dark:text-red-300'
                )}
              >
                {toolInfo.name}
              </span>
              {/* Show tool summary inline when available */}
              {toolSummary && (
                <span className="text-xs text-surface-600 dark:text-surface-400 font-normal">
                  - {toolSummary}
                </span>
              )}
            </div>
            <p className="text-xs text-surface-500 dark:text-surface-400 mt-0.5">
              {isRunning ? toolInfo.description : isError ? (isKo ? '오류가 발생했습니다' : 'An error occurred') : getCompletedStatusText(toolCall, isKo)}
            </p>
          </div>
          {/* Expand/collapse button */}
          {(hasInput || hasResult || toolCall.error || isRunning) && (
            <button
              onClick={() => setIsExpanded(!isExpanded)}
              aria-expanded={isExpanded}
              aria-label={isExpanded ? (isKo ? '도구 상세 접기' : 'Collapse tool details') : (isKo ? '도구 상세 펼치기' : 'Expand tool details')}
              className="p-1 hover:bg-surface-200 dark:hover:bg-surface-700 rounded transition-colors"
            >
              {isExpanded ? (
                <ChevronDown className="w-4 h-4 text-surface-500 dark:text-surface-400" />
              ) : (
                <ChevronRight className="w-4 h-4 text-surface-500 dark:text-surface-400" />
              )}
            </button>
          )}
        </div>

        {/* Expanded details */}
        {isExpanded && (
          <div className="mt-3 pt-3 border-t border-surface-200 dark:border-surface-700 space-y-3">
            {/* Input */}
            {hasInput && (
              <div>
                <div className="text-xs font-medium text-surface-500 dark:text-surface-400 mb-1">Input / 입력</div>
                <pre className="text-xs bg-surface-100 dark:bg-surface-800 text-surface-800 dark:text-surface-200 p-2 rounded overflow-x-auto max-h-40 overflow-y-auto">
                  {JSON.stringify(toolCall.input, null, 2)}
                </pre>
              </div>
            )}
            {/* Result */}
            {hasResult && (
              <div>
                <div className="text-xs font-medium text-surface-500 dark:text-surface-400 mb-1">Result / 결과</div>
                {renderToolResult(toolCall.tool, toolCall.result, toolCall.input)}
              </div>
            )}
            {/* Error */}
            {toolCall.error && (
              <div>
                <div className="text-xs font-medium text-red-500 dark:text-red-400 mb-1">Error / 오류</div>
                <pre className="text-xs bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300 p-2 rounded overflow-x-auto">
                  {toolCall.error}
                </pre>
              </div>
            )}
          </div>
        )}

        {/* Timestamp */}
        <div className="text-xs text-surface-400 dark:text-surface-500 mt-2">
          {formatDate(message.timestamp)}
        </div>
      </div>
    </div>
  );
}

/**
 * Thinking Bubble Component
 *
 * Displays agent's reasoning/thinking process
 */
function ThinkingBubble({ message }: MessageBubbleProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const language = useBuilderStore(state => state.language);
  const isKo = language === 'ko-KR';

  return (
    <div className="flex justify-start animate-fade-in">
      <div className="max-w-[80%] rounded-2xl px-4 py-3 bg-primary-50 dark:bg-primary-900/20 border border-primary-200 dark:border-primary-800">
        {/* Header */}
        <div
          className="flex items-center gap-2 cursor-pointer"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="w-8 h-8 rounded-full bg-primary-100 dark:bg-primary-800/50 flex items-center justify-center">
            <Brain className="w-4 h-4 text-primary-600 dark:text-primary-400" />
          </div>
          <div className="flex-1">
            <span className="text-sm font-medium text-primary-700 dark:text-primary-300">{isKo ? '에이전트 사고 과정' : 'Agent Thinking'}</span>
          </div>
          <button
            aria-expanded={isExpanded}
            aria-label={isExpanded ? (isKo ? '접기' : 'Collapse') : (isKo ? '펼치기' : 'Expand')}
            className="p-1 hover:bg-primary-100 dark:hover:bg-primary-800/50 rounded transition-colors"
          >
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 text-primary-500 dark:text-primary-400" />
            ) : (
              <ChevronRight className="w-4 h-4 text-primary-500 dark:text-primary-400" />
            )}
          </button>
        </div>

        {/* Content */}
        {isExpanded && message.content && (
          <div className="mt-3 pt-3 border-t border-primary-200 dark:border-primary-800">
            <p className="text-sm text-primary-800 dark:text-primary-200 whitespace-pre-wrap">{message.content}</p>
          </div>
        )}

        {/* Timestamp */}
        <div className="text-xs text-primary-400 dark:text-primary-500/70 mt-2">
          {formatDate(message.timestamp)}
        </div>
      </div>
    </div>
  );
}

/**
 * Get icon component for asset type
 */
function getAssetIcon(assetType: string) {
  const iconMap: Record<string, typeof FileCode> = {
    lambda: FileCode,
    openapi: FileText,
    prompt: FileText,
    contact_flow: Workflow,
    cdk: Server,
    faq: BookOpen,
    research: BookOpen,
    package: Package,
    workspace_file: FileCode,
  };
  return iconMap[assetType] || FileText;
}

/**
 * Get color classes for asset type (theme-aware: light + dark variants)
 */
function getAssetColors(assetType: string): { bg: string; border: string; icon: string; text: string } {
  const colorMap: Record<string, { bg: string; border: string; icon: string; text: string }> = {
    lambda: { bg: 'bg-orange-50 dark:bg-orange-900/20', border: 'border-orange-200 dark:border-orange-800', icon: 'text-orange-600 dark:text-orange-400', text: 'text-orange-700 dark:text-orange-300' },
    openapi: { bg: 'bg-blue-50 dark:bg-blue-900/20', border: 'border-blue-200 dark:border-blue-800', icon: 'text-blue-600 dark:text-blue-400', text: 'text-blue-700 dark:text-blue-300' },
    prompt: { bg: 'bg-violet-50 dark:bg-violet-900/20', border: 'border-violet-200 dark:border-violet-800', icon: 'text-violet-600 dark:text-violet-400', text: 'text-violet-700 dark:text-violet-300' },
    contact_flow: { bg: 'bg-teal-50 dark:bg-teal-900/20', border: 'border-teal-200 dark:border-teal-800', icon: 'text-teal-600 dark:text-teal-400', text: 'text-teal-700 dark:text-teal-300' },
    cdk: { bg: 'bg-slate-50 dark:bg-surface-800', border: 'border-slate-200 dark:border-surface-600', icon: 'text-slate-600 dark:text-surface-300', text: 'text-slate-700 dark:text-surface-200' },
    faq: { bg: 'bg-emerald-50 dark:bg-emerald-900/20', border: 'border-emerald-200 dark:border-emerald-800', icon: 'text-emerald-600 dark:text-emerald-400', text: 'text-emerald-700 dark:text-emerald-300' },
    research: { bg: 'bg-cyan-50 dark:bg-cyan-900/20', border: 'border-cyan-200 dark:border-cyan-800', icon: 'text-cyan-600 dark:text-cyan-400', text: 'text-cyan-700 dark:text-cyan-300' },
    package: { bg: 'bg-indigo-50 dark:bg-indigo-900/20', border: 'border-indigo-200 dark:border-indigo-800', icon: 'text-indigo-600 dark:text-indigo-400', text: 'text-indigo-700 dark:text-indigo-300' },
    workspace_file: { bg: 'bg-amber-50 dark:bg-amber-900/20', border: 'border-amber-200 dark:border-amber-800', icon: 'text-amber-600 dark:text-amber-400', text: 'text-amber-700 dark:text-amber-300' },
  };
  return colorMap[assetType] || { bg: 'bg-gray-50 dark:bg-surface-800', border: 'border-gray-200 dark:border-surface-600', icon: 'text-gray-600 dark:text-surface-300', text: 'text-gray-700 dark:text-surface-200' };
}

/**
 * Asset Bubble Component
 *
 * Displays a marker for where an asset was generated in the conversation flow.
 * Clicking on it expands/collapses the full asset preview.
 */
function AssetBubble({ message }: MessageBubbleProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const assetRef = message.assetRef!;
  const Icon = getAssetIcon(assetRef.assetType);
  const colors = getAssetColors(assetRef.assetType);

  // Get the full asset preview from store
  const assetPreviews = useBuilderStore(state => state.assetPreviews);
  const language = useBuilderStore(state => state.language);
  const setActiveAssetKey = useBuilderStore(state => state.setActiveAssetKey);

  // Build asset base key to match store key format
  // Store keys have timestamp suffix (e.g., "lambda-opId-handler.py-1707123456789")
  // so we need prefix matching, not exact matching
  const assetBaseKey = (() => {
    if (assetRef.fileName && assetRef.operationId) {
      return `${assetRef.assetType}-${assetRef.operationId}-${assetRef.fileName}`;
    } else if (assetRef.fileName) {
      return `${assetRef.assetType}-${assetRef.fileName}`;
    } else if (assetRef.operationId) {
      return `${assetRef.assetType}-${assetRef.operationId}`;
    }
    return assetRef.assetType;
  })();

  // Find matching asset preview by prefix (keys have timestamp suffix)
  const matchedKey = (() => {
    if (assetPreviews[assetBaseKey]) return assetBaseKey;
    return Object.keys(assetPreviews)
      .filter(k => k.startsWith(`${assetBaseKey}-`))
      .sort()
      .pop();
  })();
  const fullPreview = matchedKey ? assetPreviews[matchedKey] : undefined;

  // Build display label
  const rawLabel = message.content || `${assetRef.assetType} - ${assetRef.fileName || assetRef.operationId || ''}`;
  const ko = language === 'ko-KR';
  const assetLabel = ko ? `생성됨: ${rawLabel}` : `Generated ${rawLabel}`;

  const openInWorkspace = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (matchedKey) setActiveAssetKey(matchedKey);
  };

  return (
    <div className="flex flex-col justify-start animate-fade-in">
      {/* Compact marker — click to expand inline, or "Open" in the workspace pane */}
      <div
        className={cn(
          'max-w-[70%] rounded-xl px-3 py-2 border flex items-center gap-2 cursor-pointer hover:shadow-sm transition-shadow',
          colors.bg,
          colors.border
        )}
        onClick={() => setIsExpanded(!isExpanded)}
        title={isExpanded ? "Click to collapse" : "Click to expand preview"}
      >
        <div className={cn('w-7 h-7 rounded-full flex items-center justify-center', colors.bg)}>
          <Icon className={cn('w-4 h-4', colors.icon)} />
        </div>
        <div className="flex-1 min-w-0">
          <span className={cn('text-sm font-medium truncate block', colors.text)}>
            {assetLabel}
          </span>
          <span className="text-xs text-surface-400 dark:text-surface-500">
            {formatDate(message.timestamp)}
          </span>
        </div>
        {fullPreview && (
          <button
            onClick={openInWorkspace}
            className={cn(
              'flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium transition-colors',
              'bg-white/60 dark:bg-white/10 hover:bg-white/90 dark:hover:bg-white/20',
              colors.text
            )}
            aria-label={ko ? '워크스페이스에서 열기' : 'Open in workspace'}
          >
            <PanelRight className="w-3.5 h-3.5" />
            {ko ? '열기' : 'Open'}
          </button>
        )}
        <ChevronDown className={cn(
          'w-4 h-4 transition-transform',
          colors.icon,
          isExpanded && 'rotate-180'
        )} />
      </div>

      {/* Expanded preview (inline fallback) */}
      {isExpanded && fullPreview && (
        <div className="mt-2 w-full">
          <AssetPreviewBubble
            preview={fullPreview}
            language={language}
          />
        </div>
      )}
      {isExpanded && !fullPreview && (
        <div className="mt-2 px-3 py-2 text-xs text-surface-400 dark:text-surface-500 italic">
          {language === 'ko-KR' ? '에셋 내용을 불러올 수 없습니다.' : 'Asset content not available.'}
        </div>
      )}
    </div>
  );
}
