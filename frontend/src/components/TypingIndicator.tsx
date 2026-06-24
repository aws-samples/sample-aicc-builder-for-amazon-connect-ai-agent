/**
 * Typing Indicator Component
 *
 * Shows animated dots when the agent is thinking
 */

export function TypingIndicator() {
  return (
    <div className="flex justify-start animate-fade-in">
      <div className="bg-surface-100 dark:bg-surface-800 rounded-2xl px-4 py-3">
        <div className="flex items-center gap-1">
          <span className="typing-dot w-2 h-2 rounded-full bg-surface-400 dark:bg-surface-500"></span>
          <span className="typing-dot w-2 h-2 rounded-full bg-surface-400 dark:bg-surface-500"></span>
          <span className="typing-dot w-2 h-2 rounded-full bg-surface-400 dark:bg-surface-500"></span>
        </div>
      </div>
    </div>
  );
}
