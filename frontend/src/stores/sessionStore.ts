/**
 * Session Store for AICC Builder
 *
 * Manages chat session state including:
 * - List of user's sessions
 * - Current active session
 * - Session CRUD operations
 */

import { create } from "zustand";
import {
  ChatSession,
  listSessions,
  createSession,
  updateSession,
  deleteSession,
  generateTitleFromMessage,
} from "../services/sessions";

interface SessionState {
  // State
  sessions: ChatSession[];
  currentSessionId: string | null;
  isLoading: boolean;
  error: string | null;

  // Actions
  loadSessions: () => Promise<void>;
  setCurrentSession: (sessionId: string | null) => void;
  createNewSession: (sessionId: string, title?: string) => Promise<ChatSession | null>;
  updateSessionTitle: (sessionId: string, title: string) => Promise<void>;
  updateSessionActivity: (sessionId: string, messageCount?: number) => Promise<void>;
  deleteSessionById: (sessionId: string) => Promise<boolean>;
  deleteAllSessions: () => Promise<void>;
  getCurrentSession: () => ChatSession | undefined;
  clearSessions: () => void;
}

export const useSessionStore = create<SessionState>((set, get) => ({
  // Initial state
  sessions: [],
  currentSessionId: null,
  isLoading: false,
  error: null,

  // Load all sessions for current user
  loadSessions: async () => {
    set({ isLoading: true, error: null });

    try {
      const sessions = await listSessions();
      set({ sessions, isLoading: false });
    } catch (error) {
      set({
        error: error instanceof Error ? error.message : "Failed to load sessions",
        isLoading: false,
      });
    }
  },

  // Set current active session
  setCurrentSession: (sessionId: string | null) => {
    set({ currentSessionId: sessionId });
  },

  // Create a new session
  createNewSession: async (sessionId: string, title: string = "New Chat") => {
    set({ isLoading: true, error: null });

    try {
      const session = await createSession(sessionId, title);

      if (session) {
        set((state) => ({
          sessions: [session, ...state.sessions],
          currentSessionId: sessionId,
          isLoading: false,
        }));
        return session;
      }

      set({ isLoading: false });
      return null;
    } catch (error) {
      set({
        error: error instanceof Error ? error.message : "Failed to create session",
        isLoading: false,
      });
      return null;
    }
  },

  // Update session title
  updateSessionTitle: async (sessionId: string, title: string) => {
    const cleanTitle = generateTitleFromMessage(title);

    try {
      await updateSession(sessionId, { title: cleanTitle });

      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.sessionId === sessionId ? { ...s, title: cleanTitle } : s
        ),
      }));
    } catch (error) {
      console.error("Failed to update session title:", error);
    }
  },

  // Update session activity (lastMessageAt, messageCount)
  updateSessionActivity: async (sessionId: string, messageCount?: number) => {
    const now = Date.now();

    try {
      await updateSession(sessionId, {
        lastMessageAt: now,
        ...(messageCount !== undefined && { messageCount }),
      });

      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.sessionId === sessionId
            ? {
                ...s,
                lastMessageAt: now,
                ...(messageCount !== undefined && { messageCount }),
              }
            : s
        ),
      }));
    } catch (error) {
      console.error("Failed to update session activity:", error);
    }
  },

  // Delete a session
  deleteSessionById: async (sessionId: string) => {
    try {
      // Immediately null out currentSessionId to prevent auto-save race condition
      // (ChatWindow's save guards check `if (!currentSessionId)` before writing)
      const wasCurrentSession = get().currentSessionId === sessionId;
      if (wasCurrentSession) {
        set({ currentSessionId: null });
      }

      const success = await deleteSession(sessionId);

      if (success) {
        set((state) => {
          const newSessions = state.sessions.filter((s) => s.sessionId !== sessionId);
          const newCurrentId =
            state.currentSessionId === sessionId
              ? newSessions[0]?.sessionId || null
              : state.currentSessionId;

          return {
            sessions: newSessions,
            currentSessionId: newCurrentId,
          };
        });
      }

      return success;
    } catch (error) {
      console.error("Failed to delete session:", error);
      return false;
    }
  },

  // Delete all sessions
  deleteAllSessions: async () => {
    const { sessions } = get();
    set({ currentSessionId: null });
    await Promise.all(sessions.map((s) => deleteSession(s.sessionId)));
    set({ sessions: [] });
  },

  // Get current session object
  getCurrentSession: () => {
    const { sessions, currentSessionId } = get();
    return sessions.find((s) => s.sessionId === currentSessionId);
  },

  // Clear all sessions (on logout)
  clearSessions: () => {
    set({
      sessions: [],
      currentSessionId: null,
      isLoading: false,
      error: null,
    });
  },
}));
