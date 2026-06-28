/**
 * Shared loading / empty / error states. Every async-rendered block on every
 * page wraps its content in one of these so the visual language is
 * consistent.
 */
import React from 'react'
import { Fleuron, Diamond } from './marks'

export interface EmptyStateProps {
  title?: string
  body?: React.ReactNode
  action?: React.ReactNode
}

export function EmptyState({
  title = 'Nothing yet',
  body,
  action,
}: EmptyStateProps) {
  return (
    <div
      style={{
        padding: '40px 24px',
        textAlign: 'center',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 14,
        color: 'var(--ink-dimmer)',
      }}
    >
      <Diamond size={10} color="var(--or-sombre)" filled={false} />
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 16,
          letterSpacing: '0.24em',
          color: 'var(--or-ancien)',
          textTransform: 'uppercase',
        }}
      >
        {title}
      </div>
      {body && (
        <div
          style={{
            fontFamily: 'var(--font-body)',
            fontSize: 17,
            lineHeight: 1.55,
            color: 'var(--ink-dim)',
            maxWidth: 480,
          }}
        >
          {body}
        </div>
      )}
      {action}
    </div>
  )
}

export interface LoadingStateProps {
  label?: string
}

export function LoadingState({ label = 'Loading' }: LoadingStateProps) {
  return (
    <div
      style={{
        padding: '40px 24px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 12,
        fontFamily: 'var(--font-display)',
        fontSize: 13,
        letterSpacing: '0.32em',
        color: 'var(--ink-dim)',
        textTransform: 'uppercase',
      }}
      aria-busy="true"
    >
      <Fleuron size={10} color="var(--or-ancien)" opacity={0.6} />
      {label}…
      <Fleuron size={10} color="var(--or-ancien)" opacity={0.6} />
    </div>
  )
}

export interface ErrorStateProps {
  message?: string
  detail?: React.ReactNode
  onRetry?: () => void
}

export function ErrorState({ message = 'Something went wrong', detail, onRetry }: ErrorStateProps) {
  return (
    <div
      role="alert"
      style={{
        padding: '20px 22px',
        background: 'rgba(184, 46, 46, 0.06)',
        border: '1px solid var(--pourpre-fonce)',
        borderLeft: '3px solid var(--pourpre)',
        borderRadius: 'var(--radius-tight)',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 13,
          letterSpacing: '0.24em',
          color: 'var(--pourpre-clair)',
          textTransform: 'uppercase',
        }}
      >
        {message}
      </div>
      {detail && (
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 13,
            color: 'var(--ink-dim)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {detail}
        </div>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          style={{
            alignSelf: 'flex-start',
            padding: '6px 14px',
            background: 'transparent',
            border: '1px solid var(--pourpre)',
            borderRadius: 'var(--radius-tight)',
            color: 'var(--pourpre-clair)',
            fontFamily: 'var(--font-display)',
            fontSize: 12,
            letterSpacing: '0.2em',
            textTransform: 'uppercase',
          }}
        >
          Retry
        </button>
      )}
    </div>
  )
}
