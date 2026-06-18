/**
 * Asset Fullscreen Modal
 *
 * Zoomed, full-screen overlay for a single asset preview. Reuses
 * AssetPreviewBubble (which delegates contact flows to ContactFlowPreview).
 */

import { useCallback, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import type { Language } from '../types';
import { AssetPreviewBubble } from './AssetPreviewBubble';

interface AssetFullscreenModalProps {
  language: Language;
}

export function AssetFullscreenModal({ language }: AssetFullscreenModalProps) {
  const fullscreenAssetKey = useBuilderStore((s) => s.fullscreenAssetKey);
  const setFullscreenAssetKey = useBuilderStore((s) => s.setFullscreenAssetKey);
  const assetPreviews = useBuilderStore((s) => s.assetPreviews);

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

  if (!fullscreenAssetKey) return null;
  const preview = assetPreviews[fullscreenAssetKey];
  if (!preview) return null;

  const ko = language === 'ko-KR';

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/70 backdrop-blur-sm animate-in fade-in duration-200 p-4 lg:p-8"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative w-full max-w-6xl max-h-[92vh] bg-white dark:bg-surface-850 rounded-2xl shadow-2xl border border-surface-200 dark:border-surface-700 flex flex-col overflow-hidden animate-in zoom-in-95 duration-200">
        <button
          onClick={onClose}
          className="absolute top-3 right-3 z-10 p-1.5 rounded-lg text-surface-400 hover:text-surface-600 dark:text-surface-500 dark:hover:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800 transition-colors"
          aria-label={ko ? '닫기' : 'Close'}
        >
          <X className="w-5 h-5" />
        </button>
        <div className="overflow-y-auto p-5">
          <AssetPreviewBubble preview={preview} language={language} />
        </div>
      </div>
    </div>,
    document.body
  );
}
