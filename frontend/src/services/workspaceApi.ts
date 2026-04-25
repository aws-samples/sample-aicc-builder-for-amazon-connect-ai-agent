/**
 * Workspace API Service
 *
 * REST API calls for the File Explorer feature.
 * Communicates with ECS backend endpoints for workspace file tree and content.
 */

import { getIdToken } from "./auth";

export interface FileNode {
  name: string;
  type: "file" | "dir";
  size?: number;
  children?: FileNode[];
}

/**
 * Get the workspace API base URL.
 *
 * In production, uses same-origin (CloudFront proxies /api/* to ALB).
 * Falls back to localhost for local development.
 */
function getApiBase(): string {
  // Same-origin in production (CloudFront proxies /api/* to ALB)
  if (typeof window !== "undefined" && window.location.hostname !== "localhost") {
    return "";
  }
  return "http://localhost:8080";
}

async function getAuthHeaders(): Promise<HeadersInit> {
  const idToken = await getIdToken();
  return {
    Authorization: idToken ? `Bearer ${idToken}` : "",
  };
}

export async function fetchWorkspaceTree(
  sessionId: string,
  path: string = "",
  depth: number = 6
): Promise<FileNode[]> {
  try {
    const base = getApiBase();
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    if (depth !== 6) params.set("depth", String(depth));
    const qs = params.toString();
    const url = `${base}/api/workspace/${encodeURIComponent(sessionId)}/tree${qs ? `?${qs}` : ""}`;

    const headers = await getAuthHeaders();
    const res = await fetch(url, { headers });
    if (!res.ok) {
      console.warn("[workspaceApi] tree fetch failed:", res.status);
      return [];
    }
    const data = await res.json();
    return data.tree || [];
  } catch (e) {
    console.warn("[workspaceApi] tree fetch error:", e);
    return [];
  }
}

export interface NfsDiagnostics {
  mount_path: string;
  mount_exists: boolean;
  mount_contents: string[];
  sessions_dir_exists: boolean;
  session_dirs_count: number;
  recent_sessions: string[];
}

/** Fallback diagnostics gathered without /api/debug/nfs endpoint */
export interface FallbackDiagnostics {
  fallback: true;
  treeStatus: number;
  treeOk: boolean;
  treeBody: string;
  pingStatus: number | null;
  pingBody: string | null;
}

export async function fetchNfsDiagnostics(
  sessionId?: string
): Promise<NfsDiagnostics | FallbackDiagnostics | null> {
  const base = getApiBase();

  // Try the dedicated debug endpoint first
  try {
    const res = await fetch(`${base}/api/debug/nfs`);
    if (res.ok) {
      return await res.json();
    }
  } catch {
    // endpoint not deployed yet — fall through to fallback
  }

  // Fallback: probe the tree endpoint + /ping to gather what we can
  try {
    const headers = await getAuthHeaders();
    const treeUrl = sessionId
      ? `${base}/api/workspace/${encodeURIComponent(sessionId)}/tree`
      : null;

    const [treeRes, pingRes] = await Promise.all([
      treeUrl ? fetch(treeUrl, { headers }) : Promise.resolve(null),
      fetch(`${base}/ping`).catch(() => null),
    ]);

    return {
      fallback: true,
      treeStatus: treeRes?.status ?? -1,
      treeOk: treeRes?.ok ?? false,
      treeBody: treeRes ? await treeRes.text().then(t => t.slice(0, 200)) : "(no sessionId)",
      pingStatus: pingRes?.status ?? null,
      pingBody: pingRes ? await pingRes.text().then(t => t.slice(0, 300)) : null,
    };
  } catch {
    return null;
  }
}

export async function fetchWorkspaceFile(
  sessionId: string,
  path: string
): Promise<{ content: string; size: number; language: string } | null> {
  try {
    const base = getApiBase();
    const url = `${base}/api/workspace/${encodeURIComponent(sessionId)}/file?path=${encodeURIComponent(path)}`;

    const headers = await getAuthHeaders();
    const res = await fetch(url, { headers });
    if (!res.ok) {
      console.warn("[workspaceApi] file fetch failed:", res.status);
      return null;
    }
    return await res.json();
  } catch (e) {
    console.warn("[workspaceApi] file fetch error:", e);
    return null;
  }
}


/**
 * Request a fresh asset package from the backend. The backend reads directly from
 * S3 (bypassing NFS cache) on every call, so the returned presigned URL always
 * points at the latest content. Pass `assetType` to download a single type, or
 * omit/use "all" for the full package.
 */
export async function fetchAssetDownloadUrl(
  sessionId: string,
  assetType?: string,
): Promise<{ downloadUrl: string; s3Key: string } | null> {
  try {
    const base = getApiBase();
    const qs = assetType && assetType !== "all" ? `?asset_type=${encodeURIComponent(assetType)}` : "";
    const url = `${base}/api/assets/${encodeURIComponent(sessionId)}/download${qs}`;
    const headers = await getAuthHeaders();
    const res = await fetch(url, { headers });
    if (!res.ok) {
      console.warn("[workspaceApi] asset download fetch failed:", res.status);
      return null;
    }
    const data = await res.json();
    if (!data.success || !data.downloadUrl) return null;
    return { downloadUrl: data.downloadUrl, s3Key: data.s3Key };
  } catch (e) {
    console.warn("[workspaceApi] asset download error:", e);
    return null;
  }
}
