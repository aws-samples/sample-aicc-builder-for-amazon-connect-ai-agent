/**
 * Session Management Service
 *
 * Manages user chat sessions via REST API (API Gateway + Lambda + DynamoDB).
 * Sessions are stored per-user and can be listed, created, updated, and deleted.
 */

import { getIdToken } from "./auth";

// Session API URL from environment (ensure trailing slash for URL concatenation)
const RAW_SESSION_API_URL = (import.meta as any).env?.VITE_SESSION_API_URL || "";
const SESSION_API_URL = RAW_SESSION_API_URL && !RAW_SESSION_API_URL.endsWith("/")
  ? RAW_SESSION_API_URL + "/"
  : RAW_SESSION_API_URL;

export interface ChatSession {
  userId: string;
  sessionId: string;
  title: string;
  createdAt: number;
  lastMessageAt: number;
  messageCount: number;
}

/**
 * Get authorization headers for API requests
 */
async function getAuthHeaders(): Promise<HeadersInit> {
  const idToken = await getIdToken();
  if (!idToken) {
    throw new Error("Not authenticated");
  }
  return {
    Authorization: `Bearer ${idToken}`,
    "Content-Type": "application/json",
  };
}

/**
 * List all sessions for the current user
 */
export async function listSessions(): Promise<ChatSession[]> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return [];
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(`${SESSION_API_URL}sessions`, {
      method: "GET",
      headers,
    });

    if (!response.ok) {
      throw new Error(`Failed to list sessions: ${response.status}`);
    }

    const data = await response.json();
    return data.sessions || [];
  } catch (error) {
    console.error("[sessions] Failed to list sessions:", error);
    return [];
  }
}

/**
 * Create a new session
 */
export async function createSession(
  sessionId: string,
  title: string = "New Chat"
): Promise<ChatSession | null> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return null;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(`${SESSION_API_URL}sessions`, {
      method: "POST",
      headers,
      body: JSON.stringify({ sessionId, title }),
    });

    if (!response.ok) {
      throw new Error(`Failed to create session: ${response.status}`);
    }

    const data = await response.json();
    return data.session || null;
  } catch (error) {
    console.error("[sessions] Failed to create session:", error);
    return null;
  }
}

/**
 * Update session metadata
 */
export async function updateSession(
  sessionId: string,
  updates: {
    title?: string;
    lastMessageAt?: number;
    messageCount?: number;
  }
): Promise<ChatSession | null> {
  if (!SESSION_API_URL) {
    return null;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PUT",
        headers,
        body: JSON.stringify(updates),
      }
    );

    if (!response.ok) {
      throw new Error(`Failed to update session: ${response.status}`);
    }

    const data = await response.json();
    return data.session || null;
  } catch (error) {
    console.error("[sessions] Failed to update session:", error);
    return null;
  }
}

/**
 * Delete a session
 */
export async function deleteSession(sessionId: string): Promise<boolean> {
  if (!SESSION_API_URL) {
    return false;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers,
      }
    );

    return response.ok;
  } catch (error) {
    console.error("[sessions] Failed to delete session:", error);
    return false;
  }
}

/**
 * Generate a title from the first message
 */
