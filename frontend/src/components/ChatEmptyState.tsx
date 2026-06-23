/**
 * Chat Empty State — Mode-first start screen
 *
 * Three mode cards: Full Build / Single Segment / Improve Existing.
 * - "Single Segment" expands a radio row: Contact Flow / AI Prompt / FAQ.
 * A single shared composer (description + attachments) sits below for ALL modes:
 * attach one or more files (flow JSON / prompt YAML / a flow-diagram image / docs),
 * type a prompt, then Start. Attaching never auto-starts — the conversation begins
 * only on Start, exactly like the in-chat composer.
 */

import { useCallback, useRef, useState } from 'react';
import {
  LayoutGrid,
  Puzzle,
  Pencil,
  Workflow,
  MessageSquare,
  BookOpen,
  ArrowRight,
  AlertTriangle,
  Paperclip,
} from 'lucide-react';
import { cn } from '../lib/utils';
import type { AttachedFile, Language } from '../types';
import {
  useBuilderStore,
  type StartMode,
  type SegmentType,
} from '../stores/builderStore';
import { ModelSelector } from './ModelSelector';
import { validateFile, MAX_FILES } from './ChatAttachmentButton';
import { AttachmentPreview } from './AttachmentPreview';

interface ChatEmptyStateProps {
  language: Language;
  /**
   * Start a run. mode=full → scope []; segment → scope [segment]; improve →
   * scope derived from the first attachment. `attachments` are the staged raw
   * files (may be empty). The conversation begins with `description` + files.
   */
  onStart: (
    mode: StartMode,
    segment: SegmentType | null,
    description: string,
    attachments: File[],
  ) => void;
}

