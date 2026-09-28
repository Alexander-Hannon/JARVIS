// Inline SVG icons (no emoji: they render inconsistently across platforms).
const base = { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true };

export function PauseIcon() {
  return (
    <svg {...base}>
      <rect x="6" y="5" width="4" height="14" rx="1" />
      <rect x="14" y="5" width="4" height="14" rx="1" />
    </svg>
  );
}
export function PlayIcon() {
  return (
    <svg {...base}>
      <path d="M7 5l12 7-12 7z" />
    </svg>
  );
}
export function BellIcon() {
  return (
    <svg {...base}>
      <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
      <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
    </svg>
  );
}
