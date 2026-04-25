/**
 * Cognito Authentication Service for AICC Builder
 *
 * Handles user authentication via Amazon Cognito User Pool.
 * Self-sign-up is disabled - users must be created by admin.
 */

import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserSession,
} from "amazon-cognito-identity-js";

// Configuration from environment variables
const userPoolId = (import.meta as any).env?.VITE_USER_POOL_ID || "";
const clientId = (import.meta as any).env?.VITE_USER_POOL_CLIENT_ID || "";
const region = (import.meta as any).env?.VITE_COGNITO_REGION || "ap-northeast-1";

// Initialize Cognito User Pool
const poolData = {
  UserPoolId: userPoolId,
  ClientId: clientId,
};

let userPool: CognitoUserPool | null = null;
let pendingCognitoUser: CognitoUser | null = null;
let pendingUserAttributes: Record<string, string> | null = null;

function getUserPool(): CognitoUserPool {
  if (!userPool) {
    if (!userPoolId || !clientId) {
      throw new Error(
        "Cognito configuration missing. Please check VITE_USER_POOL_ID and VITE_USER_POOL_CLIENT_ID."
      );
    }
    userPool = new CognitoUserPool(poolData);
  }
  return userPool;
}

export interface AuthResult {
  success: boolean;
  session?: CognitoUserSession;
  error?: string;
  newPasswordRequired?: boolean;
  userAttributes?: Record<string, string>;
}

/**
 * Sign in with username/email and password
 */
export async function signIn(
  username: string,
  password: string
): Promise<AuthResult> {
  return new Promise((resolve) => {
    try {
      const pool = getUserPool();
      const cognitoUser = new CognitoUser({
        Username: username,
        Pool: pool,
      });

      const authDetails = new AuthenticationDetails({
        Username: username,
        Password: password,
      });

      cognitoUser.authenticateUser(authDetails, {
        onSuccess: (session: CognitoUserSession) => {
          // Clear any pending challenge state
          pendingCognitoUser = null;
          pendingUserAttributes = null;
          resolve({ success: true, session });
        },
        onFailure: (err: Error) => {
          pendingCognitoUser = null;
          pendingUserAttributes = null;
          resolve({ success: false, error: err.message || "Authentication failed" });
        },
        newPasswordRequired: (userAttributes: Record<string, string>) => {
          // Remove immutable attributes that cannot be modified
          delete userAttributes.email_verified;
          delete userAttributes.phone_number_verified;
          delete userAttributes.email;
          delete userAttributes.sub;
          // Store the CognitoUser instance for completing the challenge
          pendingCognitoUser = cognitoUser;
          pendingUserAttributes = userAttributes;
          resolve({
            success: false,
            newPasswordRequired: true,
            userAttributes,
          });
        },
      });
    } catch (error) {
      resolve({
        success: false,
        error: error instanceof Error ? error.message : "Authentication failed",
      });
    }
  });
}

/**
 * Complete new password challenge (for first-time login)
 * Uses the stored CognitoUser instance from the initial signIn attempt
 */
