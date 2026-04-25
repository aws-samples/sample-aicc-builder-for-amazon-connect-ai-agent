/**
 * Chat Window Component
 *
 * Main chat interface for interacting with the AICC Builder Agent
 */

import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { Send, AlertCircle, Loader2, WifiOff, Wifi, ChevronDown, ChevronUp, CheckCircle2, Circle } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { useSessionStore } from '../stores/sessionStore';
import { useWebSocket } from '../hooks/useWebSocket';
import { useAutoSave } from '../hooks/useAutoSave';
import { MessageBubble } from './MessageBubble';
import { TypingIndicator } from './TypingIndicator';
import { AssetPreviewBubble } from './AssetPreviewBubble';
import { ChatAttachmentButton, validateFile } from './ChatAttachmentButton';
import { AttachmentPreview } from './AttachmentPreview';
import { ChatEmptyState } from './ChatEmptyState';
import { cn } from '../lib/utils';
import type { AttachedFile, MessageAttachment } from '../types';
import { VirtualizedItem } from './VirtualizedItem';

const MAX_INPUT_LENGTH = 30000;
const CHAR_COUNT_THRESHOLD = 28000;

export function ChatWindow() {
  const [inputValue, setInputValue] = useState('');
  const [hasUpdatedTitle, setHasUpdatedTitle] = useState(false);
  const [attachments, setAttachments] = useState<AttachedFile[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [isUploadingAttachments, setIsUploadingAttachments] = useState(false);
  const [showProgressDetail, setShowProgressDetail] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const attachmentButtonRef = useRef<HTMLButtonElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const attachmentErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Granular selectors - only re-render when the specific slice changes
  const messages = useBuilderStore(s => s.messages);
  const isTyping = useBuilderStore(s => s.isTyping);
  const isConnected = useBuilderStore(s => s.isConnected);
  const language = useBuilderStore(s => s.language);
  const assetPreviews = useBuilderStore(s => s.assetPreviews);
  const isSessionReady = useBuilderStore(s => s.isSessionReady);
  const isLoadingSession = useBuilderStore(s => s.isLoadingSession);
  const reconnectStatus = useBuilderStore(s => s.reconnectStatus);
  const progress = useBuilderStore(s => s.progress);
  const inputHint = useBuilderStore(s => s.inputHint);
  const session = useBuilderStore(s => s.session);

  const { currentSessionId, updateSessionTitle, updateSessionActivity, createNewSession, sessions } = useSessionStore();
  const { sendMessage, sendMessageWithAttachments, connect } = useWebSocket();

  // Auto-save runs entirely outside render cycle via zustand subscribe
  useAutoSave();

  // E1: Mobile progress bar state
  const activeProgress = useMemo(() => {
    const inProgress = progress.filter(p => p.status === 'in_progress');
    const completed = progress.filter(p => p.status === 'completed');
    const total = progress.length;
    const completedCount = completed.length;
    const currentItem = inProgress[0];
    const hasAnyActivity = completedCount > 0 || inProgress.length > 0;
    const overallPercent = total > 0 ? Math.round((completedCount / total) * 100) : 0;
    return { currentItem, completedCount, total, hasAnyActivity, overallPercent, inProgress, completed };
  }, [progress]);

  // E2: Requirements checklist derived from session state
  const requirementsChecklist = useMemo(() => {
    const items = [
      { id: 'company', label: language === 'ko-KR' ? '회사명' : 'Company', done: !!session.companyName },
      { id: 'industry', label: language === 'ko-KR' ? '산업' : 'Industry', done: !!session.industry },
      { id: 'operations', label: language === 'ko-KR' ? 'Operations' : 'Operations', done: (session.operations?.length ?? 0) > 0 },
      { id: 'db', label: language === 'ko-KR' ? 'DB 정보' : 'DB Info', done: !!session.dbConnected },
    ];
    const doneCount = items.filter(i => i.done).length;
    const allRequired = items.slice(0, 3).every(i => i.done); // company, industry, ops = minimum
    return { items, doneCount, total: items.length, allRequired };
  }, [session, language]);

  // Build asset key helper function
  const buildAssetKey = (assetType: string, operationId?: string, fileName?: string): string => {
    if (fileName && operationId) return `${assetType}-${operationId}-${fileName}`;
    if (fileName) return `${assetType}-${fileName}`;
    if (operationId) return `${assetType}-${operationId}`;
    return assetType;
  };

  // Merge messages and asset previews into a single timeline
  type TimelineItem =
    | { type: 'message'; data: typeof messages[0]; timestamp: number }
    | { type: 'asset'; data: (typeof assetPreviews)[keyof typeof assetPreviews]; key: string; timestamp: number };

  const timeline = useMemo<TimelineItem[]>(() => {
    const assetsWithMarkers = new Set(
      messages
        .filter(m => m.role === 'asset' && m.assetRef)
        .map(m => buildAssetKey(m.assetRef!.assetType, m.assetRef!.operationId, m.assetRef!.fileName))
    );

    const items: TimelineItem[] = [
      ...messages.map((msg) => ({
        type: 'message' as const,
        data: msg,
        timestamp: msg.timestamp instanceof Date ? msg.timestamp.getTime() : new Date(msg.timestamp).getTime(),
      })),
      ...Object.entries(assetPreviews)
        .filter(([key]) => {
          const preview = assetPreviews[key];
          // Regenerated assets should always be shown (bypass marker check)
          if (preview.isRegeneration) return true;

          const baseKey = key.replace(/-\d{13,}$/, '');
          return !assetsWithMarkers.has(baseKey);
        })
        .map(([key, preview]) => ({
          type: 'asset' as const,
          data: preview,
          key,
          timestamp: preview.createdAt || Date.now(),
        })),
    ];

    return items.sort((a, b) => a.timestamp - b.timestamp);
  }, [messages, assetPreviews]);

  // Auto-scroll: only when user is near the bottom
  const isNearBottomRef = useRef(true);

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const threshold = 150;
    isNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
  }, []);

  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    el.addEventListener('scroll', handleScroll, { passive: true });
    return () => el.removeEventListener('scroll', handleScroll);
  }, [handleScroll]);

  useEffect(() => {
    if (isNearBottomRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, isTyping, assetPreviews]);

  // Connect on mount
  useEffect(() => {
    connect();
  }, [connect]);

  // Reset title update flag when session changes
  useEffect(() => {
    setHasUpdatedTitle(false);
  }, [currentSessionId]);

  // Handle file selection for attachments
  const handleFilesSelected = useCallback((files: File[]) => {
    setAttachmentError(null);
    const newAttachments: AttachedFile[] = files.map((file) => {
      const validation = validateFile(file);
      return {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`,
        file,
        type: validation.type || 'document',
        status: validation.valid ? 'ready' : 'error',
        error: validation.error,
        preview: undefined,
      };
    });

    newAttachments.forEach((att) => {
      if (att.type === 'image' && att.status === 'ready') {
        const reader = new FileReader();
        reader.onload = (e) => {
          setAttachments((prev) =>
            prev.map((a) =>
              a.id === att.id ? { ...a, preview: e.target?.result as string } : a
            )
          );
        };
        reader.readAsDataURL(att.file);
      }
    });

    setAttachments((prev) => [...prev, ...newAttachments]);
  }, []);

  const handleRemoveAttachment = useCallback((fileId: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== fileId));
  }, []);

  const handleAttachmentError = useCallback((error: string) => {
    setAttachmentError(error);
    if (attachmentErrorTimerRef.current) clearTimeout(attachmentErrorTimerRef.current);
    attachmentErrorTimerRef.current = setTimeout(() => setAttachmentError(null), 5000);
  }, []);

  // Cleanup attachment error timer on unmount
  useEffect(() => {
    return () => {
      if (attachmentErrorTimerRef.current) clearTimeout(attachmentErrorTimerRef.current);
    };
  }, []);

  // Handle form submission
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const message = inputValue.trim();
    const hasAttachments = attachments.filter(a => a.status === 'ready').length > 0;

    if ((!message && !hasAttachments) || !isConnected) return;

    const userMessageCount = messages.filter(m => m.role === 'user').length;

    if (hasAttachments) {
      const validAttachments = attachments.filter(a => a.status === 'ready');
      const attachmentsMeta: MessageAttachment[] = validAttachments.map(a => ({
        name: a.file.name,
        type: a.type,
        mimeType: a.file.type,
        size: a.file.size,
        preview: a.preview,
      }));

      setIsUploadingAttachments(true);
      try {
        await sendMessageWithAttachments(message, validAttachments, attachmentsMeta);
      } finally {
        setIsUploadingAttachments(false);
      }
      setAttachments([]);
    } else {
      const sent = sendMessage(message);
      if (!sent) return;
    }

    setInputValue('');
    inputRef.current?.focus();

    if (currentSessionId && userMessageCount === 0) {
      const sessionExists = sessions.some(s => s.sessionId === currentSessionId);
      if (!sessionExists) {
        await createNewSession(currentSessionId, message);
        setHasUpdatedTitle(true);
      } else if (!hasUpdatedTitle) {
        setHasUpdatedTitle(true);
        updateSessionTitle(currentSessionId, message);
      }
    } else if (currentSessionId && !hasUpdatedTitle && userMessageCount === 0) {
      setHasUpdatedTitle(true);
      updateSessionTitle(currentSessionId, message);
    }

    if (currentSessionId) {
      updateSessionActivity(currentSessionId, userMessageCount + 1);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    if (value.length <= MAX_INPUT_LENGTH) {
      setInputValue(value);
    } else {
      setInputValue(value.slice(0, MAX_INPUT_LENGTH));
    }
    e.target.style.height = 'auto';
    e.target.style.height = `${Math.min(e.target.scrollHeight, 150)}px`;
  };

  const isAtCharLimit = inputValue.length >= MAX_INPUT_LENGTH;
  const showCharCounter = inputValue.length > CHAR_COUNT_THRESHOLD;

  const defaultPlaceholderText = {
    'en-US': 'Type your message...',
    'ko-KR': '메시지를 입력하세요...',
    'ja-JP': 'メッセージを入力...',
  }[language] || 'Type your message...';

  // Backend pushes a phase-aware hint via the `input_hint` WS event; fall back
  // to the static per-language default if no hint has arrived yet.
  const placeholderText = inputHint?.placeholder?.trim() || defaultPlaceholderText;

  const handleStarterPromptClick = useCallback((message: string) => {
    setInputValue(message);
    inputRef.current?.focus();
  }, []);

  const handleFileUploadClick = useCallback(() => {
    attachmentButtonRef.current?.click();
  }, []);

  return (
    <div className="flex flex-col h-full bg-white dark:bg-surface-850 rounded-xl shadow-sm dark:shadow-none border border-surface-200 dark:border-surface-700 overflow-hidden transition-colors">
      {/* Chat Header */}
      <div className="flex items-center gap-3 px-4 lg:px-6 py-3 lg:py-4 border-b border-surface-200 dark:border-surface-700 bg-surface-50/50 dark:bg-surface-900/50 flex-shrink-0">
        <div className="w-9 h-9 lg:w-10 lg:h-10 rounded-full bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center shadow-sm dark:shadow-glow">
          <span className="text-white font-semibold text-base lg:text-lg">🤖</span>
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="font-semibold text-surface-900 dark:text-surface-100 text-sm lg:text-base">AICC Builder Agent</h2>
          <p className="text-xs lg:text-sm text-surface-500 dark:text-surface-400">
            {isConnected ? (
              <span className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span>
                {language === 'ko-KR' ? '연결됨' : 'Connected'}
              </span>
            ) : (
              <span className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse"></span>
                {language === 'ko-KR' ? '연결 중...' : 'Connecting...'}
              </span>
            )}
          </p>
        </div>
      </div>

      {/* Reconnection Banner */}
      {reconnectStatus && (
        <div className={cn(
          'px-4 py-2 text-sm flex items-center justify-center gap-2 transition-all',
          reconnectStatus === 'reconnecting'
            ? 'bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-300'
            : 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300'
        )}>
          {reconnectStatus === 'reconnecting' ? (
            <><WifiOff className="w-4 h-4" />{language === 'ko-KR' ? '재연결 중...' : 'Reconnecting...'}</>
          ) : (
            <><Wifi className="w-4 h-4" />{language === 'ko-KR' ? '재연결 완료' : 'Reconnected'}</>
          )}
        </div>
      )}

      {/* E1: Mobile Progress Bar (shown when generation is active) */}
      {activeProgress.hasAnyActivity && (
        <div className="lg:hidden flex-shrink-0 border-b border-surface-200 dark:border-surface-700">
          <button
            onClick={() => setShowProgressDetail(prev => !prev)}
            className="w-full px-4 py-2 flex items-center gap-2 text-xs"
          >
            <div className="flex-1">
              <div className="flex items-center justify-between mb-1">
                <span className="text-surface-600 dark:text-surface-300 font-medium">
                  {activeProgress.currentItem
                    ? (language === 'ko-KR' ? activeProgress.currentItem.labelKo : activeProgress.currentItem.label)
                    : (language === 'ko-KR' ? '생성 완료' : 'Generation Complete')}
                </span>
                <span className="text-surface-400 dark:text-surface-500">
                  {activeProgress.completedCount}/{activeProgress.total}
                </span>
              </div>
              <div className="w-full h-1.5 bg-surface-200 dark:bg-surface-700 rounded-full overflow-hidden">
                <div
                  className="h-full bg-primary-500 dark:bg-primary-400 rounded-full transition-all duration-500"
                  style={{ width: `${activeProgress.overallPercent}%` }}
                />
              </div>
            </div>
            {showProgressDetail ? <ChevronUp className="w-3 h-3 text-surface-400" /> : <ChevronDown className="w-3 h-3 text-surface-400" />}
          </button>
          {showProgressDetail && (
            <div className="px-4 pb-2 space-y-1">
              {progress.map(item => (
                <div key={item.id} className="flex items-center gap-2 text-xs">
                  {item.status === 'completed' ? (
                    <CheckCircle2 className="w-3 h-3 text-green-500" />
                  ) : item.status === 'in_progress' ? (
                    <Loader2 className="w-3 h-3 text-primary-500 animate-spin" />
                  ) : (
                    <Circle className="w-3 h-3 text-surface-300 dark:text-surface-600" />
                  )}
                  <span className={cn(
                    item.status === 'completed' ? 'text-surface-400 dark:text-surface-500 line-through' :
                    item.status === 'in_progress' ? 'text-primary-600 dark:text-primary-400 font-medium' :
                    'text-surface-400 dark:text-surface-500'
                  )}>
                    {language === 'ko-KR' ? item.labelKo : item.label}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* E2: Requirements Checklist (shown during interview phase, before generation starts) */}
      {!activeProgress.hasAnyActivity && requirementsChecklist.doneCount > 0 && messages.length > 0 && (
        <div className="flex-shrink-0 px-4 py-2 border-b border-surface-200 dark:border-surface-700 bg-surface-50/80 dark:bg-surface-900/50">
          <div className="flex items-center gap-3 flex-wrap">
            {requirementsChecklist.items.map(item => (
              <span key={item.id} className="inline-flex items-center gap-1 text-xs">
                {item.done ? (
                  <CheckCircle2 className="w-3 h-3 text-green-500" />
                ) : (
                  <Circle className="w-3 h-3 text-surface-300 dark:text-surface-600" />
                )}
                <span className={item.done ? 'text-surface-600 dark:text-surface-300' : 'text-surface-400 dark:text-surface-500'}>
                  {item.label}
                </span>
              </span>
            ))}
            {requirementsChecklist.allRequired && (
              <span className="ml-auto text-xs font-medium text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-900/20 px-2 py-0.5 rounded-full">
                {language === 'ko-KR' ? '필수 항목 완료' : 'Ready'}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Messages Area */}
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto px-4 lg:px-6 py-4 space-y-4 bg-surface-50/30 dark:bg-surface-900/30">
        {isLoadingSession && timeline.length === 0 && !isTyping ? (
          <div className="space-y-4 animate-pulse pt-4">
            <div className="flex justify-start"><div className="h-10 w-3/5 bg-surface-200 dark:bg-surface-700 rounded-xl" /></div>
            <div className="flex justify-end"><div className="h-8 w-2/5 bg-primary-100 dark:bg-primary-900/30 rounded-xl" /></div>
            <div className="flex justify-start"><div className="h-16 w-4/5 bg-surface-200 dark:bg-surface-700 rounded-xl" /></div>
            <div className="flex justify-end"><div className="h-8 w-1/3 bg-primary-100 dark:bg-primary-900/30 rounded-xl" /></div>
          </div>
        ) : timeline.length === 0 && !isTyping ? (
          <ChatEmptyState
            language={language}
            onStarterPromptClick={handleStarterPromptClick}
            onFileUploadClick={handleFileUploadClick}
          />
        ) : (
          <>
            {timeline.map((item, index) => {
              // Always render last 10 items without virtualization
              const isRecent = index >= timeline.length - 10;

              if (item.type === 'message') {
                if (isRecent) {
                  return <MessageBubble key={item.data.id} message={item.data} />;
                }
                return (
                  <VirtualizedItem key={item.data.id} rootRef={scrollContainerRef}>
                    <MessageBubble message={item.data} />
                  </VirtualizedItem>
                );
              } else {
                if (isRecent) {
                  return <AssetPreviewBubble key={item.key} preview={item.data} language={language} />;
                }
                return (
                  <VirtualizedItem key={item.key} rootRef={scrollContainerRef}>
                    <AssetPreviewBubble preview={item.data} language={language} />
                  </VirtualizedItem>
                );
              }
            })}

            {isTyping && <TypingIndicator />}

            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      {/* Input Area */}
      <form onSubmit={handleSubmit} className="px-4 lg:px-6 py-3 lg:py-4 border-t border-surface-200 dark:border-surface-700 bg-white dark:bg-surface-850 flex-shrink-0">
        {attachmentError && (
          <div className="mb-3 flex items-start gap-2 p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
            <AlertCircle className="w-4 h-4 text-red-500 flex-shrink-0 mt-0.5" />
            <p className="text-sm text-red-600 dark:text-red-400 whitespace-pre-line">{attachmentError}</p>
          </div>
        )}

        {attachments.length > 0 && (
          <AttachmentPreview
            files={attachments}
            onRemove={handleRemoveAttachment}
            isUploading={isUploadingAttachments}
            className="mb-3"
          />
        )}

        <div className="flex items-end gap-2 lg:gap-3">
          <ChatAttachmentButton
            buttonRef={attachmentButtonRef}
            onFilesSelected={handleFilesSelected}
            onError={handleAttachmentError}
            disabled={!isConnected || isUploadingAttachments}
            currentFileCount={attachments.length}
          />

          <div className="flex-1 relative">
            <textarea
              ref={inputRef}
              value={inputValue}
              onChange={handleInputChange}
              onKeyDown={handleKeyPress}
              placeholder={placeholderText}
              disabled={!isConnected || isUploadingAttachments || (isLoadingSession && !isSessionReady)}
              rows={1}
              className={cn(
                'w-full px-3 lg:px-4 py-2.5 lg:py-3 rounded-xl border',
                'border-surface-300 dark:border-surface-600',
                'bg-white dark:bg-surface-800',
                'focus:outline-none focus:ring-2 focus:ring-primary-500 focus:border-transparent',
                'disabled:bg-surface-100 dark:disabled:bg-surface-900 disabled:cursor-not-allowed',
                'resize-none transition-all text-sm lg:text-base',
                'text-surface-900 dark:text-surface-100 placeholder-surface-400 dark:placeholder-surface-500'
              )}
            />
            {isLoadingSession && !isSessionReady && (
              <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-surface-100/80 dark:bg-surface-800/80 backdrop-blur-sm">
                <div className="flex items-center gap-2 text-sm text-surface-500 dark:text-surface-400">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>{language === 'ko-KR' ? '세션 로딩 중...' : 'Loading session...'}</span>
                </div>
              </div>
            )}
          </div>
          <button
            type="submit"
            disabled={!isConnected || isUploadingAttachments || isAtCharLimit || (isLoadingSession && !isSessionReady) || (!inputValue.trim() && attachments.filter(a => a.status === 'ready').length === 0)}
            className={cn(
              'p-2.5 lg:p-3 rounded-xl transition-all flex-shrink-0',
              'bg-primary-600 dark:bg-primary-500 text-white shadow-sm dark:shadow-glow',
              'hover:bg-primary-700 dark:hover:bg-primary-600 hover:shadow-md',
              'disabled:bg-surface-300 dark:disabled:bg-surface-700 disabled:shadow-none disabled:cursor-not-allowed',
              'focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 dark:focus:ring-offset-surface-850'
            )}
          >
            <Send className="w-4 h-4 lg:w-5 lg:h-5" />
          </button>
        </div>
        <div className="mt-2 flex items-center justify-between">
          <p className="text-xs text-surface-400 dark:text-surface-500">
            {language === 'ko-KR'
              ? '📎 파일 첨부 가능 • Shift + Enter로 줄바꿈, Enter로 전송'
              : '📎 Attach files • Shift + Enter for new line, Enter to send'}
          </p>
          {showCharCounter && (
            <p className={cn(
              'text-xs font-mono',
              isAtCharLimit
                ? 'text-red-500 dark:text-red-400 font-semibold'
                : 'text-amber-500 dark:text-amber-400'
            )}>
              {inputValue.length.toLocaleString()} / {MAX_INPUT_LENGTH.toLocaleString()}
            </p>
          )}
        </div>
      </form>
    </div>
  );
}
