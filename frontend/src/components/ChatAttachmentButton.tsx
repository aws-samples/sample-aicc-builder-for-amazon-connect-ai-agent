/**
 * Chat Attachment Button
 *
 * A button component that allows users to attach files (images and documents)
 * to their chat messages. Supports both click-to-select and drag-and-drop.
 */

import React, { useRef } from 'react';
import { Paperclip } from 'lucide-react';
import { cn } from '../lib/utils';
import type { Language } from '../types';

// Supported MIME types
const SUPPORTED_IMAGE_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
];

const SUPPORTED_DOCUMENT_TYPES = [
  'application/pdf',
  'text/plain',
  'text/markdown',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'text/csv',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-excel',
];

// All supported MIME types (exported for external validation if needed)
export const ALL_SUPPORTED_TYPES = [...SUPPORTED_IMAGE_TYPES, ...SUPPORTED_DOCUMENT_TYPES];

// File extension to MIME type mapping for accept attribute
const ACCEPT_STRING = [
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.pdf',
  '.txt',
  '.md',
  '.docx',
  '.csv',
  '.xlsx',
  '.xls',
].join(',');

// Size limits in bytes
const MAX_IMAGE_SIZE = 3.75 * 1024 * 1024; // 3.75 MB
const MAX_DOCUMENT_SIZE = 4.5 * 1024 * 1024; // 4.5 MB
const MAX_FILES = 5;

export interface FileValidationResult {
  valid: boolean;
  error?: string;
  type?: 'image' | 'document';
}

export function validateFile(file: File): FileValidationResult {
  const mimeType = file.type.toLowerCase();

  // Check if image
  if (SUPPORTED_IMAGE_TYPES.includes(mimeType)) {
    if (file.size > MAX_IMAGE_SIZE) {
      return {
        valid: false,
        error: `Image "${file.name}" is too large (${(file.size / 1024 / 1024).toFixed(2)} MB). Maximum: 3.75 MB`,
      };
    }
    return { valid: true, type: 'image' };
  }

  // Check if document
  if (SUPPORTED_DOCUMENT_TYPES.includes(mimeType)) {
    if (file.size > MAX_DOCUMENT_SIZE) {
      return {
        valid: false,
        error: `Document "${file.name}" is too large (${(file.size / 1024 / 1024).toFixed(2)} MB). Maximum: 4.5 MB`,
      };
    }
    return { valid: true, type: 'document' };
  }

  // Unsupported type
  return {
    valid: false,
    error: `File "${file.name}" has unsupported type: ${mimeType || 'unknown'}`,
  };
}

interface ChatAttachmentButtonProps {
  onFilesSelected: (files: File[]) => void;
  onError?: (error: string) => void;
  disabled?: boolean;
  currentFileCount?: number;
  className?: string;
  /** External ref for triggering file picker programmatically */
  buttonRef?: React.RefObject<HTMLButtonElement>;
  /** UI language for localized labels */
  language?: Language;
}

const ATTACH_STRINGS: Record<Language, { attach: (n: number) => string; maxReached: string; ariaInput: string }> = {
  'en-US': {
    attach: (n) => `Attach files (${n} slot${n !== 1 ? 's' : ''} remaining)`,
    maxReached: `Maximum ${MAX_FILES} files reached`,
    ariaInput: 'Attach files',
  },
  'ko-KR': {
    attach: (n) => `파일 첨부 (${n}개 남음)`,
    maxReached: `최대 ${MAX_FILES}개 파일에 도달했습니다`,
    ariaInput: '파일 첨부',
  },
  'ja-JP': {
    attach: (n) => `ファイルを添付 (残り${n}件)`,
    maxReached: `最大${MAX_FILES}件のファイルに達しました`,
    ariaInput: 'ファイルを添付',
  },
};

export function ChatAttachmentButton({
  onFilesSelected,
  onError,
  disabled = false,
  currentFileCount = 0,
  className,
  buttonRef,
  language = 'en-US',
}: ChatAttachmentButtonProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const t = ATTACH_STRINGS[language] || ATTACH_STRINGS['en-US'];

  const handleClick = () => {
    if (disabled) return;
    inputRef.current?.click();
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (files.length === 0) return;

    // Check max files limit
    const totalFiles = currentFileCount + files.length;
    if (totalFiles > MAX_FILES) {
      onError?.(`Maximum ${MAX_FILES} files allowed. You already have ${currentFileCount} file(s) attached.`);
      // Reset input so same file can be selected again
      if (inputRef.current) inputRef.current.value = '';
      return;
    }

    // Validate each file
    const validFiles: File[] = [];
    const errors: string[] = [];

    for (const file of files) {
      const result = validateFile(file);
      if (result.valid) {
        validFiles.push(file);
      } else {
        errors.push(result.error || 'Unknown validation error');
      }
    }

    // Report errors
    if (errors.length > 0) {
      onError?.(errors.join('\n'));
    }

    // Pass valid files
    if (validFiles.length > 0) {
      onFilesSelected(validFiles);
    }

    // Reset input so same file can be selected again
    if (inputRef.current) inputRef.current.value = '';
  };

  const remainingSlots = MAX_FILES - currentFileCount;
  const isMaxReached = remainingSlots <= 0;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={handleClick}
        disabled={disabled || isMaxReached}
        title={isMaxReached ? t.maxReached : t.attach(remainingSlots)}
        aria-label={isMaxReached ? t.maxReached : t.attach(remainingSlots)}
        className={cn(
          'flex-shrink-0 p-2 rounded-lg transition-colors',
          'focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-1',
          disabled || isMaxReached
            ? 'text-surface-400 dark:text-surface-600 cursor-not-allowed'
            : 'text-surface-500 dark:text-surface-400 hover:text-primary-600 dark:hover:text-primary-400 hover:bg-surface-100 dark:hover:bg-surface-800',
          className
        )}
      >
        <Paperclip className="w-5 h-5" />
      </button>

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_STRING}
        multiple
        onChange={handleFileChange}
        className="hidden"
        aria-label={t.ariaInput}
      />
    </>
  );
}

// Export constants for use in other components
export { MAX_FILES, MAX_IMAGE_SIZE, MAX_DOCUMENT_SIZE, SUPPORTED_IMAGE_TYPES, SUPPORTED_DOCUMENT_TYPES };
