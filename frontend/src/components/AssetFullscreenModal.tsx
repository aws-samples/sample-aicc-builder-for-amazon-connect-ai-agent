/**
 * Asset Fullscreen Modal
 *
 * Zoomed, full-screen overlay with a browser-style two-pane layout:
 *  - LEFT: a list of ALL produced assets, grouped by type (Contact Flow, AI
 *    Prompt, FAQ, OpenAPI, Lambda, Infrastructure). Every file is clickable, so
 *    the user can switch between assets without leaving fullscreen.
 *  - RIGHT: the selected asset's content (reuses AssetPreviewBubble, which
 *    delegates contact flows to ContactFlowPreview).
 *
 * The selected asset IS `fullscreenAssetKey`; clicking a list item just points
 * it at a different key. The modal is open whenever that key is non-null.
 */

import { useCallback, useEffect, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { cn } from '../lib/utils';
import { TAB_META, tabIdFor, itemLabel, groupAssetTabs } from '../lib/assetTabs';
import type { Language } from '../types';
import { AssetPreviewBubble } from './AssetPreviewBubble';

interface AssetFullscreenModalProps {
  language: Language;
}

export function AssetFullscreenModal({ language }: AssetFullscreenModalProps) {
  const fullscreenAssetKey = useBuilderStore((s) => s.fullscreenAssetKey);
  const setFullscreenAssetKey = useBuilderStore((s) => s.setFullscreenAssetKey);
  const assetPreviews = useBuilderStore((s) => s.assetPreviews);
  const ko = language === 'ko-KR';

  const onClose = useCallback(() => setFullscreenAssetKey(null), [setFullscreenAssetKey]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    },
    [onClose]
  );

  useEffect(() => {
    if (!fullscreenAssetKey) return;
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [fullscreenAssetKey, handleKeyDown]);

  // All assets grouped by type — the same grouping the right-pane workspace uses.
  const tabs = useMemo(() => groupAssetTabs(assetPreviews), [assetPreviews]);

  // The selected asset. If the key no longer resolves (e.g. asset replaced),
  // fall back to the first item so the modal never renders empty while open.
  const selectedKey =
    fullscreenAssetKey && assetPreviews[fullscreenAssetKey]
      ? fullscreenAssetKey
      : tabs[0]?.key ?? null;
  const selectedTabId = selectedKey ? tabIdFor(assetPreviews[selectedKey]?.assetType || '') : null;

  if (!fullscreenAssetKey) return null;
  const preview = selectedKey ? assetPreviews[selectedKey] : undefined;
  if (!preview) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 backdrop-blur-sm animate-in fade-in duration-200 p-4 lg:p-8"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative w-full max-w-7xl h-[92vh] bg-white dark:bg-surface-850 rounded-2xl shadow-2xl border border-surface-200 dark:border-surface-700 flex flex-col overflow-hidden animate-in zoom-in-95 duration-200">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-surface-200 dark:border-surface-700 flex-shrink-0">
          <h2 className="font-semibold text-surface-900 dark:text-surface-100 text-sm">
            {ko ? '에셋 워크스페이스' : 'Asset Workspace'}
          </h2>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-surface-400 hover:text-surface-600 dark:text-surface-500 dark:hover:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800 transition-colors"
            aria-label={ko ? '닫기' : 'Close'}
            title={ko ? '닫기' : 'Close'}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Two-pane body: asset list (left) + selected content (right) */}
        <div className="flex flex-1 min-h-0">
          {/* LEFT: all assets, grouped by type, every file clickable */}
          <nav
            data-testid="asset-fullscreen-nav"
            className="w-60 lg:w-72 flex-shrink-0 border-r border-surface-200 dark:border-surface-700 overflow-y-auto p-2 bg-surface-50 dark:bg-surface-900/40"
          >
            {tabs.map(({ tabId, items }) => {
              const meta = TAB_META[tabId];
              const Icon = meta.icon;
              return (
                <div key={tabId} className="mb-2">
                  {/* Type group header */}
                  <div className="flex items-center gap-1.5 px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-surface-500 dark:text-surface-400">
                    <Icon className="w-3.5 h-3.5" />
                    {ko ? meta.labelKo : meta.label}
                    {items.length > 1 && (
                      <span className="ml-0.5 px-1 rounded-full text-[10px] leading-none py-0.5 bg-surface-200 dark:bg-surface-700 text-surface-600 dark:text-surface-300">
                        {items.length}
                      </span>
                    )}
                  </div>
                  {/* Per-file items */}
                  <div className="flex flex-col gap-0.5">
                    {items.map(({ key, preview: p }) => {
                      const isActive = key === selectedKey;
                      const label = itemLabel(p) || (ko ? meta.labelKo : meta.label);
                      return (
                        <button
                          key={key}
                          onClick={() => setFullscreenAssetKey(key)}
                          className={cn(
                            'text-left px-2.5 py-1.5 rounded-md text-xs font-mono truncate transition-colors',
                            isActive
                              ? 'bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300 font-medium'
                              : 'text-surface-600 dark:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800'
                          )}
                          aria-pressed={isActive}
                          title={label}
                        >
                          {label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </nav>

          {/* RIGHT: selected asset content */}
          <div data-testid="asset-fullscreen-content" className="flex-1 min-w-0 overflow-y-auto p-5">
            {preview.language && selectedTabId !== 'contact_flow' && (
              <div className="mb-2 flex items-center gap-2">
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wide bg-surface-100 dark:bg-surface-800 text-surface-500 dark:text-surface-400 border border-surface-200 dark:border-surface-700">
                  {preview.language}
                </span>
              </div>
            )}
            <AssetPreviewBubble preview={preview} language={language} />
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}
