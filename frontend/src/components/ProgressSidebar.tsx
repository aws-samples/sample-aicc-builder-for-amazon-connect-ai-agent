/**
 * Progress Sidebar Component
 *
 * Shows the current progress through the customization workflow
 * with granular progress bars and sub-step tracking.
 * Includes individual asset download buttons.
 */

import { useState, useEffect, useCallback, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  Check,
  Circle,
  Loader2,
  Download,
  FileCode,
  Database,
  MessageSquare,
  Settings,
  FileJson,
  Sparkles,
  Workflow,
  Boxes,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  CircleDot,
  BookOpen,
  AlertTriangle,
  Package,
  Maximize2,
  Minimize2,
  Lightbulb,
  ExternalLink,
  X,
  PartyPopper,
  Terminal,
  Rocket,
} from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { useSessionStore } from '../stores/sessionStore';
import { cn } from '../lib/utils';
import type { ProgressItem, ProgressSubStep, BuilderPhase } from '../types';
import { PHASE_LABELS, PHASE_ORDER } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { fetchAssetDownloadUrl } from '../services/workspaceApi';
import { createZip, downloadBlob } from '../lib/zipUtils';

const STEP_ICONS: Record<string, ReactNode> = {
  company: <Settings className="w-4 h-4" />,
  operations: <FileCode className="w-4 h-4" />,
  database: <Database className="w-4 h-4" />,
  validation: <Check className="w-4 h-4" />,
  lambda: <FileCode className="w-4 h-4" />,
  prompt: <MessageSquare className="w-4 h-4" />,
  openapi: <FileJson className="w-4 h-4" />,
  contact_flow: <Workflow className="w-4 h-4" />,
  cdk: <Boxes className="w-4 h-4" />,
  knowledge_base: <BookOpen className="w-4 h-4" />,
  ready: <Sparkles className="w-4 h-4" />,
};

// Color schemes for different progress states
const STATUS_COLORS = {
  pending: {
    bg: 'bg-surface-50 dark:bg-surface-800/50',
    iconBg: 'bg-surface-200 dark:bg-surface-700',
    iconText: 'text-surface-400 dark:text-surface-500',
    text: 'text-surface-500 dark:text-surface-400',
    progressBg: 'bg-surface-200 dark:bg-surface-700',
    progressFill: 'bg-surface-300 dark:bg-surface-600',
  },
  in_progress: {
    bg: 'bg-primary-50 dark:bg-primary-900/30',
    iconBg: 'bg-primary-100 dark:bg-primary-800/50',
    iconText: 'text-primary-600 dark:text-primary-400',
    text: 'text-primary-700 dark:text-primary-300',
    progressBg: 'bg-primary-100 dark:bg-primary-900/50',
    progressFill: 'bg-primary-500 dark:bg-primary-400',
  },
  completed: {
    bg: 'bg-green-50 dark:bg-green-900/30',
    iconBg: 'bg-green-100 dark:bg-green-800/50',
    iconText: 'text-green-600 dark:text-green-400',
    text: 'text-green-700 dark:text-green-300',
    progressBg: 'bg-green-100 dark:bg-green-900/50',
    progressFill: 'bg-green-500 dark:bg-green-400',
  },
};

