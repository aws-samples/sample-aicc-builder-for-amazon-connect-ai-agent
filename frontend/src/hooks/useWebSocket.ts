/**
 * WebSocket hook for real-time communication with the ECS backend
 *
 * Connects via wss://{cloudfront-domain}/ws with a Cognito JWT id_token.
 * CloudFront proxies /ws to the ALB, solving Mixed Content for HTTPS origins.
 */

import { useCallback, useEffect, useRef } from "react";
import { useBuilderStore } from "../stores/builderStore";
import { useAuthStore } from "../stores/authStore";
import type { WebSocketMessage, SubagentActivity, SubagentToolCall, AttachedFile, MessageAttachment, AttachmentData, AssetPreview, BuilderPhase } from "../types";
import { PHASE_LABELS } from "../types";
import { getSessionHistory, getSessionAssets, getSessionData, getMessageLog, generatePresignedUrl, generateUploadPresignedUrl, uploadFileToS3, fetchAssetContent, type StoredAsset, type ConversationMessage } from "../services/sessions";
import { fetchNfsDiagnostics } from "../services/workspaceApi";

// Streaming timeout configuration
// Increased to 15 minutes to handle very long-running sub-agent operations (e.g., infrastructure_generator)
// Some complex operations can take 100+ seconds, and heartbeats may not always arrive reliably
const STREAM_IDLE_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes

// Performance optimization: Throttling configuration
const TOOL_INPUT_THROTTLE_MS = 100; // Throttle tool_input_update events (batching uses requestAnimationFrame)

// Global WebSocket state to persist across component remounts
let globalWs: WebSocket | null = null;
let globalReconnectTimeout: ReturnType<typeof setTimeout> | null = null;
let globalReconnectAttempts = 0;
let globalCurrentSessionId: string | null = null;

// Reconnection tracking state - used to detect when WebSocket reconnects to same session
let globalPreviouslyConnected = false; // Track if we've ever connected in this page session
let globalLastConnectedSessionId: string | null = null; // Last successfully connected session ID
let globalIntentionalClose = false; // Prevents auto-reconnect when intentionally closing (e.g., session switch)

// Keepalive ping interval to prevent WebSocket idle timeout
// Send ping every 15 seconds to keep the ALB/ECS WebSocket connection alive.
let globalPingInterval: ReturnType<typeof setInterval> | null = null;
const PING_INTERVAL_MS = 15000; // 15 seconds

// Proactive reconnect before 60-minute WebSocket hard limit
let globalProactiveReconnectTimer: ReturnType<typeof setTimeout> | null = null;
const PROACTIVE_RECONNECT_MS = 55 * 60 * 1000; // 55 minutes
let globalIsProactiveReconnect = false; // Silent reconnect flag (no UI banner)

// Session-ready watchdog: guarantees the chat input un-freezes even when the
// backend never replies with history_injected / session_created / connected.
// Any code path that flips isLoadingSession=true + isSessionReady=false MUST
// call armSessionReadyWatchdog() so we auto-recover after SESSION_READY_WATCHDOG_MS.
let globalSessionReadyWatchdog: ReturnType<typeof setTimeout> | null = null;
const SESSION_READY_WATCHDOG_MS = 15000;

// Liveness probe: tracks which session IDs we've already checked against
// `/api/debug/nfs` so we only pay the cost once per session load.
// `dead` means NFS dir missing AND DynamoDB history empty → safe to auto-rotate.
const globalLivenessChecked = new Set<string>();
function armSessionReadyWatchdog(reason: string) {
  if (globalSessionReadyWatchdog) clearTimeout(globalSessionReadyWatchdog);
  globalSessionReadyWatchdog = setTimeout(() => {
    const state = useBuilderStore.getState();
    if (!state.isSessionReady) {
      console.warn(`[useWebSocket] session-ready watchdog fired (${reason}) — forcing ready`);
      state.setSessionReady(true);
      state.setLoadingSession(false);
      state.setConnectionError(
        "Session is taking longer than expected — you can keep typing."
      );
    }
    globalSessionReadyWatchdog = null;
  }, SESSION_READY_WATCHDOG_MS);
}
function disarmSessionReadyWatchdog() {
  if (globalSessionReadyWatchdog) {
    clearTimeout(globalSessionReadyWatchdog);
    globalSessionReadyWatchdog = null;
  }
}

// Session ID storage key prefix for localStorage
const SESSION_ID_STORAGE_KEY = "aicc-session-id";
const CURRENT_SESSION_STORAGE_KEY = "aicc-current-session";

/**
 * Get or create a stable session ID for the current user.
 * Uses Cognito user sub + random suffix to create a session that persists
 * across page refreshes but is unique per browser session.
 */
function getOrCreateSessionId(userSub: string | null): string {
  // If there's an override session ID, use it
  if (globalCurrentSessionId) {
    console.log("[useWebSocket] Using override session ID:", globalCurrentSessionId);
    return globalCurrentSessionId;
  }

  // Check for stored current session (for page refresh persistence)
  const currentSessionKey = userSub
    ? `${CURRENT_SESSION_STORAGE_KEY}-${userSub}`
    : CURRENT_SESSION_STORAGE_KEY;
  const storedCurrentSession = localStorage.getItem(currentSessionKey);

  if (storedCurrentSession) {
    console.log("[useWebSocket] Using stored current session:", storedCurrentSession);
    globalCurrentSessionId = storedCurrentSession;
    return storedCurrentSession;
  }

  // Fall back to default session ID storage
  const storageKey = userSub
    ? `${SESSION_ID_STORAGE_KEY}-${userSub}`
    : SESSION_ID_STORAGE_KEY;

  let sessionId = localStorage.getItem(storageKey);

  if (!sessionId) {
    // Generate new session ID using UUID v4 for guaranteed uniqueness
    // Format: session-xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx (44 chars total)
    sessionId = `session-${crypto.randomUUID()}`;
    localStorage.setItem(storageKey, sessionId);
    console.log("[useWebSocket] Created new session ID:", sessionId);
  } else {
    console.log("[useWebSocket] Using existing session ID:", sessionId);
  }

  globalCurrentSessionId = sessionId;
  return sessionId;
}

/**
 * Set the current session ID (for session switching)
 */
function setCurrentSessionId(sessionId: string, userSub: string | null): void {
  globalCurrentSessionId = sessionId;

  // Persist to localStorage for page refresh
  const currentSessionKey = userSub
    ? `${CURRENT_SESSION_STORAGE_KEY}-${userSub}`
    : CURRENT_SESSION_STORAGE_KEY;
  localStorage.setItem(currentSessionKey, sessionId);

  console.log("[useWebSocket] Set current session ID:", sessionId);
}

/**
 * Clear the session ID from localStorage (used on logout or explicit reset)
 */
function clearSessionId(userSub: string | null): void {
  const storageKey = userSub
    ? `${SESSION_ID_STORAGE_KEY}-${userSub}`
    : SESSION_ID_STORAGE_KEY;
  const currentSessionKey = userSub
    ? `${CURRENT_SESSION_STORAGE_KEY}-${userSub}`
    : CURRENT_SESSION_STORAGE_KEY;

  localStorage.removeItem(storageKey);
  localStorage.removeItem(currentSessionKey);
  globalCurrentSessionId = null;
  console.log("[useWebSocket] Cleared session ID");
}

/**
 * Get WebSocket URL — same-origin CloudFront proxy to ALB.
 * wss://{cloudfront-domain}/ws?token={idToken}&sessionId={id}
 * CloudFront proxies /ws to the ALB over HTTP, solving Mixed Content.
 */
async function getWebSocketUrl(
  idToken: string,
  sessionId: string
): Promise<string> {
  const baseUrl = `wss://${window.location.host}/ws`;
  const params = new URLSearchParams({
    token: idToken,
    sessionId: sessionId,
  });
  const url = `${baseUrl}?${params.toString()}`;
  console.log("[useWebSocket] WebSocket URL:", baseUrl);
  return url;
}

// localStorage key for tracking last received message log sequence
const MSG_LOG_SEQ_KEY_PREFIX = "aicc-msg-log-seq-";

/**
 * Deserialize history messages from DynamoDB ConversationMessage[] format
 * back into the UI Message format. Handles tool and subagent messages
 * with their associated data. Unknown/missing fields are safely defaulted.
 */
function deserializeHistoryMessages(
  history: ConversationMessage[]
): Array<{
  role: 'user' | 'assistant' | 'system' | 'tool' | 'subagent';
  content: string;
  timestamp?: Date;
  toolCall?: import("../types").ToolCall;
  subagentActivity?: import("../types").SubagentActivity;
}> {
  return history
    .filter(msg => {
      // user/assistant/system must have content
      if (msg.role === 'user' || msg.role === 'assistant' || msg.role === 'system') {
        return msg.content && msg.content.trim() !== '';
      }
      // tool messages must have toolCall data
      if (msg.role === 'tool') return !!msg.toolCall;
      // subagent messages must have subagentActivity data
      if (msg.role === 'subagent') return !!msg.subagentActivity;
      return false;
    })
    .map(msg => {
      const ts = msg.timestamp ? new Date(msg.timestamp) : undefined;

      // Deserialize tool messages
      if (msg.role === 'tool' && msg.toolCall) {
        const tc = msg.toolCall;
        return {
          role: 'tool' as const,
          content: msg.content || '',
          timestamp: ts,
          toolCall: {
            tool: tc.tool,
            toolUseId: tc.toolUseId,
            input: tc.input,
            result: tc.result,
            error: tc.error,
            status: tc.status,
          },
        };
      }

      // Deserialize subagent messages
      if (msg.role === 'subagent' && msg.subagentActivity) {
        const sa = msg.subagentActivity;
        return {
          role: 'subagent' as const,
          content: msg.content || '',
          timestamp: ts,
          subagentActivity: {
            subagent: sa.subagent,
            displayName: sa.displayName,
            status: sa.status,
            content: sa.content,
            thinking: '', // Don't restore thinking (too large, not useful on reload)
            toolCalls: (sa.toolCalls || []).map(tc => ({
              tool: tc.tool,
              displayName: tc.displayName,
              input: tc.input,
              result: tc.result,
              status: tc.status,
              timestamp: new Date(tc.timestamp),
            })),
            timestamp: new Date(sa.timestamp),
          },
        };
      }

      // Regular messages (user/assistant/system)
      return {
        role: msg.role as 'user' | 'assistant' | 'system',
        content: msg.content,
        timestamp: ts,
      };
    });
}

