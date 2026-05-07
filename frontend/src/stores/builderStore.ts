/**
 * Zustand store for AICC Builder state management
 */

import { create } from 'zustand';
import type {
  Message,
  GeneratedAsset,
  SessionState,
  ProgressItem,
  Language,
  AssetPreview,
  BuilderPhase,
} from '../types';

// Performance limits to prevent memory issues in long sessions
const MAX_MESSAGES = 200;           // Keep last 200 messages in memory
const MAX_ASSET_PREVIEWS = 50;      // Keep last 50 asset previews

export type Theme = 'light' | 'dark' | 'system';

interface BuilderState {
  // Theme
  theme: Theme;

  // Connection state
  isConnected: boolean;
  isConnecting: boolean;
  connectionError: string | null;

  // Session readiness (blocks UI until session is fully initialized)
  isSessionReady: boolean;
  isLoadingSession: boolean;

  // Chat state
  messages: Message[];
  isTyping: boolean;

  // Session state
  session: SessionState;

  // Progress tracking
  progress: ProgressItem[];

  // Generated assets
  assets: Record<string, GeneratedAsset>;

  // Asset previews (streaming during generation)
  assetPreviews: Record<string, AssetPreview>;

  // Download URL for packaged assets
  downloadUrl: string | null;
  downloadExpiresAt: string | null;
  packageS3Key: string | null;

  // Download completion modal
  showDownloadModal: boolean;

  // Reconnection status for UX banners
  reconnectStatus: 'reconnecting' | 'reconnected' | null;

  // Tracks repeated backend "Agent is still processing" errors so the UI can
  // surface a Reset Session button after the user hits the wall twice.
  stillProcessingCount: number;

  // Workspace file explorer refresh trigger
  workspaceRefreshTrigger: number;

  // Language
  language: Language;

  // Phase tracking
  currentPhase: BuilderPhase;

  // Chat input hint (placeholder text pushed by backend per turn)
  inputHint: { placeholder: string; phase?: string } | null;

  // Actions
  setConnected: (connected: boolean) => void;
  setConnecting: (connecting: boolean) => void;
  setConnectionError: (error: string | null) => void;
  setSessionReady: (ready: boolean) => void;
  setLoadingSession: (loading: boolean) => void;
  addMessage: (message: Omit<Message, 'id' | 'timestamp'>) => void;
  updateLastMessage: (contentToAppend: string) => void;
  setTyping: (typing: boolean) => void;
  updateSession: (session: Partial<SessionState>) => void;
  updateProgress: (itemId: string, status: ProgressItem['status'], progress?: number) => void;
  updateProgressPercent: (itemId: string, progress: number) => void;
  completeSubStep: (itemId: string, subStepId: string) => void;
  addAsset: (asset: GeneratedAsset) => void;
  updateAssetPreview: (preview: AssetPreview) => void;
  completeAssetPreview: (assetKey: string) => void;
  clearAssetPreviews: () => void;
  setDownloadUrl: (url: string | null, expiresAt: string | null, s3Key?: string | null) => void;
  setShowDownloadModal: (show: boolean) => void;
  setReconnectStatus: (status: 'reconnecting' | 'reconnected' | null) => void;
  bumpStillProcessingCount: () => void;
  resetStillProcessingCount: () => void;
  triggerWorkspaceRefresh: () => void;
  setCurrentPhase: (phase: BuilderPhase) => void;
  setInputHint: (hint: { placeholder: string; phase?: string } | null) => void;
  setLanguage: (language: Language) => void;
  setTheme: (theme: Theme) => void;
  clearMessages: () => void;
  setMessages: (messages: Array<Omit<Message, 'id' | 'timestamp'> & { id?: string; timestamp?: Date }>) => void;
  updateMessageAt: (index: number, updater: (msg: Message) => Message) => void;
  resetForSessionSwitch: () => void;
  reset: () => void;
}

