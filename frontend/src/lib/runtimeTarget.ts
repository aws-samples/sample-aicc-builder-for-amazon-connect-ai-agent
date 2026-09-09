import type { RuntimeTarget } from '../types';

export const RUNTIME_TARGETS = ['classic', 'acxd'] as const satisfies readonly RuntimeTarget[];

/**
 * Returns the target selected by the standard radio-group arrow/Home/End keys.
 * A null result means the caller should preserve the browser's default behavior.
 */
export function runtimeTargetForKey(
  current: RuntimeTarget,
  key: string,
): RuntimeTarget | null {
  const currentIndex = RUNTIME_TARGETS.indexOf(current);

  switch (key) {
    case 'ArrowLeft':
    case 'ArrowUp':
      return RUNTIME_TARGETS[(currentIndex - 1 + RUNTIME_TARGETS.length) % RUNTIME_TARGETS.length];
    case 'ArrowRight':
    case 'ArrowDown':
      return RUNTIME_TARGETS[(currentIndex + 1) % RUNTIME_TARGETS.length];
    case 'Home':
      return RUNTIME_TARGETS[0];
    case 'End':
      return RUNTIME_TARGETS[RUNTIME_TARGETS.length - 1];
    default:
      return null;
  }
}
