/**
 * Asset Workspace (split-view RIGHT pane)
 *
 * A peer view to ProgressSidebar. Classic asset tabs remain unchanged; ACXD
 * assets are surfaced in a dedicated group, with generated flow JSON rendered
 * as a deterministic React Flow diagram.
 */

import { useEffect, useMemo } from 'react';
import { Maximize2, X, FileText, Inbox } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { cn } from '../lib/utils';
import { AssetPreviewBubble } from './AssetPreviewBubble';
import { AcxdFlowDiagram } from './AcxdFlowDiagram';
import { TAB_META, itemLabel, groupAssetTabs, tabLabel } from '../lib/assetTabs';
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

  const tabs = useMemo(() => groupAssetTabs(assetPreviews), [assetPreviews]);
  const classicTabs = useMemo(
    () => tabs.filter((tab) => TAB_META[tab.tabId].group === 'classic'),
    [tabs],
  );
  const acxdTabs = useMemo(
    () => tabs.filter((tab) => TAB_META[tab.tabId].group === 'acxd'),
    [tabs],
  );

  const active = useMemo(() => {
    if (activeAssetKey && assetPreviews[activeAssetKey]) {
      const preview = assetPreviews[activeAssetKey];
      const tab = tabs.find((candidate) => candidate.items.some((item) => item.key === activeAssetKey));
      if (tab) return { tabId: tab.tabId, key: activeAssetKey, preview };
    }
    return tabs[0] ? { tabId: tabs[0].tabId, key: tabs[0].key, preview: tabs[0].preview } : null;
  }, [activeAssetKey, assetPreviews, tabs]);

  const activeTabItems = useMemo(() => {
    if (!active) return [];
    return tabs.find((tab) => tab.tabId === active.tabId)?.items || [];
  }, [tabs, active]);

  useEffect(() => {
    if (active && active.key !== activeAssetKey) setActiveAssetKey(active.key);
  }, [active, activeAssetKey, setActiveAssetKey]);

  const renderTab = (tabId: string, key: string, itemCount: number) => {
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
            : 'text-surface-500 dark:text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800',
        )}
        aria-pressed={isActive}
      >
        <Icon className="w-3.5 h-3.5" />
        {tabLabel(meta, language)}
        {itemCount > 1 && (
          <span className="ml-0.5 px-1 rounded-full text-[10px] leading-none py-0.5 bg-surface-200 dark:bg-surface-700 text-surface-600 dark:text-surface-300">
            {itemCount}
          </span>
        )}
      </button>
    );
  };

  const isFlowTab = active?.tabId === 'acxd_flows';

  return (
    <div className="bg-white dark:bg-surface-850 rounded-xl shadow-sm dark:shadow-none border border-surface-200 dark:border-surface-700 flex flex-col h-full overflow-hidden w-full">
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

      {tabs.length > 0 && (
        <div className="border-b border-surface-200 dark:border-surface-700 overflow-x-auto flex-shrink-0">
          {classicTabs.length > 0 && (
            <div className="flex items-center gap-1 px-3 py-2">
              {classicTabs.map(({ tabId, key, items }) => renderTab(tabId, key, items.length))}
            </div>
          )}
          {acxdTabs.length > 0 && (
            <div className="flex items-center gap-1 px-3 py-2 border-t border-surface-100 dark:border-surface-800 bg-fuchsia-50/50 dark:bg-fuchsia-950/15" data-testid="acxd-asset-tabs">
              <span className="mr-1 rounded-full border border-fuchsia-200 dark:border-fuchsia-800 bg-fuchsia-100 dark:bg-fuchsia-900/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-fuchsia-700 dark:text-fuchsia-200">
                ACXD
              </span>
              {acxdTabs.map(({ tabId, key, items }) => renderTab(tabId, key, items.length))}
            </div>
          )}
        </div>
      )}

      {(activeTabItems.length > 1 || isFlowTab) && (
        <div
          className="flex items-center gap-1 px-3 py-1.5 border-b border-surface-200 dark:border-surface-700 overflow-x-auto flex-shrink-0 bg-surface-50 dark:bg-surface-900/40"
          aria-label={isFlowTab ? (ko ? 'ACXD 플로우 목록' : 'ACXD flow list') : undefined}
        >
          {isFlowTab && (
            <span className="mr-1 text-[10px] font-semibold uppercase tracking-wide text-surface-400 dark:text-surface-500">
              {ko ? '플로우' : 'Flows'}
            </span>
          )}
          {activeTabItems.map(({ key, preview }) => {
            const isActive = active?.key === key;
            const label = itemLabel(preview) || (ko ? '이름 없는 에셋' : 'Untitled asset');
            return (
              <button
                key={key}
                onClick={() => setActiveAssetKey(key)}
                className={cn(
                  'px-2 py-1 rounded-md text-[11px] font-mono whitespace-nowrap transition-colors',
                  isActive
                    ? 'bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300'
                    : 'text-surface-500 dark:text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800',
                )}
                aria-pressed={isActive}
                title={label}
              >
                {label}
              </button>
            );
          })}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-3">
        {active ? (
          <>
            {active.preview.language && active.tabId !== 'contact_flow' && active.tabId !== 'acxd_flows' && (
              <div className="mb-2 flex items-center gap-2">
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wide bg-surface-100 dark:bg-surface-800 text-surface-500 dark:text-surface-400 border border-surface-200 dark:border-surface-700">
                  <FileText className="w-3 h-3" />
                  {active.preview.language}
                </span>
              </div>
            )}
            {isFlowTab ? (
              <AcxdFlowDiagram flowJson={active.preview.content} language={language} />
            ) : (
              <AssetPreviewBubble preview={active.preview} language={language} />
            )}
          </>
        ) : (
          <div className="flex flex-col items-center justify-center h-full text-center text-surface-400 dark:text-surface-500 gap-2 py-12">
            <Inbox className="w-8 h-8" />
            <p className="text-sm">{ko ? '아직 생성된 에셋이 없습니다.' : 'No assets generated yet.'}</p>
          </div>
        )}
      </div>
    </div>
  );
}
