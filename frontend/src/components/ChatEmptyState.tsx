/**
 * Chat Empty State Component
 *
 * Displayed when the chat has no messages yet.
 * Shows starter prompt cards to help users begin their journey.
 */

import { Upload } from 'lucide-react';
import { cn } from '../lib/utils';
import type { Language } from '../types';

interface StarterPrompt {
  id: string;
  icon: string;
  title: Record<Language, string>;
  description: Record<Language, string>;
  initialMessage?: string;
  action?: 'upload';
}

const starterPrompts: StarterPrompt[] = [
  {
    id: 'hotel',
    icon: '🏨',
    title: {
      'en-US': 'Hotel Booking',
      'ko-KR': '호텔 예약',
      'ja-JP': 'ホテル予約',
    },
    description: {
      'en-US': 'Reservations, check-in/out, room service',
      'ko-KR': '예약, 체크인/아웃, 룸서비스',
      'ja-JP': '予約、チェックイン/アウト、ルームサービス',
    },
    initialMessage:
      'I want to build a hotel booking contact center with reservation management, check-in/out, and room service operations.',
  },
  {
    id: 'healthcare',
    icon: '🏥',
    title: {
      'en-US': 'Healthcare',
      'ko-KR': '의료',
      'ja-JP': '医療',
    },
    description: {
      'en-US': 'Appointments, patient records, prescriptions',
      'ko-KR': '예약, 환자 기록, 처방',
      'ja-JP': '予約、患者記録、処方箋',
    },
    initialMessage:
      'I want to build a healthcare contact center for appointment scheduling and patient inquiries.',
  },
  {
    id: 'ecommerce',
    icon: '📦',
    title: {
      'en-US': 'E-Commerce',
      'ko-KR': '이커머스',
      'ja-JP': 'Eコマース',
    },
    description: {
      'en-US': 'Order tracking, returns, support',
      'ko-KR': '주문 조회, 반품, 고객 지원',
      'ja-JP': '注文追跡、返品、サポート',
    },
    initialMessage:
      'I want to build an e-commerce contact center for order tracking and customer support.',
  },
  {
    id: 'upload',
    icon: '📋',
    title: {
      'en-US': 'Upload Requirements',
      'ko-KR': '요구사항 업로드',
      'ja-JP': '要件アップロード',
    },
    description: {
      'en-US': 'Upload your requirements document',
      'ko-KR': '요구사항 문서를 업로드하세요',
      'ja-JP': '要件ドキュメントをアップロード',
    },
    action: 'upload',
  },
];

interface ChatEmptyStateProps {
  language: Language;
  onStarterPromptClick: (message: string) => void;
  onFileUploadClick: () => void;
}

export function ChatEmptyState({
  language,
  onStarterPromptClick,
  onFileUploadClick,
}: ChatEmptyStateProps) {
  const content = {
    'en-US': {
      heading: 'What would you like to build?',
      hint: 'Attach files or share URLs for more accurate results',
    },
    'ko-KR': {
      heading: '어떤 비즈니스를 구축하시겠습니까?',
      hint: '파일을 첨부하거나 URL을 공유하면 더 정확한 결과를 얻을 수 있어요',
    },
    'ja-JP': {
      heading: '何を構築しますか?',
      hint: 'ファイルを添付またはURLを共有するとより正確な結果が得られます',
    },
  }[language] || { heading: 'What would you like to build?', hint: 'Attach files or share URLs for more accurate results' };

  const handlePromptClick = (prompt: StarterPrompt) => {
    if (prompt.action === 'upload') {
      onFileUploadClick();
    } else if (prompt.initialMessage) {
      onStarterPromptClick(prompt.initialMessage);
    }
  };

  return (
    <div className="flex flex-col items-center justify-center h-full px-4 py-8">
      {/* Logo/Icon */}
      <div className="w-16 h-16 mb-6 rounded-2xl bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center shadow-lg dark:shadow-glow">
        <span className="text-3xl">🤖</span>
      </div>

      {/* Heading */}
      <h2 className="text-xl lg:text-2xl font-semibold text-surface-900 dark:text-surface-100 mb-8 text-center">
        {content.heading}
      </h2>

      {/* Starter Prompt Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 lg:gap-4 max-w-2xl w-full mb-8">
        {starterPrompts.map((prompt) => (
          <button
            key={prompt.id}
            onClick={() => handlePromptClick(prompt)}
            className={cn(
              'group flex items-start gap-3 p-4 rounded-xl border transition-all text-left',
              'bg-white dark:bg-surface-800',
              'border-surface-200 dark:border-surface-700',
              'hover:border-primary-300 dark:hover:border-primary-600',
              'hover:bg-primary-50 dark:hover:bg-primary-900/20',
              'hover:shadow-md dark:hover:shadow-glow-sm',
              'focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 dark:focus:ring-offset-surface-850'
            )}
          >
            <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-surface-100 dark:bg-surface-700 flex items-center justify-center text-xl group-hover:bg-primary-100 dark:group-hover:bg-primary-800/50 transition-colors">
              {prompt.action === 'upload' ? (
                <Upload className="w-5 h-5 text-surface-600 dark:text-surface-400 group-hover:text-primary-600 dark:group-hover:text-primary-400" />
              ) : (
                prompt.icon
              )}
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="font-medium text-surface-900 dark:text-surface-100 group-hover:text-primary-700 dark:group-hover:text-primary-300 transition-colors">
                {prompt.title[language]}
              </h3>
              <p className="text-sm text-surface-500 dark:text-surface-400 mt-0.5 line-clamp-2">
                {prompt.description[language]}
              </p>
            </div>
          </button>
        ))}
      </div>

      {/* Hint */}
      <p className="text-sm text-surface-500 dark:text-surface-400 text-center flex items-center gap-2">
        <span>💡</span>
        <span>{content.hint}</span>
      </p>
    </div>
  );
}