export async function completeNewPasswordChallenge(
  username: string,
  oldPassword: string,
  newPassword: string
): Promise<AuthResult> {
  return new Promise((resolve) => {
    try {
      // If we have a pending user from the same session, use it
      if (pendingCognitoUser && pendingUserAttributes) {
        console.log("Using pending Cognito user for password challenge");
        const cognitoUser = pendingCognitoUser;
        const userAttributes = pendingUserAttributes;

        cognitoUser.completeNewPasswordChallenge(newPassword, userAttributes, {
          onSuccess: (session: CognitoUserSession) => {
            pendingCognitoUser = null;
            pendingUserAttributes = null;
            resolve({ success: true, session });
          },
          onFailure: (err: Error) => {
            console.error("completeNewPasswordChallenge failed:", err);
            resolve({
              success: false,
              error: err.message || "Failed to set new password",
            });
          },
        });
        return;
      }

      // If no pending user (e.g., page refresh), re-authenticate to get the challenge
      console.log("No pending user, re-authenticating to complete password challenge");
      const pool = getUserPool();
      const cognitoUser = new CognitoUser({
        Username: username,
        Pool: pool,
      });

      const authDetails = new AuthenticationDetails({
        Username: username,
        Password: oldPassword,
      });

      cognitoUser.authenticateUser(authDetails, {
        onSuccess: (session: CognitoUserSession) => {
          // User already set password elsewhere
          resolve({ success: true, session });
        },
        onFailure: (err: Error) => {
          console.error("Re-authentication failed:", err);
          resolve({ success: false, error: err.message || "Authentication failed" });
        },
        newPasswordRequired: (userAttributes: Record<string, string>) => {
          // Remove immutable attributes that cannot be modified
          delete userAttributes.email_verified;
          delete userAttributes.phone_number_verified;
          delete userAttributes.email;
          delete userAttributes.sub;

          cognitoUser.completeNewPasswordChallenge(newPassword, userAttributes, {
            onSuccess: (session: CognitoUserSession) => {
              resolve({ success: true, session });
            },
            onFailure: (err: Error) => {
              console.error("completeNewPasswordChallenge failed:", err);
              resolve({
                success: false,
                error: err.message || "Failed to set new password",
              });
            },
          });
        },
      });
    } catch (error) {
      console.error("Password change error:", error);
      resolve({
        success: false,
        error: error instanceof Error ? error.message : "Password change failed",
      });
    }
  });
}

/**
 * Sign out the current user
 */
export function signOut(): void {
  try {
    const pool = getUserPool();
    const currentUser = pool.getCurrentUser();
    if (currentUser) {
      currentUser.signOut();
    }
  } catch (error) {
    console.error("Sign out error:", error);
  }
}

/**
 * Get the current authenticated session
 */
export async function getCurrentSession(): Promise<CognitoUserSession | null> {
  return new Promise((resolve) => {
    try {
      const pool = getUserPool();
      const currentUser = pool.getCurrentUser();

      if (!currentUser) {
        resolve(null);
        return;
      }

      currentUser.getSession(
        (err: Error | null, session: CognitoUserSession | null) => {
          if (err) {
            console.error("Get session error:", err);
            resolve(null);
            return;
          }
          resolve(session);
        }
      );
    } catch (error) {
      console.error("Get session error:", error);
      resolve(null);
    }
  });
}

/**
 * Get the current ID token (for API authentication)
 */
export async function getIdToken(): Promise<string | null> {
  const session = await getCurrentSession();
  if (!session || !session.isValid()) {
    return null;
  }
  return session.getIdToken().getJwtToken();
}

/**
 * Get the current access token
 */
export async function getAccessToken(): Promise<string | null> {
  const session = await getCurrentSession();
  if (!session || !session.isValid()) {
    return null;
  }
  return session.getAccessToken().getJwtToken();
}

/**
 * Check if the user is authenticated
 */
export async function isAuthenticated(): Promise<boolean> {
  const session = await getCurrentSession();
  return session !== null && session.isValid();
}

/**
 * Get current user's email
 */
export function getCurrentUserEmail(): string | null {
  try {
    const pool = getUserPool();
    const currentUser = pool.getCurrentUser();
    return currentUser?.getUsername() || null;
  } catch {
    return null;
  }
}

/**
 * Get current user's Cognito sub (unique user ID)
 * This is stable across sessions and can be used as an actor ID
 */
export async function getCurrentUserSub(): Promise<string | null> {
  const session = await getCurrentSession();
  if (!session || !session.isValid()) {
    return null;
  }
  const idToken = session.getIdToken();
  const payload = idToken.decodePayload();
  return payload.sub || null;
}

// Export configuration for debugging
export const authConfig = {
  userPoolId,
  clientId,
  region,
  isConfigured: Boolean(userPoolId && clientId),
};