export function useWebSocket() {
  // NOTE: We deliberately do NOT keep a per-hook `wsRef`. The WebSocket is a
  // singleton tracked by the module-level `globalWs`. Multiple components can
  // call useWebSocket() (e.g. App and ChatWindow); per-hook refs would become
  // stale after switchSession()/connect() since only the calling hook's
  // wsRef would be updated, leaving other hook instances pointing at a
  // closed socket. Always read/write through `globalWs`.
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    globalReconnectTimeout
  );
  const reconnectAttemptsRef = useRef(globalReconnectAttempts);
  const maxReconnectAttempts = 5;
  // Forward-ref to switchSession so sendMessage (declared earlier) can rotate
  // the session if the pre-send liveness probe says it's dead.
  const switchSessionRef = useRef<
    ((newSessionId: string, isNewSession?: boolean) => Promise<void>) | null
  >(null);
  // Buffered outbound message awaiting a fresh session after liveness-triggered rotation
  const pendingOutboundRef = useRef<{ message: string } | null>(null);
  const streamingMessageIdRef = useRef<string | null>(null);
  const streamTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastStreamContentRef = useRef<string>(""); // Track last content to detect duplicates

  // Performance optimization refs
  // Map: toolUseId -> message index for O(1) lookup instead of O(n) array search
  const toolMessageIndexMapRef = useRef<Map<string, number>>(new Map());
  // Stream batching: accumulate content and flush on animation frame
  const pendingStreamContentRef = useRef<string>("");
  const streamBatchRafRef = useRef<number | null>(null);
  // Tool input throttling
  const lastToolInputUpdateRef = useRef<number>(0);
  const pendingToolInputRef = useRef<Map<string, any>>(new Map());

  const { isAuthenticated, getIdToken, getUserSub, userSub } = useAuthStore();

  const {
    setConnected,
    setConnecting,
    setConnectionError,
    addMessage,
    updateLastMessage,
    setTyping,
    updateSession,
    updateProgress,
    updateProgressPercent,
    completeSubStep,
    updateAssetPreview,
    completeAssetPreview,
    setDownloadUrl,
    setShowDownloadModal,
    setMessages,
    updateMessageAt,
    triggerWorkspaceRefresh,
    setInputHint,
  } = useBuilderStore();

  // Generate or get session ID (persisted in localStorage, keyed by user sub)
  const getSessionId = useCallback(async (): Promise<string> => {
    // Get user sub for stable session ID keying
    const sub = await getUserSub();
    return getOrCreateSessionId(sub);
  }, [getUserSub]);

  // Helper function to clear stream timeout
  const clearStreamTimeout = useCallback(() => {
    if (streamTimeoutRef.current) {
      clearTimeout(streamTimeoutRef.current);
      streamTimeoutRef.current = null;
    }
  }, []);

  // Helper function to set stream timeout with auto-complete fallback
  const setStreamTimeout = useCallback(() => {
    clearStreamTimeout();
    streamTimeoutRef.current = setTimeout(() => {
      console.log("[useWebSocket] Stream timeout - auto-completing");
      setTyping(false);
      streamingMessageIdRef.current = null;
      lastStreamContentRef.current = "";
      // Flush any pending stream content
      if (pendingStreamContentRef.current) {
        updateLastMessage(pendingStreamContentRef.current);
        pendingStreamContentRef.current = "";
      }
    }, STREAM_IDLE_TIMEOUT_MS);
  }, [clearStreamTimeout, setTyping, updateLastMessage]);

  // Performance: Batched stream update - accumulates content and flushes at 60fps
  const flushStreamBatch = useCallback(() => {
    if (pendingStreamContentRef.current) {
      updateLastMessage(pendingStreamContentRef.current);
      pendingStreamContentRef.current = "";
    }
    streamBatchRafRef.current = null;
  }, [updateLastMessage]);

  const queueStreamUpdate = useCallback((content: string) => {
    pendingStreamContentRef.current += content;
    if (!streamBatchRafRef.current) {
      streamBatchRafRef.current = requestAnimationFrame(flushStreamBatch);
    }
  }, [flushStreamBatch]);

  // Performance: Register tool message index for O(1) lookup
  const registerToolMessage = useCallback((toolUseId: string, messageIndex: number) => {
    toolMessageIndexMapRef.current.set(toolUseId, messageIndex);
  }, []);

  // Performance: Get tool message index with O(1) lookup, fallback to O(n) search
  const getToolMessageIndex = useCallback((toolUseId: string | undefined, toolName: string): number => {
    const messages = useBuilderStore.getState().messages;
    // Try O(1) Map lookup first, but validate the cached index is still valid
    if (toolUseId && toolMessageIndexMapRef.current.has(toolUseId)) {
      const cachedIdx = toolMessageIndexMapRef.current.get(toolUseId)!;
      // Validate: index in bounds, message has toolCall, and toolUseId matches
      if (cachedIdx < messages.length && messages[cachedIdx]?.toolCall?.toolUseId === toolUseId) {
        return cachedIdx;
      }
      // Cached index is stale, remove it
      toolMessageIndexMapRef.current.delete(toolUseId);
    }
    // Fallback to O(n) search
    // First pass: exact toolUseId match
    // Second pass: match by tool name (handles race where callback handler's
    // tool_start arrives before stream loop's tool_start, creating a message
    // without toolUseId that the stream loop's tool_end needs to find)
    let nameMatchIdx = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m.role === "tool" && m.toolCall?.status === "running") {
        if (toolUseId && m.toolCall?.toolUseId === toolUseId) {
          // Cache for future lookups
          toolMessageIndexMapRef.current.set(toolUseId, i);
          return i;
        }
        if (m.toolCall?.tool === toolName && nameMatchIdx === -1) {
          nameMatchIdx = i;
        }
      }
    }
    return nameMatchIdx;
  }, []);

  // Performance: Throttled tool input update
  const throttledToolInputUpdate = useCallback((toolUseId: string, toolName: string, input: any) => {
    const now = Date.now();
    pendingToolInputRef.current.set(toolUseId || toolName, { toolUseId, toolName, input });

    if (now - lastToolInputUpdateRef.current < TOOL_INPUT_THROTTLE_MS) {
      return;
    }

    lastToolInputUpdateRef.current = now;

    pendingToolInputRef.current.forEach((pending, _key) => {
      const idx = getToolMessageIndex(pending.toolUseId, pending.toolName);
      if (idx !== -1) {
        updateMessageAt(idx, (msg) => {
          if (!msg.toolCall) return msg;
          return { ...msg, toolCall: { ...msg.toolCall, input: pending.input } };
        });
      }
    });
    pendingToolInputRef.current.clear();
  }, [getToolMessageIndex, updateMessageAt]);

  // Clear tool message index map when messages are cleared
  const clearToolMessageIndexMap = useCallback(() => {
    toolMessageIndexMapRef.current.clear();
    pendingToolInputRef.current.clear();
  }, []);

  // =================================================================
  // Sub-Agent Activity Helpers
  // =================================================================

  // Sub-Agent display information
  const SUBAGENT_INFO: Record<string, { name: string; icon: string; color: string }> = {
    research_agent: { name: 'Research Agent', icon: '🔍', color: 'cyan' },
    faq_generator: { name: 'FAQ Generator', icon: '📚', color: 'emerald' },
    lambda_generator: { name: 'Lambda Generator', icon: '⚡', color: 'orange' },
    openapi_generator: { name: 'OpenAPI Generator', icon: '📄', color: 'blue' },
    prompt_generator: { name: 'Prompt Generator', icon: '💬', color: 'purple' },
    contact_flow_generator: { name: 'Contact Flow Generator', icon: '📞', color: 'green' },
    infrastructure_generator: { name: 'Infrastructure Generator', icon: '🏗️', color: 'slate' },
    interviewer: { name: 'Interviewer', icon: '🎤', color: 'pink' },
    reviewer_agent: { name: 'Reviewer Agent', icon: '✅', color: 'teal' },
  };

  // Sub-Agent tool display labels
  const SUBAGENT_TOOL_LABELS: Record<string, Record<string, { icon: string; label: string }>> = {
    research_agent: {
      brave_web_search: { icon: '🔎', label: '웹 검색' },
      brave_search_tracked: { icon: '🔎', label: '웹 검색' },
      fetch_webpage: { icon: '🌐', label: '페이지 분석' },
      fetch_page_tracked: { icon: '🌐', label: '페이지 분석' },
      save_research_result: { icon: '💾', label: '결과 저장' },
      save_result_tracked: { icon: '💾', label: '결과 저장' },
    },
    faq_generator: {
      save_faq_document: { icon: '📝', label: 'FAQ 저장' },
      save_faq_tracked: { icon: '📝', label: 'FAQ 저장' },
      list_generated_documents: { icon: '📋', label: '문서 목록' },
      list_docs_tracked: { icon: '📋', label: '문서 목록' },
      create_knowledge_base_package: { icon: '📦', label: '패키지 생성' },
      create_package_tracked: { icon: '📦', label: '패키지 생성' },
    },
    lambda_generator: {
      save_generated_code: { icon: '💾', label: '코드 저장' },
      save_generated_code_tracked: { icon: '💾', label: '코드 저장' },
    },
    openapi_generator: {
      save_openapi_spec: { icon: '💾', label: '스펙 저장' },
      generate_openapi_spec: { icon: '📄', label: '스펙 생성' },
    },
    prompt_generator: {
      save_ai_prompt: { icon: '💾', label: '프롬프트 저장' },
      generate_ai_prompt: { icon: '📄', label: '프롬프트 생성' },
    },
    contact_flow_generator: {
      save_contact_flow: { icon: '💾', label: '플로우 저장' },
      generate_contact_flow: { icon: '📄', label: '플로우 생성' },
      search_amazon_connect_docs: { icon: '🔎', label: 'AWS 문서 검색' },
      fetch_documentation_page: { icon: '🌐', label: '문서 페이지 로드' },
      retrieve_contact_flow_knowledge: { icon: '📚', label: 'Knowledge Base 검색' },
    },
    infrastructure_generator: {
      save_generated_code: { icon: '💾', label: '인프라 코드 저장' },
    },
    reviewer_agent: {
      lookup_assets: { icon: '📂', label: '에셋 목록 조회' },
      get_asset_content: { icon: '📄', label: '에셋 내용 로드' },
      validate_openapi_schema: { icon: '✅', label: 'OpenAPI 스키마 검증' },
      check_field_consistency: { icon: '🔗', label: '필드 일관성 검증' },
      list_operations: { icon: '📋', label: '작업 목록 조회' },
      get_operation_spec: { icon: '📋', label: '작업 사양 조회' },
      validate_parameter_consistency: { icon: '🔗', label: '파라미터 일관성 검증' },
    },
    interviewer: {
      save_interview_notes: { icon: '📝', label: '인터뷰 메모 저장' },
    },
  };

  // Track active sub-agent message index for efficient updates
  const activeSubagentIndexRef = useRef<Map<string, number>>(new Map());

  // Get sub-agent display name
  const getSubagentDisplayName = useCallback((subagent: string): string => {
    return SUBAGENT_INFO[subagent]?.name || subagent;
  }, []);

  // Get tool display info
  const getToolDisplayInfo = useCallback((subagent: string, tool: string): { icon: string; label: string } => {
    const subagentTools = SUBAGENT_TOOL_LABELS[subagent];
    if (subagentTools && subagentTools[tool]) {
      return subagentTools[tool];
    }
    // Fallback: format tool name
    return { icon: '🔧', label: tool.replace(/_/g, ' ').replace(/tracked$/, '').trim() };
  }, []);

  // Find or create a subagent message and return its index
  const findOrCreateSubagentMessage = useCallback((subagent: string, initialStatus: 'started' | 'running' | 'completed' | 'error', content?: string): number => {
    // Check if we already have an active subagent message
    const existingIdx = activeSubagentIndexRef.current.get(subagent);
    const messages = useBuilderStore.getState().messages;

    if (existingIdx !== undefined && existingIdx < messages.length) {
      const msg = messages[existingIdx];
      if (msg.role === 'subagent' && msg.subagentActivity?.subagent === subagent) {
        return existingIdx;
      }
    }

    // Create new subagent message
    const newActivity: SubagentActivity = {
      subagent,
      displayName: getSubagentDisplayName(subagent),
      status: initialStatus,
      content,
      thinking: '',
      toolCalls: [],
      timestamp: new Date(),
    };

    addMessage({
      role: 'subagent',
      content: content || `${getSubagentDisplayName(subagent)} ${initialStatus}`,
      subagentActivity: newActivity,
    });

    const newIdx = useBuilderStore.getState().messages.length - 1;
    activeSubagentIndexRef.current.set(subagent, newIdx);
    return newIdx;
  }, [addMessage, getSubagentDisplayName]);

  // Update existing subagent message
  const updateSubagentMessage = useCallback((subagent: string, update: Partial<SubagentActivity>) => {
    const idx = activeSubagentIndexRef.current.get(subagent);
    if (idx === undefined) return;

    updateMessageAt(idx, (msg) => {
      if (msg.role !== 'subagent' || !msg.subagentActivity) return msg;
      return {
        ...msg,
        subagentActivity: { ...msg.subagentActivity, ...update },
      };
    });

    // Clear from active map if completed or error
    if (update.status === 'completed' || update.status === 'error') {
      activeSubagentIndexRef.current.delete(subagent);
    }
  }, [updateMessageAt]);

  // Add tool call to subagent message
  const addSubagentToolCall = useCallback((subagent: string, tool: string, input?: Record<string, unknown>) => {
    const idx = activeSubagentIndexRef.current.get(subagent);
    if (idx === undefined) return;

    updateMessageAt(idx, (msg) => {
      if (msg.role !== 'subagent' || !msg.subagentActivity) return msg;

      const existing = msg.subagentActivity.toolCalls;
      // If the last tool call is the same tool and still running, update its input (streaming)
      const last = existing[existing.length - 1];
      if (last && last.tool === tool && last.status === 'running') {
        const updated = [...existing];
        updated[updated.length - 1] = { ...last, input };
        return {
          ...msg,
          subagentActivity: { ...msg.subagentActivity, toolCalls: updated },
        };
      }

      // Otherwise add new tool call
      const toolInfo = getToolDisplayInfo(subagent, tool);
      const newToolCall: SubagentToolCall = {
        tool,
        displayName: `${toolInfo.icon} ${toolInfo.label}`,
        input,
        status: 'running',
        timestamp: new Date(),
      };
      return {
        ...msg,
        subagentActivity: {
          ...msg.subagentActivity,
          toolCalls: [...existing, newToolCall],
        },
      };
    });
  }, [getToolDisplayInfo, updateMessageAt]);

  // Update tool call result in subagent message
  const updateSubagentToolResult = useCallback((subagent: string, tool: string, result: unknown, status: 'completed' | 'error' = 'completed') => {
    const idx = activeSubagentIndexRef.current.get(subagent);
    if (idx === undefined) return;

    updateMessageAt(idx, (msg) => {
      if (msg.role !== 'subagent' || !msg.subagentActivity) return msg;
      const toolCallIdx = msg.subagentActivity.toolCalls.findIndex(
        (tc) => tc.tool === tool && tc.status === 'running'
      );
      if (toolCallIdx === -1) return msg;

      const updatedToolCalls = [...msg.subagentActivity.toolCalls];
      updatedToolCalls[toolCallIdx] = { ...updatedToolCalls[toolCallIdx], result, status };

      return {
        ...msg,
        subagentActivity: { ...msg.subagentActivity, toolCalls: updatedToolCalls },
      };
    });
  }, [updateMessageAt]);

  // Append to subagent thinking content
  const appendSubagentThinking = useCallback((subagent: string, content: string) => {
    const idx = activeSubagentIndexRef.current.get(subagent);
    if (idx === undefined) return;

    updateMessageAt(idx, (msg) => {
      if (msg.role !== 'subagent' || !msg.subagentActivity) return msg;
      return {
        ...msg,
        subagentActivity: {
          ...msg.subagentActivity,
          thinking: (msg.subagentActivity.thinking || '') + content,
        },
      };
    });
  }, [updateMessageAt]);

  // =================================================================
  // End Sub-Agent Activity Helpers
  // =================================================================

  // Throttle buffer for subagent_stream events to prevent render thrashing
  const subagentStreamBufferRef = useRef<Record<string, string>>({});
  const subagentStreamRafRef = useRef<number | null>(null);

  // ── Message Log Catch-up (ref to break circular dep with handleMessage) ──
  const handleMessageRef = useRef<((data: WebSocketMessage) => void) | null>(null);

  const catchUpFromMessageLog = useCallback(async (sessionId: string) => {
    const seqKey = `${MSG_LOG_SEQ_KEY_PREFIX}${sessionId}`;
    const storedSeq = parseInt(localStorage.getItem(seqKey) || "0", 10);
    let afterSeq = storedSeq;

    console.log("[useWebSocket] Starting message log catch-up from seq:", afterSeq);

    try {
      const response = await getMessageLog(sessionId, afterSeq);
      const { entries, isAgentActive } = response;

      if (entries.length > 0) {
        console.log("[useWebSocket] Replaying", entries.length, "missed events from message log");
        for (const entry of entries) {
          const event = entry.event as unknown as WebSocketMessage;
          // Don't re-dispatch heartbeats or typing indicators during catch-up
          if (event.type !== 'heartbeat' && event.type !== 'pong' && event.type !== 'typing' && handleMessageRef.current) {
            handleMessageRef.current(event);
          }
          if (entry.seq > afterSeq) {
            afterSeq = entry.seq;
          }
        }
        // Persist the latest seq
        localStorage.setItem(seqKey, String(afterSeq));
      }

      // After catch-up, the live WebSocket is already reattached by the backend.
      // New events will arrive via the WebSocket in real-time, so no polling needed.
      // Just log whether agent is still active for diagnostic purposes.
      if (isAgentActive) {
        console.log("[useWebSocket] Agent still active after catch-up — live events via WebSocket");
        setTyping(true);
      } else {
        console.log("[useWebSocket] Agent finished, catch-up complete at seq:", afterSeq);
      }
    } catch (error) {
      console.error("[useWebSocket] Message log catch-up error:", error);
    }
  }, [setTyping]);

  // Define handleMessage first so connect can reference it
  const handleMessage = useCallback(
    (data: WebSocketMessage) => {
      switch (data.type) {
        case "typing":
          setTyping(true);
          clearStreamTimeout();
          lastStreamContentRef.current = "";
          streamingMessageIdRef.current = `msg-${Date.now()}-${Math.random()
            .toString(36)
            .slice(2, 9)}`;
          addMessage({
            role: "assistant",
            content: "",
          });
          // Set initial timeout - if no stream data arrives, auto-complete
          setStreamTimeout();
          break;

        case "stream":
          setTyping(true);
          // Any successful streaming means the previous "still processing" condition cleared.
          useBuilderStore.getState().resetStillProcessingCount();
          if (data.content) {
            // Duplicate detection: skip if this exact content was just received
            // This can happen with network retries
            if (data.content === lastStreamContentRef.current && data.content.length > 10) {
              console.log("[useWebSocket] Skipping duplicate stream content");
              break;
            }
            lastStreamContentRef.current = data.content;

            // Reset timeout on each stream chunk
            setStreamTimeout();

            // Use message_id from backend if available for precise message targeting
            const messageId = data.message_id as string | undefined;
            const messages = useBuilderStore.getState().messages;
            const lastMessage = messages[messages.length - 1];

            // Check if we need to create a new message
            if (!lastMessage || lastMessage.role !== "assistant") {
              // No assistant message exists, create one first
              streamingMessageIdRef.current = messageId || `msg-${Date.now()}-${Math.random()
                .toString(36)
                .slice(2, 9)}`;
              addMessage({
                role: "assistant",
                content: "",
              });
            } else if (messageId && streamingMessageIdRef.current !== messageId) {
              // Different message ID - this is a new stream, create new message
              streamingMessageIdRef.current = messageId;
              addMessage({
                role: "assistant",
                content: "",
              });
            }
            // Performance: Use batched update instead of immediate setState
            queueStreamUpdate(data.content);
          }
          break;

        case "stream_end":
          clearStreamTimeout();
          setTyping(false);
          lastStreamContentRef.current = "";
          // Performance: Flush any pending batched stream content
          if (pendingStreamContentRef.current) {
            updateLastMessage(pendingStreamContentRef.current);
            pendingStreamContentRef.current = "";
          }
          if (streamBatchRafRef.current) {
            cancelAnimationFrame(streamBatchRafRef.current);
            streamBatchRafRef.current = null;
          }
          if (data.content && !streamingMessageIdRef.current) {
            addMessage({
              role: "assistant",
              content: data.content,
            });
          }
          streamingMessageIdRef.current = null;
          break;

        case "message":
          setTyping(false);
          if (data.content) {
            addMessage({
              role: data.role || "assistant",
              content: data.content,
            });
          }
          break;

        case "error":
          clearStreamTimeout();
          setTyping(false);
          lastStreamContentRef.current = "";
          streamingMessageIdRef.current = null;
          // Detect backend "Agent is still processing" guard (app.py:1652).
          // Ensure the chat input is re-enabled immediately and bump a counter
          // so the UI can offer a Reset Session affordance after repeated hits.
          if (
            typeof data.content === "string" &&
            data.content.toLowerCase().includes("still processing")
          ) {
            useBuilderStore.getState().setSessionReady(true);
            useBuilderStore.getState().setLoadingSession(false);
            useBuilderStore.getState().bumpStillProcessingCount();
          }
          // Log detailed debug info to console for development
          if (data.debug) {
            console.group("🚨 Backend Error Details");
            console.error("Error Type:", data.debug.error_type);
            console.error("Error Message:", data.debug.error_message);
            console.error("Session ID:", data.debug.session_id);
            console.error("Context:", data.debug.context);
            console.error("Last Tool:", data.debug.last_tool);
            console.error("Traceback:\n", data.debug.traceback);
            console.groupEnd();
          }
          addMessage({
            role: "system",
            content: data.debug
              ? `❌ Error: ${data.content}\n\n**Debug Info:**\n- Type: ${data.debug.error_type}\n- Context: ${data.debug.context || "unknown"}\n- Last Tool: ${data.debug.last_tool || "none"}\n\n<details>\n<summary>Traceback</summary>\n\n\`\`\`\n${data.debug.traceback}\n\`\`\`\n</details>`
              : data.content || "An error occurred",
          });
          break;

        case "session_update":
          if (data.session && typeof data.session === 'object') {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const session = data.session as any;
            updateSession({
              companyName: session.company_name ?? session.companyName ?? null,
              industry: session.industry ?? null,
              dbConnected: session.db_connected ?? session.dbConnected ?? false,
              operations: session.operations ?? [],
              assetsGenerated:
                session.assets_generated ?? session.assetsGenerated ?? [],
            });

            // Auto-update progress based on session data
            if (session.company_name || session.companyName) {
              completeSubStep("company", "company_name");
            }
            if (session.industry) {
              completeSubStep("company", "industry");
            }
            if (session.language) {
              completeSubStep("company", "language");
            }
            if (session.operations && session.operations.length > 0) {
              completeSubStep("operations", "ops_identified");
            }
            if (session.db_connected || session.dbConnected) {
              completeSubStep("database", "db_connected");
            }
          }
          if (data.progressUpdates) {
            for (const update of data.progressUpdates) {
              updateProgress(update.id, update.status, update.progress);
            }
          }
          break;

        case "progress_update":
          if (data.itemId && data.status) {
            updateProgress(
              data.itemId,
              data.status as "pending" | "in_progress" | "completed",
              data.progressPercent
            );
          }
          // Handle sub-step completion
          if (data.itemId && data.subStepId) {
            completeSubStep(data.itemId, data.subStepId);
          }
          break;

        case "input_hint":
          if (typeof data.placeholder === "string" && data.placeholder.length > 0) {
            setInputHint({
              placeholder: data.placeholder,
              phase: typeof data.phase === "string" ? data.phase : undefined,
            });
          }
          break;

        case "tool_status":
          console.log(`Tool ${data.tool}: ${data.status}`);
          // Map tool names to progress item IDs and their progress percentages
          const toolToProgressMap: Record<string, { id: string; runningProgress: number }> = {
            introspect_database: { id: "database", runningProgress: 50 },
            save_operation_spec: { id: "operations", runningProgress: 50 },
            generate_lambda_function: { id: "lambda", runningProgress: 50 },
            generate_ai_prompt: { id: "prompt", runningProgress: 50 },
            generate_openapi_spec: { id: "openapi", runningProgress: 50 },
            generate_contact_flow: { id: "contact_flow", runningProgress: 50 },
            generate_flow_mermaid_only: { id: "contact_flow", runningProgress: 30 },
            update_contact_flow_greeting: { id: "contact_flow", runningProgress: 70 },
            generate_cdk_infrastructure: { id: "cdk", runningProgress: 50 },
            merge_infrastructure_fragments: { id: "cdk", runningProgress: 90 },
            package_and_upload_assets: { id: "ready", runningProgress: 50 },
          };
          if (data.tool && data.status) {
            const mapping = toolToProgressMap[data.tool];
            if (mapping) {
              const status = data.status === "running" ? "in_progress" :
                            data.status === "completed" ? "completed" : "pending";
              const progress = data.status === "running" ? mapping.runningProgress :
                              data.status === "completed" ? 100 : 0;
              updateProgress(mapping.id, status as "pending" | "in_progress" | "completed", progress);

              // Also complete sub-steps for database introspection
              if (data.tool === "introspect_database" && data.status === "completed") {
                completeSubStep("database", "db_introspected");
              }
            }

            // Fallback: Create tool message if tool_start wasn't received
            // Check if there's already a running tool message for this tool
            if (data.status === "running") {
              const messages = useBuilderStore.getState().messages;
              const hasRunningToolMsg = messages.some(
                (m) => m.role === "tool" && m.toolCall?.tool === data.tool && m.toolCall?.status === "running"
              );
              if (!hasRunningToolMsg) {
                addMessage({
                  role: "tool",
                  content: "",
                  toolCall: {
                    tool: data.tool,
                    input: data.input,
                    status: "running",
                  },
                });
              }
            }
          }
          break;

        case "progress":
          if (data.progress?.session) {
            updateSession(data.progress.session);
          }
          break;

        case "template":
          if (data.content && data.fileName) {
            downloadFile(data.content, data.fileName);
          }
          break;

        case "tool_start":
          // Add tool call message to chat - use toolUseId for deduplication (unique per invocation)
          // This allows same tool (e.g., save_operation_spec) to be called multiple times
          if (data.tool) {
            const messages = useBuilderStore.getState().messages;
            // Performance: Check map first for O(1), fallback to array check
            const toolUseIdKey = data.toolUseId as string;
            const hasInMap = toolUseIdKey && toolMessageIndexMapRef.current.has(toolUseIdKey);
            const hasRunningToolMsg = hasInMap || messages.some(
              (m) => m.role === "tool" &&
                     (m.toolCall?.toolUseId === data.toolUseId || (!data.toolUseId && m.toolCall?.tool === data.tool)) &&
                     m.toolCall?.status === "running"
            );
            if (!hasRunningToolMsg) {
              addMessage({
                role: "tool",
                content: "",
                toolCall: {
                  tool: data.tool,
                  toolUseId: data.toolUseId,
                  input: data.input,
                  status: "running",
                },
              });
              // Performance: Register in map for O(1) lookup in subsequent events
              if (toolUseIdKey) {
                const newMessages = useBuilderStore.getState().messages;
                registerToolMessage(toolUseIdKey, newMessages.length - 1);
              }
            }

            // Also update progress sidebar
            const toolToProgressMapStart: Record<string, { id: string; runningProgress: number }> = {
              // Phase A: Interview & Requirements
              introspect_database: { id: "database", runningProgress: 50 },
              save_operation_spec: { id: "operations", runningProgress: 50 },
              format_operation_summary: { id: "operations", runningProgress: 80 },
              infer_missing_tools: { id: "operations", runningProgress: 90 },
              save_session_flow_config: { id: "operations", runningProgress: 70 },
              save_requirement_document: { id: "requirements", runningProgress: 50 },
              // Phase B: Generation
              generate_lambda_function: { id: "lambda", runningProgress: 50 },
              lambda_generator_agent: { id: "lambda", runningProgress: 50 },
              generate_ai_prompt: { id: "prompt", runningProgress: 50 },
              prompt_generator_agent: { id: "prompt", runningProgress: 50 },
              generate_openapi_spec: { id: "openapi", runningProgress: 50 },
              openapi_generator_agent: { id: "openapi", runningProgress: 50 },
              generate_contact_flow: { id: "contact_flow", runningProgress: 50 },
              contact_flow_generator_agent: { id: "contact_flow", runningProgress: 50 },
              generate_cdk_infrastructure: { id: "cdk", runningProgress: 50 },
              infrastructure_generator_agent: { id: "cdk", runningProgress: 50 },
              merge_infrastructure_fragments: { id: "cdk", runningProgress: 90 },
              merge_openapi_fragments: { id: "openapi", runningProgress: 90 },
              faq_generator_agent: { id: "knowledge_base", runningProgress: 50 },
              research_agent: { id: "research", runningProgress: 50 },
              // Phase C: Review & Package
              reviewer_agent: { id: "review", runningProgress: 50 },
              validate_parameter_consistency: { id: "review", runningProgress: 30 },
              package_and_upload_assets: { id: "ready", runningProgress: 50 },
            };
            const mapping = toolToProgressMapStart[data.tool];
            if (mapping) {
              updateProgress(mapping.id, "in_progress", mapping.runningProgress);
            }
          }
          // Reset stream timeout on tool activity to prevent auto-complete during long-running tools
          setStreamTimeout();
          break;

        case "tool_input_update":
          // Performance: Use throttled update instead of immediate setState on every event
          if (data.tool && data.input) {
            throttledToolInputUpdate(data.toolUseId as string, data.tool, data.input);
          }
          break;

        case "tool_end":
          // Update the tool message with result and input
          // Performance: Use updateMessageAt instead of full array copy
          if (data.tool) {
            // Flush any pending throttled tool input updates first
            if (pendingToolInputRef.current.size > 0) {
              pendingToolInputRef.current.forEach((pending, _key) => {
                const idx = getToolMessageIndex(pending.toolUseId, pending.toolName);
                if (idx !== -1) {
                  updateMessageAt(idx, (msg) => {
                    if (!msg.toolCall) return msg;
                    return { ...msg, toolCall: { ...msg.toolCall, input: pending.input } };
                  });
                }
              });
              pendingToolInputRef.current.clear();
            }

            let toolEndIndex = getToolMessageIndex(data.toolUseId as string, data.tool);
            // Fallback: also search for completed tool messages without result
            if (toolEndIndex === -1) {
              const msgs = useBuilderStore.getState().messages;
              for (let i = msgs.length - 1; i >= 0; i--) {
                if (msgs[i].role === "tool" && msgs[i].toolCall?.tool === data.tool &&
                    !msgs[i].toolCall?.result) {
                  toolEndIndex = i;
                  break;
                }
              }
            }
            if (toolEndIndex !== -1) {
              updateMessageAt(toolEndIndex, (msg) => {
                if (!msg.toolCall) return msg;
                return {
                  ...msg,
                  toolCall: {
                    ...msg.toolCall,
                    input: data.input || msg.toolCall.input,
                    result: data.result != null
                      ? (typeof data.result === 'string' ? data.result : JSON.stringify(data.result))
                      : msg.toolCall.result,
                    error: data.error,
                    status: data.status === "error" ? "error" : "completed",
                  },
                };
              });

              // Performance: Clean up map entry for this tool
              if (data.toolUseId) {
                toolMessageIndexMapRef.current.delete(data.toolUseId as string);
              }
            } else {
              // Fallback: if tool message not found, add a completed tool message directly
              addMessage({
                role: "tool",
                content: "",
                toolCall: {
                  tool: data.tool,
                  toolUseId: data.toolUseId,
                  input: data.input,
                  result: data.result != null
                    ? (typeof data.result === 'string' ? data.result : JSON.stringify(data.result))
                    : undefined,
                  error: data.error,
                  status: data.status === "error" ? "error" : "completed",
                },
              });
            }

            // Update progress sidebar
            const toolToProgressMapEnd: Record<string, string> = {
              // Phase A: Interview & Requirements
              introspect_database: "database",
              format_operation_summary: "operations",
              infer_missing_tools: "operations",
              save_requirement_document: "requirements",
              // Phase B: Generation
              generate_lambda_function: "lambda",
              lambda_generator_agent: "lambda",
              generate_ai_prompt: "prompt",
              prompt_generator_agent: "prompt",
              generate_openapi_spec: "openapi",
              openapi_generator_agent: "openapi",
              generate_contact_flow: "contact_flow",
              contact_flow_generator_agent: "contact_flow",
              generate_cdk_infrastructure: "cdk",
              infrastructure_generator_agent: "cdk",
              merge_infrastructure_fragments: "cdk",
              merge_openapi_fragments: "openapi",
              faq_generator_agent: "knowledge_base",
              research_agent: "research",
              // Phase C: Review & Package
              reviewer_agent: "review",
              validate_parameter_consistency: "review",
              package_and_upload_assets: "ready",
            };
            const progressId = toolToProgressMapEnd[data.tool];
            if (progressId) {
              const status = data.status === "error" ? "pending" : "completed";
              updateProgress(progressId, status as "pending" | "in_progress" | "completed", data.status === "error" ? 0 : 100);

              // Complete sub-steps
              if (data.tool === "introspect_database" && data.status !== "error") {
                completeSubStep("database", "db_introspected");
              }
            }
          }
          // Reset stream timeout on tool completion
          setStreamTimeout();
          break;

        case "thinking":
          // Merge thinking chunks into a single message
          if (data.content) {
            const currentMsgs = useBuilderStore.getState().messages;
            const lastMsg = currentMsgs[currentMsgs.length - 1];
            if (lastMsg && lastMsg.role === "thinking") {
              // Append to existing thinking message
              updateLastMessage(data.content);
            } else {
              // Create new thinking message
              addMessage({
                role: "thinking",
                content: data.content,
              });
            }
          }
          break;

        case "asset_generating":
          // Handle "generating" indicator (no content yet)
          if (data.assetType) {
            updateAssetPreview({
              assetType: data.assetType as 'lambda' | 'openapi' | 'prompt' | 'contact_flow' | 'cdk' | 'cloudformation' | 'company' | 'operations' | 'validation',
              operationId: data.operationId,
              fileName: data.fileName,
              content: "",  // No content yet
              isComplete: false,
              language: data.language || "text",
            });
          }
          // Reset stream timeout on asset generation activity
          setStreamTimeout();
          break;

        case "asset_preview":
          // Handle complete asset delivery (single event with full content)
          if (data.assetPreview) {
            // workspace_update: trigger file tree refresh without adding to asset previews
            if (data.assetPreview.assetType === "workspace_update") {
              triggerWorkspaceRefresh();
              setStreamTimeout();
              break;
            }

            // workspace_file: inline preview AND file tree refresh
            if (data.assetPreview.assetType === "workspace_file") {
              triggerWorkspaceRefresh();
              // fall through to updateAssetPreview
            }

            // Set messageIndex to current message count for proper placement during session restore
            // This ensures assets appear AFTER the message that triggered their generation
            const currentMessages = useBuilderStore.getState().messages;
            const messageIndex = data.assetPreview.messageIndex ?? currentMessages.length;
            updateAssetPreview({
              ...data.assetPreview,
              messageIndex,
            });

            // When asset is complete, also update progress sidebar
            if (data.assetPreview.isComplete) {
              const assetTypeToProgressId: Record<string, string> = {
                lambda: "lambda",
                openapi: "openapi",
                prompt: "prompt",
                contact_flow: "contact_flow",
                cdk: "cdk",
                cloudformation: "cdk",  // infrastructure_generator uses cloudformation
                faq: "knowledge_base",
                package: "knowledge_base",
              };
              const progressIdFromAsset = assetTypeToProgressId[data.assetPreview.assetType as string];
              if (progressIdFromAsset) {
                updateProgress(progressIdFromAsset, "completed", 100);
              }
            }
          }
          // Reset stream timeout on asset preview delivery
          setStreamTimeout();
          break;

        case "subagent_progress":
          // Handle real-time Sub-Agent progress updates with new subagent message type
          console.log("[useWebSocket] Sub-Agent progress:", data.subagent, data.status, data.content);

          if (data.subagent && typeof data.subagent === 'string' && data.status) {
            const status = data.status as 'started' | 'running' | 'completed' | 'error';

            if (status === 'started') {
              // Create new subagent message
              findOrCreateSubagentMessage(data.subagent, status, data.content as string | undefined);
            } else {
              // Update existing subagent message
              updateSubagentMessage(data.subagent, {
                status,
                content: data.content as string | undefined,
              });
            }

            // Also update progress sidebar
            const subagentToProgressId: Record<string, string> = {
              lambda_generator: "lambda",
              openapi_generator: "openapi",
              prompt_generator: "prompt",
              contact_flow_generator: "contact_flow",
              infrastructure_generator: "cdk",  // CloudFormation/CDK infrastructure
              faq_generator: "knowledge_base",
              research_agent: "research",
              reviewer_agent: "review",
            };
            const progressId = subagentToProgressId[data.subagent];
            if (progressId) {
              const statusMap: Record<string, "pending" | "in_progress" | "completed"> = {
                started: "in_progress",
                running: "in_progress",
                completed: "completed",
                error: "pending",
              };
              const mappedStatus = statusMap[status] || "in_progress";
              const progress = status === "completed" ? 100 :
                              status === "started" ? 10 :
                              status === "running" ? 50 : 0;
              updateProgress(progressId, mappedStatus, progress);
            }
          }
          // Reset stream timeout on sub-agent activity to prevent auto-complete during long-running agents
          setStreamTimeout();
          break;

        case "subagent_tool_use":
          // Handle Sub-Agent's internal tool calls with rich display
          if (data.subagent && data.tool) {
            // Ensure subagent message exists
            findOrCreateSubagentMessage(data.subagent, 'running');

            // Throttle: buffer tool input updates, flush on rAF
            const toolKey = `${data.subagent}::${data.tool}`;
            subagentStreamBufferRef.current[`__tool__${toolKey}`] = JSON.stringify(data.input || {});

            if (subagentStreamRafRef.current === null) {
              subagentStreamRafRef.current = requestAnimationFrame(() => {
                const buf = subagentStreamBufferRef.current;
                subagentStreamBufferRef.current = {};
                subagentStreamRafRef.current = null;
                for (const [key, val] of Object.entries(buf)) {
                  if (key.startsWith('__tool__')) {
                    const tk = key.slice(8); // remove '__tool__'
                    const [sa, tl] = tk.split('::');
                    try { addSubagentToolCall(sa, tl, JSON.parse(val)); } catch { addSubagentToolCall(sa, tl); }
                  } else {
                    appendSubagentThinking(key, val);
                  }
                }
              });
            }
          }
          setStreamTimeout();
          break;

        case "subagent_tool_result":
          // Handle Sub-Agent's internal tool results
          if (data.subagent && data.tool) {
            // Update tool call in subagent message
            updateSubagentToolResult(
              data.subagent,
              data.tool as string,
              data.result
            );

            // FAQ progress counter: update content with doc count
            if (data.subagent === 'faq_generator' && data.tool === 'save_faq_tracked') {
              const res = data.result as Record<string, unknown> | undefined;
              if (res?.success) {
                const docName = (res.filename || res.title || '') as string;
                updateSubagentMessage('faq_generator', {
                  content: `FAQ 문서 저장: ${docName}`,
                });
              }
            }
          }
          setStreamTimeout();
          break;

        case "subagent_stream":
          // Handle Sub-Agent's thinking/reasoning stream — buffered to prevent render thrashing
          if (data.subagent && data.content) {
            // Ensure subagent message exists (cheap — only creates on first call)
            findOrCreateSubagentMessage(data.subagent, 'running');

            // Buffer chunks per subagent and flush via rAF (with size limit to prevent memory issues)
            const agent = data.subagent as string;
            const existing = subagentStreamBufferRef.current[agent] || '';
            const MAX_BUFFER_SIZE = 512 * 1024; // 512KB per subagent
            if (existing.length < MAX_BUFFER_SIZE) {
              subagentStreamBufferRef.current[agent] = existing + (data.content as string);
            }

            if (subagentStreamRafRef.current === null) {
              subagentStreamRafRef.current = requestAnimationFrame(() => {
                const buf = subagentStreamBufferRef.current;
                subagentStreamBufferRef.current = {};
                subagentStreamRafRef.current = null;
                for (const [sa, text] of Object.entries(buf)) {
                  appendSubagentThinking(sa, text);
                }
              });
            }
          }
          setStreamTimeout();
          break;

        case "subagent_error":
          // Handle Sub-Agent errors
          if (data.subagent) {
            console.log("[useWebSocket] Sub-Agent error:", data.subagent, data.error);

            // Update subagent message with error
            updateSubagentMessage(data.subagent, {
              status: 'error',
              content: data.error as string || 'An error occurred',
            });
          }
          break;

        case "asset_complete":
          // Mark asset preview as complete
          if (data.assetPreview) {
            // Build unique key including fileName for multiple files of same type
            // Key format: assetType-operationId-fileName or assetType-fileName or assetType
            let key: string;
            if (data.assetPreview.fileName && data.assetPreview.operationId) {
              key = `${data.assetPreview.assetType}-${data.assetPreview.operationId}-${data.assetPreview.fileName}`;
            } else if (data.assetPreview.fileName) {
              key = `${data.assetPreview.assetType}-${data.assetPreview.fileName}`;
            } else if (data.assetPreview.operationId) {
              key = `${data.assetPreview.assetType}-${data.assetPreview.operationId}`;
            } else {
              key = data.assetPreview.assetType;
            }
            completeAssetPreview(key);
          }
          break;

        case "download_ready":
          // Set download URL for packaged assets (handle both snake_case and camelCase)
          const downloadUrl = data.downloadUrl || (data as any).download_url;
          const expiresAt = data.expiresAt || (data as any).expires_at || null;
          const s3Key = data.s3Key || (data as any).s3_key || null;
          if (downloadUrl) {
            setDownloadUrl(downloadUrl, expiresAt, s3Key);
            // Auto-open download in new window
            window.open(downloadUrl, '_blank');
            // Show deployment guide modal
            setShowDownloadModal(true);
          }
          break;

        case "history":
          // Restore conversation history from server
          if (data.history && Array.isArray(data.history)) {
            console.log("[useWebSocket] Restoring history:", data.history.length, "messages");

            // Use setMessages for efficient bulk restore (clears existing and sets new)
            const restoredMessages = data.history.map((msg: { role: string; content: string; timestamp?: number }) => ({
              role: msg.role as 'user' | 'assistant' | 'system',
              content: msg.content,
            }));
            setMessages(restoredMessages);

            // Restore session data if available (backend sends snake_case)
            if (data.session) {
              const session = data.session as unknown as Record<string, unknown>;
              updateSession({
                companyName: (session.company_name ?? session.companyName ?? null) as string | null,
                industry: (session.industry ?? null) as string | null,
                dbConnected: (session.db_connected ?? session.dbConnected ?? false) as boolean,
                operations: (session.operations ?? []) as [],
                assetsGenerated: (session.assets_generated ?? session.assetsGenerated ?? []) as [],
              });
            }
          }
          break;

        case "history_injected":
          // Acknowledgment from backend that history was injected
          // CRITICAL: Only NOW mark session as ready to accept messages
          // This ensures history is fully loaded before user can send messages
          disarmSessionReadyWatchdog();
          useBuilderStore.getState().setSessionReady(true);
          useBuilderStore.getState().setLoadingSession(false);
          // Restore phase from the injected session's NFS state
          if (data.phase) {
            useBuilderStore.getState().setCurrentPhase(data.phase as BuilderPhase);
          }
          // Restore progress from NFS-backed progressState
          if (data.progressState) {
            for (const [progressId, state] of Object.entries(data.progressState as Record<string, { status: string; progress: number }>)) {
              updateProgress(progressId, state.status as "pending" | "in_progress" | "completed", state.progress);
            }
            console.log("[useWebSocket] Restored NFS progress on history_injected:", Object.keys(data.progressState));
          }
          if (data.success) {
            console.log("[useWebSocket] History injected successfully, count:", data.injectedCount, "phase:", data.phase || "n/a", "- session NOW ready");
          } else {
            console.warn("[useWebSocket] History injection failed:", data.error, "- session ready anyway");
          }
          break;

        case "session_created":
          // Acknowledgment from backend that a fresh session was created
          // CRITICAL: Only NOW mark session as ready to accept messages
          disarmSessionReadyWatchdog();
          useBuilderStore.getState().setSessionReady(true);
          useBuilderStore.getState().setLoadingSession(false);
          // Restore phase from backend
          if (data.phase) {
            useBuilderStore.getState().setCurrentPhase(data.phase as BuilderPhase);
          }
          console.log("[useWebSocket] New session created:", data.sessionId, "phase:", data.phase || "interview", "- session NOW ready");
          // Flush any outbound message the liveness probe had buffered while rotating
          if (pendingOutboundRef.current && globalWs?.readyState === WebSocket.OPEN) {
            const buffered = pendingOutboundRef.current;
            pendingOutboundRef.current = null;
            addMessage({ role: "user", content: buffered.message });
            globalWs.send(JSON.stringify({
              action: "sendMessage",
              message: buffered.message,
              language: useBuilderStore.getState().language,
            }));
            console.log("[useWebSocket] Flushed buffered message after session rotation");
          }
          break;

        case "context_injected":
          // Acknowledgment from backend that context was injected
          if (data.success) {
            console.log("[useWebSocket] Context injected successfully:", data.message);
          } else {
            console.warn("[useWebSocket] Context injection failed");
          }
          break;

        case "connected":
          // Backend sends sessionId on WebSocket connect — detect mismatch
          console.log("[useWebSocket] Backend connected event, backend sessionId:", data.sessionId, "phase:", data.phase);
          // Restore phase from backend
          if (data.phase) {
            useBuilderStore.getState().setCurrentPhase(data.phase as BuilderPhase);
          }
          // Restore progress from NFS-backed progressState
          if (data.progressState) {
            for (const [progressId, state] of Object.entries(data.progressState as Record<string, { status: string; progress: number }>)) {
              updateProgress(progressId, state.status as "pending" | "in_progress" | "completed", state.progress);
            }
            console.log("[useWebSocket] Restored NFS progress on connected:", Object.keys(data.progressState));
          }
          if (data.sessionId && globalCurrentSessionId && data.sessionId !== globalCurrentSessionId) {
            console.warn("[useWebSocket] Session ID MISMATCH! frontend:", globalCurrentSessionId, "backend:", data.sessionId);
            // Force history injection — backend has a different session, needs context restoration
            if (globalWs?.readyState === WebSocket.OPEN) {
              console.log("[useWebSocket] Forcing injectHistory due to session mismatch");
              // Load history from DynamoDB and send to backend
              getSessionHistory(globalCurrentSessionId).then(history => {
                if (!history || history.length === 0) {
                  console.log("[useWebSocket] No history to inject for mismatch recovery");
                  disarmSessionReadyWatchdog();
                  useBuilderStore.getState().setSessionReady(true);
                  useBuilderStore.getState().setLoadingSession(false);
                  return;
                }
                getSessionData(globalCurrentSessionId!).then(sessionData => {
                  if (globalWs?.readyState !== WebSocket.OPEN) return;
                  globalWs.send(JSON.stringify({
                    action: "injectHistory",
                    history: history.map(msg => ({
                      role: msg.role,
                      content: msg.content,
                      timestamp: msg.timestamp,
                    })),
                    sessionContext: {
                      ...(sessionData ? {
                        companyName: sessionData.companyName,
                        industry: sessionData.industry,
                        operations: sessionData.operations,
                        dbConnected: sessionData.dbConnected,
                      } : {}),
                      originalSessionId: globalCurrentSessionId,
                    },
                  }));
                  console.log("[useWebSocket] Sent injectHistory for mismatch recovery");
                });
              });
            }
          } else {
            // No mismatch: backend accepted our session ID. Ensure the chat input un-freezes
            // even if no separate history_injected/session_created event is coming
            // (e.g. a plain reconnect where backend already had our history cached in memory).
            disarmSessionReadyWatchdog();
            useBuilderStore.getState().setSessionReady(true);
            useBuilderStore.getState().setLoadingSession(false);
          }
          break;

        case "phase_changed": {
          const { setCurrentPhase, addMessage } = useBuilderStore.getState();
          if (data.phase) {
            setCurrentPhase(data.phase as BuilderPhase);
            // Insert a phase divider into the chat
            const lang = useBuilderStore.getState().language;
            const label = PHASE_LABELS[data.phase as BuilderPhase]?.[lang] || data.phase;
            addMessage({
              role: 'system',
              content: `phase_divider:${data.phase}`,
            });
            console.log("[useWebSocket] Phase changed:", data.previousPhase, "->", data.phase, `(${label})`);
          }
          break;
        }

        case "pong":
          // Keepalive pong response - connection is alive
          // No action needed, just confirms connection is active
          break;

        case "background_task_active":
          // Backend has an agent task still running for this session.
          // Start polling the message log to catch up on missed events.
          console.log("[useWebSocket] Background task active for session:", data.sessionId);
          setTyping(true);
          if (data.sessionId) {
            // Use the stable ref to avoid stale closure
            catchUpFromMessageLog(data.sessionId as string);
          }
          break;

        case "heartbeat":
          // Keep-alive from backend during long thinking/tool generation — ignore silently
          break;

        default:
          console.log("Unknown message type:", data.type);
      }
    },
    [
      setTyping,
      addMessage,
      updateLastMessage,
      updateSession,
      updateProgress,
      updateProgressPercent,
      completeSubStep,
      updateAssetPreview,
      completeAssetPreview,
      setDownloadUrl,
      setShowDownloadModal,
      clearStreamTimeout,
      setStreamTimeout,
      // Performance optimization functions
      queueStreamUpdate,
      registerToolMessage,
      getToolMessageIndex,
      throttledToolInputUpdate,
      updateMessageAt,
      catchUpFromMessageLog,
    ]
  );

  // Keep handleMessageRef updated so catchUpFromMessageLog can use it
  handleMessageRef.current = handleMessage;

  /**
   * Inject history on reconnect to restore conversation context.
   * This is called when WebSocket reconnects to the same session after a disconnect.
   *
   * When WebSocket disconnects and reconnects, the ECS task may have
   * been replaced (deploy, scale-in), losing the in-memory session state.
   * This function restores the conversation context by loading history
   * from DynamoDB and sending it to the backend via the injectHistory
   * action.
   */
  const injectHistoryOnReconnect = useCallback(async (
    sessionId: string,
    ws: WebSocket
  ) => {
    console.log("[useWebSocket] Injecting history on reconnect for session:", sessionId);

    try {
      // Load history and session data from DynamoDB
      const [history, sessionData] = await Promise.all([
        getSessionHistory(sessionId),
        getSessionData(sessionId),
      ]);

      if (history && history.length > 0) {
        console.log("[useWebSocket] Found", history.length, "messages to inject on reconnect");

        // Restore UI messages including tool/subagent from DynamoDB
        const deserialized = deserializeHistoryMessages(history);
        if (deserialized.length > 0) {
          setMessages(deserialized);
          console.log("[useWebSocket] Restored", deserialized.length, "UI messages (incl tool/subagent) on reconnect");
        }

        // Restore session data + progress from DynamoDB (immediate visual update)
        // NFS-backed progress in history_injected response will overwrite with latest state
        if (sessionData) {
          const { updateSession: _us, updateProgress: _up } = useBuilderStore.getState();
          _us({
            companyName: sessionData.companyName,
            industry: sessionData.industry,
            language: sessionData.language || 'en-US',
            dbConnected: sessionData.dbConnected || false,
          });
          if (sessionData.progressState) {
            for (const [progressId, state] of Object.entries(sessionData.progressState)) {
              const ps = state as { status: string; progress: number };
              _up(progressId, ps.status as "pending" | "in_progress" | "completed", ps.progress);
            }
            console.log("[useWebSocket] Restored DynamoDB progress on reconnect:", Object.keys(sessionData.progressState));
          }
        }

        // Send injectHistory to backend — only user/assistant/system messages
        const backendHistory = history.filter(msg =>
          msg.role === 'user' || msg.role === 'assistant' || msg.role === 'system'
        );
        ws.send(JSON.stringify({
          action: "injectHistory",
          history: backendHistory.map(msg => ({
            role: msg.role,
            content: msg.content,
            timestamp: msg.timestamp,
          })),
          sessionContext: {
            ...(sessionData ? {
              companyName: sessionData.companyName,
              industry: sessionData.industry,
              operations: sessionData.operations,
              dbConnected: sessionData.dbConnected,
            } : {}),
            originalSessionId: sessionId,  // The session ID where assets are stored in S3
          },
        }));

        // DO NOT set isSessionReady here!
        // Wait for backend's "history_injected" response in handleMessage
        console.log("[useWebSocket] Waiting for history_injected response after reconnect...");

        // Check message log for events missed while disconnected
        try {
          const seqKey = `${MSG_LOG_SEQ_KEY_PREFIX}${sessionId}`;
          const lastSeq = parseInt(localStorage.getItem(seqKey) || "0", 10);
          const logResponse = await getMessageLog(sessionId, lastSeq);
          if (logResponse.entries.length > 0) {
            console.log("[useWebSocket] Replaying", logResponse.entries.length, "missed events from message log on reconnect");
            let maxSeq = lastSeq;
            for (const entry of logResponse.entries) {
              const event = entry.event as unknown as WebSocketMessage;
              if (event.type !== 'heartbeat' && event.type !== 'pong' && event.type !== 'typing' && handleMessageRef.current) {
                handleMessageRef.current(event);
              }
              if (entry.seq > maxSeq) maxSeq = entry.seq;
            }
            localStorage.setItem(seqKey, String(maxSeq));

            // If agent is still active, start polling
            if (logResponse.isAgentActive) {
              catchUpFromMessageLog(sessionId);
            }
          }
        } catch (logError) {
          console.warn("[useWebSocket] Message log catch-up failed on reconnect:", logError);
        }

        // Also reload assets from S3 in case they were generated while disconnected
        try {
          const assets = await getSessionAssets(sessionId);
          if (assets && assets.length > 0) {
            console.log("[useWebSocket] Reloading", assets.length, "assets after reconnect");
            // Step 1: Restore asset previews with metadata (content may be empty for S3-backed assets)
            for (const asset of assets) {
              updateAssetPreview({
                assetType: asset.assetType as AssetPreview['assetType'],
                operationId: asset.operationId,
                fileName: asset.fileName,
                content: asset.content || "",
                isComplete: true,
                language: asset.language || "text",
                createdAt: asset.createdAt,
                messageIndex: asset.messageIndex,
                s3Key: asset.s3Key,
              });
            }

            // Step 2: Lazy-load content from S3 for assets with s3Key but empty content
            const assetsNeedingContent = assets.filter(a => a.s3Key && !a.content);
            if (assetsNeedingContent.length > 0) {
              console.log("[useWebSocket] Lazy-loading content from S3 for", assetsNeedingContent.length, "assets (reconnect)");
              const BATCH_SIZE = 6;
              for (let i = 0; i < assetsNeedingContent.length; i += BATCH_SIZE) {
                const batch = assetsNeedingContent.slice(i, i + BATCH_SIZE);
                await Promise.all(batch.map(async (asset) => {
                  const content = await fetchAssetContent(sessionId, asset.s3Key!);
                  if (content) {
                    updateAssetPreview({
                      assetType: asset.assetType as AssetPreview['assetType'],
                      operationId: asset.operationId,
                      fileName: asset.fileName,
                      content,
                      isComplete: true,
                      language: asset.language || "text",
                      createdAt: asset.createdAt,
                      messageIndex: asset.messageIndex,
                      s3Key: asset.s3Key,
                    });
                  } else {
                    console.warn("[useWebSocket] Failed to load content from S3 (reconnect):", asset.assetType, asset.fileName);
                  }
                }));
              }
              console.log("[useWebSocket] S3 lazy-load complete (reconnect)");
            }
          }
        } catch (assetError) {
          console.warn("[useWebSocket] Failed to reload assets on reconnect:", assetError);
        }
      } else {
        console.log("[useWebSocket] No history found for reconnect, sending createNewSession");
        // No history to inject, send createNewSession to backend
        ws.send(JSON.stringify({ action: "createNewSession" }));
        // Wait for session_created response
        console.log("[useWebSocket] Waiting for session_created response after reconnect...");
      }
    } catch (error) {
      console.error("[useWebSocket] Failed to inject history on reconnect:", error);
      // On error, still try to create a new session so user can continue
      try {
        ws.send(JSON.stringify({ action: "createNewSession" }));
        console.log("[useWebSocket] Sent createNewSession after reconnect error");
      } catch (sendError) {
        console.error("[useWebSocket] Failed to send createNewSession:", sendError);
        // Last resort: allow messages anyway
        useBuilderStore.getState().setSessionReady(true);
        useBuilderStore.getState().setLoadingSession(false);
      }
    }
  }, [updateAssetPreview, setMessages, catchUpFromMessageLog]);

  const connect = useCallback(async () => {
    if (!isAuthenticated) {
      console.log("[useWebSocket] User not authenticated, skipping connect");
      setConnectionError("Please sign in to continue");
      return;
    }

    if (globalWs?.readyState === WebSocket.OPEN) {
      setConnected(true);
      return;
    }

    if (globalWs?.readyState === WebSocket.CONNECTING) {
      // A connection is already being established — don't open a duplicate
      return;
    }

    setConnecting(true);
    setConnectionError(null);

    try {
      const sessionId = await getSessionId();

      // Get ID token for Cognito JWT auth on WebSocket
      const idToken = await getIdToken();
      if (!idToken) {
        throw new Error("Failed to get ID token");
      }

      console.log("[useWebSocket] Session ID:", sessionId);
      const wsUrl = await getWebSocketUrl(idToken, sessionId);

      console.log("[useWebSocket] Connecting to WebSocket...");
      console.log("[useWebSocket] WebSocket URL:", wsUrl.substring(0, 100) + "...");
      const ws = new WebSocket(wsUrl);
      globalWs = ws;

      ws.onopen = async () => {
        console.log("[useWebSocket] WebSocket connected");

        // Guard against a stale onopen firing for a socket that's already been
        // superseded (e.g. switchSession opened a newer connection while this
        // one was still in CONNECTING). Close ourselves quietly and bail.
        if (globalWs !== ws) {
          console.log("[useWebSocket] Stale onopen — newer socket is active; closing this one");
          try { ws.close(1000, "superseded"); } catch { /* no-op */ }
          return;
        }

        setConnected(true);
        setConnecting(false);

        // Start keepalive ping interval to prevent idle timeout
        if (globalPingInterval) {
          clearInterval(globalPingInterval);
        }
        globalPingInterval = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            try {
              // Send a lightweight ping action to keep connection alive
              ws.send(JSON.stringify({ action: "ping" }));
              console.log("[useWebSocket] Sent keepalive ping");
            } catch (e) {
              console.warn("[useWebSocket] Failed to send ping:", e);
            }
          }
        }, PING_INTERVAL_MS);

        // Start proactive reconnect timer (55 min) to avoid 60-min hard limit
        if (globalProactiveReconnectTimer) {
          clearTimeout(globalProactiveReconnectTimer);
        }
        globalProactiveReconnectTimer = setTimeout(() => {
          console.log("[useWebSocket] Proactive reconnect at 55 minutes");
          globalIsProactiveReconnect = true;
          if (ws.readyState === WebSocket.OPEN) {
            ws.close(1000, "proactive-reconnect");
          }
        }, PROACTIVE_RECONNECT_MS);

        // Get current session ID for reconnection detection
        const currentSessionId = globalCurrentSessionId || sessionId;

        // Detect reconnection: previously connected AND same session ID AND reconnect attempts > 0
        // reconnectAttemptsRef.current > 0 means this connection came from the onclose reconnect logic
        // globalIsProactiveReconnect is excluded — proactive reconnects don't need history injection
        const isReconnecting = globalPreviouslyConnected &&
                               globalLastConnectedSessionId === currentSessionId &&
                               reconnectAttemptsRef.current > 0 &&
                               !globalIsProactiveReconnect;

        console.log("[useWebSocket] Connection state:", {
          isReconnecting,
          previouslyConnected: globalPreviouslyConnected,
          lastSessionId: globalLastConnectedSessionId,
          currentSessionId,
          reconnectAttempts: reconnectAttemptsRef.current,
        });

        // Reset reconnect counters and proactive flag AFTER detecting reconnection
        const wasProactiveReconnect = globalIsProactiveReconnect;
        reconnectAttemptsRef.current = 0;
        globalReconnectAttempts = 0;
        globalIsProactiveReconnect = false;

        if (isReconnecting) {
          // RECONNECTION: Inject history from DynamoDB to restore context
          console.log("[useWebSocket] Detected reconnection, injecting history from DynamoDB");
          await injectHistoryOnReconnect(currentSessionId, ws);

          // Show "reconnected" banner briefly
          useBuilderStore.getState().setReconnectStatus('reconnected');
          setTimeout(() => useBuilderStore.getState().setReconnectStatus(null), 3000);
        } else if (wasProactiveReconnect) {
          // PROACTIVE RECONNECT: Silently mark session ready without banner
          console.log("[useWebSocket] Proactive reconnect complete — no history injection needed");
          disarmSessionReadyWatchdog();
          useBuilderStore.getState().setSessionReady(true);
          useBuilderStore.getState().setLoadingSession(false);
        } else {
          // NEW CONNECTION: Mark session as ready (greeting is now handled by ChatEmptyState)
          disarmSessionReadyWatchdog();
          useBuilderStore.getState().setSessionReady(true);
          useBuilderStore.getState().setLoadingSession(false);
        }

        // Update tracking state for future reconnection detection
        globalPreviouslyConnected = true;
        globalLastConnectedSessionId = currentSessionId;
      };

      ws.onmessage = (event) => {
        try {
          const data: WebSocketMessage = JSON.parse(event.data);
          // Debug: Only log asset_preview related messages for debugging streaming
          if (data.type === "asset_preview" || data.type === "asset_complete") {
            console.log("[useWebSocket] Asset event:", data.type, data);
          }
          handleMessage(data);
        } catch (error) {
          console.error("Failed to parse WebSocket message:", error);
        }
      };

      ws.onerror = (error) => {
        console.error("[useWebSocket] WebSocket error:", error);
        setConnectionError("Connection error occurred");
      };

      ws.onclose = (event) => {
        console.log(
          "[useWebSocket] WebSocket closed:",
          event.code,
          event.reason || "(no reason provided)"
        );
        console.log("[useWebSocket] Close event wasClean:", event.wasClean);
        console.log("[useWebSocket] Intentional close:", globalIntentionalClose);
        console.log("[useWebSocket] Proactive reconnect:", globalIsProactiveReconnect);

        // If a newer WebSocket has already replaced this one (e.g. switchSession
        // closed us and immediately opened a fresh socket), this stale onclose
        // must NOT mutate any shared state — otherwise it would wipe out the
        // active connection's globalWs and trigger a spurious reconnect.
        const isStaleClose = globalWs !== null && globalWs !== ws;
        if (isStaleClose) {
          console.log("[useWebSocket] Ignoring stale onclose (a newer socket is already active)");
          return;
        }

        // Clear keepalive ping interval
        if (globalPingInterval) {
          clearInterval(globalPingInterval);
          globalPingInterval = null;
        }

        // Clear proactive reconnect timer
        if (globalProactiveReconnectTimer) {
          clearTimeout(globalProactiveReconnectTimer);
          globalProactiveReconnectTimer = null;
        }

        // Only clear globalWs if it still points at this (now-closed) socket
        if (globalWs === ws) {
          globalWs = null;
        }
        setConnected(false);
        setConnecting(false);

        // IMPORTANT: Reset session ready state on disconnect
        // This ensures reconnection properly waits for history injection before allowing messages
        useBuilderStore.getState().setSessionReady(false);
        armSessionReadyWatchdog("onclose-disconnect");

        // Proactive reconnect: treat as auto-reconnect, not intentional close
        if (globalIsProactiveReconnect) {
          console.log("[useWebSocket] Proactive reconnect — reconnecting immediately");
          globalIsProactiveReconnect = false;
          reconnectAttemptsRef.current = 1; // Mark as reconnect so injectHistory fires
          globalReconnectAttempts = 1;
          reconnectTimeoutRef.current = setTimeout(() => {
            connect();
          }, 500);
          globalReconnectTimeout = reconnectTimeoutRef.current;
          return;
        }

        // Skip auto-reconnect if this was an intentional close (e.g., session switch)
        if (globalIntentionalClose) {
          console.log("[useWebSocket] Skipping auto-reconnect (intentional close)");
          globalIntentionalClose = false; // Reset flag
          return;
        }

        if (
          isAuthenticated &&
          reconnectAttemptsRef.current < maxReconnectAttempts
        ) {
          // 1006 (abnormal closure) or 1009 (message too big / 60min WS limit) = reconnect immediately
          const isImmediateReconnect = (event.code === 1006 || event.code === 1009) && reconnectAttemptsRef.current === 0;
          const delay = isImmediateReconnect
            ? 500
            : Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current), 30000);
          console.log(`[useWebSocket] Reconnecting in ${delay}ms... (code: ${event.code})`);

          // Show reconnecting banner (not for proactive reconnect — already returned above)
          useBuilderStore.getState().setReconnectStatus('reconnecting');

          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectAttemptsRef.current++;
            globalReconnectAttempts++;
            connect();
          }, delay);
          globalReconnectTimeout = reconnectTimeoutRef.current;
        } else if (reconnectAttemptsRef.current >= maxReconnectAttempts) {
          setConnectionError("Failed to connect after multiple attempts");
        }
      };
    } catch (error) {
      console.error("[useWebSocket] Failed to create WebSocket:", error);
      setConnecting(false);
      setConnectionError(
        error instanceof Error ? error.message : "Failed to create connection"
      );
    }
  }, [
    isAuthenticated,
    getIdToken,
    getSessionId,
    setConnected,
    setConnecting,
    setConnectionError,
    addMessage,
    handleMessage,
  ]);

  const sendMessage = useCallback(
    (message: string) => {
      if (globalWs?.readyState !== WebSocket.OPEN) {
        console.error("[useWebSocket] WebSocket is not connected");
        return false;
      }

      // Check if session is ready (prevents context leakage between sessions)
      if (!useBuilderStore.getState().isSessionReady) {
        console.warn("[useWebSocket] Session not ready yet, please wait...");
        // Return false to indicate message was not sent
        // The UI should show a loading state or retry
        return false;
      }

      // Pre-send liveness probe (runs once per session). If the backend's NFS
      // session dir is missing AND DynamoDB has no history for this ID, the
      // session is effectively dead — rotate to a fresh one and re-send.
      const currentId = globalCurrentSessionId;
      if (currentId && !globalLivenessChecked.has(currentId)) {
        globalLivenessChecked.add(currentId);
        (async () => {
          try {
            const [diag, history] = await Promise.all([
              fetchNfsDiagnostics(currentId),
              getSessionHistory(currentId).catch(() => null),
            ]);
            const nfsMissing =
              diag &&
              "recent_sessions" in diag &&
              Array.isArray(diag.recent_sessions) &&
              !diag.recent_sessions.includes(currentId);
            const historyEmpty = !history || history.length === 0;
            if (nfsMissing && historyEmpty && switchSessionRef.current) {
              const lang = useBuilderStore.getState().language;
              useBuilderStore.getState().setConnectionError(
                lang === "ko-KR"
                  ? "이전 세션이 만료되었습니다 — 새 세션을 시작합니다."
                  : "Previous session expired — started a new one."
              );
              pendingOutboundRef.current = { message };
              const freshId = `session-${crypto.randomUUID()}`;
              await switchSessionRef.current(freshId, true);
            }
          } catch (e) {
            console.warn("[useWebSocket] liveness probe failed (non-fatal):", e);
          }
        })();
      }

      addMessage({
        role: "user",
        content: message,
      });

      globalWs.send(
        JSON.stringify({
          action: "sendMessage",
          message,
          language: useBuilderStore.getState().language,
        })
      );

      return true;
    },
    [addMessage]
  );

  /**
   * Convert a File to base64 string
   */
  const fileToBase64 = async (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result as string;
        // Remove data URL prefix if present (e.g., "data:image/png;base64,")
        const base64 = result.includes(',') ? result.split(',')[1] : result;
        resolve(base64);
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  };

  // Threshold for S3 upload vs WebSocket base64
  // ALB WebSocket frames are limited; keep base64 payloads small.
  // Base64 encoding adds ~33% overhead, so max safe file size is ~20KB
  // Using 15KB to be safe with JSON wrapper overhead
  const S3_UPLOAD_THRESHOLD = 15 * 1024; // 15KB

  /**
   * Send a message with file attachments (images, documents)
   *
   * - Small files (< 500KB): Uses WebSocket with base64 encoding
   * - Large files (>= 500KB): Uploads to S3 first, then sends S3 reference
   *
   * @param message - User's text message
   * @param attachments - Array of AttachedFile objects
   * @param attachmentsMeta - Attachment metadata for display (without base64 data)
   */
  const sendMessageWithAttachments = useCallback(
    async (
      message: string,
      attachments: AttachedFile[],
      attachmentsMeta: MessageAttachment[]
    ): Promise<boolean> => {
      if (globalWs?.readyState !== WebSocket.OPEN) {
        console.error("[useWebSocket] WebSocket is not connected");
        return false;
      }

      // Check if session is ready
      if (!useBuilderStore.getState().isSessionReady) {
        console.warn("[useWebSocket] Session not ready yet, please wait...");
        return false;
      }

      // Add user message to UI immediately (with attachment metadata)
      addMessage({
        role: "user",
        content: message,
        attachments: attachmentsMeta,
      });

      try {
        // Check if any file needs S3 upload (larger than threshold)
        const hasLargeFiles = attachments.some(att => att.file.size >= S3_UPLOAD_THRESHOLD);

        console.log("[useWebSocket] Attachment analysis:", {
          totalFiles: attachments.length,
          hasLargeFiles,
          fileSizes: attachments.map(a => ({ name: a.file.name, size: a.file.size, needsS3: a.file.size >= S3_UPLOAD_THRESHOLD })),
        });

        if (hasLargeFiles) {
          // Use S3 upload for large files
          console.log("[useWebSocket] Using S3 upload for large files");

          const sessionId = globalCurrentSessionId;
          if (!sessionId) {
            console.error("[useWebSocket] No session ID for S3 upload");
            return false;
          }

          // Upload all files to S3 and collect references
          const s3Attachments: Array<{
            s3Key: string;
            contentType: string;
            filename: string;
            size: number;
          }> = [];

          for (const att of attachments) {
            console.log(`[useWebSocket] Uploading ${att.file.name} (${att.file.size} bytes) to S3...`);

            // Get presigned URL
            const presignedResult = await generateUploadPresignedUrl(
              sessionId,
              att.file.name,
              att.file.type,
              att.file.size
            );

            if (!presignedResult.success || !presignedResult.uploadUrl || !presignedResult.s3Key) {
              console.error(`[useWebSocket] Failed to get presigned URL for ${att.file.name}:`, presignedResult.error);
              addMessage({
                role: "assistant",
                content: `파일 업로드 실패: ${att.file.name} - ${presignedResult.error || "알 수 없는 오류"}`,
              });
              return false;
            }

            // Upload to S3
            const uploadResult = await uploadFileToS3(
              presignedResult.uploadUrl,
              att.file,
              att.file.type
            );

            if (!uploadResult.success) {
              console.error(`[useWebSocket] Failed to upload ${att.file.name} to S3:`, uploadResult.error);
              addMessage({
                role: "assistant",
                content: `S3 업로드 실패: ${att.file.name} - ${uploadResult.error || "알 수 없는 오류"}`,
              });
              return false;
            }

            console.log(`[useWebSocket] Successfully uploaded ${att.file.name} to S3: ${presignedResult.s3Key}`);

            s3Attachments.push({
              s3Key: presignedResult.s3Key,
              contentType: att.file.type,
              filename: att.file.name,
              size: att.file.size,
            });
          }

          // Send message with S3 references
          console.log("[useWebSocket] Sending message with S3 attachments:", {
            messageLength: message.length,
            s3AttachmentCount: s3Attachments.length,
          });

          if (globalWs?.readyState !== WebSocket.OPEN) {
            console.error("[useWebSocket] WebSocket dropped before sending S3 attachments");
            return false;
          }
          globalWs.send(
            JSON.stringify({
              action: "sendMessageWithS3Attachments",
              message,
              s3Attachments,
              language: useBuilderStore.getState().language,
            })
          );

          return true;
        } else {
          // Use WebSocket base64 for small files
          console.log("[useWebSocket] Using WebSocket base64 for small files");

          const attachmentData: AttachmentData[] = await Promise.all(
            attachments.map(async (att) => ({
              name: att.file.name,
              type: att.type,
              mimeType: att.file.type,
              size: att.file.size,
              data: await fileToBase64(att.file),
            }))
          );

          // Debug: Log attachment data sizes
          console.log("[useWebSocket] Sending message with base64 attachments:", {
            messageLength: message.length,
            attachmentCount: attachmentData.length,
            attachmentTypes: attachmentData.map(a => a.mimeType),
            attachmentDataSizes: attachmentData.map(a => ({
              name: a.name,
              mimeType: a.mimeType,
              originalSize: a.size,
              base64Length: a.data?.length || 0,
              hasData: !!a.data && a.data.length > 0,
            })),
          });

          // Send to backend
          if (globalWs?.readyState !== WebSocket.OPEN) {
            console.error("[useWebSocket] WebSocket dropped before sending base64 attachments");
            return false;
          }
          globalWs.send(
            JSON.stringify({
              action: "sendMessageWithAttachments",
              message,
              attachments: attachmentData,
              language: useBuilderStore.getState().language,
            })
          );

          return true;
        }
      } catch (error) {
        console.error("[useWebSocket] Failed to prepare attachments:", error);
        return false;
      }
    },
    [addMessage]
  );

  const requestAssets = useCallback(() => {
    if (globalWs?.readyState !== WebSocket.OPEN) {
      console.error("[useWebSocket] WebSocket is not connected");
      return false;
    }

    globalWs.send(
      JSON.stringify({
        action: "downloadAssets",
      })
    );

    return true;
  }, []);

  const requestProgress = useCallback(() => {
    if (globalWs?.readyState !== WebSocket.OPEN) {
      return false;
    }

    globalWs.send(
      JSON.stringify({
        action: "getProgress",
      })
    );

    return true;
  }, []);

  const requestHistory = useCallback(() => {
    if (globalWs?.readyState !== WebSocket.OPEN) {
      console.error("[useWebSocket] WebSocket is not connected for history request");
      return false;
    }

    console.log("[useWebSocket] Requesting history...");
    globalWs.send(
      JSON.stringify({
        action: "getHistory",
      })
    );

    return true;
  }, []);

  const downloadTemplate = useCallback(() => {
    if (globalWs?.readyState !== WebSocket.OPEN) {
      console.error("[useWebSocket] WebSocket is not connected");
      return false;
    }

    globalWs.send(
      JSON.stringify({
        action: "downloadTemplate",
      })
    );

    return true;
  }, []);

  const disconnect = useCallback(
    (force = false, clearSession = false) => {
      if (!force) {
        console.log("[useWebSocket] Skipping disconnect (not forced)");
        return;
      }

      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        globalReconnectTimeout = null;
      }

      if (globalWs) {
        globalWs.close();
        globalWs = null;
      }

      // Only clear session ID on explicit logout, not on regular disconnect
      if (clearSession) {
        clearSessionId(userSub);
      }

      setConnected(false);
    },
    [setConnected, userSub]
  );

  useEffect(() => {
    return () => {
      console.log(
        "[useWebSocket] Component unmounting, cleaning up timers"
      );
      // Clean up timers to prevent memory leaks
      if (globalPingInterval) {
        clearInterval(globalPingInterval);
        globalPingInterval = null;
      }
      if (globalProactiveReconnectTimer) {
        clearTimeout(globalProactiveReconnectTimer);
        globalProactiveReconnectTimer = null;
      }
      // Cancel pending RAF
      if (streamBatchRafRef.current) {
        cancelAnimationFrame(streamBatchRafRef.current);
        streamBatchRafRef.current = null;
      }
      if (subagentStreamRafRef.current) {
        cancelAnimationFrame(subagentStreamRafRef.current);
        subagentStreamRafRef.current = null;
      }
    };
  }, [disconnect]);

  /**
   * Switch to a different session.
   * This closes the current WebSocket and reconnects with the new session ID.
   * For existing sessions, it loads conversation history and assets from REST API (DynamoDB).
   */
  const switchSession = useCallback(
    async (newSessionId: string, isNewSession: boolean = false) => {
      console.log("[useWebSocket] Switching to session:", newSessionId, "isNew:", isNewSession);

      // CRITICAL: Mark session as NOT ready to block messages until initialization completes
      // This prevents context leakage between sessions
      useBuilderStore.getState().setSessionReady(false);
      armSessionReadyWatchdog("switchSession");

      // Reset ALL session-specific state to prevent data bleeding between sessions
      // This clears: messages, assetPreviews, session, progress, downloadUrl
      // Also sets isLoadingSession=true, isSessionReady=false
      useBuilderStore.getState().resetForSessionSwitch();
      clearToolMessageIndexMap(); // Performance: Clear tool message index map

      // Update the global session ID
      const sub = await getUserSub();
      setCurrentSessionId(newSessionId, sub);

      // Close existing connection - mark as intentional to prevent auto-reconnect
      if (globalWs?.readyState === WebSocket.OPEN) {
        globalIntentionalClose = true; // Prevent onclose from auto-reconnecting
        globalWs.close();
        globalWs = null;
      }

      // Reset reconnect counters for fresh connection
      reconnectAttemptsRef.current = 0;
      globalReconnectAttempts = 0;

      // Reconnect with new session ID
      await connect();

      // For NEW sessions, tell backend to create a fresh session (skip history load)
      if (isNewSession) {
        // Wait for WebSocket to be ready, then send createNewSession
        const waitForWsAndCreateSession = () => {
          if (globalWs?.readyState === WebSocket.OPEN) {
            console.log("[useWebSocket] Sending createNewSession to backend");
            globalWs.send(
              JSON.stringify({
                action: "createNewSession",
              })
            );
            // DO NOT set isSessionReady here!
            // Wait for backend's "session_created" response in handleMessage
            console.log("[useWebSocket] Waiting for session_created response from backend...");
          } else {
            // WebSocket not ready yet, retry in 100ms (with limit)
            setTimeout(waitWithRetryLimit, 100);
          }
        };
        // Start checking immediately (no initial delay), with max retries
        let wsRetries = 0;
        const MAX_WS_RETRIES = 50; // 5 seconds max (50 * 100ms)
        const waitWithRetryLimit = () => {
          if (wsRetries >= MAX_WS_RETRIES) {
            console.error("[useWebSocket] Timed out waiting for WebSocket to open for createNewSession");
            useBuilderStore.getState().setSessionReady(true);
            useBuilderStore.getState().setLoadingSession(false);
            return;
          }
          wsRetries++;
          waitForWsAndCreateSession();
        };
        setTimeout(waitWithRetryLimit, 100);
      }

      // For existing sessions, load history, assets, and session data from REST API (DynamoDB)
      if (!isNewSession) {
        console.log("[useWebSocket] Loading history, assets, and session data from REST API for existing session");

        try {
          // Load all data in parallel
          const [history, assets, sessionData] = await Promise.all([
            getSessionHistory(newSessionId),
            getSessionAssets(newSessionId),
            getSessionData(newSessionId),
          ]);

          // Restore conversation history and assets, interleaved by timestamp
          // This ensures assets appear in their original positions in the conversation flow
          const { updateAssetPreview } = useBuilderStore.getState();

          // First, add all assets to the asset preview store (for right panel display)
          // Assets with s3Key but no content will show as placeholders until S3 lazy-load completes
          if (assets && assets.length > 0) {
            console.log("[useWebSocket] Restoring", assets.length, "assets to preview store");
            for (const asset of assets) {
              updateAssetPreview({
                assetType: asset.assetType as AssetPreview['assetType'],
                operationId: asset.operationId,
                fileName: asset.fileName,
                content: asset.content || '',
                isComplete: asset.isComplete,
                language: asset.language,
                createdAt: asset.createdAt,
                downloadData: asset.downloadData,
                s3Key: asset.s3Key,
                messageIndex: asset.messageIndex,
              });
            }

            // Lazy-load content from S3 for assets that have s3Key but empty content
            const assetsNeedingContent = assets.filter(a => a.s3Key && !a.content);
            if (assetsNeedingContent.length > 0) {
              console.log("[useWebSocket] Lazy-loading content from S3 for", assetsNeedingContent.length, "assets");
              const BATCH_SIZE = 6;
              for (let i = 0; i < assetsNeedingContent.length; i += BATCH_SIZE) {
                const batch = assetsNeedingContent.slice(i, i + BATCH_SIZE);
                await Promise.all(batch.map(async (asset) => {
                  const content = await fetchAssetContent(newSessionId, asset.s3Key!);
                  if (content) {
                    updateAssetPreview({
                      assetType: asset.assetType as AssetPreview['assetType'],
                      operationId: asset.operationId,
                      fileName: asset.fileName,
                      content,
                      isComplete: asset.isComplete,
                      language: asset.language,
                      createdAt: asset.createdAt,
                      s3Key: asset.s3Key,
                      messageIndex: asset.messageIndex,
                    });
                  } else {
                    console.warn("[useWebSocket] Failed to load content from S3 for:", asset.assetType, asset.fileName);
                  }
                }));
              }
              console.log("[useWebSocket] S3 lazy-load complete");
            }
          }

          // Interleave messages and asset markers by messageIndex
          // Assets are placed AFTER the message that triggered their generation
          if (history && history.length > 0) {
            console.log("[useWebSocket] Restoring", history.length, "messages from DynamoDB");

            // Deserialize all messages including tool/subagent
            const deserialized = deserializeHistoryMessages(history);

            console.log("[useWebSocket] Deserialized", deserialized.length, "messages (incl tool/subagent)");

            // Convert to message format for interleaving with assets
            const historyMessages: Array<{
              role: 'user' | 'assistant' | 'system' | 'tool' | 'subagent' | 'asset';
              content: string;
              timestamp?: Date;
              toolCall?: import("../types").ToolCall;
              subagentActivity?: import("../types").SubagentActivity;
              assetRef?: {
                assetType: string;
                operationId?: string;
                fileName?: string;
              };
            }> = deserialized;

            // Group assets by their messageIndex
            // Assets with the same messageIndex will be grouped together
            const assetsByMessageIndex = new Map<number, StoredAsset[]>();
            if (assets && assets.length > 0) {
              for (const asset of assets) {
                // ISSUE #3 FIX: Use messageIndex directly, fallback to end of messages
                // No longer using timestamp-based inference which caused ordering issues
                let idx: number;
                if (asset.messageIndex !== undefined && asset.messageIndex !== null) {
                  idx = asset.messageIndex;
                } else {
                  // Legacy assets without messageIndex - place at end
                  console.warn("[useWebSocket] Asset missing messageIndex, placing at end:", asset.assetType, asset.fileName);
                  idx = history.length;
                }
                if (!assetsByMessageIndex.has(idx)) {
                  assetsByMessageIndex.set(idx, []);
                }
                assetsByMessageIndex.get(idx)!.push(asset);
              }
            }

            // Build final message list by inserting asset markers after their corresponding messages
            const restoredMessages: typeof historyMessages = [];
            for (let i = 0; i < historyMessages.length; i++) {
              // Add the message
              restoredMessages.push(historyMessages[i]);

              // Add any assets that should appear after this message (messageIndex === i + 1)
              // Assets with messageIndex N appear after message N-1 (0-indexed)
              const assetsAfterThisMessage = assetsByMessageIndex.get(i + 1);
              if (assetsAfterThisMessage) {
                for (const asset of assetsAfterThisMessage) {
                  const assetLabel = getAssetTypeLabel(asset.assetType);
                  const fileName = asset.fileName || asset.operationId || '';
                  restoredMessages.push({
                    role: 'asset' as const,
                    content: `[${assetLabel}] ${fileName}`,
                    timestamp: asset.createdAt ? new Date(asset.createdAt) : undefined,
                    assetRef: {
                      assetType: asset.assetType,
                      operationId: asset.operationId,
                      fileName: asset.fileName,
                    },
                  });
                }
              }
            }

            // Add any remaining assets that have messageIndex >= history.length (placed at end)
            for (const [idx, assetList] of assetsByMessageIndex) {
              if (idx > history.length) {
                for (const asset of assetList) {
                  const assetLabel = getAssetTypeLabel(asset.assetType);
                  const fileName = asset.fileName || asset.operationId || '';
                  restoredMessages.push({
                    role: 'asset' as const,
                    content: `[${assetLabel}] ${fileName}`,
                    timestamp: asset.createdAt ? new Date(asset.createdAt) : undefined,
                    assetRef: {
                      assetType: asset.assetType,
                      operationId: asset.operationId,
                      fileName: asset.fileName,
                    },
                  });
                }
              }
            }

            if (assets && assets.length > 0) {
              console.log("[useWebSocket] Interleaved", history.length, "messages with", assets.length, "assets by messageIndex");
            }

            setMessages(restoredMessages);
            console.log("[useWebSocket] Restored", restoredMessages.length, "items (messages + asset markers)");

            // Inject history into the ECS session so the agent has context
            // (only inject user/assistant/system messages, not tool/subagent/asset markers)
            // Timeout configuration: 10 seconds max wait for WebSocket to be ready
            const HISTORY_INJECT_TIMEOUT_MS = 10000;
            const HISTORY_INJECT_RETRY_INTERVAL_MS = 100;
            const maxRetries = HISTORY_INJECT_TIMEOUT_MS / HISTORY_INJECT_RETRY_INTERVAL_MS;
            let retryCount = 0;

            // Filter to only user/assistant/system for backend injection
            const backendHistory = history.filter(msg =>
              msg.role === 'user' || msg.role === 'assistant' || msg.role === 'system'
            );

            const injectHistoryToBackend = () => {
              if (globalWs?.readyState === WebSocket.OPEN) {
                console.log("[useWebSocket] Injecting history into ECS session");
                // Include originalSessionId so assets stored under this session can be accessed
                // even if the backend assigns a different session_id on reconnection
                globalWs.send(
                  JSON.stringify({
                    action: "injectHistory",
                    history: backendHistory.map((msg) => ({
                      role: msg.role,
                      content: msg.content,
                      timestamp: msg.timestamp,
                    })),
                    sessionContext: {
                      ...(sessionData ? {
                        companyName: sessionData.companyName,
                        industry: sessionData.industry,
                        operations: sessionData.operations,
                        dbConnected: sessionData.dbConnected,
                      } : {}),
                      originalSessionId: newSessionId,  // The session ID where assets are stored in S3
                    },
                  })
                );
                // DO NOT set isSessionReady here!
                // Wait for backend's "history_injected" response in handleMessage
                console.log("[useWebSocket] Waiting for history_injected response from backend...");

                // Set a timeout to handle case where backend never responds
                setTimeout(() => {
                  if (!useBuilderStore.getState().isSessionReady) {
                    console.warn("[useWebSocket] History injection response timeout - enabling session anyway");
                    useBuilderStore.getState().setSessionReady(true);
                    useBuilderStore.getState().setLoadingSession(false);
                    useBuilderStore.getState().setConnectionError(
                      "Session history sync may be incomplete. You can continue chatting."
                    );
                  }
                }, HISTORY_INJECT_TIMEOUT_MS);
              } else {
                retryCount++;
                if (retryCount >= maxRetries) {
                  console.error("[useWebSocket] History injection timeout - WebSocket not ready after", HISTORY_INJECT_TIMEOUT_MS, "ms");
                  useBuilderStore.getState().setSessionReady(true);
                  useBuilderStore.getState().setLoadingSession(false);
                  useBuilderStore.getState().setConnectionError(
                    "Failed to sync session history. Connection may be unstable."
                  );
                  return;
                }
                // WebSocket not ready yet, retry
                setTimeout(injectHistoryToBackend, HISTORY_INJECT_RETRY_INTERVAL_MS);
              }
            };
            // Start injecting history after a short delay to ensure WebSocket is stable
            setTimeout(injectHistoryToBackend, 500);
          } else if (assets && assets.length > 0) {
            // No history but has assets - show asset markers only (sorted by timestamp)
            console.log("[useWebSocket] No history but", assets.length, "assets found");
            const assetMessages = assets
              .sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0))
              .map(asset => ({
                role: 'asset' as const,
                content: `[${getAssetTypeLabel(asset.assetType)}] ${asset.fileName || asset.operationId || ''}`,
                timestamp: asset.createdAt ? new Date(asset.createdAt) : undefined,
                assetRef: {
                  assetType: asset.assetType,
                  operationId: asset.operationId,
                  fileName: asset.fileName,
                },
              }));
            setMessages(assetMessages);
            // No history to inject, but still need to notify backend to create session
            // Send createNewSession so backend knows this is a fresh start
            const SESSION_CREATE_TIMEOUT_MS = 10000;
            let assetsOnlyRetryCount = 0;
            const maxAssetsOnlyRetries = SESSION_CREATE_TIMEOUT_MS / 100;

            const notifyBackendAssetsOnly = () => {
              if (globalWs?.readyState === WebSocket.OPEN) {
                console.log("[useWebSocket] Sending createNewSession for assets-only session");
                globalWs.send(JSON.stringify({ action: "createNewSession" }));
                // Set timeout for session_created response
                setTimeout(() => {
                  if (!useBuilderStore.getState().isSessionReady) {
                    console.warn("[useWebSocket] Session create response timeout (assets-only) - enabling session");
                    useBuilderStore.getState().setSessionReady(true);
                    useBuilderStore.getState().setLoadingSession(false);
                  }
                }, SESSION_CREATE_TIMEOUT_MS);
              } else {
                assetsOnlyRetryCount++;
                if (assetsOnlyRetryCount >= maxAssetsOnlyRetries) {
                  console.error("[useWebSocket] Session create timeout (assets-only) - WebSocket not ready");
                  useBuilderStore.getState().setSessionReady(true);
                  useBuilderStore.getState().setLoadingSession(false);
                  return;
                }
                setTimeout(notifyBackendAssetsOnly, 100);
              }
            };
            setTimeout(notifyBackendAssetsOnly, 100);
            console.log("[useWebSocket] Waiting for session_created response (assets only)...");
          } else {
            console.log("[useWebSocket] No history or assets found in DynamoDB for session:", newSessionId);
            // No history to inject, send createNewSession to backend
            const EMPTY_SESSION_TIMEOUT_MS = 10000;
            let emptyRetryCount = 0;
            const maxEmptyRetries = EMPTY_SESSION_TIMEOUT_MS / 100;

            const notifyBackendEmpty = () => {
              if (globalWs?.readyState === WebSocket.OPEN) {
                console.log("[useWebSocket] Sending createNewSession for empty session");
                globalWs.send(JSON.stringify({ action: "createNewSession" }));
                // Set timeout for session_created response
                setTimeout(() => {
                  if (!useBuilderStore.getState().isSessionReady) {
                    console.warn("[useWebSocket] Session create response timeout (empty) - enabling session");
                    useBuilderStore.getState().setSessionReady(true);
                    useBuilderStore.getState().setLoadingSession(false);
                  }
                }, EMPTY_SESSION_TIMEOUT_MS);
              } else {
                emptyRetryCount++;
                if (emptyRetryCount >= maxEmptyRetries) {
                  console.error("[useWebSocket] Session create timeout (empty) - WebSocket not ready");
                  useBuilderStore.getState().setSessionReady(true);
                  useBuilderStore.getState().setLoadingSession(false);
                  return;
                }
                setTimeout(notifyBackendEmpty, 100);
              }
            };
            setTimeout(notifyBackendEmpty, 100);
            console.log("[useWebSocket] Waiting for session_created response (empty session)...");
          }

          // Restore session data (company, operations, progress state)
          if (sessionData) {
            console.log("[useWebSocket] Restoring session data:", sessionData);
            const { updateSession, updateProgress, setDownloadUrl } = useBuilderStore.getState();

            // Update session state
            // Note: operations and assetsGenerated are stored in a simplified format
            // We only restore basic session info here; full operations are not restored
            updateSession({
              companyName: sessionData.companyName,
              industry: sessionData.industry,
              language: sessionData.language || 'en-US',
              dbConnected: sessionData.dbConnected || false,
            });

            // Restore progress state if available
            if (sessionData.progressState) {
              for (const [progressId, state] of Object.entries(sessionData.progressState)) {
                updateProgress(progressId, state.status, state.progress);
              }
            }

            // Restore download URL or regenerate if expired
            if (sessionData.packageS3Key) {
              const s3Key = sessionData.packageS3Key;
              const storedUrl = sessionData.packageDownloadUrl;
              const expiresAt = sessionData.packageExpiresAt;

              // Check if URL is expired or doesn't exist
              const isExpired = !storedUrl || !expiresAt || new Date(expiresAt) < new Date();

              if (isExpired) {
                console.log("[useWebSocket] Download URL expired or missing, regenerating presigned URL for:", s3Key);
                // Regenerate presigned URL from S3 key
                try {
                  const presignedResult = await generatePresignedUrl(newSessionId, s3Key);
                  if (presignedResult.success && presignedResult.downloadUrl) {
                    const newExpiresAt = presignedResult.expiresAt
                      ? new Date(presignedResult.expiresAt * 1000).toISOString()
                      : null;
                    setDownloadUrl(presignedResult.downloadUrl, newExpiresAt, s3Key);
                    console.log("[useWebSocket] Regenerated presigned URL successfully");
                  } else {
                    console.warn("[useWebSocket] Failed to regenerate presigned URL:", presignedResult.error);
                    // Still store the s3Key so user can try again
                    setDownloadUrl(null, null, s3Key);
                  }
                } catch (error) {
                  console.error("[useWebSocket] Error regenerating presigned URL:", error);
                  setDownloadUrl(null, null, s3Key);
                }
              } else {
                // URL is still valid, restore it
                console.log("[useWebSocket] Restoring valid download URL, expires:", expiresAt);
                setDownloadUrl(storedUrl, expiresAt, s3Key);
              }
            }

            // Note: Context injection is now done via injectHistory above
            // which includes both conversation history and session context.
            // The separate injectContext call is no longer needed.
          } else {
            console.log("[useWebSocket] No session data found in DynamoDB for session:", newSessionId);
          }
        } catch (error) {
          console.error("[useWebSocket] Failed to load session data from REST API:", error);
          useBuilderStore.getState().setLoadingSession(false);
        }
      }
    },
    [connect, getUserSub, setMessages, clearToolMessageIndexMap]
  );

  // Expose switchSession via ref so the pre-send liveness probe inside
  // sendMessage (declared earlier) can rotate the session when it detects a dead one.
  useEffect(() => {
    switchSessionRef.current = switchSession;
  }, [switchSession]);

  /**
   * Get the current session ID
   */
  const getCurrentSessionId = useCallback((): string | null => {
    return globalCurrentSessionId;
  }, []);

  return {
    connect,
    disconnect,
    sendMessage,
    sendMessageWithAttachments,
    requestAssets,
    requestProgress,
    requestHistory,
    downloadTemplate,
    switchSession,
    getCurrentSessionId,
  };
}

function downloadFile(content: string, fileName: string) {
  const blob = new Blob([content], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Get human-readable label for asset type
 */
function getAssetTypeLabel(assetType: string): string {
  const labels: Record<string, string> = {
    lambda: "Lambda",
    openapi: "OpenAPI",
    prompt: "AI Prompt",
    contact_flow: "Contact Flow",
    cdk: "CloudFormation",
    faq: "FAQ",
    research: "Research",
    package: "Package",
    company: "Company Info",
    operations: "Operations",
    validation: "Validation",
  };
  return labels[assetType] || assetType;
}

