/**
 * Asset Workspace (split-view RIGHT pane)
 *
 * When an asset streams/completes, this pane shows it in a focused, larger
 * surface than the inline chat marker. It has:
 *  - an asset-tab strip (only the in-scope / produced asset types)
 *  - reuses ContactFlowPreview (interactive diagram + JSON) for contact flows
 *  - a language badge + readable code for other asset types
 *  - download + fullscreen controls
 *
 * It is a peer view to ProgressSidebar — App.tsx toggles between them.
 */

import { useEffect, useMemo } from 'react';
import { Maximize2, X, FileText, Inbox } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { cn } from '../lib/utils';
import { AssetPreviewBubble } from './AssetPreviewBubble';
import { TAB_META, tabIdFor, itemLabel, groupAssetTabs } from '../lib/assetTabs';
import type { Language } from '../types';

interface AssetWorkspaceProps {
  language: Language;
  onClose: () => void;
}

export function AssetWorkspace({ language, onClose }: AssetWorkspaceProps) {
  const assetPreviews = useBuilderStore((s) => s.assetPreviews);
  const activeAssetKey = useBuilderStore((s) => s.activeAssetKey);
  const setActiveAssetKey = useBuilderStore((s) => s.setActiveAssetKey);
  const setFullscreenAssetKey = useBuilderStore((s) => s.setFullscreenAssetKey);
  const ko = language === 'ko-KR';

  // Group ALL previews by tab id (a type can have several assets — e.g. one
  // Lambda per operation). Each tab keeps its full item list (newest first) so
  // a sub-selector can expose every file; the tab's representative is the newest.
  const tabs = useMemo(() => groupAssetTabs(assetPreviews), [assetPreviews]);

  // Resolve the active preview from the active key, else fall back to first tab.
  const active = useMemo(() => {
    if (activeAssetKey && assetPreviews[activeAssetKey]) {
      const preview = assetPreviews[activeAssetKey];
      const tabId = tabIdFor(preview.assetType);
      if (tabId) return { tabId, key: activeAssetKey, preview };
    }
    return tabs[0] ? { tabId: tabs[0].tabId, key: tabs[0].key, preview: tabs[0].preview } : null;
  }, [activeAssetKey, assetPreviews, tabs]);

  // Items belonging to the active tab (for the per-tab sub-selector).
  const activeTabItems = useMemo(() => {
    if (!active) return [];
    return tabs.find((t) => t.tabId === active.tabId)?.items || [];
  }, [tabs, active]);

  // Keep activeAssetKey pointing at a valid preview.
  useEffect(() => {
    if (active && active.key !== activeAssetKey) {
      setActiveAssetKey(active.key);
    }
  }, [active, activeAssetKey, setActiveAssetKey]);

  return (
    <div className="bg-white dark:bg-surface-850 rounded-xl shadow-sm dark:shadow-none border border-surface-200 dark:border-surface-700 flex flex-col h-full overflow-hidden w-full">
      {/* Header + controls */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-surface-200 dark:border-surface-700 flex-shrink-0">
        <h2 className="font-semibold text-surface-900 dark:text-surface-100 text-sm">
          {ko ? '에셋 워크스페이스' : 'Asset Workspace'}
        </h2>
        <div className="flex items-center gap-1">
          {active && (
            <button
              onClick={() => setFullscreenAssetKey(active.key)}
              className="p-1.5 rounded-md text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800 transition-colors"
              aria-label={ko ? '전체 화면' : 'Fullscreen'}
              title={ko ? '전체 화면' : 'Fullscreen'}
            >
              <Maximize2 className="w-4 h-4" />
            </button>
          )}
          <button
            onClick={onClose}
            className="p-1.5 rounded-md text-surface-400 hover:text-surface-600 dark:hover:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800 transition-colors"
            aria-label={ko ? '닫기' : 'Close'}
            title={ko ? '닫기' : 'Close'}
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Asset-tab strip */}
      {tabs.length > 0 && (
        <div className="flex items-center gap-1 px-3 py-2 border-b border-surface-200 dark:border-surface-700 overflow-x-auto flex-shrink-0">
          {tabs.map(({ tabId, key, items }) => {
            const meta = TAB_META[tabId];
            const Icon = meta.icon;
            const isActive = active?.tabId === tabId;
            return (
              <button
                key={tabId}
                onClick={() => setActiveAssetKey(key)}
                className={cn(
                  'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-colors',
                  isActive
                    ? 'bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300'
                    : 'text-surface-500 dark:text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800'
                )}
                aria-pressed={isActive}
              >
                <Icon className="w-3.5 h-3.5" />
                {ko ? meta.labelKo : meta.label}
                {items.length > 1 && (
                  <span className="ml-0.5 px-1 rounded-full text-[10px] leading-none py-0.5 bg-surface-200 dark:bg-surface-700 text-surface-600 dark:text-surface-300">
                    {items.length}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* Per-tab sub-selector: when a type has multiple assets (e.g. one Lambda
          per operation), let the user pick which file. Hidden for single-asset tabs. */}
      {activeTabItems.length > 1 && (
        <div className="flex items-center gap-1 px-3 py-1.5 border-b border-surface-200 dark:border-surface-700 overflow-x-auto flex-shrink-0 bg-surface-50 dark:bg-surface-900/40">
          {activeTabItems.map(({ key, preview }) => {
            const isActive = active?.key === key;
            return (
              <button
                key={key}
                onClick={() => setActiveAssetKey(key)}
                className={cn(
                  'px-2 py-1 rounded-md text-[11px] font-mono whitespace-nowrap transition-colors',
                  isActive
                    ? 'bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300'
                    : 'text-surface-500 dark:text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800'
                )}
                aria-pressed={isActive}
                title={itemLabel(preview)}
              >
                {itemLabel(preview)}
              </button>
            );
          })}
        </div>
      )}

      {/* Active asset body */}
      <div className="flex-1 overflow-y-auto p-3">
        {active ? (
          <>
            {active.preview.language && active.tabId !== 'contact_flow' && (
              <div className="mb-2 flex items-center gap-2">
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wide bg-surface-100 dark:bg-surface-800 text-surface-500 dark:text-surface-400 border border-surface-200 dark:border-surface-700">
                  <FileText className="w-3 h-3" />
                  {active.preview.language}
                </span>
              </div>
            )}
            <AssetPreviewBubble preview={active.preview} language={language} />
          </>
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-center text-surface-400 dark:text-surface-500 gap-2 py-12">
            <Inbox className="w-8 h-8" />
            <p className="text-sm">
              {ko ? '아직 생성된 에셋이 없습니다.' : 'No assets generated yet.'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
