/**
 * Type definitions for AICC Builder
 */

export interface ToolCall {
  tool: string;
  /** Unique identifier for this specific tool invocation (allows same tool to be called multiple times) */
  toolUseId?: string;
  input?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  status: 'running' | 'completed' | 'error';
}

/** Tool call made by a Sub-Agent */
export interface SubagentToolCall {
  tool: string;
  displayName: string;
  input?: Record<string, unknown>;
  result?: unknown;
  status: 'running' | 'completed' | 'error';
  timestamp: Date;
}

/** Sub-Agent activity information */
export interface SubagentActivity {
  subagent: string;
  displayName: string;
  status: 'started' | 'running' | 'completed' | 'error';
  content?: string;
  thinking?: string;
  toolCalls: SubagentToolCall[];
  timestamp: Date;
}

/** File attachment information for multimodal messages */
export interface MessageAttachment {
  /** Original file name */
  name: string;
  /** Attachment type: 'image' or 'document' */
  type: 'image' | 'document';
  /** MIME type (e.g., 'image/png', 'application/pdf') */
  mimeType: string;
  /** File size in bytes */
  size?: number;
  /** Base64 encoded thumbnail for images (for preview display) */
  preview?: string;
}

/** Attachment data sent to backend (includes full base64 content) */
export interface AttachmentData extends MessageAttachment {
  /** Base64 encoded file content */
  data: string;
}

/** Attached file state during upload flow */
export interface AttachedFile {
  /** Unique ID for this attachment instance */
  id: string;
  /** The actual File object */
  file: File;
  /** Base64 preview for images */
  preview?: string;
  /** Attachment type */
  type: 'image' | 'document';
  /** Upload status */
  status: 'pending' | 'uploading' | 'ready' | 'error';
  /** Error message if status is 'error' */
  error?: string;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool' | 'thinking' | 'asset' | 'subagent';
  content: string;
  timestamp: Date;
  /** Tool call information (for role='tool') */
  toolCall?: ToolCall;
  /** Whether this message is still streaming */
  isStreaming?: boolean;
  /** Asset reference (for role='asset' - marks where an asset was generated in the conversation) */
  assetRef?: {
    assetType: string;
    operationId?: string;
    fileName?: string;
  };
  /** Sub-Agent activity information (for role='subagent') */
  subagentActivity?: SubagentActivity;
  /** File attachments (for multimodal messages) */
  attachments?: MessageAttachment[];
}

export interface Operation {
  operationId: string;
  operationType: string;
  httpMethod: string;
  path: string;
  summary: string;
  inputFieldCount: number;
  hasSideEffects: boolean;
}

export interface GeneratedAsset {
  type: 'lambda' | 'openapi' | 'prompt' | 'flow';
  operationId?: string;
  files: Record<string, string>;
}

export interface SessionState {
  companyName: string | null;
  industry: string | null;
  language: string;
  operations: Operation[];
  dbConnected: boolean;
  assetsGenerated: GeneratedAsset[];
}

export interface ProgressSubStep {
  id: string;
  label: string;
  labelKo: string;
  completed: boolean;
}

export interface ProgressItem {
  id: string;
  label: string;
  labelKo: string;
  status: 'pending' | 'in_progress' | 'completed';
  /** Progress percentage (0-100) for granular tracking */
  progress?: number;
  /** Optional sub-steps for detailed progress */
  subSteps?: ProgressSubStep[];
  /** Timestamp when status last changed */
  updatedAt?: number;
}

export interface AssetPreview {
  assetType: 'lambda' | 'openapi' | 'prompt' | 'contact_flow' | 'mermaid' | 'cdk' | 'cloudformation' | 'company' | 'operations' | 'validation' | 'research' | 'faq' | 'package' | 'review' | 'operation_spec' | 'workspace_update' | 'workspace_file' | 'requirement';
  operationId?: string;
  fileName?: string;
  content: string;
  isComplete: boolean;
  language?: string; // For syntax highlighting: 'python', 'yaml', 'json', 'markdown', etc.
  createdAt?: number; // Timestamp for ordering (ms since epoch)
  downloadData?: string; // Base64 encoded data for downloadable packages
  s3Key?: string; // S3 key for asset download via presigned URL
  messageIndex?: number; // Message index for asset placement in session restore (null = use timestamp)
  // Delta streaming support - prevents 32KB WebSocket limit for large assets
  isDelta?: boolean; // True if content is incremental (append to existing)
  totalLength?: number; // Total accumulated content length (for progress tracking)
  // Regeneration support - side-by-side comparison
  isRegeneration?: boolean; // Whether this is a regenerated version of a previous asset
  previousContent?: string; // Content of the previous version (for comparison)
  previousCreatedAt?: number; // Timestamp of the previous version
  // Diff support - unified diff from workspace modifications
  diffContent?: string; // Unified diff text (from patch_workspace_file or write_with_diff)
}

