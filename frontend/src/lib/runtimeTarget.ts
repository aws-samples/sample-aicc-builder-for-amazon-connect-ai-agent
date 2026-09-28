import type { RuntimeTarget } from '../types';

export const RUNTIME_TARGETS = ['classic', 'acxd', 'acxd_only'] as const satisfies readonly RuntimeTarget[];

/** True for a value the backend may echo as a session's runtime target. */
export function isRuntimeTarget(value: unknown): value is RuntimeTarget {
  return typeof value === 'string' && (RUNTIME_TARGETS as readonly string[]).includes(value);
}

/** Both ACXD targets build an Agentic CX Designer application. */
export function isAcxdRuntime(target: RuntimeTarget): boolean {
  return target === 'acxd' || target === 'acxd_only';
}

/**
 * Progress steps that never run for a target. ACXD replaces the AI Prompt with
 * the ACXD application; ACXD only (v3.1) also builds no database, backend
 * (Lambda, OpenAPI, CloudFormation) or Contact Flow.
 */
const HIDDEN_PROGRESS_STEPS: Record<RuntimeTarget, ReadonlySet<string>> = {
  classic: new Set(['acxd_application']),
  acxd: new Set(['prompt']),
  acxd_only: new Set(['prompt', 'database', 'lambda', 'openapi', 'contact_flow', 'cdk']),
};

export function isProgressStepForTarget(stepId: string, target: RuntimeTarget): boolean {
  return !(HIDDEN_PROGRESS_STEPS[target] ?? HIDDEN_PROGRESS_STEPS.classic).has(stepId);
}

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
