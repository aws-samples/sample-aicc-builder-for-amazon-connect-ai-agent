/**
 * Right Pane — resizable split between the chat and a peer view.
 *
 * Hosts a two-tab switcher (Progress · Assets), a drag handle to resize the
 * pane width, the ProgressSidebar (progress + notes), and the AssetWorkspace
 * (the split-view asset surface). Asset markers in chat call setActiveAssetKey
 * which flips rightPaneView to 'assets' and reveals the workspace.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ListChecks, LayoutPanelLeft } from 'lucide-react';
import { useBuilderStore } from '../stores/builderStore';
import { cn } from '../lib/utils';
import { ProgressSidebar } from './ProgressSidebar';
import { AssetWorkspace } from './AssetWorkspace';
import { AssetFullscreenModal } from './AssetFullscreenModal';

const MIN_WIDTH = 320;
const MAX_WIDTH = 760;
const DEFAULT_WIDTH = 420;

export function RightPane() {
  const language = useBuilderStore((s) => s.language);
  const rightPaneView = useBuilderStore((s) => s.rightPaneView);
  const setRightPaneView = useBuilderStore((s) => s.setRightPaneView);
  const setActiveAssetKey = useBuilderStore((s) => s.setActiveAssetKey);
  const ko = language === 'ko-KR';

  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const dragging = useRef(false);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      // Pane is on the right, so width grows as the cursor moves left.
      const next = window.innerWidth - e.clientX;
      setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, next)));
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  // The ProgressSidebar manages its own width when collapsed; only constrain
  // width for the asset workspace view.
  const isAssets = rightPaneView === 'assets';

  return (
    <div className="hidden lg:flex flex-shrink-0 h-full">
      {/* Drag handle (only meaningful for the asset workspace) */}
      {isAssets && (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={ko ? '패널 크기 조절' : 'Resize panel'}
          onMouseDown={onMouseDown}
          className="w-1.5 cursor-col-resize group flex items-center justify-center"
        >
          <div className="w-0.5 h-10 rounded-full bg-surface-300 dark:bg-surface-600 group-hover:bg-primary-500 transition-colors" />
        </div>
      )}

      <div
        className="flex flex-col h-full"
        style={isAssets ? { width } : undefined}
      >
        {/* View switcher tabs */}
        <div className="flex items-center gap-1 mb-2 flex-shrink-0">
          <button
            onClick={() => setRightPaneView('progress')}
            aria-pressed={rightPaneView === 'progress'}
            className={cn(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
              rightPaneView === 'progress'
                ? 'bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300'
                : 'text-surface-500 dark:text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800'
            )}
          >
            <ListChecks className="w-4 h-4" />
            {ko ? '진행 상황' : 'Progress'}
          </button>
          <button
            onClick={() => {
              setRightPaneView('assets');
              // Default-select the first available asset if none is active.
              if (!useBuilderStore.getState().activeAssetKey) {
                const keys = Object.keys(useBuilderStore.getState().assetPreviews);
                if (keys.length > 0) setActiveAssetKey(keys[keys.length - 1]);
              }
            }}
            aria-pressed={rightPaneView === 'assets'}
            className={cn(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors',
              rightPaneView === 'assets'
                ? 'bg-primary-100 dark:bg-primary-900/40 text-primary-700 dark:text-primary-300'
                : 'text-surface-500 dark:text-surface-400 hover:bg-surface-100 dark:hover:bg-surface-800'
            )}
          >
            <LayoutPanelLeft className="w-4 h-4" />
            {ko ? '에셋' : 'Assets'}
          </button>
        </div>

        <div className="flex-1 min-h-0">
          {rightPaneView === 'assets' ? (
            <AssetWorkspace language={language} onClose={() => setRightPaneView('progress')} />
          ) : (
            <ProgressSidebar />
          )}
        </div>
      </div>

      <AssetFullscreenModal language={language} />
    </div>
  );
}
