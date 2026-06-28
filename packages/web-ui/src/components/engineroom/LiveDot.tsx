/**
 * LiveDot — a glowing status dot with an optional pulsing ring while a job is
 * live. Shared by the engine-room surfaces (popover, log modal, procession)
 * and the top-bar engine/wake indicators. Extracted from StageProcession.tsx so
 * the OnePagerTopBar no longer carries a byte-identical copy.
 */
export function LiveDot({
  running,
  color,
  size,
}: {
  running: boolean
  color: string
  size: number
}) {
  return (
    <span
      style={{
        position: 'relative',
        width: size,
        height: size,
        display: 'inline-block',
        flexShrink: 0,
      }}
    >
      <span
        style={{
          position: 'absolute',
          inset: 0,
          background: color,
          borderRadius: '50%',
          boxShadow: `0 0 6px ${color}`,
        }}
      />
      {running && (
        <span
          style={{
            position: 'absolute',
            inset: -3,
            border: `1.5px solid ${color}`,
            borderRadius: '50%',
            opacity: 0.55,
            animation: 'estormi-live-pulse 1.4s ease-out infinite',
          }}
        />
      )}
    </span>
  )
}
