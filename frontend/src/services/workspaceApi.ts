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
  // Always same-origin: in production CloudFront proxies /api/* to the ALB; in
  // local dev the Vite dev server proxies /api → localhost:8080 (see
  // vite.config.ts). Using an absolute localhost URL here would bypass the proxy
  // and trip CORS, so we always return "" and rely on relative /api paths.
  return "";
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
  /** Authoritative existence of the queried session dir (when session_id passed).
   *  Prefer this over `recent_sessions` membership — the latter is truncated. */
  queried_session_id?: string | null;
  session_exists?: boolean | null;
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

  // Try the dedicated debug endpoint first. Pass session_id so the backend
  // returns an authoritative `session_exists` for THIS session (not just the
  // truncated top-10 recent_sessions list).
  try {
    const url = sessionId
      ? `${base}/api/debug/nfs?session_id=${encodeURIComponent(sessionId)}`
      : `${base}/api/debug/nfs`;
    const res = await fetch(url);
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
export type AssetDownloadResult =
  | { ok: true; downloadUrl: string; s3Key: string }
  | { ok: false; status: number; error: string; problems: string[] };

/**
 * Ask the backend to package the session (or one asset type) and return a
 * presigned URL. A refusal comes back as `ok: false` with the backend's reason
 * and, for ACXD bundles, the consistency problems that blocked packaging —
 * the caller shows those instead of guessing.
 */
export async function fetchAssetDownloadUrl(
  sessionId: string,
  assetType?: string,
): Promise<AssetDownloadResult> {
  const base = getApiBase();
  const qs = assetType && assetType !== "all" ? `?asset_type=${encodeURIComponent(assetType)}` : "";
  const url = `${base}/api/assets/${encodeURIComponent(sessionId)}/download${qs}`;
  try {
    const headers = await getAuthHeaders();
    const res = await fetch(url, { headers });
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    // CloudFront rewrites 403/404 to the SPA's index.html: never parse HTML as JSON.
    const data = isJson ? await res.json().catch(() => null) : null;
    if (!res.ok || !data || !data.success || !data.downloadUrl) {
      const error =
        (data && (data.error || data.detail)) ||
        (isJson ? "Download unavailable" : `Download failed (HTTP ${res.status})`);
      console.warn("[workspaceApi] asset download refused:", res.status, error);
      return {
        ok: false,
        status: res.status,
        error: typeof error === "string" ? error : JSON.stringify(error),
        problems: Array.isArray(data?.problems) ? data.problems.map(String) : [],
      };
    }
    return { ok: true, downloadUrl: data.downloadUrl, s3Key: data.s3Key };
  } catch (e) {
    console.warn("[workspaceApi] asset download error:", e);
    return { ok: false, status: 0, error: e instanceof Error ? e.message : String(e), problems: [] };
  }
}
