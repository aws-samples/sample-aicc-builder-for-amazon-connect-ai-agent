/**
 * Chat Empty State — Mode-first start screen
 *
 * Three mode cards: Full Build / Single Segment / Improve Existing.
 * - "Single Segment" expands a radio row: Contact Flow / AI Prompt / FAQ.
 * - "Improve Existing" shows a file-upload dropzone (.json flow / .yaml prompt)
 *   plus a lint-result strip after upload.
 * Below: a description composer with the model selector + a Start button.
 */

import { useCallback, useRef, useState } from 'react';
import {
  LayoutGrid,
  Puzzle,
  Pencil,
  UploadCloud,
  Workflow,
  MessageSquare,
  BookOpen,
  ArrowRight,
  CheckCircle2,
  AlertTriangle,
  FileJson,
  Loader2,
} from 'lucide-react';
import { cn } from '../lib/utils';
import type { Language } from '../types';
import {
  useBuilderStore,
  type StartMode,
  type SegmentType,
  type ImportAssetType,
} from '../stores/builderStore';
import { ModelSelector } from './ModelSelector';

interface ChatEmptyStateProps {
  language: Language;
  /** Start a full build (scope=[]) or single-segment run (scope=[segment]) with a description. */
  onStart: (mode: StartMode, segment: SegmentType | null, description: string) => void;
  /** Import an external asset file for editing. */
  onImport: (assetType: ImportAssetType, content: string, name: string) => void;
}

const STRINGS: Record<Language, Record<string, string>> = {
  'en-US': {
    heading: 'What would you like to build?',
    full: 'Full Build',
    fullDesc: 'Run the full interview and generate the complete 6-asset bundle.',
    segment: 'Single Segment',
    segmentDesc: 'Generate just one Connect asset: Contact Flow, AI Prompt, or FAQ.',
    improve: 'Improve Existing',
    improveDesc: 'Upload an external Contact Flow or AI Prompt and edit it in place.',
    cf: 'Contact Flow',
    prompt: 'AI Prompt',
    faq: 'FAQ',
    dropHint: 'Drop a .json flow or .yaml prompt here, or click to browse',
    dropBrowse: 'Browse files',
    descPlaceholder: 'Describe what you want to build…',
    descPlaceholderSegment: 'Describe this segment…',
    start: 'Start',
    importing: 'Importing…',
    lintOk: 'Validated',
    lintBlocks: 'blocks',
    lintFixed: 'fixed',
    lintErrors: 'issue(s)',
    unsupported: 'Unsupported file. Use a .json Contact Flow or a .yaml/.yml AI Prompt.',
  },
  'ko-KR': {
    heading: '무엇을 만들고 싶으신가요?',
    full: '전체 빌드',
    fullDesc: '전체 인터뷰를 진행하고 6개 에셋 번들을 생성합니다.',
    segment: '단일 세그먼트',
    segmentDesc: 'Connect 에셋 하나만 생성: Contact Flow, AI 프롬프트, FAQ.',
    improve: '기존 에셋 개선',
    improveDesc: '외부 Contact Flow 또는 AI 프롬프트를 업로드해 바로 수정합니다.',
    cf: 'Contact Flow',
    prompt: 'AI 프롬프트',
    faq: 'FAQ',
    dropHint: '.json 플로우 또는 .yaml 프롬프트를 끌어다 놓거나 클릭해 선택하세요',
    dropBrowse: '파일 선택',
    descPlaceholder: '무엇을 만들지 설명해 주세요…',
    descPlaceholderSegment: '이 세그먼트를 설명해 주세요…',
    start: '시작',
    importing: '가져오는 중…',
    lintOk: '검증 완료',
    lintBlocks: '블록',
    lintFixed: '수정',
    lintErrors: '건의 문제',
    unsupported: '지원되지 않는 파일입니다. .json Contact Flow 또는 .yaml/.yml AI 프롬프트를 사용하세요.',
  },
  'ja-JP': {
    heading: '何を構築しますか?',
    full: 'フルビルド',
    fullDesc: 'フルインタビューを実施し、6つのアセットバンドルを生成します。',
    segment: '単一セグメント',
    segmentDesc: 'Connectアセットを1つだけ生成: Contact Flow、AIプロンプト、FAQ。',
    improve: '既存を改善',
    improveDesc: '外部のContact FlowまたはAIプロンプトをアップロードして編集します。',
    cf: 'Contact Flow',
    prompt: 'AIプロンプト',
    faq: 'FAQ',
    dropHint: '.jsonフローまたは.yamlプロンプトをドロップ、またはクリックして選択',
    dropBrowse: 'ファイルを選択',
    descPlaceholder: '構築したい内容を説明してください…',
    descPlaceholderSegment: 'このセグメントを説明してください…',
    start: '開始',
    importing: 'インポート中…',
    lintOk: '検証済み',
    lintBlocks: 'ブロック',
    lintFixed: '修正',
    lintErrors: '件の問題',
    unsupported: 'サポートされていないファイルです。.json Contact Flowまたは.yaml/.yml AIプロンプトを使用してください。',
  },
};

