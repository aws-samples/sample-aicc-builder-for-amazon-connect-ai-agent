/**
 * useAutoSave Hook
 *
 * Extracts DynamoDB save logic from ChatWindow to prevent
 * save-related state/effects from triggering ChatWindow re-renders.
 * Uses zustand subscribe() to watch store changes outside React render cycle.
 */

import { useCallback, useEffect, useRef } from 'react';
import { useBuilderStore } from '../stores/builderStore';
import { useSessionStore } from '../stores/sessionStore';
import {
  saveSessionHistory,
  saveSessionAssets,
  saveSessionData,
  type ConversationMessage,
  type StoredAsset,
  type StoredToolCall,
  type StoredSubagentActivity,
  type StoredSubagentToolCall,
  type SessionData,
} from '../services/sessions';

export function useAutoSave() {
  const currentSessionId = useSessionStore(s => s.currentSessionId);

  const saveHistoryTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveAssetsTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveSessionDataTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const streamingSaveIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const lastSavedMessagesRef = useRef<string>('');
  const lastSavedAssetsRef = useRef<string>('');
  const lastSavedSessionDataRef = useRef<string>('');
  const savedAssetKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    lastSavedMessagesRef.current = '';
    lastSavedAssetsRef.current = '';
    lastSavedSessionDataRef.current = '';
    savedAssetKeysRef.current = new Set();
    if (streamingSaveIntervalRef.current) {
      clearInterval(streamingSaveIntervalRef.current);
      streamingSaveIntervalRef.current = null;
    }
  }, [currentSessionId]);

  const saveHistoryToDynamoDB = useCallback(async () => {
    const { messages } = useBuilderStore.getState();
    if (!currentSessionId || messages.length === 0) return;

    const persistableMessages = messages.filter(msg => {
      // Always exclude thinking and asset markers
      if (msg.role === 'thinking' || msg.role === 'asset') return false;
      // Tool messages: exclude running (in-progress), keep completed/error
      if (msg.role === 'tool') {
        if (!msg.toolCall || msg.toolCall.status === 'running') return false;
        return true;
      }
      // Subagent messages: exclude started/running, keep completed/error
      if (msg.role === 'subagent') {
        if (!msg.subagentActivity) return false;
        const st = msg.subagentActivity.status;
        if (st === 'started' || st === 'running') return false;
        return true;
      }
      // user/assistant/system: must have content
      if (!msg.content || msg.content.trim() === '') return false;
      return true;
    });
    if (persistableMessages.length === 0) return;

    const history: ConversationMessage[] = persistableMessages.map(msg => {
      const ts = msg.timestamp instanceof Date ? msg.timestamp.getTime() : new Date(msg.timestamp).getTime();

      // Serialize tool messages
      if (msg.role === 'tool' && msg.toolCall) {
        const tc = msg.toolCall;
        const storedTool: StoredToolCall = {
          tool: tc.tool,
          toolUseId: tc.toolUseId,
          input: truncateObject(tc.input, 200),
          result: typeof tc.result === 'string'
            ? tc.result.substring(0, 500)
            : tc.result != null ? JSON.stringify(tc.result).substring(0, 500) : undefined,
          error: tc.error,
          status: tc.status === 'error' ? 'error' : 'completed',
        };
        return {
          role: 'tool' as const,
          content: msg.content || '',
          timestamp: ts,
          toolCall: storedTool,
        };
      }

      // Serialize subagent messages
      if (msg.role === 'subagent' && msg.subagentActivity) {
        const sa = msg.subagentActivity;
        const storedToolCalls: StoredSubagentToolCall[] = (sa.toolCalls || [])
          .filter(tc => tc.status === 'completed' || tc.status === 'error')
          .map(tc => ({
            tool: tc.tool,
            displayName: tc.displayName,
            input: truncateObject(tc.input, 200),
            result: typeof tc.result === 'string'
              ? tc.result.substring(0, 200)
              : tc.result != null ? JSON.stringify(tc.result).substring(0, 200) : undefined,
            status: tc.status === 'error' ? 'error' as const : 'completed' as const,
            timestamp: tc.timestamp instanceof Date ? tc.timestamp.getTime() : new Date(tc.timestamp).getTime(),
          }));
        const storedSA: StoredSubagentActivity = {
          subagent: sa.subagent,
          displayName: sa.displayName,
          status: sa.status === 'error' ? 'error' : 'completed',
          content: sa.content,
          toolCalls: storedToolCalls,
          timestamp: sa.timestamp instanceof Date ? sa.timestamp.getTime() : new Date(sa.timestamp).getTime(),
        };
        return {
          role: 'subagent' as const,
          content: msg.content || '',
          timestamp: ts,
          subagentActivity: storedSA,
        };
      }

      return {
        role: msg.role as 'user' | 'assistant' | 'system',
        content: msg.content,
        timestamp: ts,
      };
    });

    const historyHash = JSON.stringify(history.map(h => ({ r: h.role, c: (h.content || '').substring(0, 50), t: h.toolCall?.tool, s: h.subagentActivity?.subagent })));
    if (historyHash === lastSavedMessagesRef.current) return;

    console.log("[useAutoSave] Saving history:", history.length, "messages (incl tool/subagent)");
    const success = await saveSessionHistory(currentSessionId, history);
    if (success) lastSavedMessagesRef.current = historyHash;
  }, [currentSessionId]);

  const saveAssetsToDynamoDB = useCallback(async () => {
    const { assetPreviews } = useBuilderStore.getState();
    if (!currentSessionId) return;

    const assetsToSave: StoredAsset[] = Object.values(assetPreviews)
      .filter(asset => asset.isComplete)
      .map(asset => ({
        assetType: asset.assetType,
        operationId: asset.operationId,
        fileName: asset.fileName,
        // s3Key가 있으면 content를 DynamoDB에 저장하지 않음 (S3에서 lazy-load)
        content: asset.s3Key ? '' : asset.content,
        isComplete: asset.isComplete,
        language: asset.language,
        createdAt: asset.createdAt,
        // downloadData는 DynamoDB에 저장하지 않음 (binary, 너무 큼)
        s3Key: asset.s3Key,
        messageIndex: asset.messageIndex,
      }));
    if (assetsToSave.length === 0) return;

    const assetsHash = JSON.stringify(assetsToSave.map(a => ({ t: a.assetType, f: a.fileName, c: (a.content || '').substring(0, 50) })));
    if (assetsHash === lastSavedAssetsRef.current) return;

    console.log("[useAutoSave] Saving assets:", assetsToSave.length);
    const success = await saveSessionAssets(currentSessionId, assetsToSave);
    if (success) lastSavedAssetsRef.current = assetsHash;
  }, [currentSessionId]);

  const saveSessionDataToDynamoDB = useCallback(async () => {
    const { session, progress, packageS3Key, downloadUrl, downloadExpiresAt } = useBuilderStore.getState();
    if (!currentSessionId) return;

    const sessionDataToSave: SessionData = {
      companyName: session.companyName,
      industry: session.industry,
      language: session.language,
      operations: session.operations.map(op => ({
        id: op.operationId, name: op.operationType, description: op.summary, status: 'defined',
      })),
      dbConnected: session.dbConnected,
      assetsGenerated: session.assetsGenerated.map(a => a.type),
      progressState: progress.reduce((acc, item) => {
        acc[item.id] = { status: item.status, progress: item.progress ?? 0 };
        return acc;
      }, {} as Record<string, { status: 'pending' | 'in_progress' | 'completed'; progress: number }>),
      packageS3Key: packageS3Key || undefined,
      packageDownloadUrl: downloadUrl || undefined,
      packageExpiresAt: downloadExpiresAt || undefined,
    };

    const sessionDataHash = JSON.stringify(sessionDataToSave);
    if (sessionDataHash === lastSavedSessionDataRef.current) return;
    if (!sessionDataToSave.companyName && (!sessionDataToSave.operations || sessionDataToSave.operations.length === 0)) return;

    console.log("[useAutoSave] Saving session data");
    const success = await saveSessionData(currentSessionId, sessionDataToSave);
    if (success) lastSavedSessionDataRef.current = sessionDataHash;
  }, [currentSessionId]);

  // Subscribe to store changes outside React render cycle
  useEffect(() => {
    let prevMessages = useBuilderStore.getState().messages;
    let prevAssetPreviews = useBuilderStore.getState().assetPreviews;
    let prevSession = useBuilderStore.getState().session;
    let prevProgress = useBuilderStore.getState().progress;
    let prevIsTyping = useBuilderStore.getState().isTyping;

    const unsub = useBuilderStore.subscribe((state) => {
      const isTypingChanged = state.isTyping !== prevIsTyping;

      // ── History save ──
      // Two strategies: immediate save on transitions, periodic save during streaming
      if (state.messages !== prevMessages && state.messages.length > 0) {
        prevMessages = state.messages;
        if (!state.isTyping) {
          // Not streaming: save with short debounce (response complete or user message before streaming)
          if (saveHistoryTimeoutRef.current) clearTimeout(saveHistoryTimeoutRef.current);
          saveHistoryTimeoutRef.current = setTimeout(saveHistoryToDynamoDB, 2000);
        }
      }

      if (isTypingChanged) {
        prevIsTyping = state.isTyping;
        if (state.isTyping) {
          // Streaming started → save immediately (captures user's question + initial state)
          saveHistoryToDynamoDB();
          // Start periodic saves during streaming (every 15s)
          if (streamingSaveIntervalRef.current) clearInterval(streamingSaveIntervalRef.current);
          streamingSaveIntervalRef.current = setInterval(saveHistoryToDynamoDB, 15_000);
        } else {
          // Streaming ended → stop periodic saves, do final save
          if (streamingSaveIntervalRef.current) {
            clearInterval(streamingSaveIntervalRef.current);
            streamingSaveIntervalRef.current = null;
          }
          if (saveHistoryTimeoutRef.current) clearTimeout(saveHistoryTimeoutRef.current);
          saveHistoryTimeoutRef.current = setTimeout(saveHistoryToDynamoDB, 2000);
        }
      }

      // ── Asset save ──
      if (state.assetPreviews !== prevAssetPreviews) {
        prevAssetPreviews = state.assetPreviews;
        const completedAssets = Object.entries(state.assetPreviews).filter(([, a]) => a.isComplete);
        const newlyCompleted = completedAssets.filter(([key]) => !savedAssetKeysRef.current.has(key));
        if (newlyCompleted.length > 0) {
          newlyCompleted.forEach(([key]) => savedAssetKeysRef.current.add(key));
          saveAssetsToDynamoDB();
        }
        if (!state.isTyping) {
          if (saveAssetsTimeoutRef.current) clearTimeout(saveAssetsTimeoutRef.current);
          saveAssetsTimeoutRef.current = setTimeout(saveAssetsToDynamoDB, 1000);
        }
      }

      // ── Session data save ──
      if (!state.isTyping && (state.session !== prevSession || state.progress !== prevProgress)) {
        prevSession = state.session;
        prevProgress = state.progress;
        if (!state.session.companyName && (!state.session.operations || state.session.operations.length === 0)) return;
        if (saveSessionDataTimeoutRef.current) clearTimeout(saveSessionDataTimeoutRef.current);
        saveSessionDataTimeoutRef.current = setTimeout(saveSessionDataToDynamoDB, 3000);
      }
    });

    return () => {
      unsub();
      if (saveHistoryTimeoutRef.current) clearTimeout(saveHistoryTimeoutRef.current);
      if (saveAssetsTimeoutRef.current) clearTimeout(saveAssetsTimeoutRef.current);
      if (saveSessionDataTimeoutRef.current) clearTimeout(saveSessionDataTimeoutRef.current);
      if (streamingSaveIntervalRef.current) clearInterval(streamingSaveIntervalRef.current);
    };
  }, [saveHistoryToDynamoDB, saveAssetsToDynamoDB, saveSessionDataToDynamoDB]);
}

/**
 * Truncate object values to a maximum string length for DynamoDB storage.
 * Returns the object with string values truncated.
 */
function truncateObject(obj: Record<string, unknown> | undefined, maxLen: number): Record<string, unknown> | undefined {
  if (!obj) return obj;
  const result: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(obj)) {
    if (typeof val === 'string' && val.length > maxLen) {
      result[key] = val.substring(0, maxLen) + `... [${val.length} chars]`;
    } else {
      result[key] = val;
    }
  }
  return result;
}
