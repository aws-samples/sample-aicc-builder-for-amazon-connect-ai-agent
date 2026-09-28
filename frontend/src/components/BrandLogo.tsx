import { cn } from '../lib/utils';

type BrandLogoProps = {
  variant?: 'agent' | 'symbol';
  className?: string;
};

const LOGO_SOURCES = {
  agent: '/brand/aicc-builder-agent.png?v=transparent-2',
  symbol: '/brand/aicc-builder-symbol.png?v=transparent-2',
} as const;

export function BrandLogo({ variant = 'agent', className }: BrandLogoProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex flex-shrink-0 items-center justify-center overflow-visible',
        className
      )}
    >
      <img
        src={LOGO_SOURCES[variant]}
        alt=""
        draggable={false}
        className="block h-full w-full select-none object-contain"
      />
    </span>
  );
}
