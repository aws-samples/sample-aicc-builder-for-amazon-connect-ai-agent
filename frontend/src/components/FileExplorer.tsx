/**
 * File Explorer Component
 *
 * VS Code-style file tree for browsing session workspace files.
 * Auto-refreshes when workspace_update events arrive via WebSocket.
 */

import { useState, useEffect, useCallback } from 'react';
import {
  Folder,
  FolderOpen,
  File,
  FileCode,
  FileJson,
  FileText,
  FileSpreadsheet,
  ChevronRight,
  RefreshCw,
  Loader2,
  AlertCircle,
  Bug,
} from 'lucide-react';
import { cn } from '../lib/utils';
import { useBuilderStore } from '../stores/builderStore';
import { useSessionStore } from '../stores/sessionStore';
import { fetchWorkspaceTree, fetchNfsDiagnostics, type FileNode, type NfsDiagnostics, type FallbackDiagnostics } from '../services/workspaceApi';
import { FileViewer } from './FileViewer';

function getFileIcon(name: string) {
  if (name.endsWith('.py')) return <FileCode className="w-4 h-4 text-yellow-500 flex-shrink-0" />;
  if (name.endsWith('.yaml') || name.endsWith('.yml')) return <FileCode className="w-4 h-4 text-blue-500 flex-shrink-0" />;
  if (name.endsWith('.json')) return <FileJson className="w-4 h-4 text-green-500 flex-shrink-0" />;
  if (name.endsWith('.md')) return <FileText className="w-4 h-4 text-surface-500 flex-shrink-0" />;
  if (name.endsWith('.js') || name.endsWith('.ts') || name.endsWith('.tsx')) return <FileCode className="w-4 h-4 text-blue-400 flex-shrink-0" />;
  if (name.endsWith('.sh')) return <FileCode className="w-4 h-4 text-green-400 flex-shrink-0" />;
  if (name.endsWith('.csv') || name.endsWith('.xlsx') || name.endsWith('.tsv')) return <FileSpreadsheet className="w-4 h-4 text-emerald-500 flex-shrink-0" />;
  if (name.endsWith('.pdf')) return <FileText className="w-4 h-4 text-red-500 flex-shrink-0" />;
  if (name.endsWith('.docx') || name.endsWith('.doc')) return <FileText className="w-4 h-4 text-blue-600 flex-shrink-0" />;
  return <File className="w-4 h-4 text-surface-400 flex-shrink-0" />;
}

