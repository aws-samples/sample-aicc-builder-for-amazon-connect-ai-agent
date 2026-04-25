/**
 * AICC Builder - Main Application
 *
 * AI-powered customization platform for Amazon Connect workshops
 * Using Amazon Bedrock (Claude) with Cognito authentication
 */

import { useEffect, useState, useCallback, Component, type ErrorInfo, type ReactNode } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Header } from './components/Header';
import { ChatWindow } from './components/ChatWindow';
import { ProgressSidebar } from './components/ProgressSidebar';
import { SessionSidebar } from './components/SessionSidebar';
import { LoginPage } from './pages/LoginPage';
import { useBuilderStore } from './stores/builderStore';
import { useSessionStore } from './stores/sessionStore';
import { useAuthStore } from './stores/authStore';
import { useWebSocket } from './hooks/useWebSocket';
import { getSessionHistory } from './services/sessions';
import { Loader2, AlertTriangle } from 'lucide-react';

// Error Boundary to prevent white-screen crashes
class ErrorBoundary extends Component<
  { children: ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: { children: ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('[ErrorBoundary] Uncaught error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-surface-50 dark:bg-surface-950 flex items-center justify-center p-8">
          <div className="max-w-md text-center space-y-4">
            <AlertTriangle className="w-12 h-12 text-amber-500 mx-auto" />
            <h1 className="text-xl font-semibold text-surface-900 dark:text-surface-100">
              Something went wrong
            </h1>
            <p className="text-sm text-surface-500 dark:text-surface-400">
              {this.state.error?.message || 'An unexpected error occurred'}
            </p>
            <button
              onClick={() => {
                this.setState({ hasError: false, error: null });
                window.location.reload();
              }}
              className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
            >
              Reload Page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// Protected Route wrapper
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuthStore();

  if (isLoading) {
    return (
      <div className="min-h-screen bg-surface-50 dark:bg-surface-950 flex items-center justify-center transition-colors">
        <Loader2 className="w-8 h-8 animate-spin text-primary-600 dark:text-primary-400" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function App() {
  const { checkAuth } = useAuthStore();
  const { theme } = useBuilderStore();

  // Apply theme on mount and when theme changes
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') {
      const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      root.classList.toggle('dark', systemDark);

      // Listen for system theme changes
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
      const handleChange = (e: MediaQueryListEvent) => {
        root.classList.toggle('dark', e.matches);
      };
      mediaQuery.addEventListener('change', handleChange);
      return () => mediaQuery.removeEventListener('change', handleChange);
    } else {
      root.classList.toggle('dark', theme === 'dark');
    }
  }, [theme]);

  // Check authentication on mount
  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  return (
    <ErrorBoundary>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/*"
          element={
            <ProtectedRoute>
              <div className="h-screen flex flex-col bg-surface-50 dark:bg-surface-950 overflow-hidden transition-colors">
                <Header />
                <Routes>
                  <Route path="/" element={<BuilderPage />} />
                  <Route path="/builder" element={<Navigate to="/" replace />} />
                </Routes>
              </div>
            </ProtectedRoute>
          }
        />
      </Routes>
    </ErrorBoundary>
  );
}

function BuilderPage() {
  const { createNewSession, setCurrentSession, currentSessionId: storeSessionId, loadSessions } = useSessionStore();
  const { switchSession, getCurrentSessionId } = useWebSocket();
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [isInitialized, setIsInitialized] = useState(false);

  // Initialize session on page load
  useEffect(() => {
    if (isInitialized) return;

    const initSession = async () => {
      // Load sessions first
      await loadSessions();

      // Get session ID from WebSocket hook (which reads from localStorage)
      const sessionId = getCurrentSessionId();
      console.log("[BuilderPage] Init session ID:", sessionId);

      if (sessionId) {
        setCurrentSessionId(sessionId);
        setCurrentSession(sessionId);

        // Check if this is an existing session with history
        console.log("[BuilderPage] Checking for existing history before connecting...");
        const existingHistory = await getSessionHistory(sessionId);
        const hasExistingHistory = existingHistory && existingHistory.length > 0;

        // Use switchSession which properly handles:
        // - Session state reset
        // - WebSocket connection
        // - History injection
        // - Session ready state
        await switchSession(sessionId, !hasExistingHistory);
      }

      setIsInitialized(true);
    };

    initSession();
  }, [isInitialized, loadSessions, getCurrentSessionId, setCurrentSession, switchSession]);

  // Sync store session ID with local state
  useEffect(() => {
    if (storeSessionId && storeSessionId !== currentSessionId) {
      setCurrentSessionId(storeSessionId);
    }
  }, [storeSessionId, currentSessionId]);

  // Handle session selection from sidebar
  const handleSessionSelect = useCallback(
    async (sessionId: string, isNew: boolean) => {
      console.log("[BuilderPage] Session selected:", sessionId, "isNew:", isNew);

      // Update local state and store
      setCurrentSessionId(sessionId);
      setCurrentSession(sessionId);

      // If it's a new session, create it in the backend
      if (isNew) {
        await createNewSession(sessionId);
      }

      // Switch the WebSocket connection to the new session
      // This will reset all session-specific state (messages, assets, progress, etc.)
      await switchSession(sessionId, isNew);
    },
    [createNewSession, setCurrentSession, switchSession]
  );

  return (
    <main className="flex-1 flex overflow-hidden">
      {/* Session Sidebar */}
      <SessionSidebar
        onSessionSelect={handleSessionSelect}
        currentSessionId={currentSessionId}
      />

      {/* Main Content Area - flex-1 takes remaining space, overflow-hidden for child scrolling */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <div className="flex-1 flex gap-4 lg:gap-6 p-4 lg:p-6 overflow-hidden">
          {/* Chat Area - min-w-0 prevents flex item from growing beyond container */}
          <div className="flex-1 min-w-0 flex flex-col overflow-hidden">
            <ChatWindow />
          </div>

          {/* Progress Sidebar - hidden on small screens, flex-shrink-0 ensures it never shrinks */}
          <div className="hidden lg:block flex-shrink-0">
            <ProgressSidebar />
          </div>
        </div>
      </div>
    </main>
  );
}

export default App;