const initialProgress: ProgressItem[] = [
  {
    id: 'database',
    label: 'Database Analysis',
    labelKo: '데이터베이스 분석',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'operations',
    label: 'Operation Specs',
    labelKo: '작업 사양 정의',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'requirements',
    label: 'Requirements Analysis',
    labelKo: '요구사항 분석',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'lambda',
    label: 'Lambda Functions',
    labelKo: 'Lambda 함수',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'prompt',
    label: 'AI Prompt',
    labelKo: 'AI 프롬프트',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'openapi',
    label: 'OpenAPI Spec',
    labelKo: 'OpenAPI 스펙',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'contact_flow',
    label: 'Contact Flow',
    labelKo: 'Contact Flow',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'cdk',
    label: 'Infrastructure',
    labelKo: '인프라',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'knowledge_base',
    label: 'Knowledge Base',
    labelKo: 'Knowledge Base',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'research',
    label: 'Research',
    labelKo: '리서치',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'review',
    label: 'Review & Validation',
    labelKo: '검증',
    status: 'pending',
    progress: 0,
  },
  {
    id: 'ready',
    label: 'Package & Download',
    labelKo: '패키징 & 다운로드',
    status: 'pending',
    progress: 0,
  },
];

const initialSession: SessionState = {
  companyName: null,
  industry: null,
  language: 'en-US',
  operations: [],
  dbConnected: false,
  assetsGenerated: [],
};

// Get initial theme from localStorage or system preference
const getInitialTheme = (): Theme => {
  if (typeof window !== 'undefined') {
    const stored = localStorage.getItem('theme') as Theme | null;
    if (stored && ['light', 'dark', 'system'].includes(stored)) {
      return stored;
    }
  }
  return 'dark'; // Default to dark mode for Kiro-like experience
};

// Get initial language from localStorage
const getInitialLanguage = (): Language => {
  if (typeof window !== 'undefined') {
    const stored = localStorage.getItem('language') as Language | null;
    if (stored && ['en-US', 'ko-KR', 'ja-JP'].includes(stored)) {
      return stored;
    }
  }
  return 'ko-KR';
};

