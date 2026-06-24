/**
 * Login Page for AICC Builder
 *
 * Handles user authentication via Cognito.
 * Self-sign-up is disabled - users must be created by admin.
 *
 * Themed to the app's violet/zinc palette (dark default). Localized (en/ko/ja)
 * with a language selector, live password-policy checkmarks, and aria-live
 * error announcements.
 */

import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/authStore";
import { useBuilderStore } from "../stores/builderStore";
import { Loader2, Lock, Mail, Eye, EyeOff, AlertCircle, Globe, Check, X } from "lucide-react";
import type { Language } from "../types";
import { LANGUAGES } from "../types";
import { cn } from "../lib/utils";

const STRINGS: Record<Language, Record<string, string>> = {
  'en-US': {
    title: 'AICC Builder',
    subtitle: 'Sign in to build your AI Contact Center',
    email: 'Email',
    emailPlaceholder: 'Enter your email',
    password: 'Password',
    passwordPlaceholder: 'Enter your password',
    signIn: 'Sign In',
    signingIn: 'Signing in…',
    enterCreds: 'Please enter email and password',
    noAccount: "Don't have an account? Contact your administrator.",
    poweredBy: 'Powered by Amazon Bedrock',
    setNewTitle: 'Set New Password',
    setNewSubtitle: 'Please set a new password for your account',
    newPassword: 'New Password',
    newPasswordPlaceholder: 'Enter new password',
    confirmPassword: 'Confirm Password',
    confirmPlaceholder: 'Confirm new password',
    setPassword: 'Set Password & Continue',
    settingPassword: 'Setting password…',
    enterConfirm: 'Please enter and confirm your new password',
    noMatch: 'Passwords do not match',
    policyLen: 'At least 8 characters',
    policyUpper: 'An uppercase letter',
    policyLower: 'A lowercase letter',
    policyNum: 'A number',
  },
  'ko-KR': {
    title: 'AICC Builder',
    subtitle: 'AI 컨택센터를 구축하려면 로그인하세요',
    email: '이메일',
    emailPlaceholder: '이메일을 입력하세요',
    password: '비밀번호',
    passwordPlaceholder: '비밀번호를 입력하세요',
    signIn: '로그인',
    signingIn: '로그인 중…',
    enterCreds: '이메일과 비밀번호를 입력하세요',
    noAccount: '계정이 없으신가요? 관리자에게 문의하세요.',
    poweredBy: 'Amazon Bedrock 기반',
    setNewTitle: '새 비밀번호 설정',
    setNewSubtitle: '계정의 새 비밀번호를 설정하세요',
    newPassword: '새 비밀번호',
    newPasswordPlaceholder: '새 비밀번호를 입력하세요',
    confirmPassword: '비밀번호 확인',
    confirmPlaceholder: '새 비밀번호를 다시 입력하세요',
    setPassword: '비밀번호 설정 후 계속',
    settingPassword: '비밀번호 설정 중…',
    enterConfirm: '새 비밀번호를 입력하고 확인하세요',
    noMatch: '비밀번호가 일치하지 않습니다',
    policyLen: '8자 이상',
    policyUpper: '대문자 포함',
    policyLower: '소문자 포함',
    policyNum: '숫자 포함',
  },
  'ja-JP': {
    title: 'AICC Builder',
    subtitle: 'AIコンタクトセンターを構築するにはサインインしてください',
    email: 'メール',
    emailPlaceholder: 'メールアドレスを入力',
    password: 'パスワード',
    passwordPlaceholder: 'パスワードを入力',
    signIn: 'サインイン',
    signingIn: 'サインイン中…',
    enterCreds: 'メールとパスワードを入力してください',
    noAccount: 'アカウントがありませんか? 管理者にお問い合わせください。',
    poweredBy: 'Amazon Bedrock 提供',
    setNewTitle: '新しいパスワードの設定',
    setNewSubtitle: 'アカウントの新しいパスワードを設定してください',
    newPassword: '新しいパスワード',
    newPasswordPlaceholder: '新しいパスワードを入力',
    confirmPassword: 'パスワード確認',
    confirmPlaceholder: '新しいパスワードを再入力',
    setPassword: 'パスワードを設定して続行',
    settingPassword: 'パスワード設定中…',
    enterConfirm: '新しいパスワードを入力して確認してください',
    noMatch: 'パスワードが一致しません',
    policyLen: '8文字以上',
    policyUpper: '大文字を含む',
    policyLower: '小文字を含む',
    policyNum: '数字を含む',
  },
};

