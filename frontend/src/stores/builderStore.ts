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

// ── Model selection ──────────────────────────────────────────────────────
// Top Bedrock Claude models the user can pick from. Default 4.8.
// The id is sent to the backend on every outbound WS message (mirrors `language`).
export interface ModelOption {
  id: string;
  label: string;
}

export const MODELS: ModelOption[] = [
  { id: 'global.anthropic.claude-opus-4-8', label: 'Opus 4.8' },
  { id: 'global.anthropic.claude-opus-4-7', label: 'Opus 4.7' },
  // NOTE: 4.6 carries the `-v1` suffix in Bedrock (verified ACTIVE inference
  // profile in ap-northeast-2); 4.7/4.8 do not. Must match the backend allowlist.
  { id: 'global.anthropic.claude-opus-4-6-v1', label: 'Opus 4.6' },
];

export const DEFAULT_MODEL_ID = 'global.anthropic.claude-opus-4-8';

// ── Start-screen mode / generation scope ─────────────────────────────────
export type StartMode = 'full' | 'segment' | 'improve';
export type SegmentType = 'contact_flow' | 'prompt' | 'faq';
// Asset types that can be imported & edited from an external file.
// 'contact_flow_image' = a whiteboard/sketch photo the backend transcribes (via
// vision) into a draft Contact Flow before the normal lint/seed import path.
export type ImportAssetType = 'contact_flow' | 'prompt' | 'contact_flow_image';

/** Lint summary returned by the backend after `importAsset`. */
export interface ImportLintResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  fixesApplied: number;
}

/** Result of an `asset_imported` event from the backend. */
export interface ImportedAssetInfo {
  assetType: ImportAssetType;
  operationId: string;
  fileName: string;
  lint: ImportLintResult;
  phase?: string;
}

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

  // Selected Claude model (sent on every outbound WS message)
  selectedModel: string;

  // Start-screen mode + active generation scope
  startMode: StartMode;
  segment: SegmentType | null;
  // Active scope for this run (from session_created / asset_imported).
  // null = full build (all assets). A non-null list = scoped run.
  scope: string[] | null;
  // Most recent imported-asset info (for the lint summary strip)
  importedAsset: ImportedAssetInfo | null;

  // Split-view asset workspace
  // Which right-pane view is active: progress sidebar or the asset workspace.
  rightPaneView: 'progress' | 'assets';
  // When set, the asset workspace is open and focused on this asset key.
  activeAssetKey: string | null;
  // Fullscreen modal for an asset (zoom). null = closed.
  fullscreenAssetKey: string | null;

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
  setSelectedModel: (modelId: string) => void;
  setStartMode: (mode: StartMode) => void;
  setSegment: (segment: SegmentType | null) => void;
  setScope: (scope: string[] | null) => void;
  setImportedAsset: (info: ImportedAssetInfo | null) => void;
  setRightPaneView: (view: 'progress' | 'assets') => void;
  setActiveAssetKey: (key: string | null) => void;
  setFullscreenAssetKey: (key: string | null) => void;
  setTheme: (theme: Theme) => void;
  clearMessages: () => void;
  setMessages: (messages: Array<Omit<Message, 'id' | 'timestamp'> & { id?: string; timestamp?: Date }>) => void;
  updateMessageAt: (index: number, updater: (msg: Message) => Message) => void;
  resetForSessionSwitch: () => void;
  reset: () => void;
}

// The 12 progress steps, each tagged with the phase it belongs to so the
// sidebar can group them under the 4-phase stepper (interview / generation /
// review / post_generation).
const initialProgress: ProgressItem[] = [
  {
    id: 'database',
    label: 'Database Analysis',
    labelKo: '데이터베이스 분석',
    status: 'pending',
    progress: 0,
    phase: 'interview',
  },
  {
    id: 'operations',
    label: 'Operation Specs',
    labelKo: '작업 사양 정의',
    status: 'pending',
    progress: 0,
    phase: 'interview',
  },
  {
    id: 'requirements',
    label: 'Requirements Analysis',
    labelKo: '요구사항 분석',
    status: 'pending',
    progress: 0,
    phase: 'interview',
  },
  {
    id: 'research',
    label: 'Research',
    labelKo: '리서치',
    status: 'pending',
    progress: 0,
    phase: 'interview',
  },
  {
    id: 'lambda',
    label: 'Lambda Functions',
    labelKo: 'Lambda 함수',
    status: 'pending',
    progress: 0,
    phase: 'generation',
  },
  {
    id: 'prompt',
    label: 'AI Prompt',
    labelKo: 'AI 프롬프트',
    status: 'pending',
    progress: 0,
    phase: 'generation',
  },
  {
    id: 'openapi',
    label: 'OpenAPI Spec',
    labelKo: 'OpenAPI 스펙',
    status: 'pending',
    progress: 0,
    phase: 'generation',
  },
  {
    id: 'contact_flow',
    label: 'Contact Flow',
    labelKo: 'Contact Flow',
    status: 'pending',
    progress: 0,
    phase: 'generation',
  },
  {
    id: 'cdk',
    label: 'Infrastructure',
    labelKo: '인프라',
    status: 'pending',
    progress: 0,
    phase: 'generation',
  },
  {
    id: 'knowledge_base',
    label: 'Knowledge Base',
    labelKo: 'Knowledge Base',
    status: 'pending',
    progress: 0,
    phase: 'generation',
  },
  {
    id: 'review',
    label: 'Review & Validation',
    labelKo: '검증',
    status: 'pending',
    progress: 0,
    phase: 'review',
  },
  {
    id: 'ready',
    label: 'Package & Download',
    labelKo: '패키징 & 다운로드',
    status: 'pending',
    progress: 0,
    phase: 'post_generation',
  },
];