/** Phase Stepper — shows all 4 phases as connected steps with current position highlighted */
function PhaseStepper({ currentPhase, language }: { currentPhase: BuilderPhase; language: string }) {
  const currentIdx = PHASE_ORDER.indexOf(currentPhase);

  return (
    <div className="mx-3 mt-3 mb-1">
      <div className="flex items-start">
        {PHASE_ORDER.map((phase, i) => {
          const isCompleted = i < currentIdx;
          const isCurrent = i === currentIdx;
          const label = PHASE_LABELS[phase]?.[language] || phase;

          return (
            <div key={phase} className="flex-1 flex flex-col items-center relative">
              {/* Connector line (before circle, except first) */}
              {i > 0 && (
                <div
                  className={cn(
                    'absolute top-[11px] right-1/2 w-full h-0.5',
                    isCompleted || isCurrent
                      ? 'bg-primary-400 dark:bg-primary-500'
                      : 'bg-surface-200 dark:bg-surface-700'
                  )}
                />
              )}
              {/* Circle */}
              <div
                className={cn(
                  'relative z-10 w-[22px] h-[22px] rounded-full flex items-center justify-center text-[10px] font-bold transition-all duration-300',
                  isCurrent
                    ? 'bg-primary-500 dark:bg-primary-400 text-white ring-2 ring-primary-200 dark:ring-primary-800'
                    : isCompleted
                      ? 'bg-primary-400 dark:bg-primary-500 text-white'
                      : 'bg-surface-200 dark:bg-surface-700 text-surface-400 dark:text-surface-500'
                )}
              >
                {isCompleted ? (
                  <Check className="w-3 h-3" />
                ) : (
                  <span>{i + 1}</span>
                )}
              </div>
              {/* Label */}
              <span
                className={cn(
                  'mt-1 text-[10px] leading-tight text-center font-medium transition-colors',
                  isCurrent
                    ? 'text-primary-600 dark:text-primary-400'
                    : isCompleted
                      ? 'text-surface-500 dark:text-surface-400'
                      : 'text-surface-400 dark:text-surface-500'
                )}
              >
                {label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

type TabType = 'assets' | 'notes' | null;

// Derived selector: compute which asset types are available from assetPreviews
// This avoids subscribing to the full assetPreviews object (which changes on every stream chunk)
function useAvailableAssetTypes() {
  return useBuilderStore((state) => {
    let flags = 0;
    for (const p of Object.values(state.assetPreviews)) {
      if (!p.isComplete) continue;
      if (p.assetType === 'lambda') flags |= 1;
      else if (p.assetType === 'prompt') flags |= 2;
      else if (p.assetType === 'openapi') flags |= 4;
      else if (p.assetType === 'contact_flow') flags |= 8;
      else if (p.assetType === 'cdk' || p.assetType === 'cloudformation') flags |= 16;
      else if (p.assetType === 'faq' || p.assetType === 'package') flags |= 32;
    }
    return flags;
  });
}

export function ProgressSidebar() {
  const progress = useBuilderStore(s => s.progress);
  const session = useBuilderStore(s => s.session);
  const language = useBuilderStore(s => s.language);
  const downloadUrl = useBuilderStore(s => s.downloadUrl);
  const downloadExpiresAt = useBuilderStore(s => s.downloadExpiresAt);
  const packageS3Key = useBuilderStore(s => s.packageS3Key);
  const setDownloadUrl = useBuilderStore(s => s.setDownloadUrl);
  const { currentSessionId } = useSessionStore();
  const { requestAssets } = useWebSocket();
  const isConnected = useBuilderStore(s => s.isConnected);
  const assetPreviews = useBuilderStore(s => s.assetPreviews);
  const showDownloadModal = useBuilderStore(s => s.showDownloadModal);
  const setShowDownloadModal = useBuilderStore(s => s.setShowDownloadModal);
  const currentPhase = useBuilderStore(s => s.currentPhase);
  const [isDownloading, setIsDownloading] = useState(false);
  const [activeTab, setActiveTab] = useState<TabType>('assets');
  const [isExpanded, setIsExpanded] = useState(false);

  const completedCount = progress.filter((p) => p.status === 'completed').length;
  const totalCount = progress.length;

  const assetFlags = useAvailableAssetTypes();
  const hasLambdaAsset = !!(assetFlags & 1);
  const hasPromptAsset = !!(assetFlags & 2);
  const hasOpenapiAsset = !!(assetFlags & 4);
  const hasContactFlowAsset = !!(assetFlags & 8);
  const hasCdkAsset = !!(assetFlags & 16);
  const hasKnowledgeBaseAsset = !!(assetFlags & 32);

  const hasAnyAsset = assetFlags > 0;

  // "Download All" is ready when any asset exists or we have an s3Key from previous package
  const isReady = hasAnyAsset || !!packageS3Key;

  // Check if download URL is still valid
  const isDownloadUrlValid = downloadUrl && downloadExpiresAt
    ? new Date(downloadExpiresAt) > new Date()
    : false;

  const handleDownloadAll = async () => {
    if (!currentSessionId) return;
    setIsDownloading(true);
    try {
      // Always fetch a fresh package from the backend (bypasses NFS cache and
      // any stale packageS3Key / downloadUrl cached on the client).
      const result = await fetchAssetDownloadUrl(currentSessionId, 'all');
      if (result?.downloadUrl) {
        const expiresAt = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
        setDownloadUrl(result.downloadUrl, expiresAt, result.s3Key);
        window.open(result.downloadUrl, '_blank');
        setShowDownloadModal(true);
      } else if (isConnected) {
        // Fallback: ask the agent via WebSocket (legacy path)
        requestAssets();
      } else {
        // Last-resort client-side ZIP from streamed previews
        const entries: Array<{ name: string; content: string }> = [];
        for (const p of Object.values(assetPreviews)) {
          if (!p.isComplete || !p.content) continue;
          const folder = p.operationId ? `${p.assetType}/${p.operationId}` : p.assetType;
          entries.push({ name: `${folder}/${p.fileName || 'content'}`, content: p.content });
        }
        if (entries.length > 0) {
          const blob = await createZip(entries);
          downloadBlob(blob, `${session.companyName || 'aicc'}_assets.zip`);
        }
      }
    } catch (error) {
      console.error('[ProgressSidebar] Download All error:', error);
    } finally {
      setTimeout(() => setIsDownloading(false), 2000);
    }
  };

  return (
    <div className={cn(
        'bg-white dark:bg-surface-850 rounded-xl shadow-sm dark:shadow-none border border-surface-200 dark:border-surface-700 flex flex-col h-full overflow-hidden transition-all duration-300',
        activeTab === 'notes'
          ? isExpanded ? 'w-[32rem]' : 'w-96 xl:w-[28rem]'
          : activeTab === 'assets' ? 'w-72 xl:w-80'
          : 'w-10'
      )}>
      {/* Tab Header */}
      <div className={cn('flex items-center border-b border-surface-200 dark:border-surface-700 flex-shrink-0', !activeTab && 'flex-col border-b-0')}>
        {activeTab ? (
          <>
            <button
              onClick={() => setActiveTab('assets')}
              className={cn(
                'flex-1 py-3 text-sm font-medium transition-colors',
                activeTab === 'assets'
                  ? 'border-b-2 border-primary-500 text-primary-600 dark:text-primary-400'
                  : 'text-surface-500 dark:text-surface-400 hover:text-surface-700 dark:hover:text-surface-300'
              )}
            >
              Assets
            </button>
            <button
              onClick={() => setActiveTab(activeTab === 'notes' ? null : 'notes')}
              className={cn(
                'flex-1 py-3 text-sm font-medium transition-colors',
                activeTab === 'notes'
                  ? 'border-b-2 border-primary-500 text-primary-600 dark:text-primary-400'
                  : 'text-surface-500 dark:text-surface-400 hover:text-surface-700 dark:hover:text-surface-300'
              )}
            >
              Notes
            </button>
            {activeTab === 'notes' && (
              <button
                onClick={() => setIsExpanded(!isExpanded)}
                className="p-2 mr-1 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 transition-colors"
                title={isExpanded ? 'Collapse' : 'Expand'}
              >
                {isExpanded ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
              </button>
            )}
          </>
        ) : (
          <>
            <button
              onClick={() => setActiveTab('assets')}
              className="p-2 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 transition-colors"
              title="Assets"
            >
              <Boxes className="w-4 h-4" />
            </button>
            <button
              onClick={() => setActiveTab('notes')}
              className="p-2 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 transition-colors"
              title="Notes"
            >
              <BookOpen className="w-4 h-4" />
            </button>
          </>
        )}
      </div>

      {/* Phase Stepper */}
      {activeTab && (
        <PhaseStepper currentPhase={currentPhase} language={language} />
      )}

      {/* Tab Content */}
      {activeTab === 'notes' ? (
        <NotesContent language={language} />
      ) : activeTab === 'assets' ? (
        <>
          {/* Progress Header */}
          <div className="px-6 py-4 border-b border-surface-200 dark:border-surface-700 flex-shrink-0">
            <h2 className="font-semibold text-surface-900 dark:text-surface-100">
              {language === 'ko-KR' ? '진행 상황' : 'Progress'}
            </h2>
            <div className="mt-2">
              <span className={cn(
                'text-sm font-medium',
                completedCount === totalCount ? 'text-green-600 dark:text-green-400' : completedCount > 0 ? 'text-primary-600 dark:text-primary-400' : 'text-surface-500 dark:text-surface-400'
              )}>
                {completedCount} / {totalCount} {language === 'ko-KR' ? '완료' : 'complete'}
              </span>
            </div>
          </div>

          {/* Progress Steps */}
          <div className="flex-1 overflow-y-auto px-4 py-4">
            <div className="space-y-1">
              {progress.map((item) => (
                <ProgressStep key={item.id} item={item} language={language} />
              ))}
            </div>
          </div>

      {/* Session Info */}
      {session.companyName && (
        <div className="px-6 py-4 border-t border-surface-200 dark:border-surface-700 bg-surface-50 dark:bg-surface-900/50 flex-shrink-0">
          <h3 className="text-sm font-medium text-surface-700 dark:text-surface-300 mb-2">
            {language === 'ko-KR' ? '세션 정보' : 'Session Info'}
          </h3>
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-surface-500 dark:text-surface-400">
                {language === 'ko-KR' ? '회사' : 'Company'}
              </dt>
              <dd className="text-surface-900 dark:text-surface-100 font-medium">{session.companyName}</dd>
            </div>
            {session.industry && (
              <div className="flex justify-between">
                <dt className="text-surface-500 dark:text-surface-400">
                  {language === 'ko-KR' ? '산업' : 'Industry'}
                </dt>
                <dd className="text-surface-900 dark:text-surface-100">{session.industry}</dd>
              </div>
            )}
            <div className="flex justify-between">
              <dt className="text-surface-500 dark:text-surface-400">
                {language === 'ko-KR' ? '작업 수' : 'Operations'}
              </dt>
              <dd className="text-surface-900 dark:text-surface-100">{session.operations.length}</dd>
            </div>
          </dl>
        </div>
      )}

      {/* Individual Asset Downloads */}
      {hasAnyAsset && (
        <div className="px-6 py-4 border-t border-surface-200 dark:border-surface-700 flex-shrink-0">
          <h3 className="text-sm font-medium text-surface-700 dark:text-surface-300 mb-3">
            {language === 'ko-KR' ? '생성된 에셋' : 'Generated Assets'}
          </h3>
          <div className="space-y-2">
            {hasLambdaAsset && (
              <AssetDownloadButton
                icon={<FileCode className="w-4 h-4" />}
                label={language === 'ko-KR' ? 'Lambda 함수' : 'Lambda Functions'}
                assetType="lambda"
              />
            )}
            {hasPromptAsset && (
              <AssetDownloadButton
                icon={<MessageSquare className="w-4 h-4" />}
                label={language === 'ko-KR' ? 'AI 프롬프트' : 'AI Prompt'}
                assetType="prompt"
              />
            )}
            {hasOpenapiAsset && (
              <AssetDownloadButton
                icon={<FileJson className="w-4 h-4" />}
                label={language === 'ko-KR' ? 'OpenAPI 스펙' : 'OpenAPI Spec'}
                assetType="openapi"
              />
            )}
            {hasContactFlowAsset && (
              <AssetDownloadButton
                icon={<Workflow className="w-4 h-4" />}
                label="Contact Flow"
                assetType="contact_flow"
              />
            )}
            {hasCdkAsset && (
              <AssetDownloadButton
                icon={<Boxes className="w-4 h-4" />}
                label={language === 'ko-KR' ? 'CloudFormation' : 'CloudFormation'}
                assetType="cdk"
              />
            )}
            {hasKnowledgeBaseAsset && (
              <AssetDownloadButton
                icon={<BookOpen className="w-4 h-4" />}
                label="Knowledge Base"
                assetType="knowledge_base"
              />
            )}
          </div>
        </div>
      )}

      {/* Download All Button */}
      <div className="px-6 py-4 border-t border-surface-200 dark:border-surface-700 flex-shrink-0">
        <button
          disabled={!isReady || isDownloading}
          onClick={handleDownloadAll}
          className={cn(
            'w-full flex items-center justify-center gap-2 px-4 py-3 rounded-xl',
            'font-medium transition-all',
            isReady && !isDownloading
              ? 'bg-primary-600 dark:bg-primary-500 text-white hover:bg-primary-700 dark:hover:bg-primary-600 shadow-sm dark:shadow-glow'
              : 'bg-surface-100 dark:bg-surface-800 text-surface-400 dark:text-surface-500 cursor-not-allowed'
          )}
        >
          {isDownloading ? (
            <Loader2 className="w-5 h-5 animate-spin" />
          ) : (
            <Download className="w-5 h-5" />
          )}
          {isDownloadUrlValid || packageS3Key
            ? (language === 'ko-KR' ? '에셋 다운로드' : 'Download Assets')
            : (language === 'ko-KR' ? '에셋 패키징 및 다운로드' : 'Package & Download Assets')
          }
        </button>
        {isDownloadUrlValid && downloadExpiresAt && (
          <p className="mt-2 text-xs text-center text-surface-500 dark:text-surface-400">
            {language === 'ko-KR'
              ? `다운로드 링크 만료: ${new Date(downloadExpiresAt).toLocaleString('ko-KR')}`
              : `Link expires: ${new Date(downloadExpiresAt).toLocaleString()}`
            }
          </p>
        )}
      </div>
        </>
      ) : null}

      {/* Download Complete Modal */}
      {showDownloadModal && (
        <DownloadCompleteModal
          language={language}
          onClose={() => setShowDownloadModal(false)}
        />
      )}
    </div>
  );
}

/**
 * Collapsible Note Section
 */
function NoteSection({ icon, title, defaultOpen = true, variant, children }: {
  icon: ReactNode;
  title: string;
  defaultOpen?: boolean;
  variant?: 'warning';
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const isWarning = variant === 'warning';
  return (
    <section className={cn(
      'rounded-lg border overflow-hidden',
      isWarning
        ? 'bg-yellow-50 dark:bg-yellow-900/20 border-yellow-200 dark:border-yellow-800'
        : 'bg-surface-50 dark:bg-surface-800/50 border-surface-200 dark:border-surface-700'
    )}>
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          'w-full flex items-center gap-2 px-3 py-2.5 text-left font-semibold text-sm transition-colors',
          isWarning
            ? 'text-yellow-800 dark:text-yellow-300 hover:bg-yellow-100/50 dark:hover:bg-yellow-900/30'
            : 'text-surface-800 dark:text-surface-200 hover:bg-surface-100 dark:hover:bg-surface-700/50'
        )}
      >
        {icon}
        <span className="flex-1">{title}</span>
        {open ? <ChevronDown className="w-4 h-4 opacity-50" /> : <ChevronRight className="w-4 h-4 opacity-50" />}
      </button>
      {open && <div className="px-3 pb-3">{children}</div>}
    </section>
  );
}

const WORKSHOP_URL = 'https://sukwonie.gitbook.io/amazon-connect-aicc-builder-agent-workshop/gWzCDnQYz8mQUQ0GtYa4/';

/**
 * Notes Tab Content
 */
function NotesContent({ language }: { language: string }) {
  const ko = language === 'ko-KR';
  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-3 text-sm">
      {/* How to Use */}
      <NoteSection
        icon={<Lightbulb className="w-4 h-4 text-amber-500" />}
        title={ko ? 'How to Use / 사용 팁' : 'How to Use'}
      >
        <ul className="space-y-2 text-surface-600 dark:text-surface-400">
          <li className="flex items-start gap-2">
            <span className="text-amber-500 mt-0.5">•</span>
            <span>{ko
              ? '구체적일수록 좋습니다 — 업무별 입력 필드, 검증 규칙, 비즈니스 로직을 상세히 설명하면 더 정확한 코드가 생성됩니다'
              : 'Be specific — describe input fields, validation rules, and business logic in detail for more accurate code generation'
            }</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="text-amber-500 mt-0.5">•</span>
            <span>{ko
              ? '실제 DB와 유사 — 생성되는 DynamoDB 테이블은 실제 운영 환경과 유사한 스키마로 설계됩니다. 기존 테이블 구조를 미리 알려주면 더 정확합니다'
              : 'Real DB-like schema — generated DynamoDB tables mirror production schemas. Share existing table structures for better accuracy'
            }</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="text-amber-500 mt-0.5">•</span>
            <span>{ko
              ? '엣지 케이스 언급 — 예외 상황(환불 기한 초과, 재고 부족 등)을 미리 알려주면 Lambda 함수에 반영됩니다'
              : 'Mention edge cases — exceptions like refund deadlines or out-of-stock scenarios will be reflected in Lambda functions'
            }</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="text-amber-500 mt-0.5">•</span>
            <span>{ko
              ? '요구사항 문서 업로드 — PDF/Word/텍스트 파일을 직접 업로드하면 대화 시간을 크게 단축할 수 있습니다'
              : 'Upload requirements docs — PDF/Word/text files can be uploaded directly to significantly reduce conversation time'
            }</span>
          </li>
        </ul>
      </NoteSection>

      {/* Limitations */}
      <NoteSection
        icon={<AlertTriangle className="w-4 h-4 text-yellow-600 dark:text-yellow-400" />}
        title={ko ? 'Limitations / 제한사항' : 'Limitations'}
        variant="warning"
        defaultOpen={false}
      >
        <ul className="space-y-1 text-yellow-700 dark:text-yellow-400">
          <li className="flex items-start gap-2">
            <span className="mt-0.5">•</span>
            <span>{ko ? 'Operations 10개 이상: OpenAPI/Contact Flow 생성이 느려질 수 있음' : '10+ operations may slow down OpenAPI/Contact Flow generation'}</span>
          </li>
          <li className="flex items-start gap-2">
            <span className="mt-0.5">•</span>
            <span>{ko ? 'Lambda 함수당 최대 ~500줄 권장' : 'Recommended max ~500 lines per Lambda function'}</span>
          </li>
        </ul>
      </NoteSection>

      {/* Workshop Guide */}
      <NoteSection
        icon={<BookOpen className="w-4 h-4 text-primary-500" />}
        title={ko ? 'Workshop Guide / 워크숍 가이드' : 'Workshop Guide'}
      >
        <ol className="space-y-2 text-surface-600 dark:text-surface-400">
          {[
            { n: 1, t: ko ? 'AICC Builder에서 에셋 생성 + ZIP 다운로드' : 'Generate assets with AICC Builder + download ZIP' },
            { n: 2, t: ko ? 'CloudFormation 배포 (infrastructure.yaml)' : 'Deploy CloudFormation (infrastructure.yaml)' },
            { n: 3, t: ko ? 'OpenAPI 스펙 URL 업데이트 + S3 업로드' : 'Update OpenAPI spec URL + upload to S3' },
            { n: 4, t: ko ? 'Lambda 함수 코드 업로드 (각 함수별 Deploy 클릭)' : 'Upload Lambda code (click Deploy for each function)' },
            { n: 5, t: ko ? 'Amazon Connect 인스턴스 생성' : 'Create Amazon Connect instance' },
            { n: 6, t: ko ? 'AgentCore Gateway (MCP 서버) 구성 + Connect 연결' : 'Configure AgentCore Gateway (MCP server) + connect to Connect' },
            { n: 7, t: ko ? 'AI Agent 설정 (프롬프트 적용 + MCP 도구 추가)' : 'Set up AI Agent (apply prompt + add MCP tools)' },
            { n: 8, t: ko ? 'Contact Flow 가져오기 + Lex 봇 + 전화번호 연결' : 'Import Contact Flow + Lex bot + assign phone number' },
            { n: 9, t: ko ? 'Knowledge Base (FAQ를 S3 업로드 + Bedrock KB 동기화)' : 'Knowledge Base (upload FAQ to S3 + sync Bedrock KB)' },
          ].map(({ n, t }) => (
            <li key={n} className="flex items-start gap-2">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 flex items-center justify-center text-xs font-medium">{n}</span>
              <span>{t}</span>
            </li>
          ))}
        </ol>
        <a
          href={WORKSHOP_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 flex items-center gap-1.5 text-primary-600 dark:text-primary-400 hover:underline text-sm"
        >
          <ExternalLink className="w-3.5 h-3.5" />
          {ko ? '워크숍 상세 가이드 보기' : 'View full workshop guide'}
        </a>
      </NoteSection>

      {/* Package Structure */}
      <NoteSection
        icon={<Package className="w-4 h-4 text-green-500" />}
        title={ko ? 'Package Structure / 패키지 구조' : 'Package Structure'}
        defaultOpen={false}
      >
        <pre className="text-xs bg-surface-100 dark:bg-surface-800 p-3 rounded-lg overflow-x-auto text-surface-600 dark:text-surface-400 font-mono">
{`{project-name}/
├── cloudformation/infrastructure.yaml
├── lambda/{operation_id}/handler.py
├── openapi/openapi.yaml
├── prompts/ai_agent_prompt.md
├── contact-flow/contact_flow.json
└── knowledge-base/*.md`}
        </pre>
      </NoteSection>
    </div>
  );
}

/**
 * Individual Asset Download Button
 */
interface AssetDownloadButtonProps {
  icon: ReactNode;
  label: string;
  assetType: string;
}

function AssetDownloadButton({ icon, label, assetType }: AssetDownloadButtonProps) {
  const assets = useBuilderStore(s => s.assets);
  const assetPreviews = useBuilderStore(s => s.assetPreviews);
  const language = useBuilderStore(s => s.language);
  const { currentSessionId } = useSessionStore();
  const [isDownloading, setIsDownloading] = useState(false);

  const handleDownload = async () => {
    if (!currentSessionId) return;
    setIsDownloading(true);
    try {
      const result = await fetchAssetDownloadUrl(currentSessionId, assetType);
      if (result?.downloadUrl) {
        window.open(result.downloadUrl, '_blank');
      } else {
        console.error('[Download] Failed to get fresh download URL for', assetType);
      }
    } catch (error) {
      console.error('[Download] error:', error);
    } finally {
      setTimeout(() => setIsDownloading(false), 1000);
    }
  };

  // Check if we have any content to download
  // For knowledge_base type, check both 'faq' and 'package' asset types
  // For cdk type, also check 'cloudformation' (new naming convention)
  const assetTypesToCheck = assetType === 'knowledge_base'
    ? ['faq', 'package']
    : assetType === 'cdk'
    ? ['cdk', 'cloudformation']
    : [assetType];
  const hasContent = Object.values(assetPreviews).some(
    (p) => assetTypesToCheck.includes(p.assetType) && p.isComplete
  ) || (assets[assetType]?.files && Object.keys(assets[assetType].files).length > 0);

  // Show ZIP indicator for Lambda and knowledge_base (includes package)
  const isZipDownload = assetType === 'lambda' || assetType === 'knowledge_base' || assetType === 'package';

  return (
    <button
      onClick={handleDownload}
      disabled={isDownloading || !hasContent}
      className={cn(
        'w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm',
        'bg-surface-50 dark:bg-surface-800 hover:bg-surface-100 dark:hover:bg-surface-700 text-surface-700 dark:text-surface-300 transition-colors',
        'border border-surface-200 dark:border-surface-600',
        !hasContent && 'opacity-50 cursor-not-allowed'
      )}
    >
      {isDownloading ? (
        <Loader2 className="w-4 h-4 animate-spin text-surface-500 dark:text-surface-400" />
      ) : (
        <span className="text-surface-500 dark:text-surface-400">{icon}</span>
      )}
      <span className="flex-1 text-left">
        {label}
        {isZipDownload && (
          <span className="ml-1 text-xs text-surface-400 dark:text-surface-500">
            ({language === 'ko-KR' ? 'ZIP' : 'ZIP'})
          </span>
        )}
      </span>
      <Download className="w-3.5 h-3.5 text-surface-400 dark:text-surface-500" />
    </button>
  );
}

interface ProgressStepProps {
  item: ProgressItem;
  language: string;
}

function ProgressStep({ item, language }: ProgressStepProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const label = language === 'ko-KR' ? item.labelKo : item.label;
  const icon = STEP_ICONS[item.id] || <Circle className="w-4 h-4" />;
  const colors = STATUS_COLORS[item.status] || STATUS_COLORS.pending;
  const hasSubSteps = item.subSteps && item.subSteps.length > 0;

  return (
    <div className="rounded-lg overflow-hidden">
      {/* Main Step Row */}
      <div
        className={cn(
          'flex items-center gap-3 p-3 transition-colors',
          colors.bg,
          hasSubSteps && 'cursor-pointer hover:opacity-90'
        )}
        onClick={() => hasSubSteps && setIsExpanded(!isExpanded)}
      >
        {/* Icon - shows status: check for completed, spinner for in_progress, default icon for pending */}
        <div
          className={cn(
            'flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center transition-colors',
            colors.iconBg,
            colors.iconText
          )}
        >
          {item.status === 'completed' ? (
            <Check className="w-4 h-4" />
          ) : item.status === 'in_progress' ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            icon
          )}
        </div>

        {/* Label + progress bar */}
        <div className="flex-1 min-w-0">
          <span className={cn('text-sm font-medium truncate block', colors.text)}>
            {label}
          </span>
          {/* Segmented progress bar */}
          {item.status !== 'pending' && (
            <div className={cn('h-1 mt-1.5 rounded-full overflow-hidden', colors.progressBg)}>
              <div
                className={cn(
                  'h-full rounded-full transition-all duration-500',
                  item.status === 'in_progress'
                    ? 'animate-progress-shimmer bg-gradient-to-r from-primary-500 via-primary-300 to-primary-500 bg-[length:200%_100%]'
                    : colors.progressFill
                )}
                style={{ width: `${item.progress || 0}%` }}
              />
            </div>
          )}
        </div>

        {/* Expand/Collapse Arrow */}
        {hasSubSteps && (
          <div className={cn('flex-shrink-0', colors.iconText)}>
            {isExpanded ? (
              <ChevronDown className="w-4 h-4" />
            ) : (
              <ChevronRight className="w-4 h-4" />
            )}
          </div>
        )}
      </div>

      {/* Sub-steps */}
      {hasSubSteps && isExpanded && (
        <div className={cn('px-3 pb-2 space-y-1', colors.bg)}>
          {item.subSteps!.map((subStep) => (
            <SubStepItem key={subStep.id} subStep={subStep} language={language} />
          ))}
        </div>
      )}
    </div>
  );
}

interface SubStepItemProps {
  subStep: ProgressSubStep;
  language: string;
}

function SubStepItem({ subStep, language }: SubStepItemProps) {
  const label = language === 'ko-KR' ? subStep.labelKo : subStep.label;

  return (
    <div className="flex items-center gap-2 pl-11 py-1">
      {subStep.completed ? (
        <CheckCircle2 className="w-3.5 h-3.5 text-green-500 dark:text-green-400 flex-shrink-0" />
      ) : (
        <CircleDot className="w-3.5 h-3.5 text-surface-300 dark:text-surface-600 flex-shrink-0" />
      )}
      <span
        className={cn(
          'text-xs',
          subStep.completed ? 'text-green-600 dark:text-green-400' : 'text-surface-400 dark:text-surface-500'
        )}
      >
        {label}
      </span>
    </div>
  );
}

/**
 * Download Complete Modal — full-screen overlay with deployment guide
 */
function DownloadCompleteModal({ language, onClose }: { language: string; onClose: () => void }) {
  const ko = language === 'ko-KR';

  // Close on Escape key
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') onClose();
  }, [onClose]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/60 backdrop-blur-sm animate-in fade-in duration-200"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="relative w-full max-w-2xl max-h-[85vh] m-4 bg-white dark:bg-surface-850 rounded-2xl shadow-2xl dark:shadow-none border border-surface-200 dark:border-surface-700 flex flex-col overflow-hidden animate-in zoom-in-95 duration-200">
        {/* Close button */}
        <button
          onClick={onClose}
          className="absolute top-4 right-4 p-1.5 rounded-lg text-surface-400 hover:text-surface-600 dark:text-surface-500 dark:hover:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800 transition-colors z-10"
        >
          <X className="w-5 h-5" />
        </button>

        {/* Scrollable content */}
        <div className="overflow-y-auto p-8 space-y-6">
          {/* Hero section */}
          <div className="text-center space-y-3 pb-4 border-b border-surface-200 dark:border-surface-700">
            <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-green-100 dark:bg-green-900/30">
              <PartyPopper className="w-8 h-8 text-green-600 dark:text-green-400" />
            </div>
            <h2 className="text-2xl font-bold text-surface-900 dark:text-surface-100">
              {ko
                ? '에셋 생성이 완료되셨군요! 수고하셨습니다!'
                : 'Asset Generation Complete! Great job!'}
            </h2>
            <p className="text-surface-500 dark:text-surface-400">
              {ko
                ? 'ZIP 파일이 다운로드되었습니다. deploy.sh가 인프라부터 Connect + MCP 서버까지 자동으로 설정합니다.'
                : 'Your ZIP has been downloaded. deploy.sh automates everything from infra to Connect + MCP server setup.'}
            </p>
          </div>

          {/* Deploy Guide */}
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-lg font-semibold text-surface-800 dark:text-surface-200">
              <Rocket className="w-5 h-5 text-primary-500" />
              <span>{ko ? 'deploy.sh 배포 가이드' : 'Deployment with deploy.sh'}</span>
            </div>

            <div className="bg-surface-50 dark:bg-surface-800 rounded-xl p-5 space-y-4 border border-surface-200 dark:border-surface-700">
              <div className="flex items-center gap-2 text-sm font-medium text-surface-700 dark:text-surface-300">
                <Terminal className="w-4 h-4 text-amber-500" />
                <span>{ko ? 'AWS CloudShell에서 실행하는 단계' : 'Steps in AWS CloudShell'}</span>
              </div>

              <ol className="space-y-3 text-sm text-surface-600 dark:text-surface-400">
                {[
                  {
                    ko: 'AWS Console 상단 > CloudShell 열기',
                    en: 'AWS Console top bar > Open CloudShell',
                  },
                  {
                    ko: '다운 ZIP 파일을 Actions > Upload file로 업로드',
                    en: 'Upload the downloaded ZIP via Actions > Upload file',
                  },
                  {
                    ko: 'ZIP 파일을 풀고 deploy.sh 실행:',
                    en: 'Extract ZIP and run deploy.sh:',
                    code: true,
                  },
                ].map((step, i) => (
                  <li key={i} className="flex items-start gap-3">
                    <span className="flex-shrink-0 w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 flex items-center justify-center text-xs font-bold">
                      {i + 1}
                    </span>
                    <div className="flex-1">
                      <span>{ko ? step.ko : step.en}</span>
                      {step.code && (
                        <pre className="mt-2 bg-surface-900 dark:bg-black text-green-400 text-xs rounded-lg p-3 overflow-x-auto font-mono">
{`unzip *.zip && cd */
chmod +x deploy.sh
./deploy.sh`}
                        </pre>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </div>

            {/* What deploy.sh does */}
            <div className="bg-surface-50 dark:bg-surface-800 rounded-xl p-5 space-y-3 border border-surface-200 dark:border-surface-700">
              <div className="text-sm font-medium text-surface-700 dark:text-surface-300">
                {ko ? 'deploy.sh가 자동으로 처리하는 작업:' : 'deploy.sh automatically handles:'}
              </div>
              <ul className="space-y-1.5 text-sm text-surface-600 dark:text-surface-400">
                {[
                  { ko: 'CloudFormation 스택 배포 (S3 업로드 포함)', en: 'Deploy CloudFormation stack (incl. S3 upload)' },
                  { ko: 'Lambda 함수 코드 업데이트 (재시도 포함)', en: 'Update Lambda function code (with retry)' },
                  { ko: 'OpenAPI 스펙 업데이트 & S3 업로드', en: 'Update OpenAPI spec & upload to S3' },
                  { ko: 'FAQ 문서 S3 업로드', en: 'Upload FAQ documents to S3' },
                  { ko: 'Amazon Connect 인스턴스 생성/선택', en: 'Create or select Amazon Connect instance' },
                  { ko: 'Q in Connect Assistant 생성 & 연결', en: 'Create Q in Connect Assistant & link' },
                  { ko: 'Lambda 환경변수 자동 주입', en: 'Inject Lambda environment variables' },
                  { ko: 'AgentCore Gateway (MCP 서버) 생성 & Connect 연결', en: 'Create AgentCore Gateway (MCP server) & connect' },
                ].map((item, i) => (
                  <li key={i} className="flex items-start gap-2">
                    <Check className="w-4 h-4 text-green-500 flex-shrink-0 mt-0.5" />
                    <span>{ko ? item.ko : item.en}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>

          {/* Next Steps after deploy */}
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-lg font-semibold text-surface-800 dark:text-surface-200">
              <BookOpen className="w-5 h-5 text-primary-500" />
              <span>{ko ? '다음 단계 (Workshop Guide)' : 'Next Steps (Workshop Guide)'}</span>
            </div>
            <ol className="space-y-2 text-sm text-surface-600 dark:text-surface-400">
              {[
                { ko: 'AI Agent 프롬프트 설정 (prompts/ 내용 붙여넣기)', en: 'Set up AI Agent prompt (paste from prompts/)' },
                { ko: 'Contact Flow Import + Lex Bot 연결 + 전화번호 할당', en: 'Import Contact Flow + Lex bot + assign phone number' },
                { ko: 'Knowledge Base S3 Data Source 연결 (FAQ 있는 경우)', en: 'Connect Knowledge Base S3 data source (if FAQ exists)' },
              ].map((step, i) => (
                <li key={i} className="flex items-start gap-3">
                  <span className="flex-shrink-0 w-6 h-6 rounded-full bg-surface-200 dark:bg-surface-700 text-surface-600 dark:text-surface-300 flex items-center justify-center text-xs font-bold">
                    {i + 1}
                  </span>
                  <span>{ko ? step.ko : step.en}</span>
                </li>
              ))}
            </ol>
            <a
              href={WORKSHOP_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2.5 bg-primary-600 dark:bg-primary-500 text-white rounded-lg hover:bg-primary-700 dark:hover:bg-primary-600 transition-colors text-sm font-medium"
            >
              <ExternalLink className="w-4 h-4" />
              {ko ? '워크샵 상세 가이드 보기' : 'View Full Workshop Guide'}
            </a>
          </div>
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 px-8 py-4 border-t border-surface-200 dark:border-surface-700 bg-surface-50 dark:bg-surface-800/50">
          <button
            onClick={onClose}
            className="w-full px-4 py-2.5 bg-surface-200 dark:bg-surface-700 text-surface-700 dark:text-surface-300 rounded-lg hover:bg-surface-300 dark:hover:bg-surface-600 transition-colors text-sm font-medium"
          >
            {ko ? '닫기' : 'Close'}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