export const useBuilderStore = create<BuilderState>((set) => ({
  // Initial state
  theme: getInitialTheme(),
  isConnected: false,
  isConnecting: false,
  connectionError: null,
  isSessionReady: false,
  isLoadingSession: false,
  messages: [],
  isTyping: false,
  session: initialSession,
  progress: initialProgress,
  assets: {},
  assetPreviews: {},
  downloadUrl: null,
  downloadExpiresAt: null,
  packageS3Key: null,
  showDownloadModal: false,
  reconnectStatus: null,
  stillProcessingCount: 0,
  workspaceRefreshTrigger: 0,
  language: getInitialLanguage(),
  currentPhase: 'interview' as BuilderPhase,
  inputHint: null,

  // Actions
  setConnected: (connected) => set({ isConnected: connected }),

  setConnecting: (connecting) => set({ isConnecting: connecting }),

  setConnectionError: (error) => set({ connectionError: error }),

  setSessionReady: (ready) => set({ isSessionReady: ready }),

  setLoadingSession: (loading) => set({ isLoadingSession: loading }),

  addMessage: (message) =>
    set((state) => {
      const newMessage = {
        ...message,
        id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
        timestamp: new Date(),
      };
      // Limit messages to prevent memory issues in long sessions
      const allMessages = [...state.messages, newMessage];
      const limitedMessages = allMessages.length > MAX_MESSAGES
        ? allMessages.slice(-MAX_MESSAGES)
        : allMessages;
      return { messages: limitedMessages };
    }),

  updateLastMessage: (contentToAppend) =>
    set((state) => {
      const len = state.messages.length;
      if (len === 0) return state;
      const lastIndex = len - 1;
      const lastMessage = state.messages[lastIndex];
      // Performance: Only create new reference for the last element
      // Previous elements maintain their references (shallow copy optimization)
      const updatedMessage = { ...lastMessage, content: lastMessage.content + contentToAppend };
      return {
        messages: len === 1
          ? [updatedMessage]
          : [...state.messages.slice(0, lastIndex), updatedMessage]
      };
    }),

  setTyping: (typing) => set({ isTyping: typing }),

  updateSession: (sessionUpdate) =>
    set((state) => ({
      session: { ...state.session, ...sessionUpdate },
    })),

  updateProgress: (itemId, status, progress) =>
    set((state) => ({
      progress: state.progress.map((item) =>
        item.id === itemId
          ? {
              ...item,
              status,
              progress: progress ?? (status === 'completed' ? 100 : status === 'in_progress' ? item.progress || 10 : 0),
              updatedAt: Date.now(),
            }
          : item
      ),
    })),

  updateProgressPercent: (itemId, progress) =>
    set((state) => ({
      progress: state.progress.map((item) =>
        item.id === itemId
          ? {
              ...item,
              progress: Math.min(100, Math.max(0, progress)),
              status: progress >= 100 ? 'completed' : progress > 0 ? 'in_progress' : item.status,
              updatedAt: Date.now(),
            }
          : item
      ),
    })),

  completeSubStep: (itemId, subStepId) =>
    set((state) => ({
      progress: state.progress.map((item) => {
        if (item.id !== itemId || !item.subSteps) return item;

        const updatedSubSteps = item.subSteps.map((sub) =>
          sub.id === subStepId ? { ...sub, completed: true } : sub
        );

        const completedCount = updatedSubSteps.filter((s) => s.completed).length;
        const totalCount = updatedSubSteps.length;
        const newProgress = Math.round((completedCount / totalCount) * 100);

        return {
          ...item,
          subSteps: updatedSubSteps,
          progress: newProgress,
          status: newProgress >= 100 ? 'completed' : newProgress > 0 ? 'in_progress' : 'pending',
          updatedAt: Date.now(),
        };
      }),
    })),

  addAsset: (asset) =>
    set((state) => ({
      assets: {
        ...state.assets,
        [asset.operationId || asset.type]: asset,
      },
    })),

  updateAssetPreview: (preview) =>
    set((state) => {
      // Handle diff events: attach diffContent to existing preview for the same fileName
      if (preview.operationId === 'diff' && preview.fileName) {
        // Find existing preview with matching fileName (any operationId)
        const matchingKey = Object.keys(state.assetPreviews).find(k => {
          const p = state.assetPreviews[k];
          return p.fileName === preview.fileName && p.operationId !== 'diff';
        });
        if (matchingKey) {
          // Attach diff content to existing preview
          return {
            assetPreviews: {
              ...state.assetPreviews,
              [matchingKey]: {
                ...state.assetPreviews[matchingKey],
                diffContent: preview.content,
              },
            },
          };
        }
        // No existing preview found — create standalone diff preview
        const diffKey = `${preview.assetType}-diff-${preview.fileName}-${Date.now()}`;
        return {
          assetPreviews: {
            ...state.assetPreviews,
            [diffKey]: {
              ...preview,
              diffContent: preview.content,
              createdAt: Date.now(),
              messageIndex: state.messages.length,
            },
          },
        };
      }

      // Build base key including fileName for multiple files of same type
      // Key format: assetType-operationId-fileName or assetType-fileName or assetType
      let baseKey: string;
      if (preview.fileName && preview.operationId) {
        baseKey = `${preview.assetType}-${preview.operationId}-${preview.fileName}`;
      } else if (preview.fileName) {
        baseKey = `${preview.assetType}-${preview.fileName}`;
      } else if (preview.operationId) {
        baseKey = `${preview.assetType}-${preview.operationId}`;
      } else {
        baseKey = preview.assetType;
      }

      // Find existing keys for this asset (may have timestamp suffix)
      const matchingKeys = Object.keys(state.assetPreviews).filter(k =>
        k === baseKey || k.startsWith(`${baseKey}-`)
      );

      // IMPORTANT: Prioritize incomplete preview (currently streaming) over completed ones
      // This prevents creating multiple previews during regeneration streaming
      const incompleteKey = matchingKeys.find(k => !state.assetPreviews[k].isComplete);
      const completeKey = matchingKeys.find(k => state.assetPreviews[k].isComplete);

      // REGENERATION DETECTION: Only trigger when:
      // 1. There's NO incomplete preview (we're not already streaming)
      // 2. There IS a completed preview
      // 3. New chunk is incomplete (start of new stream)
      const isRegeneration = !incompleteKey && completeKey && preview.isComplete === false;

      let key: string;
      let newPreview = { ...preview };

      if (incompleteKey) {
        // STREAMING CONTINUATION: Update existing incomplete preview
        key = incompleteKey;
        const incompletePreview = state.assetPreviews[incompleteKey];
        newPreview.createdAt = incompletePreview.createdAt || Date.now();
        newPreview.messageIndex = incompletePreview.messageIndex ?? state.messages.length;

        // DELTA MODE: If this is a delta update, append to existing content
        // Backend sends isDelta=true when only sending new content since last transmission
        // This prevents exceeding 32KB WebSocket message limit for large assets
        if (preview.isDelta && incompletePreview.content) {
          newPreview.content = incompletePreview.content + (preview.content || '');
        }

        // Preserve regeneration fields if they exist
        if (incompletePreview.isRegeneration) {
          newPreview.isRegeneration = incompletePreview.isRegeneration;
          newPreview.previousContent = incompletePreview.previousContent;
          newPreview.previousCreatedAt = incompletePreview.previousCreatedAt;
        }
      } else if (isRegeneration && completeKey) {
        // REGENERATION START: Create new key with timestamp (keeps old preview at original position)
        key = `${baseKey}-${Date.now()}`;
        const completePreview = state.assetPreviews[completeKey];
        // Mark as regeneration and store previous content for side-by-side comparison
        newPreview.isRegeneration = true;
        newPreview.previousContent = completePreview.content;
        newPreview.previousCreatedAt = completePreview.createdAt;
        newPreview.createdAt = Date.now();
        newPreview.messageIndex = state.messages.length; // Current position
      } else if (completeKey && preview.isComplete) {
        // Updating a complete preview (e.g., marking complete, updating s3Key)
        key = completeKey;
        const completePreview = state.assetPreviews[completeKey];
        newPreview.createdAt = completePreview.createdAt || Date.now();
        newPreview.messageIndex = completePreview.messageIndex ?? state.messages.length;
        // Preserve existing content when incoming is a delta or shorter — prevents duplicate
        // late-delivered events (race in backend pending_ws_events flush) from clobbering
        // the fully accumulated content. Observed on cloudformation (many chunks → higher race).
        if (preview.isDelta || !preview.content || (preview.content.length < (completePreview.content || '').length)) {
          newPreview.content = completePreview.content;
        }
      } else {
        // First asset of this type
        key = `${baseKey}-${Date.now()}`;
        // Preserve passed values for session restore, fallback to defaults for new assets
        newPreview.createdAt = preview.createdAt || Date.now();
        newPreview.messageIndex = preview.messageIndex ?? state.messages.length;
      }

      let newAssetPreviews = {
        ...state.assetPreviews,
        [key]: newPreview,
      };

      // Limit asset previews to prevent memory issues
      const keys = Object.keys(newAssetPreviews);
      if (keys.length > MAX_ASSET_PREVIEWS) {
        // Sort by createdAt and remove oldest
        const sortedKeys = keys.sort((a, b) => {
          const aCreated = newAssetPreviews[a].createdAt || 0;
          const bCreated = newAssetPreviews[b].createdAt || 0;
          return aCreated - bCreated;
        });
        const keysToRemove = new Set(sortedKeys.slice(0, keys.length - MAX_ASSET_PREVIEWS));
        newAssetPreviews = Object.fromEntries(
          Object.entries(newAssetPreviews).filter(([k]) => !keysToRemove.has(k))
        );
      }

      return { assetPreviews: newAssetPreviews };
    }),

  completeAssetPreview: (assetKey) =>
    set((state) => {
      const preview = state.assetPreviews[assetKey];
      if (!preview) return state;
      return {
        assetPreviews: {
          ...state.assetPreviews,
          [assetKey]: { ...preview, isComplete: true },
        },
      };
    }),

  clearAssetPreviews: () => set({ assetPreviews: {} }),

  setDownloadUrl: (url, expiresAt, s3Key) =>
    set((state) => ({
      downloadUrl: url,
      downloadExpiresAt: expiresAt,
      packageS3Key: s3Key !== undefined ? s3Key : state.packageS3Key,
    })),

  setShowDownloadModal: (show) => set({ showDownloadModal: show }),

  setReconnectStatus: (status) => set({ reconnectStatus: status }),

  bumpStillProcessingCount: () =>
    set((state) => ({ stillProcessingCount: state.stillProcessingCount + 1 })),

  resetStillProcessingCount: () => set({ stillProcessingCount: 0 }),

  triggerWorkspaceRefresh: () =>
    set((state) => ({ workspaceRefreshTrigger: state.workspaceRefreshTrigger + 1 })),

  setCurrentPhase: (phase) => set({ currentPhase: phase }),

  setInputHint: (hint) => set({ inputHint: hint }),

  setLanguage: (language) => {
    if (typeof window !== 'undefined') {
      localStorage.setItem('language', language);
    }
    return set({ language });
  },

  setTheme: (theme) => {
    // Persist to localStorage
    if (typeof window !== 'undefined') {
      localStorage.setItem('theme', theme);
    }
    // Apply theme to document
    const root = document.documentElement;
    if (theme === 'system') {
      const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      root.classList.toggle('dark', systemDark);
    } else {
      root.classList.toggle('dark', theme === 'dark');
    }
    return set({ theme });
  },

  clearMessages: () =>
    set({
      messages: [],
      isTyping: false,
    }),

  setMessages: (messages) =>
    set({
      // Limit restored messages to prevent memory issues
      messages: messages.slice(-MAX_MESSAGES).map((msg, index) => ({
        ...msg,
        id: msg.id || `msg-restored-${index}-${Date.now()}`,
        timestamp: msg.timestamp || new Date(),
      })),
      isTyping: false,
    }),

  // Performance: Update a single message by index without copying unaffected messages' references
  updateMessageAt: (index, updater) =>
    set((state) => {
      if (index < 0 || index >= state.messages.length) return state;
      const updated = updater(state.messages[index]);
      if (updated === state.messages[index]) return state;
      const newMessages = state.messages.slice();
      newMessages[index] = updated;
      return { messages: newMessages };
    }),

  // Reset all session-specific state when switching to a different session
  // This ensures no data bleeds between sessions
  resetForSessionSwitch: () =>
    set({
      messages: [],
      isTyping: false,
      session: initialSession,
      progress: initialProgress,
      assetPreviews: {},
      downloadUrl: null,
      downloadExpiresAt: null,
      packageS3Key: null,
      showDownloadModal: false,
      isSessionReady: false,
      isLoadingSession: true,
      currentPhase: 'interview' as BuilderPhase,
      inputHint: null,
      stillProcessingCount: 0,
      connectionError: null,
      // Keep language as it's a user preference
    }),

  reset: () =>
    set({
      messages: [],
      isTyping: false,
      session: initialSession,
      progress: initialProgress,
      assets: {},
      assetPreviews: {},
      downloadUrl: null,
      downloadExpiresAt: null,
      packageS3Key: null,
      showDownloadModal: false,
      isSessionReady: false,
      isLoadingSession: false,
      currentPhase: 'interview' as BuilderPhase,
      inputHint: null,
      stillProcessingCount: 0,
      connectionError: null,
    }),
}));
