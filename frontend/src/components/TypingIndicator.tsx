/**
 * Typing Indicator Component
 *
 * Shows animated dots when the agent is thinking
 */

export function TypingIndicator() {
  return (
    <div className="flex justify-start animate-fade-in">
      <div className="bg-gray-100 rounded-2xl px-4 py-3">
        <div className="flex items-center gap-1">
          <span className="typing-dot w-2 h-2 rounded-full bg-gray-400"></span>
          <span className="typing-dot w-2 h-2 rounded-full bg-gray-400"></span>
          <span className="typing-dot w-2 h-2 rounded-full bg-gray-400"></span>
        </div>
      </div>
    </div>
  );
}