const STRINGS: Record<Language, Record<string, string>> = {
  'en-US': {
    heading: 'What would you like to build?',
    full: 'Full Build',
    fullDesc: 'Run the full interview and generate the complete 6-asset bundle.',
    segment: 'Single Segment',
    segmentDesc: 'Generate just one Connect asset: Contact Flow, AI Prompt, or FAQ.',
    improve: 'Improve Existing',
    improveDesc: 'Attach a Contact Flow, AI Prompt, or a photo of a flow and refine it.',
    cf: 'Contact Flow',
    prompt: 'AI Prompt',
    faq: 'FAQ',
    descPlaceholder: 'Describe what you want to build…',
    descPlaceholderSegment: 'Describe this segment…',
    descPlaceholderImprove: 'Attach a file and say what to do with it (e.g. "make the greeting friendlier")…',
    start: 'Start',
    unsupported: 'Unsupported file. Use an image, a .json Contact Flow, a .yaml/.yml AI Prompt, or a document (.pdf/.txt/.md/.docx/.csv/.xlsx).',
    maxFiles: `You can attach up to ${MAX_FILES} files.`,
    attachHint: 'Attach flow JSON, prompt YAML, a flow-diagram image, or docs',
  },
  'ko-KR': {
    heading: '무엇을 만들고 싶으신가요?',
    full: '전체 빌드',
    fullDesc: '전체 인터뷰를 진행하고 6개 에셋 번들을 생성합니다.',
    segment: '단일 세그먼트',
    segmentDesc: 'Connect 에셋 하나만 생성: Contact Flow, AI 프롬프트, FAQ.',
    improve: '기존 에셋 개선',
    improveDesc: 'Contact Flow, AI 프롬프트, 또는 플로우 사진을 첨부해 다듬으세요.',
    cf: 'Contact Flow',
    prompt: 'AI 프롬프트',
    faq: 'FAQ',
    descPlaceholder: '무엇을 만들지 설명해 주세요…',
    descPlaceholderSegment: '이 세그먼트를 설명해 주세요…',
    descPlaceholderImprove: '파일을 첨부하고 무엇을 할지 적어주세요 (예: "인사말을 더 친근하게")…',
    start: '시작',
    unsupported: '지원되지 않는 파일입니다. 이미지, .json Contact Flow, .yaml/.yml AI 프롬프트, 또는 문서(.pdf/.txt/.md/.docx/.csv/.xlsx)를 사용하세요.',
    maxFiles: `최대 ${MAX_FILES}개까지 첨부할 수 있습니다.`,
    attachHint: '플로우 JSON, 프롬프트 YAML, 플로우 이미지, 문서를 첨부하세요',
  },
  'ja-JP': {
    heading: '何を構築しますか?',
    full: 'フルビルド',
    fullDesc: 'フルインタビューを実施し、6つのアセットバンドルを生成します。',
    segment: '単一セグメント',
    segmentDesc: 'Connectアセットを1つだけ生成: Contact Flow、AIプロンプト、FAQ。',
    improve: '既存を改善',
    improveDesc: 'Contact Flow、AIプロンプト、またはフロー図の写真を添付して改善します。',
    cf: 'Contact Flow',
    prompt: 'AIプロンプト',
    faq: 'FAQ',
    descPlaceholder: '構築したい内容を説明してください…',
    descPlaceholderSegment: 'このセグメントを説明してください…',
    descPlaceholderImprove: 'ファイルを添付して、何をするか記入してください（例:「挨拶をもっとフレンドリーに」）…',
    start: '開始',
    unsupported: 'サポートされていないファイルです。画像、.json Contact Flow、.yaml/.yml AIプロンプト、または文書(.pdf/.txt/.md/.docx/.csv/.xlsx)を使用してください。',
    maxFiles: `最大${MAX_FILES}件まで添付できます。`,
    attachHint: 'フローJSON、プロンプトYAML、フロー図画像、文書を添付',
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

// Text-asset extensions the start screen accepts in addition to images/docs.
// These are NOT valid Bedrock attachment MIME types, so they're carried inline
// as message text by ChatWindow — staged here only for preview/removal.
const TEXT_ASSET_EXTS = ['.json', '.yaml', '.yml'];

function isTextAsset(file: File): boolean {
  const n = file.name.toLowerCase();
  return TEXT_ASSET_EXTS.some((e) => n.endsWith(e));
}

export function ChatEmptyState({ language, onStart }: ChatEmptyStateProps) {
  const t = STRINGS[language] || STRINGS['en-US'];

  const startMode = useBuilderStore((s) => s.startMode);
  const setStartMode = useBuilderStore((s) => s.setStartMode);
  const segment = useBuilderStore((s) => s.segment);
  const setSegment = useBuilderStore((s) => s.setSegment);

  const [description, setDescription] = useState('');
  const [stagedFiles, setStagedFiles] = useState<AttachedFile[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Stage files (no upload, no auto-start). Accepts images/docs (validateFile)
  // plus .json/.yaml/.yml (treated as 'document' for preview; inlined as text on Start).
  const stageFiles = useCallback(
    (files: File[]) => {
      setAttachError(null);
      if (stagedFiles.length + files.length > MAX_FILES) {
        setAttachError(t.maxFiles);
        return;
      }
      const next: AttachedFile[] = [];
      for (const file of files) {
        if (isTextAsset(file)) {
          next.push({
            id: `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
            file,
            type: 'document',
            status: 'ready',
          });
          continue;
        }
        const v = validateFile(file);
        if (!v.valid) {
          setAttachError(v.error || t.unsupported);
          continue;
        }
        next.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
          file,
          type: v.type || 'document',
          status: 'ready',
        });
      }
      // Generate image previews
      next.forEach((att) => {
        if (att.type === 'image') {
          const reader = new FileReader();
          reader.onload = (e) =>
            setStagedFiles((prev) =>
              prev.map((a) => (a.id === att.id ? { ...a, preview: e.target?.result as string } : a))
            );
          reader.readAsDataURL(att.file);
        }
      });
      if (next.length) setStagedFiles((prev) => [...prev, ...next]);
    },
    [stagedFiles.length, t.maxFiles, t.unsupported]
  );

  const removeFile = useCallback(
    (id: string) => setStagedFiles((prev) => prev.filter((a) => a.id !== id)),
    []
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragActive(false);
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length) stageFiles(files);
    },
    [stageFiles]
  );

  const handleStart = () => {
    if (!canStart) return;
    onStart(
      startMode,
      startMode === 'segment' ? segment : null,
      description.trim(),
      stagedFiles.map((a) => a.file),
    );
  };

  // Full: text optional (interview runs). Segment: needs a chosen segment + text.
  // Improve: needs at least one attachment OR text to act on.
  const hasFiles = stagedFiles.length > 0;
  const canStart =
    startMode === 'full' ||
    (startMode === 'segment' && !!segment && (description.trim().length > 0 || hasFiles)) ||
    (startMode === 'improve' && (hasFiles || description.trim().length > 0));

  const placeholder =
    startMode === 'segment'
      ? t.descPlaceholderSegment
      : startMode === 'improve'
      ? t.descPlaceholderImprove
      : t.descPlaceholder;

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
                  active ? 'text-primary-700 dark:text-primary-300' : 'text-surface-900 dark:text-surface-100'
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

        {/* Shared composer: attachments (all modes) + description + model + Start */}
        {attachError && (
          <div className="mb-3 flex items-start gap-2 p-3 rounded-lg bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
            <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-red-600 dark:text-red-400 whitespace-pre-line">{attachError}</p>
          </div>
        )}

        {stagedFiles.length > 0 && (
          <AttachmentPreview files={stagedFiles} onRemove={removeFile} className="mb-3" language={language} />
        )}

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={onDrop}
          className={cn(
            'rounded-xl border bg-white dark:bg-surface-800 overflow-hidden transition-colors',
            dragActive
              ? 'border-primary-500 ring-2 ring-primary-500/30'
              : 'border-surface-200 dark:border-surface-700'
          )}
        >
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
            placeholder={placeholder}
            className={cn(
              'w-full px-4 py-3 resize-none bg-transparent text-sm',
              'text-surface-900 dark:text-surface-100 placeholder-surface-400 dark:placeholder-surface-500',
              'focus:outline-none'
            )}
          />
          <div className="flex items-center justify-between gap-2 px-3 py-2 border-t border-surface-200 dark:border-surface-700 bg-surface-50/60 dark:bg-surface-900/40">
            <div className="flex items-center gap-1">
              {/* Attach: images, docs, AND json/yaml. The accept list adds json/yaml
                  on top of ChatAttachmentButton's image/doc set; json/yaml are staged
                  via onChange below and inlined as text on Start. */}
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={stagedFiles.length >= MAX_FILES}
                title={t.attachHint}
                aria-label={t.attachHint}
                className={cn(
                  'flex-shrink-0 p-2 rounded-lg transition-colors',
                  'focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-1',
                  stagedFiles.length >= MAX_FILES
                    ? 'text-surface-400 dark:text-surface-600 cursor-not-allowed'
                    : 'text-surface-500 dark:text-surface-400 hover:text-primary-600 dark:hover:text-primary-400 hover:bg-surface-100 dark:hover:bg-surface-700'
                )}
              >
                <Paperclip className="w-5 h-5" />
              </button>
              <ModelSelector language={language} variant="composer" />
            </div>
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

        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".json,.yaml,.yml,.png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,.docx,.csv,.xlsx,.xls,image/*"
          className="hidden"
          aria-label={t.attachHint}
          onChange={(e) => {
            const files = Array.from(e.target.files || []);
            if (files.length) stageFiles(files);
            if (fileInputRef.current) fileInputRef.current.value = '';
          }}
        />
      </div>
    </div>
  );
}
