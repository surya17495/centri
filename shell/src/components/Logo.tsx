/** CENTRI mark — a centered core with an orbit, the "one brain" the hands orbit. */
export function Logo({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-label="CENTRI"
      role="img"
    >
      <circle cx="12" cy="12" r="9.5" stroke="currentColor" strokeWidth="1.5" opacity="0.35" />
      <circle cx="12" cy="12" r="3" fill="currentColor" />
      <circle cx="12" cy="2.5" r="1.8" fill="currentColor" opacity="0.9" />
    </svg>
  );
}