export function generateTitleFromMessage(message: string): string {
  // Take first 50 characters, clean up
  const cleaned = message
    .replace(/\n/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  if (cleaned.length <= 50) {
    return cleaned;
  }

  // Find a good break point
  const truncated = cleaned.substring(0, 50);
  const lastSpace = truncated.lastIndexOf(" ");

  if (lastSpace > 30) {
    return truncated.substring(0, lastSpace) + "...";
  }

  return truncated + "...";
}

/**
 * Serialized tool call data for DynamoDB storage
 */
export interface StoredToolCall {
  tool: string;
  toolUseId?: string;
  input?: Record<string, unknown>;
  result?: string;
  error?: string;
  status: 'completed' | 'error';
}

/**
 * Serialized sub-agent tool call for DynamoDB storage
 */
export interface StoredSubagentToolCall {
  tool: string;
  displayName: string;
  input?: Record<string, unknown>;
  result?: string;
  status: 'completed' | 'error';
  timestamp: number;
}

/**
 * Serialized sub-agent activity for DynamoDB storage
 */
export interface StoredSubagentActivity {
  subagent: string;
  displayName: string;
  status: 'completed' | 'error';
  content?: string;
  toolCalls: StoredSubagentToolCall[];
  timestamp: number;
}

/**
 * Conversation message type
 */
export interface ConversationMessage {
  role: "user" | "assistant" | "system" | "tool" | "subagent";
  content: string;
  timestamp?: number;
  /** Serialized tool call (for role='tool') */
  toolCall?: StoredToolCall;
  /** Serialized sub-agent activity (for role='subagent') */
  subagentActivity?: StoredSubagentActivity;
}

/**
 * Message log entry from NFS JSONL
 */
export interface MessageLogEntry {
  seq: number;
  ts: number;
  event: Record<string, unknown>;
}

/**
 * Response from message log API
 */
export interface MessageLogResponse {
  entries: MessageLogEntry[];
  isAgentActive: boolean;
}

/**
 * Get message log entries for catch-up after reconnect
 */
export async function getMessageLog(
  sessionId: string,
  afterSeq: number = 0
): Promise<MessageLogResponse> {
  // Use same-origin /api path (proxied by CloudFront to ALB)
  const baseUrl = `${window.location.origin}/api/message-log`;
  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${baseUrl}/${encodeURIComponent(sessionId)}?after_seq=${afterSeq}`,
      { method: "GET", headers }
    );

    if (!response.ok) {
      if (response.status === 404) {
        return { entries: [], isAgentActive: false };
      }
      throw new Error(`Failed to get message log: ${response.status}`);
    }

    return await response.json();
  } catch (error) {
    console.error("[sessions] Failed to get message log:", error);
    return { entries: [], isAgentActive: false };
  }
}

/**
 * Get conversation history for a session
 */
export async function getSessionHistory(
  sessionId: string
): Promise<ConversationMessage[]> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return [];
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/history`,
      {
        method: "GET",
        headers,
      }
    );

    if (!response.ok) {
      if (response.status === 404) {
        return []; // Session not found, return empty
      }
      throw new Error(`Failed to get history: ${response.status}`);
    }

    const data = await response.json();
    return data.history || [];
  } catch (error) {
    console.error("[sessions] Failed to get session history:", error);
    return [];
  }
}

/**
 * Save conversation history for a session
 */
export async function saveSessionHistory(
  sessionId: string,
  history: ConversationMessage[]
): Promise<boolean> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return false;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/history`,
      {
        method: "PUT",
        headers,
        body: JSON.stringify({ history }),
      }
    );

    return response.ok;
  } catch (error) {
    console.error("[sessions] Failed to save session history:", error);
    return false;
  }
}

/**
 * Asset preview data structure for storage
 */
export interface StoredAsset {
  assetType: string;
  operationId?: string;
  fileName?: string;
  content: string;
  isComplete: boolean;
  language?: string;
  createdAt?: number;
  downloadData?: string;
  s3Key?: string; // S3 key for asset download
  messageIndex?: number; // Message index for asset placement in session restore
}

/**
 * Get stored assets for a session
 */
export async function getSessionAssets(
  sessionId: string
): Promise<StoredAsset[]> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return [];
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/assets`,
      {
        method: "GET",
        headers,
      }
    );

    if (!response.ok) {
      if (response.status === 404) {
        return []; // No assets found, return empty
      }
      throw new Error(`Failed to get assets: ${response.status}`);
    }

    const data = await response.json();
    return data.assets || [];
  } catch (error) {
    console.error("[sessions] Failed to get session assets:", error);
    return [];
  }
}

/**
 * Save assets for a session
 */
export async function saveSessionAssets(
  sessionId: string,
  assets: StoredAsset[]
): Promise<boolean> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return false;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/assets`,
      {
        method: "PUT",
        headers,
        body: JSON.stringify({ assets }),
      }
    );

    return response.ok;
  } catch (error) {
    console.error("[sessions] Failed to save session assets:", error);
    return false;
  }
}

/**
 * Session data structure for context persistence
 * This includes all business context that needs to survive session reconnection
 */
export interface SessionData {
  companyName?: string | null;
  industry?: string | null;
  language?: string;
  operations?: Array<{
    id: string;
    name: string;
    description?: string;
    status?: string;
  }>;
  dbConnected?: boolean;
  assetsGenerated?: string[];
  // Progress state for UI restoration
  progressState?: Record<string, {
    status: 'pending' | 'in_progress' | 'completed';
    progress: number;
  }>;
  // S3 key for the final packaged ZIP file
  packageS3Key?: string;
  // Cached presigned URL (may be expired)
  packageDownloadUrl?: string;
  packageExpiresAt?: string;
}

/**
 * Get session data (business context) for a session
 */
export async function getSessionData(
  sessionId: string
): Promise<SessionData | null> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return null;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/data`,
      {
        method: "GET",
        headers,
      }
    );

    if (!response.ok) {
      if (response.status === 404) {
        return null; // No data found
      }
      throw new Error(`Failed to get session data: ${response.status}`);
    }

    const data = await response.json();
    return data.sessionData || null;
  } catch (error) {
    console.error("[sessions] Failed to get session data:", error);
    return null;
  }
}

