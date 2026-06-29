/**
 * GoogleCalendarPanel — the Google Calendar source connection block, extracted
 * from SourceManageModal.tsx.
 *
 * Google Calendar needs an OAuth client-secrets upload (GCalSetupBlock) before
 * the consent flow, then a per-calendar selection + life-context tagging list
 * (GCalCalendarList). A single /api/google-calendar probe drives the connection
 * state machine, which lives in useGoogleCalendar. The shared OAuthConnectionBox
 * frames the connection states.
 */
import { OAuthConnectionBox } from './OAuthConnectionBox'
import { useGoogleCalendar } from './gcal/useGoogleCalendar'
import { GCalSetupBlock } from './gcal/GCalSetupBlock'
import { GCalCalendarList } from './gcal/GCalCalendarList'

export function GoogleCalendarPanel({ onChanged }: { onChanged?: () => void }) {
  const {
    state,
    error,
    calendars,
    setError,
    probe,
    startConnect,
    disconnect,
    toggleCalendar,
    setCalendarGroup,
  } = useGoogleCalendar(onChanged)

  return (
    <>
      <OAuthConnectionBox
        service="Google Calendar"
        connectWith="Google"
        state={state}
        error={error}
        onProbe={() => void probe()}
        onConnect={() => void startConnect()}
        onDisconnect={() => void disconnect()}
        disconnectedBody={
          <>
            Click below to open the Google consent screen in your default
            browser. Once you grant access, Estormi will list your calendars
            here.
          </>
        }
        connectedBody={
          <>Token persisted to the data directory. Disconnect below to revoke.</>
        }
        setup={
          <GCalSetupBlock
            onUploaded={() => {
              setError(null)
              void probe()
            }}
            onError={(msg) => setError(msg)}
          />
        }
      />

      {state === 'connected' && (
        <GCalCalendarList
          calendars={calendars}
          error={error}
          onToggle={(id, selected) => void toggleCalendar(id, selected)}
          onSetGroup={(id, group_type) => void setCalendarGroup(id, group_type)}
        />
      )}
    </>
  )
}
