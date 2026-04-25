/**
 * Header Component
 *
 * App header with theme toggle, language selector and user menu
 */

import { Globe, HelpCircle, ExternalLink, LogOut, User, Moon, Sun, Monitor } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useBuilderStore, type Theme } from '../stores/builderStore';
import { useAuthStore } from '../stores/authStore';
import { useWebSocket } from '../hooks/useWebSocket';
import type { Language } from '../types';
import { LANGUAGES } from '../types';
import { cn } from '../lib/utils';

export function Header() {
  const navigate = useNavigate();
  const { language, setLanguage, theme, setTheme } = useBuilderStore();
  const { email, signOut } = useAuthStore();
  const { disconnect } = useWebSocket();

  const handleSignOut = () => {
    disconnect(true); // Force disconnect WebSocket
    signOut();
    navigate('/login');
  };

  const cycleTheme = () => {
    const themes: Theme[] = ['light', 'dark', 'system'];
    const currentIndex = themes.indexOf(theme);
    const nextIndex = (currentIndex + 1) % themes.length;
    setTheme(themes[nextIndex]);
  };

  const getThemeIcon = () => {
    switch (theme) {
      case 'light':
        return <Sun className="w-4 h-4" />;
      case 'dark':
        return <Moon className="w-4 h-4" />;
      case 'system':
        return <Monitor className="w-4 h-4" />;
    }
  };

  const getThemeLabel = () => {
    const labels = {
      light: language === 'ko-KR' ? '라이트' : 'Light',
      dark: language === 'ko-KR' ? '다크' : 'Dark',
      system: language === 'ko-KR' ? '시스템' : 'System',
    };
    return labels[theme];
  };

  return (
    <header className="bg-surface-900 dark:bg-surface-950 text-white border-b border-surface-800 dark:border-surface-800 transition-colors">
      <div className="w-full px-4 lg:px-6 py-3 lg:py-4">
        <div className="flex items-center justify-between">
          {/* Logo and Title */}
          <div className="flex items-center gap-3 lg:gap-4">
            <div className="flex items-center gap-2">
              <div className="w-9 h-9 lg:w-10 lg:h-10 rounded-lg bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center shadow-glow">
                <span className="text-lg lg:text-xl">🏗️</span>
              </div>
              <div>
                <h1 className="text-base lg:text-lg font-semibold bg-gradient-to-r from-white to-surface-300 bg-clip-text text-transparent">
                  AICC Builder
                </h1>
                <p className="text-[10px] lg:text-xs text-surface-400">
                  {language === 'ko-KR'
                    ? 'AI 컨택센터 맞춤 설정'
                    : 'AI Contact Center Customization'}
                </p>
              </div>
            </div>
          </div>

          {/* Right side actions */}
          <div className="flex items-center gap-2 lg:gap-4">
            {/* Theme Toggle */}
            <button
              onClick={cycleTheme}
              className={cn(
                'flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm',
                'bg-surface-800 hover:bg-surface-700 dark:bg-surface-800 dark:hover:bg-surface-700',
                'text-surface-300 hover:text-white transition-all'
              )}
              title={getThemeLabel()}
            >
              {getThemeIcon()}
              <span className="hidden md:inline text-xs">{getThemeLabel()}</span>
            </button>

            {/* Language Selector */}
            <div className="relative">
              <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-surface-800 hover:bg-surface-700 transition-colors">
                <Globe className="w-4 h-4 text-surface-400" />
                <select
                  value={language}
                  onChange={(e) => setLanguage(e.target.value as Language)}
                  className="bg-transparent text-xs lg:text-sm text-surface-300 hover:text-white cursor-pointer focus:outline-none appearance-none pr-4"
                >
                  {Object.entries(LANGUAGES).map(([code, name]) => (
                    <option key={code} value={code} className="text-surface-900 bg-white">
                      {name}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Help Link */}
            <a
              href="https://docs.aws.amazon.com/connect/"
              target="_blank"
              rel="noopener noreferrer"
              className="hidden sm:flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs lg:text-sm text-surface-400 hover:text-white hover:bg-surface-800 transition-all"
            >
              <HelpCircle className="w-4 h-4" />
              <span className="hidden lg:inline">{language === 'ko-KR' ? '도움말' : 'Help'}</span>
              <ExternalLink className="w-3 h-3" />
            </a>

            {/* User Menu */}
            <div className="flex items-center gap-2 lg:gap-3 pl-2 lg:pl-4 border-l border-surface-700">
              <div className="hidden sm:flex items-center gap-2 text-xs lg:text-sm text-surface-400">
                <User className="w-4 h-4" />
                <span className="max-w-[100px] lg:max-w-[150px] truncate">{email || 'User'}</span>
              </div>
              <button
                onClick={handleSignOut}
                className="flex items-center gap-1.5 p-2 rounded-lg text-surface-400 hover:text-white hover:bg-surface-800 transition-all"
                title={language === 'ko-KR' ? '로그아웃' : 'Sign Out'}
              >
                <LogOut className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </header>
  );
}