/**
 * Save session data (business context) for a session
 */
export async function saveSessionData(
  sessionId: string,
  sessionData: SessionData
): Promise<boolean> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return false;
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/data`,
      {
        method: "PUT",
        headers,
        body: JSON.stringify({ sessionData }),
      }
    );

    return response.ok;
  } catch (error) {
    console.error("[sessions] Failed to save session data:", error);
    return false;
  }
}

/**
 * Response from presigned URL generation (download)
 */
export interface PresignedUrlResponse {
  success: boolean;
  downloadUrl?: string;
  expiresAt?: number;
  expiresInHours?: number;
  error?: string;
}

/**
 * Response from upload presigned URL generation
 */
export interface UploadPresignedUrlResponse {
  success: boolean;
  uploadUrl?: string;
  s3Key?: string;
  bucket?: string;
  expiresAt?: number;
  expiresInMinutes?: number;
  error?: string;
}

/**
 * Generate a presigned URL for downloading assets from S3
 * This is used when the stored download URL has expired
 */
export async function generatePresignedUrl(
  sessionId: string,
  s3Key: string
): Promise<PresignedUrlResponse> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return { success: false, error: "Session API URL not configured" };
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/presigned`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({ s3Key }),
      }
    );

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return {
        success: false,
        error: errorData.error || `Failed to generate presigned URL: ${response.status}`,
      };
    }

    const data = await response.json();
    return {
      success: data.success,
      downloadUrl: data.downloadUrl,
      expiresAt: data.expiresAt,
      expiresInHours: data.expiresInHours,
    };
  } catch (error) {
    console.error("[sessions] Failed to generate presigned URL:", error);
    return { success: false, error: String(error) };
  }
}

/**
 * Generate a presigned URL for uploading files to S3
 * Used for multimodal file uploads that exceed WebSocket size limits
 */
export async function generateUploadPresignedUrl(
  sessionId: string,
  filename: string,
  contentType: string,
  fileSize: number
): Promise<UploadPresignedUrlResponse> {
  if (!SESSION_API_URL) {
    console.warn("[sessions] Session API URL not configured");
    return { success: false, error: "Session API URL not configured" };
  }

  try {
    const headers = await getAuthHeaders();
    const response = await fetch(
      `${SESSION_API_URL}sessions/${encodeURIComponent(sessionId)}/upload-presigned`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({ filename, contentType, fileSize }),
      }
    );

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      return {
        success: false,
        error: errorData.error || `Failed to generate upload URL: ${response.status}`,
      };
    }

    const data = await response.json();
    return {
      success: data.success,
      uploadUrl: data.uploadUrl,
      s3Key: data.s3Key,
      bucket: data.bucket,
      expiresAt: data.expiresAt,
      expiresInMinutes: data.expiresInMinutes,
    };
  } catch (error) {
    console.error("[sessions] Failed to generate upload presigned URL:", error);
    return { success: false, error: String(error) };
  }
}

/**
 * Fetch asset content from S3 via presigned URL
 * Used during session restore to lazy-load content for assets stored in S3
 */
export async function fetchAssetContent(
  sessionId: string,
  s3Key: string
): Promise<string | null> {
  const result = await generatePresignedUrl(sessionId, s3Key);
  if (!result.success || !result.downloadUrl) return null;
  try {
    const response = await fetch(result.downloadUrl);
    if (!response.ok) return null;
    return await response.text();
  } catch {
    return null;
  }
}

/**
 * Upload a file directly to S3 using a presigned URL
 */
export async function uploadFileToS3(
  uploadUrl: string,
  file: File,
  contentType: string
): Promise<{ success: boolean; error?: string }> {
  try {
    const response = await fetch(uploadUrl, {
      method: "PUT",
      headers: {
        "Content-Type": contentType,
      },
      body: file,
    });

    if (!response.ok) {
      return {
        success: false,
        error: `S3 upload failed: ${response.status} ${response.statusText}`,
      };
    }

    return { success: true };
  } catch (error) {
    console.error("[sessions] Failed to upload file to S3:", error);
    return { success: false, error: String(error) };
  }
}
