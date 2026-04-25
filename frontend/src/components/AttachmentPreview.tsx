/**
 * Attachment Preview Component
 *
 * Displays a preview strip of attached files before sending a message.
 * Shows image thumbnails and document file cards with the ability to remove each attachment.
 */

import { useEffect, useState } from 'react';
import {
  X,
  FileText,
  FileSpreadsheet,
  File,
  Image as ImageIcon,
  Loader2,
} from 'lucide-react';
import { cn } from '../lib/utils';
import type { AttachedFile } from '../types';

interface AttachmentPreviewProps {
  files: AttachedFile[];
  onRemove: (fileId: string) => void;
  isUploading?: boolean;
  className?: string;
}

export function AttachmentPreview({
  files,
  onRemove,
  isUploading = false,
  className,
}: AttachmentPreviewProps) {
  if (files.length === 0) return null;

  return (
    <div
      className={cn(
        'flex flex-wrap gap-2 p-2 bg-surface-50 dark:bg-surface-900 rounded-lg border border-surface-200 dark:border-surface-700',
        className
      )}
    >
      {files.map((file) => (
        <AttachmentCard
          key={file.id}
          file={file}
          onRemove={() => onRemove(file.id)}
          isUploading={isUploading}
        />
      ))}
    </div>
  );
}

interface AttachmentCardProps {
  file: AttachedFile;
  onRemove: () => void;
  isUploading: boolean;
}

function AttachmentCard({ file, onRemove, isUploading }: AttachmentCardProps) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  // Generate preview URL for images
  useEffect(() => {
    if (file.type === 'image' && file.file) {
      const url = URL.createObjectURL(file.file);
      setPreviewUrl(url);
      return () => URL.revokeObjectURL(url);
    }
  }, [file]);

  const isImage = file.type === 'image';
  const hasError = file.status === 'error';

  return (
    <div
      className={cn(
        'relative group flex items-center gap-2 px-2 py-1.5 rounded-lg',
        'bg-white dark:bg-surface-800 border',
        hasError
          ? 'border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/20'
          : 'border-surface-200 dark:border-surface-700',
        'transition-all hover:shadow-sm'
      )}
    >
      {/* Preview/Icon */}
      <div className="flex-shrink-0 w-8 h-8 rounded overflow-hidden bg-surface-100 dark:bg-surface-700 flex items-center justify-center">
        {isImage && previewUrl ? (
          <img
            src={previewUrl}
            alt={file.file.name}
            className="w-full h-full object-cover"
          />
        ) : (
          <FileIcon mimeType={file.file.type} />
        )}
      </div>

      {/* File info */}
      <div className="flex-1 min-w-0 max-w-[150px]">
        <p className="text-xs font-medium text-surface-900 dark:text-surface-100 truncate">
          {file.file.name}
        </p>
        <p className="text-[10px] text-surface-500 dark:text-surface-400">
          {formatFileSize(file.file.size)}
          {hasError && file.error && (
            <span className="text-red-500 dark:text-red-400 ml-1">
              - {file.error}
            </span>
          )}
        </p>
      </div>

      {/* Status indicator */}
      {isUploading && file.status === 'uploading' && (
        <Loader2 className="w-4 h-4 text-primary-500 animate-spin" />
      )}

      {/* Remove button */}
      <button
        type="button"
        onClick={onRemove}
        disabled={isUploading}
        className={cn(
          'flex-shrink-0 p-0.5 rounded-full',
          'text-surface-400 hover:text-surface-600 dark:text-surface-500 dark:hover:text-surface-300',
          'hover:bg-surface-100 dark:hover:bg-surface-700',
          'focus:outline-none focus:ring-2 focus:ring-primary-500',
          'transition-colors',
          isUploading && 'opacity-50 cursor-not-allowed'
        )}
        title="Remove attachment"
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
}

interface FileIconProps {
  mimeType: string;
}

function FileIcon({ mimeType }: FileIconProps) {
  const iconClass = 'w-4 h-4 text-surface-500 dark:text-surface-400';

  if (mimeType.startsWith('image/')) {
    return <ImageIcon className={cn(iconClass, 'text-blue-500')} />;
  }

  if (mimeType === 'application/pdf') {
    return <FileText className={cn(iconClass, 'text-red-500')} />;
  }

  if (
    mimeType.includes('spreadsheet') ||
    mimeType.includes('excel') ||
    mimeType === 'text/csv'
  ) {
    return <FileSpreadsheet className={cn(iconClass, 'text-green-500')} />;
  }

  if (mimeType.includes('wordprocessingml') || mimeType === 'text/plain' || mimeType === 'text/markdown') {
    return <FileText className={cn(iconClass, 'text-blue-500')} />;
  }

  return <File className={iconClass} />;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/**
 * Compact version of AttachmentPreview for display in sent messages
 */
interface AttachmentChipsProps {
  attachments: Array<{
    name: string;
    type: 'image' | 'document';
    mimeType: string;
  }>;
  className?: string;
}

export function AttachmentChips({ attachments, className }: AttachmentChipsProps) {
  if (!attachments || attachments.length === 0) return null;

  return (
    <div className={cn('flex flex-wrap gap-1.5 mt-2', className)}>
      {attachments.map((att, idx) => (
        <div
          key={idx}
          className={cn(
            'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs',
            att.type === 'image'
              ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
              : 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300'
          )}
        >
          {att.type === 'image' ? (
            <ImageIcon className="w-3 h-3" />
          ) : (
            <FileText className="w-3 h-3" />
          )}
          <span className="max-w-[100px] truncate">{att.name}</span>
        </div>
      ))}
    </div>
  );
}
