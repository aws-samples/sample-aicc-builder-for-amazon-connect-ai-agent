/**
 * Authentication Store for AICC Builder
 *
 * Manages authentication state using Zustand.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import {
  signIn as cognitoSignIn,
  signOut as cognitoSignOut,
  completeNewPasswordChallenge,
  isAuthenticated as checkIsAuthenticated,
  getCurrentUserEmail,
  getIdToken as getIdTokenFromCognito,
  getCurrentUserSub,
} from "../services/auth";
import { useBuilderStore } from "./builderStore";
import { useSessionStore } from "./sessionStore";

interface AuthState {
  // State
  isAuthenticated: boolean;
  isLoading: boolean;
  email: string | null;
  userSub: string | null;
  error: string | null;
  needsNewPassword: boolean;
  tempCredentials: { username: string; password: string } | null;

  // Actions
  signIn: (username: string, password: string) => Promise<boolean>;
  setNewPassword: (newPassword: string) => Promise<boolean>;
  signOut: () => void;
  checkAuth: () => Promise<void>;
  clearError: () => void;
  getIdToken: () => Promise<string | null>;
  getUserSub: () => Promise<string | null>;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      // Initial state
      isAuthenticated: false,
      isLoading: true,
      email: null,
      userSub: null,
      error: null,
      needsNewPassword: false,
      tempCredentials: null,

      // Sign in action
      signIn: async (username: string, password: string) => {
        set({ isLoading: true, error: null });

        const result = await cognitoSignIn(username, password);

        if (result.success && result.session) {
          set({
            isAuthenticated: true,
            isLoading: false,
            email: username,
            error: null,
            needsNewPassword: false,
            tempCredentials: null,
          });
          return true;
        }

        if (result.newPasswordRequired) {
          set({
            isAuthenticated: false,
            isLoading: false,
            needsNewPassword: true,
            tempCredentials: { username, password },
            error: null,
          });
          return false;
        }

        set({
          isAuthenticated: false,
          isLoading: false,
          error: result.error || "Login failed",
        });
        return false;
      },

      // Set new password (for first-time login)
      setNewPassword: async (newPassword: string) => {
        const { tempCredentials } = get();
        if (!tempCredentials) {
          set({ error: "No pending password change" });
          return false;
        }

        set({ isLoading: true, error: null });

        const result = await completeNewPasswordChallenge(
          tempCredentials.username,
          tempCredentials.password,
          newPassword
        );

        if (result.success) {
          set({
            isAuthenticated: true,
            isLoading: false,
            email: tempCredentials.username,
            needsNewPassword: false,
            tempCredentials: null,
            error: null,
          });
          return true;
        }

        set({
          isLoading: false,
          error: result.error || "Failed to set new password",
        });
        return false;
      },

      // Sign out action
      signOut: () => {
        cognitoSignOut();
        // Reset all session-specific stores to prevent data leaks between users
        useBuilderStore.getState().reset();
        useSessionStore.getState().clearSessions();
        set({
          isAuthenticated: false,
          isLoading: false,
          email: null,
          userSub: null,
          error: null,
          needsNewPassword: false,
          tempCredentials: null,
        });
      },

      // Check authentication status
      checkAuth: async () => {
        set({ isLoading: true });

        const authenticated = await checkIsAuthenticated();
        const email = getCurrentUserEmail();

        set({
          isAuthenticated: authenticated,
          isLoading: false,
          email: authenticated ? email : null,
        });
      },

      // Clear error
      clearError: () => {
        set({ error: null });
      },

      // Get ID token for AWS credentials
      getIdToken: async () => {
        return await getIdTokenFromCognito();
      },

      // Get user's Cognito sub (stable unique ID)
      getUserSub: async () => {
        const { userSub } = get();
        if (userSub) {
          return userSub;
        }
        const sub = await getCurrentUserSub();
        if (sub) {
          set({ userSub: sub });
        }
        return sub;
      },
    }),
    {
      name: "aicc-auth-storage",
      partialize: (state) => ({
        // Only persist minimal state - actual session is in Cognito
        email: state.email,
        userSub: state.userSub,
      }),
    }
  )
);
