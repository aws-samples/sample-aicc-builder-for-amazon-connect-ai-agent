/**
 * Model Selector
 *
 * A compact, themed dropdown chip (zap icon + label + chevron) for picking the
 * Claude model. Switchable mid-session. Used in the Header and on the start
 * screen composer toolbar. Persists the choice via the builder store
 * (localStorage), mirroring how `language` is handled.
 */

import { useEffect, useRef, useState } from 'react';
import { Zap, ChevronDown, Check, Gauge } from 'lucide-react';
import { useBuilderStore, MODELS, EFFORT_LEVELS } from '../stores/builderStore';
import type { Language } from '../types';
import { cn } from '../lib/utils';

interface ModelSelectorProps {
  language: Language;
  /** Visual variant: 'header' for the dark header bar, 'composer' for light/dark surfaces. */
  variant?: 'header' | 'composer';
  className?: string;
}

const STRINGS: Record<Language, { label: string; aria: string; effort: string; effortDefault: string }> = {
  'en-US': { label: 'Model', aria: 'Select Claude model', effort: 'Effort', effortDefault: 'Default (model decides)' },
  'ko-KR': { label: '모델', aria: 'Claude 모델 선택', effort: '추론 강도 (Effort)', effortDefault: '기본값 (모델 판단)' },
  'ja-JP': { label: 'モデル', aria: 'Claudeモデルを選択', effort: '推論強度 (Effort)', effortDefault: 'デフォルト' },
};

export function ModelSelector({ language, variant = 'header', className }: ModelSelectorProps) {
  const selectedModel = useBuilderStore((s) => s.selectedModel);
  const setSelectedModel = useBuilderStore((s) => s.setSelectedModel);
  const selectedEffort = useBuilderStore((s) => s.selectedEffort);
  const setSelectedEffort = useBuilderStore((s) => s.setSelectedEffort);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const current = MODELS.find((m) => m.id === selectedModel) || MODELS[0];
  const currentEffort = EFFORT_LEVELS.find((e) => e.id === selectedEffort) || EFFORT_LEVELS[0];
  const t = STRINGS[language] || STRINGS['en-US'];

  // Close on outside click / Escape
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const triggerClasses =
    variant === 'header'
      ? 'bg-surface-800 hover:bg-surface-700 text-surface-300 hover:text-white'
      : cn(
          'border border-surface-300 dark:border-surface-600',
          'bg-white dark:bg-surface-800 text-surface-600 dark:text-surface-300',
          'hover:bg-surface-50 dark:hover:bg-surface-700'
        );

  return (
    <div className={cn('relative', className)} ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={t.aria}
        title={`${t.label}: ${current.label}`}
        className={cn(
          'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs lg:text-sm transition-colors',
          triggerClasses
        )}
      >
        <Zap className="w-4 h-4 text-primary-500 dark:text-primary-400" />
        <span className="font-medium">
          {current.label}
          {selectedEffort !== 'default' && (
            <span className="opacity-70"> · {currentEffort.label}</span>
          )}
        </span>
        <ChevronDown className={cn('w-3.5 h-3.5 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div
          role="listbox"
          aria-label={t.aria}
          className={cn(
            'absolute right-0 z-50 mt-1 min-w-[12rem] rounded-lg overflow-hidden',
            'border border-surface-200 dark:border-surface-700',
            'bg-white dark:bg-surface-850 shadow-lg'
          )}
        >
          {MODELS.map((m) => {
            const active = m.id === selectedModel;
            return (
              <button
                key={m.id}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  setSelectedModel(m.id);
                  setOpen(false);
                }}
                className={cn(
                  'w-full flex items-center justify-between gap-2 px-3 py-2 text-sm text-left transition-colors',
                  active
                    ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300'
                    : 'text-surface-700 dark:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800'
                )}
              >
                <span className="flex items-center gap-2">
                  <Zap className="w-3.5 h-3.5 opacity-70" />
                  {m.label}
                </span>
                {active && <Check className="w-4 h-4" />}
              </button>
            );
          })}

          {/* Effort control — Anthropic output_config.effort (Claude 4.6+) */}
          <div
            className="px-3 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wide
                       text-surface-400 dark:text-surface-500 border-t border-surface-200 dark:border-surface-700
                       flex items-center gap-1.5"
          >
            <Gauge className="w-3 h-3" />
            {t.effort}
          </div>
          {EFFORT_LEVELS.map((e) => {
            const active = e.id === selectedEffort;
            const label = e.id === 'default' ? t.effortDefault : e.label;
            return (
              <button
                key={e.id}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  setSelectedEffort(e.id);
                  setOpen(false);
                }}
                className={cn(
                  'w-full flex items-center justify-between gap-2 px-3 py-1.5 text-sm text-left transition-colors',
                  active
                    ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-700 dark:text-primary-300'
                    : 'text-surface-700 dark:text-surface-300 hover:bg-surface-100 dark:hover:bg-surface-800'
                )}
              >
                <span>{label}</span>
                {active && <Check className="w-4 h-4" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