function formatSize(bytes?: number): string {
  if (bytes === undefined || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface TreeNodeProps {
  node: FileNode;
  depth: number;
  basePath: string;
  onFileClick: (path: string) => void;
  variant?: 'light' | 'dark';
}

function TreeNode({ node, depth, basePath, onFileClick, variant = 'light' }: TreeNodeProps) {
  const [expanded, setExpanded] = useState(depth < 2);
  const fullPath = basePath ? `${basePath}/${node.name}` : node.name;

  const icon = node.type === 'dir'
    ? (expanded ? <FolderOpen className="w-4 h-4 text-amber-500 flex-shrink-0" /> : <Folder className="w-4 h-4 text-amber-500 flex-shrink-0" />)
    : getFileIcon(node.name);

  return (
    <>
      <div
        className={cn(
          'flex items-center gap-1.5 py-1 px-2 cursor-pointer rounded-sm transition-colors',
          variant === 'dark'
            ? 'hover:bg-surface-800'
            : 'hover:bg-surface-100 dark:hover:bg-surface-700/50'
        )}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        onClick={() => node.type === 'dir' ? setExpanded(!expanded) : onFileClick(fullPath)}
      >
        {node.type === 'dir' && (
          <ChevronRight className={cn('w-3 h-3 text-surface-400 transition-transform flex-shrink-0', expanded && 'rotate-90')} />
        )}
        {node.type === 'file' && <span className="w-3" />}
        {icon}
        <span className={cn(
          'truncate text-xs',
          variant === 'dark' ? 'text-surface-300' : 'text-surface-700 dark:text-surface-300'
        )}>{node.name}</span>
        {node.type === 'file' && node.size !== undefined && (
          <span className="ml-auto text-[10px] text-surface-500 flex-shrink-0">
            {formatSize(node.size)}
          </span>
        )}
      </div>
      {expanded && node.children?.map(child => (
        <TreeNode
          key={`${fullPath}/${child.name}`}
          node={child}
          depth={depth + 1}
          basePath={fullPath}
          onFileClick={onFileClick}
          variant={variant}
        />
      ))}
    </>
  );
}

interface FileExplorerProps {
  sessionId?: string;
  language: string;
  variant?: 'light' | 'dark';
}

export function FileExplorer({ sessionId: propSessionId, language, variant = 'light' }: FileExplorerProps) {
  const isDark = variant === 'dark';
  const [tree, setTree] = useState<FileNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [showDiag, setShowDiag] = useState(false);
  const [diag, setDiag] = useState<NfsDiagnostics | FallbackDiagnostics | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);

  const { currentSessionId } = useSessionStore();
  const workspaceRefreshTrigger = useBuilderStore(s => s.workspaceRefreshTrigger);

  const effectiveSessionId = propSessionId || currentSessionId || '';
  const ko = language === 'ko-KR';

  const loadDiagnostics = useCallback(async () => {
    setDiagLoading(true);
    const result = await fetchNfsDiagnostics();
    setDiag(result);
    setDiagLoading(false);
  }, []);

  const fetchTree = useCallback(async () => {
    if (!effectiveSessionId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchWorkspaceTree(effectiveSessionId);
      setTree(data);
      // Auto-fetch diagnostics when tree is empty (helps debug NFS issues)
      if (data.length === 0) {
        loadDiagnostics();
      }
    } catch (e) {
      setError(ko ? '파일 트리를 불러올 수 없습니다' : 'Failed to load file tree');
    } finally {
      setLoading(false);
    }
  }, [effectiveSessionId, ko, loadDiagnostics]);

  // Initial fetch
  useEffect(() => {
    fetchTree();
  }, [fetchTree]);

  // Auto-refresh on workspace_update events
  useEffect(() => {
    if (workspaceRefreshTrigger > 0) {
      fetchTree();
    }
  }, [workspaceRefreshTrigger, fetchTree]);

  const handleFileClick = useCallback((path: string) => {
    setSelectedFilePath(path);
  }, []);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className={cn(
        'flex items-center justify-between px-3 py-2 border-b flex-shrink-0',
        isDark ? 'border-surface-700' : 'border-surface-200 dark:border-surface-700'
      )}>
        <span className={cn(
          'text-sm font-medium',
          isDark ? 'text-surface-300' : 'text-surface-700 dark:text-surface-300'
        )}>
          {ko ? '워크스페이스' : 'Workspace'}
        </span>
        <button
          onClick={fetchTree}
          disabled={loading}
          className={cn(
            'p-1 transition-colors rounded',
            isDark ? 'text-surface-500 hover:text-surface-300' : 'text-surface-400 hover:text-surface-600 dark:hover:text-surface-300'
          )}
          title={ko ? '새로고침' : 'Refresh'}
        >
          <RefreshCw className={cn('w-3.5 h-3.5', loading && 'animate-spin')} />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto py-1">
        {loading && tree.length === 0 ? (
          <div className="flex items-center justify-center py-8 text-surface-400">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        ) : error ? (
          <div className="flex items-center gap-2 px-3 py-4 text-surface-400 text-xs">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            <span>{error}</span>
          </div>
        ) : tree.length === 0 ? (
          <div className={cn('px-3 py-4 text-xs text-center', isDark ? 'text-surface-500' : 'text-surface-400 dark:text-surface-500')}>
            <div>{ko ? '아직 파일이 없습니다' : 'No files yet'}</div>
            {/* NFS Diagnostics toggle */}
            <button
              onClick={() => { setShowDiag(v => !v); if (!diag && !diagLoading) loadDiagnostics(); }}
              className={cn(
                'mt-2 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded transition-colors',
                isDark
                  ? 'text-surface-500 hover:text-surface-400 hover:bg-surface-800'
                  : 'text-surface-400 hover:text-surface-600 hover:bg-surface-100'
              )}
            >
              <Bug className="w-3 h-3" />
              {showDiag ? 'Hide' : 'Debug'}
            </button>
            {showDiag && (
              <div className={cn(
                'mt-2 mx-1 p-2 rounded text-[10px] text-left font-mono leading-relaxed',
                isDark ? 'bg-surface-800 text-surface-400' : 'bg-surface-100 dark:bg-surface-800 text-surface-500'
              )}>
                <div className="font-semibold mb-1">NFS Diagnostics</div>
                {diagLoading ? (
                  <div className="flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> Loading...</div>
                ) : diag && 'fallback' in diag ? (
                  <>
                    <div className="text-amber-500">Fallback diagnostics (debug endpoint not deployed)</div>
                    <div>tree: {diag.treeOk ? <span className="text-green-500">OK ({diag.treeStatus})</span> : <span className="text-red-500">FAIL ({diag.treeStatus})</span>}</div>
                    {diag.treeBody && <div className="truncate">body: {diag.treeBody}</div>}
                    <div>ping: {diag.pingStatus != null ? diag.pingStatus : 'N/A'}</div>
                  </>
                ) : diag ? (
                  <>
                    <div>mount: <span className={diag.mount_exists ? 'text-green-500' : 'text-red-500'}>{diag.mount_path} {diag.mount_exists ? 'OK' : 'MISSING'}</span></div>
                    {diag.mount_exists && (
                      <div>root: [{diag.mount_contents?.join(', ') || 'empty'}]</div>
                    )}
                    <div>sessions/: <span className={diag.sessions_dir_exists ? 'text-green-500' : 'text-red-500'}>{diag.sessions_dir_exists ? 'OK' : 'MISSING'}</span></div>
                    <div>session dirs: {diag.session_dirs_count}</div>
                    {diag.recent_sessions?.length > 0 && (
                      <div className="mt-1">
                        <div className="font-semibold">Recent sessions:</div>
                        {diag.recent_sessions.map((s, i) => (
                          <div key={i} className={cn(
                            'truncate',
                            s === effectiveSessionId && 'text-green-500 font-semibold'
                          )}>
                            {s === effectiveSessionId ? '> ' : '  '}{s}
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="mt-1 border-t border-surface-300 dark:border-surface-700 pt-1">
                      looking for: <span className="break-all text-amber-400">{effectiveSessionId || '(none)'}</span>
                    </div>
                    {effectiveSessionId && diag.recent_sessions && !diag.recent_sessions.includes(effectiveSessionId) && (
                      <div className="text-red-400 mt-0.5">
                        Session dir NOT found on NFS
                      </div>
                    )}
                  </>
                ) : (
                  <div className="text-amber-500">{ko ? 'API 응답 없음 (백엔드 미배포?)' : 'No API response (backend not deployed?)'}</div>
                )}
                <button
                  onClick={loadDiagnostics}
                  disabled={diagLoading}
                  className="mt-1 text-[10px] underline hover:no-underline"
                >
                  {ko ? '재조회' : 'Refresh'}
                </button>
              </div>
            )}
          </div>
        ) : (
          tree.map(node => (
            <TreeNode
              key={node.name}
              node={node}
              depth={0}
              basePath=""
              onFileClick={handleFileClick}
              variant={variant}
            />
          ))
        )}
      </div>

      {/* File Viewer Modal */}
      {selectedFilePath && effectiveSessionId && (
        <FileViewer
          sessionId={effectiveSessionId}
          filePath={selectedFilePath}
          onClose={() => setSelectedFilePath(null)}
        />
      )}
    </div>
  );
}
