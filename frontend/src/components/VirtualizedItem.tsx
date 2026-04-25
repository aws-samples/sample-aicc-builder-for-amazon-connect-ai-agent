/**
 * VirtualizedItem Component
 *
 * Uses IntersectionObserver to only render children when visible in viewport.
 * When off-screen, renders a placeholder div preserving measured height.
 */

import { useState, useRef, useEffect, type ReactNode, type RefObject, memo } from 'react';

interface VirtualizedItemProps {
  children: ReactNode;
  rootRef: RefObject<HTMLDivElement | null>;
  estimatedHeight?: number;
}

export const VirtualizedItem = memo(function VirtualizedItem({
  children,
  rootRef,
  estimatedHeight = 80,
}: VirtualizedItemProps) {
  const [isVisible, setIsVisible] = useState(false);
  const [measuredHeight, setMeasuredHeight] = useState<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const observerRef = useRef<IntersectionObserver | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    observerRef.current = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
        } else if (measuredHeight !== null) {
          // Only hide if we've measured the height (so placeholder is accurate)
          setIsVisible(false);
        }
      },
      {
        root: rootRef.current,
        rootMargin: '200px 0px', // Pre-render 200px before entering viewport
      }
    );

    observerRef.current.observe(el);
    return () => observerRef.current?.disconnect();
  }, [rootRef, measuredHeight]);

  // Measure height when visible
  useEffect(() => {
    if (isVisible && containerRef.current) {
      const h = containerRef.current.getBoundingClientRect().height;
      if (h > 0) setMeasuredHeight(h);
    }
  }, [isVisible, children]);

  return (
    <div ref={containerRef}>
      {isVisible ? (
        children
      ) : (
        <div style={{ height: measuredHeight ?? estimatedHeight }} />
      )}
    </div>
  );
});