const SEGMENT_OPTIONS: { id: SegmentType; icon: typeof Workflow; key: string }[] = [
  { id: 'contact_flow', icon: Workflow, key: 'cf' },
  { id: 'prompt', icon: MessageSquare, key: 'prompt' },
  { id: 'faq', icon: BookOpen, key: 'faq' },
];

const MODE_CARDS: { id: StartMode; icon: typeof LayoutGrid; titleKey: string; descKey: string }[] = [
  { id: 'full', icon: LayoutGrid, titleKey: 'full', descKey: 'fullDesc' },
  { id: 'segment', icon: Puzzle, titleKey: 'segment', descKey: 'segmentDesc' },
  { id: 'improve', icon: Pencil, titleKey: 'improve', descKey: 'improveDesc' },
];

export function ChatEmptyState({ language, onStart, onImport }: ChatEmptyStateProps) {
  const t = STRINGS[language] || STRINGS['en-US'];

  const startMode = useBuilderStore((s) => s.startMode);
  const setStartMode = useBuilderStore((s) => s.setStartMode);
  const segment = useBuilderStore((s) => s.segment);
  const setSegment = useBuilderStore((s) => s.setSegment);
  const importedAsset = useBuilderStore((s) => s.importedAsset);

  const [description, setDescription] = useState('');
  const [dragActive, setDragActive] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [isImporting, setIsImporting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const detectAssetType = (file: File): ImportAssetType | null => {
    const name = file.name.toLowerCase();
    if (name.endsWith('.json')) return 'contact_flow';
    if (name.endsWith('.yaml') || name.endsWith('.yml')) return 'prompt';
    return null;
  };

  const handleFile = useCallback(
    async (file: File) => {
      setImportError(null);
      const assetType = detectAssetType(file);
      if (!assetType) {
        setImportError(t.unsupported);
        return;
      }
      setIsImporting(true);
      try {
        const content = await file.text();
        onImport(assetType, content, file.name);
      } finally {
        // The backend reply (asset_imported) drives the transition; release the
        // local spinner shortly after the send so the UI doesn't get stuck.
        setTimeout(() => setIsImporting(false), 1500);
      }
    },
    [onImport, t.unsupported]
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragActive(false);
      const file = e.dataTransfer.files?.[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  const handleStart = () => {
    if (startMode === 'segment' && !segment) return;
    onStart(startMode, startMode === 'segment' ? segment : null, description.trim());
  };

  // Full build can start with no text (the agent runs an interview). A single
  // segment run produces one asset directly from the description, so require a
  // non-whitespace description as well as a chosen segment.
  const canStart =
    startMode === 'full' ||
    (startMode === 'segment' && !!segment && description.trim().length > 0);

  return (
    <div className="flex flex-col items-center justify-center min-h-full px-4 py-8">
      {/* Logo */}
      <div className="w-16 h-16 mb-5 rounded-2xl bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center shadow-lg dark:shadow-glow">
        <span className="text-3xl">🤖</span>
      </div>

      <h2 className="text-xl lg:text-2xl font-semibold text-surface-900 dark:text-surface-100 mb-8 text-center">
        {t.heading}
      </h2>

      {/* Mode cards */}
      <div
        role="radiogroup"
        aria-label={t.heading}
        className="grid grid-cols-1 sm:grid-cols-3 gap-3 max-w-3xl w-full mb-4"
      >
        {MODE_CARDS.map((card) => {
          const Icon = card.icon;
          const active = startMode === card.id;
          return (
            <button
              key={card.id}
              role="radio"
              aria-checked={active}
              onClick={() => setStartMode(card.id)}
              className={cn(
                'group flex flex-col items-start gap-2 p-4 rounded-xl border text-left transition-all',
                'focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 dark:focus:ring-offset-surface-850',
                active
                  ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/30 dark:border-primary-500'
                  : 'border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800 hover:border-primary-300 dark:hover:border-primary-600'
              )}
            >
              <div
                className={cn(
                  'w-10 h-10 rounded-lg flex items-center justify-center transition-colors',
                  active
                    ? 'bg-primary-100 dark:bg-primary-800/50 text-primary-600 dark:text-primary-300'
                    : 'bg-surface-100 dark:bg-surface-700 text-surface-600 dark:text-surface-400'
                )}
              >
                <Icon className="w-5 h-5" />
              </div>
              <h3
                className={cn(
                  'font-medium',
                  active
                    ? 'text-primary-700 dark:text-primary-300'
                    : 'text-surface-900 dark:text-surface-100'
                )}
              >
                {t[card.titleKey]}
              </h3>
              <p className="text-xs text-surface-500 dark:text-surface-400 leading-relaxed">
                {t[card.descKey]}
              </p>
            </button>
          );
        })}
      </div>

      <div className="max-w-3xl w-full">
        {/* Segment radios */}
        {startMode === 'segment' && (
          <div
            role="radiogroup"
            aria-label={t.segment}
            className="flex flex-wrap gap-2 mb-4 p-3 rounded-xl border border-surface-200 dark:border-surface-700 bg-surface-50 dark:bg-surface-800/50"
          >
            {SEGMENT_OPTIONS.map((opt) => {
              const Icon = opt.icon;
              const active = segment === opt.id;
              return (
                <button
                  key={opt.id}
                  role="radio"
                  aria-checked={active}
                  onClick={() => setSegment(opt.id)}
                  className={cn(
                    'flex items-center gap-2 px-3 py-2 rounded-lg text-sm border transition-all',
                    active
                      ? 'border-primary-500 bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300'
                      : 'border-surface-200 dark:border-surface-600 bg-white dark:bg-surface-800 text-surface-600 dark:text-surface-300 hover:border-primary-300 dark:hover:border-primary-600'
                  )}
                >
                  <Icon className="w-4 h-4" />
                  {t[opt.key]}
                </button>
              );
            })}
          </div>
        )}

        {/* Improve-Existing dropzone */}
        {startMode === 'improve' && (
          <div className="mb-4">
            <div
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragActive(true);
              }}
              onDragLeave={() => setDragActive(false)}
              onDrop={onDrop}
              role="button"
              tabIndex={0}
              aria-label={t.dropHint}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  fileInputRef.current?.click();
                }
              }}
              className={cn(
                'flex flex-col items-center justify-center gap-2 px-4 py-8 rounded-xl border-2 border-dashed cursor-pointer transition-colors',
                'focus:outline-none focus:ring-2 focus:ring-primary-500',
                dragActive
                  ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/30'
                  : 'border-surface-300 dark:border-surface-600 bg-surface-50 dark:bg-surface-800/50 hover:border-primary-400 dark:hover:border-primary-500'
              )}
            >
              {isImporting ? (
                <Loader2 className="w-8 h-8 text-primary-500 animate-spin" />
              ) : (
                <UploadCloud className="w-8 h-8 text-surface-400 dark:text-surface-500" />
              )}
              <p className="text-sm text-surface-600 dark:text-surface-300 text-center">
                {isImporting ? t.importing : t.dropHint}
              </p>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,.yaml,.yml"
              className="hidden"
              aria-label={t.dropBrowse}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleFile(file);
                if (fileInputRef.current) fileInputRef.current.value = '';
              }}
            />

            {importError && (
              <div className="mt-3 flex items-start gap-2 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
                <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
                <p className="text-sm text-red-600 dark:text-red-400">{importError}</p>
              </div>
            )}

            {/* Lint-result strip after a successful import */}
            {importedAsset && (
              <div
                className={cn(
                  'mt-3 flex items-start gap-2 p-3 rounded-lg border',
                  importedAsset.lint.errors.length > 0
                    ? 'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800 text-amber-700 dark:text-amber-300'
                    : 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800 text-green-700 dark:text-green-300'
                )}
              >
                {importedAsset.lint.errors.length > 0 ? (
                  <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                ) : (
                  <CheckCircle2 className="w-4 h-4 flex-shrink-0 mt-0.5" />
                )}
                <div className="text-sm">
                  <span className="inline-flex items-center gap-1.5 font-medium">
                    <FileJson className="w-3.5 h-3.5" />
                    {importedAsset.fileName}
                  </span>
                  <span className="ml-2">
                    {t.lintOk}
                    {importedAsset.lint.errors.length > 0
                      ? ` — ${importedAsset.lint.errors.length} ${t.lintErrors}`
                      : ''}
                    {importedAsset.lint.fixesApplied > 0
                      ? `, ${importedAsset.lint.fixesApplied} ${t.lintFixed}`
                      : ''}
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Description composer + model selector + Start (full / segment only) */}
        {startMode !== 'improve' && (
          <div className="rounded-xl border border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-800 overflow-hidden">
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  if (canStart) handleStart();
                }
              }}
              rows={3}
              placeholder={
                startMode === 'segment' ? t.descPlaceholderSegment : t.descPlaceholder
              }
              className={cn(
                'w-full px-4 py-3 resize-none bg-transparent text-sm',
                'text-surface-900 dark:text-surface-100 placeholder-surface-400 dark:placeholder-surface-500',
                'focus:outline-none'
              )}
            />
            <div className="flex items-center justify-between gap-2 px-3 py-2 border-t border-surface-200 dark:border-surface-700 bg-surface-50/60 dark:bg-surface-900/40">
              <ModelSelector language={language} variant="composer" />
              <button
                onClick={handleStart}
                disabled={!canStart}
                className={cn(
                  'flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-all',
                  canStart
                    ? 'bg-primary-600 dark:bg-primary-500 text-white hover:bg-primary-700 dark:hover:bg-primary-600 shadow-sm dark:shadow-glow'
                    : 'bg-surface-200 dark:bg-surface-700 text-surface-400 dark:text-surface-500 cursor-not-allowed'
                )}
              >
                {t.start}
                <ArrowRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