function PolicyItem({ ok, label }: { ok: boolean; label: string }) {
  return (
    <li className="flex items-center gap-2">
      {ok ? (
        <Check className="w-3.5 h-3.5 text-green-500 dark:text-green-400 flex-shrink-0" />
      ) : (
        <X className="w-3.5 h-3.5 text-surface-400 dark:text-surface-500 flex-shrink-0" />
      )}
      <span className={cn(ok ? 'text-green-600 dark:text-green-400' : 'text-surface-400 dark:text-surface-500')}>
        {label}
      </span>
    </li>
  );
}

export function LoginPage() {
  const navigate = useNavigate();
  const {
    isAuthenticated,
    isLoading,
    error,
    needsNewPassword,
    signIn,
    setNewPassword,
    clearError,
  } = useAuthStore();
  const language = useBuilderStore((s) => s.language);
  const setLanguage = useBuilderStore((s) => s.setLanguage);
  const t = STRINGS[language] || STRINGS['en-US'];

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPasswordValue, setNewPasswordValue] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  // Redirect if already authenticated
  useEffect(() => {
    if (isAuthenticated) {
      navigate("/");
    }
  }, [isAuthenticated, navigate]);

  // Live password-policy checks
  const policy = {
    len: newPasswordValue.length >= 8,
    upper: /[A-Z]/.test(newPasswordValue),
    lower: /[a-z]/.test(newPasswordValue),
    num: /[0-9]/.test(newPasswordValue),
  };
  const policyOk = policy.len && policy.upper && policy.lower && policy.num;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLocalError(null);
    clearError();
    if (!email || !password) {
      setLocalError(t.enterCreds);
      return;
    }
    await signIn(email, password);
  };

  const handleNewPasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLocalError(null);
    clearError();
    if (!newPasswordValue || !confirmPassword) {
      setLocalError(t.enterConfirm);
      return;
    }
    if (newPasswordValue !== confirmPassword) {
      setLocalError(t.noMatch);
      return;
    }
    if (!policyOk) return;
    const success = await setNewPassword(newPasswordValue);
    if (success) navigate("/");
  };

  const displayError = localError || error;

  const inputClasses =
    'w-full pl-10 pr-12 py-3 bg-surface-50 dark:bg-surface-900 border border-surface-300 dark:border-surface-600 rounded-lg ' +
    'text-surface-900 dark:text-surface-100 placeholder-surface-400 dark:placeholder-surface-500 ' +
    'focus:border-primary-500 focus:ring-2 focus:ring-primary-500/40 outline-none transition-colors';

  const LanguagePicker = (
    <div className="absolute top-4 right-4">
      <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-surface-100 dark:bg-surface-800 border border-surface-200 dark:border-surface-700">
        <Globe className="w-4 h-4 text-surface-400" />
        <select
          value={language}
          onChange={(e) => setLanguage(e.target.value as Language)}
          aria-label="Language"
          className="bg-transparent text-xs text-surface-600 dark:text-surface-300 cursor-pointer focus:outline-none appearance-none pr-3"
        >
          {Object.entries(LANGUAGES).map(([code, name]) => (
            <option key={code} value={code} className="text-surface-900 bg-white">
              {name}
            </option>
          ))}
        </select>
      </div>
    </div>
  );

  // New password form (for first-time login)
  if (needsNewPassword) {
    return (
      <div className="relative min-h-screen bg-surface-50 dark:bg-gradient-dark flex items-center justify-center p-4">
        {LanguagePicker}
        <div className="w-full max-w-md">
          <div className="bg-white dark:bg-surface-850 backdrop-blur-sm rounded-2xl border border-surface-200 dark:border-surface-700 p-8 shadow-xl dark:shadow-glow">
            <div className="text-center mb-8">
              <h1 className="text-2xl font-bold text-surface-900 dark:text-surface-100 mb-2">{t.setNewTitle}</h1>
              <p className="text-surface-500 dark:text-surface-400 text-sm">{t.setNewSubtitle}</p>
            </div>

            {displayError && (
              <div role="alert" aria-live="assertive" className="mb-6 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-3">
                <AlertCircle className="w-5 h-5 text-red-500 dark:text-red-400 flex-shrink-0 mt-0.5" />
                <p className="text-red-600 dark:text-red-300 text-sm">{displayError}</p>
              </div>
            )}

            <form onSubmit={handleNewPasswordSubmit} className="space-y-6">
              <div>
                <label htmlFor="newPassword" className="block text-sm font-medium text-surface-700 dark:text-surface-300 mb-2">
                  {t.newPassword}
                </label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-surface-400" />
                  <input
                    id="newPassword"
                    type={showPassword ? "text" : "password"}
                    value={newPasswordValue}
                    onChange={(e) => setNewPasswordValue(e.target.value)}
                    className={inputClasses}
                    placeholder={t.newPasswordPlaceholder}
                    disabled={isLoading}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    aria-label={showPassword ? 'Hide password' : 'Show password'}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300"
                  >
                    {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                  </button>
                </div>
              </div>

              <div>
                <label htmlFor="confirmPassword" className="block text-sm font-medium text-surface-700 dark:text-surface-300 mb-2">
                  {t.confirmPassword}
                </label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-surface-400" />
                  <input
                    id="confirmPassword"
                    type={showPassword ? "text" : "password"}
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    className={inputClasses}
                    placeholder={t.confirmPlaceholder}
                    disabled={isLoading}
                  />
                </div>
              </div>

              {/* Live password policy */}
              <ul className="text-xs space-y-1" aria-live="polite">
                <PolicyItem ok={policy.len} label={t.policyLen} />
                <PolicyItem ok={policy.upper} label={t.policyUpper} />
                <PolicyItem ok={policy.lower} label={t.policyLower} />
                <PolicyItem ok={policy.num} label={t.policyNum} />
              </ul>

              <button
                type="submit"
                disabled={isLoading || !policyOk}
                className="w-full py-3 bg-primary-600 dark:bg-primary-500 hover:bg-primary-700 dark:hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium rounded-lg transition-colors flex items-center justify-center gap-2"
              >
                {isLoading ? (
                  <><Loader2 className="w-5 h-5 animate-spin" />{t.settingPassword}</>
                ) : (
                  t.setPassword
                )}
              </button>
            </form>
          </div>
        </div>
      </div>
    );
  }

  // Login form
  return (
    <div className="relative min-h-screen bg-surface-50 dark:bg-gradient-dark flex items-center justify-center p-4">
      {LanguagePicker}
      <div className="w-full max-w-md">
        <div className="bg-white dark:bg-surface-850 backdrop-blur-sm rounded-2xl border border-surface-200 dark:border-surface-700 p-8 shadow-xl dark:shadow-glow">
          <div className="text-center mb-8">
            <div className="w-16 h-16 bg-gradient-to-br from-primary-500 to-primary-600 rounded-2xl flex items-center justify-center mx-auto mb-4 shadow-lg dark:shadow-glow">
              <span className="text-3xl">🏗️</span>
            </div>
            <h1 className="text-2xl font-bold text-surface-900 dark:text-surface-100 mb-2">{t.title}</h1>
            <p className="text-surface-500 dark:text-surface-400 text-sm">{t.subtitle}</p>
          </div>

          {displayError && (
            <div role="alert" aria-live="assertive" className="mb-6 p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-red-500 dark:text-red-400 flex-shrink-0 mt-0.5" />
              <p className="text-red-600 dark:text-red-300 text-sm">{displayError}</p>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-6">
            <div>
              <label htmlFor="email" className="block text-sm font-medium text-surface-700 dark:text-surface-300 mb-2">
                {t.email}
              </label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-surface-400" />
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className={inputClasses}
                  placeholder={t.emailPlaceholder}
                  disabled={isLoading}
                  autoComplete="email"
                />
              </div>
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-surface-700 dark:text-surface-300 mb-2">
                {t.password}
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-surface-400" />
                <input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className={inputClasses}
                  placeholder={t.passwordPlaceholder}
                  disabled={isLoading}
                  autoComplete="current-password"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-surface-400 hover:text-surface-600 dark:hover:text-surface-300"
                >
                  {showPassword ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={isLoading}
              className="w-full py-3 bg-primary-600 dark:bg-primary-500 hover:bg-primary-700 dark:hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium rounded-lg transition-colors flex items-center justify-center gap-2"
            >
              {isLoading ? (
                <><Loader2 className="w-5 h-5 animate-spin" />{t.signingIn}</>
              ) : (
                t.signIn
              )}
            </button>
          </form>

          <div className="mt-6 text-center">
            <p className="text-surface-500 dark:text-surface-400 text-sm">{t.noAccount}</p>
          </div>
        </div>

        <div className="mt-6 text-center">
          <p className="text-surface-400 dark:text-surface-500 text-xs">{t.poweredBy}</p>
        </div>
      </div>
    </div>
  );
}