/** Debug information sent with error messages */
export interface ErrorDebugInfo {
  error_type: string;
  error_message: string;
  traceback: string;
  session_id?: string;
  last_tool?: string;
  context?: string;
}

export interface WebSocketMessage {
  type: 'message' | 'typing' | 'error' | 'attachment_error' | 'session_update' | 'assets' | 'progress' | 'stream' | 'stream_end' | 'tool_status' | 'progress_update' | 'questionnaire_status' | 'template' | 'tool_start' | 'tool_end' | 'tool_input_update' | 'thinking' | 'asset_preview' | 'asset_generating' | 'asset_complete' | 'download_ready' | 'history' | 'history_injected' | 'context_injected' | 'session_created' | 'subagent_progress' | 'subagent_tool_use' | 'subagent_tool_result' | 'subagent_stream' | 'subagent_error' | 'heartbeat' | 'pong' | 'connected' | 'background_task_active' | 'phase_changed' | 'input_hint';
  // Chat-input placeholder hint (backend-computed)
  placeholder?: string;
  role?: 'user' | 'assistant';
  content?: string;
  status?: string;
  message?: string;
  // Session management
  sessionId?: string;
  // Phase tracking
  phase?: string;
  previousPhase?: string;
  // History/context injection acknowledgments
  success?: boolean;
  injectedCount?: number;
  session?: SessionState;
  assets?: Record<string, GeneratedAsset>;
  progress?: {
    session: SessionState;
    operations: Operation[];
    operationCount: number;
  };
  // Streaming support
  tool?: string;
  /** Unique identifier for this specific tool invocation */
  toolUseId?: string;
  /** Message ID for tracking specific streaming messages */
  message_id?: string;
  // Tool call events
  input?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  // Progress updates
  itemId?: string;
  /** Progress percentage (0-100) for granular updates */
  progressPercent?: number;
  /** Sub-step ID that was completed */
  subStepId?: string;
  progressUpdates?: Array<{ id: string; status: 'pending' | 'in_progress' | 'completed'; progress?: number }>;
  // Questionnaire support
  summary?: string;
  completeness_score?: number;
  missing_fields?: string[];
  fileName?: string;
  // Asset preview streaming
  assetPreview?: AssetPreview;
  // Asset generating indicator (no content, just status)
  assetType?: string;
  operationId?: string;
  language?: string;
  // Download ready
  downloadUrl?: string;
  expiresAt?: string;
  s3Key?: string;
  // History support
  history?: Array<{ role: string; content: string; timestamp?: number }>;
  // NFS-backed progress state for reconnect restoration
  progressState?: Record<string, { status: string; progress: number }>;
  // Debug info for error messages (development only)
  debug?: ErrorDebugInfo;
  // Sub-Agent progress support
  subagent?: string;
  operation_id?: string;
  agent_name?: string;
  api_title?: string;
  flow_name?: string;
  files_count?: number;
}

export type Language = 'en-US' | 'ko-KR' | 'ja-JP';

export const LANGUAGES: Record<Language, string> = {
  'en-US': 'English',
  'ko-KR': '한국어',
  'ja-JP': '日本語',
};

// Phase-based system prompt phases
export type BuilderPhase = 'interview' | 'generation' | 'review' | 'post_generation';

export const PHASE_LABELS: Record<BuilderPhase, Record<string, string>> = {
  interview:       { 'en-US': 'Interview',       'ko-KR': '인터뷰',     'ja-JP': 'インタビュー' },
  generation:      { 'en-US': 'Generation',      'ko-KR': '생성',        'ja-JP': '生成' },
  review:          { 'en-US': 'Review',           'ko-KR': '리뷰',        'ja-JP': 'レビュー' },
  post_generation: { 'en-US': 'Re-generation',   'ko-KR': '재생성',      'ja-JP': '再生成' },
};

export const PHASE_ICONS: Record<BuilderPhase, string> = {
  interview: '\uD83D\uDCAC',
  generation: '\u26A1',
  review: '\uD83D\uDD0D',
  post_generation: '\uD83D\uDD27',
};

/** Ordered list of phases for stepper UI */
export const PHASE_ORDER: BuilderPhase[] = ['interview', 'generation', 'review', 'post_generation'];