// Map a scope id (contact_flow / prompt / faq) to the progress-step id it drives.
// FAQ maps to knowledge_base (the FAQ generator's progress lane).
export const SCOPE_TO_PROGRESS_ID: Record<string, string> = {
  contact_flow: 'contact_flow',
  prompt: 'prompt',
  faq: 'knowledge_base',
};

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

// Get initial selected model from localStorage (defaults to Opus 4.8)
const getInitialModel = (): string => {
  if (typeof window !== 'undefined') {
    const stored = localStorage.getItem('selectedModel');
    if (stored && MODELS.some((m) => m.id === stored)) {
      return stored;
    }
  }
  return DEFAULT_MODEL_ID;
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
  selectedModel: getInitialModel(),
  startMode: 'full',
  segment: null,
  scope: null,
  importedAsset: null,
  rightPaneView: 'progress',
  activeAssetKey: null,
  fullscreenAssetKey: null,
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
      // Contact Flow diagram: the mermaid asset is streamed SEPARATELY from the
      // contact_flow JSON (same operationId). Merge it onto the matching
      // contact_flow preview as diagramContent so ContactFlowPreview can render
      // the Diagram tab. The standalone 'mermaid' assetType has no tab of its own.
      if (preview.assetType === 'mermaid') {
        const cfKey = Object.keys(state.assetPreviews).find(k => {
          const p = state.assetPreviews[k];
          return p.assetType === 'contact_flow' &&
            (!preview.operationId || p.operationId === preview.operationId);
        });
        if (cfKey) {
          return {
            assetPreviews: {
              ...state.assetPreviews,
              [cfKey]: { ...state.assetPreviews[cfKey], diagramContent: preview.content },
            },
          };
        }
        // contact_flow not in yet — stash the mermaid under a holder key so the
        // contact_flow branch below can adopt it when it arrives.
        const holderKey = `__pending_mermaid-${preview.operationId || 'default'}`;
        return {
          assetPreviews: {
            ...state.assetPreviews,
            [holderKey]: { ...preview, createdAt: Date.now() },
          },
        };
      }

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
        // Preserve existing content when incoming is a delta or empty — prevents
        // late-delivered delta events (race in backend pending_ws_events flush)
        // from clobbering the fully accumulated content. Observed on
        // cloudformation (many chunks → higher race).
        //
        // EXCEPTION: an authoritative FULL replacement (isDelta === false) that
        // is legitimately SHORTER must be allowed through. The contact-flow
        // import-safety auto-fix re-streams the linted JSON full + non-delta,
        // and the repaired flow is shorter than the raw one (it strips invalid
        // DTMFConfiguration / duplicate SSML keys). The old "shorter ⇒ reject"
        // rule silently kept the broken longer version, so the user downloaded
        // a flow that fails CreateContactFlow. Only a non-delta full event may
        // shrink the content; a delta/empty event never can.
        const isAuthoritativeFull = preview.isDelta === false && !!preview.content;
        if (preview.isDelta || !preview.content ||
            (!isAuthoritativeFull && preview.content.length < (completePreview.content || '').length)) {
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

      // If this is a contact_flow and a sibling mermaid arrived first, adopt it
      // (and drop the holder) so the Diagram tab renders.
      if (newPreview.assetType === 'contact_flow' && !newPreview.diagramContent) {
        const holderKey = Object.keys(newAssetPreviews).find(k =>
          k.startsWith('__pending_mermaid-') &&
          (newAssetPreviews[k].operationId || 'default') === (newPreview.operationId || 'default'));
        // also accept a generic holder if no op-specific match
        const fallbackHolder = holderKey || Object.keys(newAssetPreviews).find(k => k.startsWith('__pending_mermaid-'));
        const useHolder = holderKey || fallbackHolder;
        if (useHolder && newAssetPreviews[useHolder]) {
          newPreview.diagramContent = newAssetPreviews[useHolder].content;
          newAssetPreviews = { ...newAssetPreviews, [key]: newPreview };
          delete newAssetPreviews[useHolder];
        }
      }

      // Track the latest asset as the workspace's active asset so the pane has
      // something to show when the user opens it. We do NOT force the pane open
      // here (that would interrupt the user) — the chat marker / tab does that.
      const nextActiveAssetKey = key;

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

      return { assetPreviews: newAssetPreviews, activeAssetKey: nextActiveAssetKey };
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

  setSelectedModel: (modelId) => {
    if (typeof window !== 'undefined') {
      localStorage.setItem('selectedModel', modelId);
    }
    return set({ selectedModel: modelId });
  },

  setStartMode: (mode) =>
    set((state) => ({
      startMode: mode,
      // Leaving segment mode clears any chosen segment.
      segment: mode === 'segment' ? state.segment : null,
    })),

  setSegment: (segment) => set({ segment }),

  setScope: (scope) => set({ scope }),

  setImportedAsset: (info) => set({ importedAsset: info }),

  setRightPaneView: (view) => set({ rightPaneView: view }),

  setActiveAssetKey: (key) =>
    set((state) => ({
      activeAssetKey: key,
      // Opening an asset always reveals the asset workspace pane.
      rightPaneView: key ? 'assets' : state.rightPaneView,
    })),

  setFullscreenAssetKey: (key) => set({ fullscreenAssetKey: key }),

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
      // Scope/imported state is per-session — clear it on switch.
      scope: null,
      importedAsset: null,
      activeAssetKey: null,
      fullscreenAssetKey: null,
      rightPaneView: 'progress',
      // Keep language and selectedModel as user preferences
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
      scope: null,
      importedAsset: null,
      activeAssetKey: null,
      fullscreenAssetKey: null,
      rightPaneView: 'progress',
    }),
}));
