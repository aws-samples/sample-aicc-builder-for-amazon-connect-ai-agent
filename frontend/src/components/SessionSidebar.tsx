/**
 * Session Sidebar Component
 *
 * Displays a list of chat sessions for the current user.
 * Allows creating new sessions, switching between sessions, and deleting sessions.
 */

import { useEffect, useState, useRef } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useBuilderStore } from "../stores/builderStore";
import { ChatSession } from "../services/sessions";
import { Pencil, GripHorizontal } from "lucide-react";
import { FileExplorer } from "./FileExplorer";

interface SessionSidebarProps {
  onSessionSelect: (sessionId: string, isNew: boolean) => void;
  currentSessionId: string | null;
}

export function SessionSidebar({
  onSessionSelect,
  currentSessionId,
}: SessionSidebarProps) {
  const {
    sessions,
    isLoading,
    loadSessions,
    deleteSessionById,
    deleteAllSessions,
    setCurrentSession,
    updateSessionTitle,
  } = useSessionStore();

  const { language } = useBuilderStore();
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [showDeleteAllConfirm, setShowDeleteAllConfirm] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const editInputRef = useRef<HTMLInputElement>(null);
  const deleteConfirmTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load sessions on mount
  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (deleteConfirmTimerRef.current) clearTimeout(deleteConfirmTimerRef.current);
    };
  }, []);

  const handleNewChat = () => {
    // Generate a new session ID using UUID v4 for guaranteed uniqueness
    const newSessionId = `session-${crypto.randomUUID()}`;
    setCurrentSession(newSessionId);
    onSessionSelect(newSessionId, true);
  };

  const handleSelectSession = (session: ChatSession) => {
    setCurrentSession(session.sessionId);
    onSessionSelect(session.sessionId, false);
  };

  const handleDeleteSession = async (
    e: React.MouseEvent,
    sessionId: string
  ) => {
    e.stopPropagation();

    if (deleteConfirm === sessionId) {
      await deleteSessionById(sessionId);
      setDeleteConfirm(null);

      // If deleted current session, start new chat
      if (currentSessionId === sessionId) {
        handleNewChat();
      }
    } else {
      setDeleteConfirm(sessionId);
      // Auto-clear confirm after 3 seconds (with cleanup)
      if (deleteConfirmTimerRef.current) clearTimeout(deleteConfirmTimerRef.current);
      deleteConfirmTimerRef.current = setTimeout(() => setDeleteConfirm(null), 3000);
    }
  };

  const formatDate = (timestamp: number) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / (1000 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    // Within the last hour - show minutes ago
    if (diffMins < 60) {
      if (diffMins < 1) {
        return language === "ko-KR" ? "방금" : "Just now";
      }
      return language === "ko-KR"
        ? `${diffMins}분 전`
        : `${diffMins}m ago`;
    }

    // Today - show time with hour and minute
    if (diffDays === 0) {
      return date.toLocaleTimeString(language, {
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    // Yesterday - show "Yesterday" with time
    if (diffDays === 1) {
      const time = date.toLocaleTimeString(language, {
        hour: "2-digit",
        minute: "2-digit",
      });
      const yesterdayText = language === "ko-KR" ? "어제" : "Yesterday";
      return `${yesterdayText} ${time}`;
    }

    // Within a week - show day name with time
    if (diffDays < 7) {
      const dayName = date.toLocaleDateString(language, { weekday: "short" });
      const time = date.toLocaleTimeString(language, {
        hour: "2-digit",
        minute: "2-digit",
      });
      return `${dayName} ${time}`;
    }

    // Older - show date with time
    return date.toLocaleDateString(language, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const texts = {
    "en-US": {
      newChat: "New Chat",
      noSessions: "No previous chats",
      loading: "Loading...",
      confirmDelete: "Click again to delete",
    },
    "ko-KR": {
      newChat: "새 대화",
      noSessions: "이전 대화 없음",
      loading: "로딩 중...",
      confirmDelete: "삭제하려면 다시 클릭",
    },
    "ja-JP": {
      newChat: "新規チャット",
      noSessions: "過去のチャットなし",
      loading: "読み込み中...",
      confirmDelete: "削除するには再度クリック",
    },
  };

  const t = texts[language as keyof typeof texts] || texts["en-US"];

  const [sidebarWidth, setSidebarWidth] = useState(256); // default ~w-64
  const isResizingRef = useRef(false);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    isResizingRef.current = true;
    const startX = e.clientX;
    const startWidth = sidebarWidth;
    const onMouseMove = (ev: MouseEvent) => {
      if (!isResizingRef.current) return;
      const newWidth = Math.min(480, Math.max(180, startWidth + ev.clientX - startX));
      setSidebarWidth(newWidth);
    };
    const onMouseUp = () => {
      isResizingRef.current = false;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  };

  // Vertical divider: drag to resize top (FileExplorer) vs bottom (Sessions)
  const [topRatio, setTopRatio] = useState(0.45); // 45% FileExplorer, 55% Sessions
  const isVResizingRef = useRef(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleVMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    isVResizingRef.current = true;
    const startY = e.clientY;
    const startRatio = topRatio;
    const containerHeight = containerRef.current?.clientHeight || 600;

    const onMouseMove = (ev: MouseEvent) => {
      if (!isVResizingRef.current) return;
      const deltaY = ev.clientY - startY;
      const newRatio = Math.min(0.75, Math.max(0.2, startRatio + deltaY / containerHeight));
      setTopRatio(newRatio);
    };
    const onMouseUp = () => {
      isVResizingRef.current = false;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  };

  return (
    <div
      ref={containerRef}
      className="bg-surface-900 dark:bg-surface-950 text-white flex flex-col h-full flex-shrink-0 border-r border-surface-800 dark:border-surface-800 transition-colors relative"
      style={{ width: sidebarWidth }}
    >
      {/* Top: File Explorer */}
      <div className="flex flex-col overflow-hidden" style={{ height: `${topRatio * 100}%` }}>
        <FileExplorer language={language} variant="dark" />
      </div>

      {/* Horizontal Resize Divider */}
      <div
        onMouseDown={handleVMouseDown}
        className="flex-shrink-0 h-1.5 cursor-row-resize hover:bg-primary-500/50 active:bg-primary-500/70 transition-colors flex items-center justify-center border-y border-surface-700"
      >
        <GripHorizontal className="w-4 h-3 text-surface-600" />
      </div>

      {/* Bottom: Sessions */}
      <div className="flex-1 flex flex-col overflow-hidden min-h-0">
        {/* New Chat Button */}
        <div className="p-3 border-b border-surface-700 flex-shrink-0">
          <button
            onClick={handleNewChat}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-primary-600 hover:bg-primary-700 rounded-lg transition-colors font-medium shadow-sm hover:shadow-glow"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 4v16m8-8H4"
              />
            </svg>
            {t.newChat}
          </button>
        </div>

        {/* Sessions List */}
        <div className="flex-1 overflow-y-auto">
          {isLoading ? (
            <div className="p-4 text-center text-surface-400">{t.loading}</div>
          ) : sessions.length === 0 ? (
            <div className="p-4 text-center text-surface-500">{t.noSessions}</div>
          ) : (
            <div className="py-2">
              {sessions.map((session) => (
                <div
                  key={session.sessionId}
                  onClick={() => handleSelectSession(session)}
                  className={`group flex items-center justify-between px-3 py-2.5 mx-2 my-0.5 rounded-lg cursor-pointer transition-all ${
                    currentSessionId === session.sessionId
                      ? "bg-surface-700 dark:bg-surface-800 shadow-sm"
                      : "hover:bg-surface-800 dark:hover:bg-surface-800/50"
                  }`}
                >
                  <div className="flex-1 min-w-0 mr-2">
                    {editingSessionId === session.sessionId ? (
                      <input
                        ref={editInputRef}
                        value={editTitle}
                        onChange={(e) => setEditTitle(e.target.value)}
                        onBlur={() => {
                          if (editTitle.trim()) updateSessionTitle(session.sessionId, editTitle.trim());
                          setEditingSessionId(null);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') { e.currentTarget.blur(); }
                          if (e.key === 'Escape') { setEditingSessionId(null); }
                        }}
                        autoFocus
                        className="w-full text-sm font-medium bg-surface-600 dark:bg-surface-700 text-surface-100 rounded px-1.5 py-0.5 outline-none ring-1 ring-primary-500"
                        onClick={(e) => e.stopPropagation()}
                      />
                    ) : (
                      <div
                        className="text-sm font-medium truncate text-surface-100 flex items-center gap-1"
                        onDoubleClick={(e) => {
                          e.stopPropagation();
                          setEditingSessionId(session.sessionId);
                          setEditTitle(session.title || '');
                        }}
                      >
                        <span className="truncate">{session.title || "Untitled"}</span>
                        <Pencil className="w-3 h-3 flex-shrink-0 opacity-0 group-hover:opacity-50 hover:!opacity-100 transition-opacity cursor-pointer"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingSessionId(session.sessionId);
                            setEditTitle(session.title || '');
                          }}
                        />
                      </div>
                    )}
                    <div className="text-xs text-surface-400 flex items-center gap-2">
                      <span>{formatDate(session.lastMessageAt)}</span>
                      {session.messageCount > 0 && (
                        <span className="text-surface-500">
                          ({session.messageCount})
                        </span>
                      )}
                    </div>
                    <div className="text-[10px] text-surface-500 dark:text-surface-500 mt-0.5 font-mono break-all">
                      {session.sessionId}
                    </div>
                  </div>

                  {/* Delete Button */}
                  <button
                    onClick={(e) => handleDeleteSession(e, session.sessionId)}
                    className={`p-1.5 rounded transition-all ${
                      deleteConfirm === session.sessionId
                        ? "bg-red-600 text-white"
                        : "opacity-0 group-hover:opacity-100 hover:bg-surface-600 text-surface-400"
                    }`}
                    title={
                      deleteConfirm === session.sessionId
                        ? t.confirmDelete
                        : "Delete"
                    }
                  >
                    <svg
                      className="w-4 h-4"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                      />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Session Count + Delete All */}
        {sessions.length > 0 && (
          <div className="p-3 border-t border-surface-700 text-xs text-surface-500 flex-shrink-0">
            {showDeleteAllConfirm ? (
              <div className="flex flex-col gap-2">
                <p className="text-center text-red-400">
                  {language === 'ko-KR'
                    ? `${sessions.length}개 대화를 모두 삭제할까요?`
                    : `Delete all ${sessions.length} chats?`}
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={() => setShowDeleteAllConfirm(false)}
                    className="flex-1 px-2 py-1.5 rounded bg-surface-700 hover:bg-surface-600 text-surface-300 transition-colors"
                  >
                    {language === 'ko-KR' ? '취소' : 'Cancel'}
                  </button>
                  <button
                    onClick={async () => {
                      await deleteAllSessions();
                      setShowDeleteAllConfirm(false);
                      handleNewChat();
                    }}
                    className="flex-1 px-2 py-1.5 rounded bg-red-600 hover:bg-red-700 text-white transition-colors"
                  >
                    {language === 'ko-KR' ? '전체 삭제' : 'Delete All'}
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-between">
                <span>{sessions.length} {sessions.length === 1 ? "chat" : "chats"}</span>
                <button
                  onClick={() => setShowDeleteAllConfirm(true)}
                  className="text-surface-500 hover:text-red-400 transition-colors"
                >
                  {language === 'ko-KR' ? '전체 삭제' : 'Delete All'}
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Width Resize handle */}
      <div
        onMouseDown={handleMouseDown}
        className="absolute top-0 right-0 w-1 h-full cursor-col-resize hover:bg-primary-500/50 active:bg-primary-500/70 transition-colors"
      />
    </div>
  );
}
