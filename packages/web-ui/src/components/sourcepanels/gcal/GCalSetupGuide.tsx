/**
 * GCalSetupGuide — the collapsible step-by-step guide for minting a Desktop-app
 * Google OAuth client. Pure static markup; extracted from GoogleCalendarPanel's
 * GCalSetupBlock so the upload target and the guide are each readable on their
 * own.
 */

const linkStyle: React.CSSProperties = {
  color: 'var(--or-clair)',
  textDecoration: 'underline',
  textDecorationColor: 'var(--gilt-line)',
}
const codeStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono)',
  fontSize: 13,
  padding: '1px 5px',
  background: 'var(--well-deepest)',
  border: '1px solid var(--gilt-line)',
  color: 'var(--parchemin)',
}

export function GCalSetupGuide() {
  return (
    <div
      style={{
        marginTop: 12,
        padding: '14px 16px 16px',
        background: 'var(--well-sunk)',
        border: '1px solid var(--gilt-line)',
        fontFamily: 'var(--font-ui)',
        fontSize: 14,
        color: 'var(--ink-dim)',
        lineHeight: 1.65,
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-display)',
          fontSize: 11,
          letterSpacing: '0.22em',
          color: 'var(--or-ancien)',
          textTransform: 'uppercase',
          marginBottom: 10,
        }}
      >
        How to mint a Desktop-app OAuth client (~5 minutes, free)
      </div>

      <p style={{ margin: '0 0 10px' }}>
        All five steps happen in Google Cloud Console. The OAuth client
        you create stays in your Google account; Estormi only sees the JSON
        you upload here.
      </p>

      <ol style={{ margin: 0, paddingLeft: 22, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <li>
          <strong style={{ color: 'var(--parchemin)' }}>
            Create or pick a Google Cloud project.
          </strong>
          <div>
            Open{' '}
            <a
              href="https://console.cloud.google.com/projectcreate"
              target="_blank"
              rel="noopener noreferrer"
              style={linkStyle}
            >
              console.cloud.google.com/projectcreate
            </a>
            . Name it anything (e.g. <em>Estormi</em>). If you already have
            a project you don't mind reusing, skip this step — just make
            sure it's selected in the top-bar dropdown for the next steps.
            The project is free and incurs no costs from Calendar API
            usage at your scale.
          </div>
        </li>

        <li>
          <strong style={{ color: 'var(--parchemin)' }}>
            Enable the Google Calendar API.
          </strong>
          <div>
            Open{' '}
            <a
              href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"
              target="_blank"
              rel="noopener noreferrer"
              style={linkStyle}
            >
              the Calendar API library page
            </a>{' '}
            with your project selected and click <strong>Enable</strong>.
            Without this, OAuth will succeed but every API call returns
            "API not enabled".
          </div>
        </li>

        <li>
          <strong style={{ color: 'var(--parchemin)' }}>
            Configure the OAuth consent screen.
          </strong>
          <div>
            Open{' '}
            <a
              href="https://console.cloud.google.com/apis/credentials/consent"
              target="_blank"
              rel="noopener noreferrer"
              style={linkStyle}
            >
              APIs &amp; Services → OAuth consent screen
            </a>
            . Pick <strong>External</strong> (works for any Gmail account
            you own — also fine for personal Google Workspace accounts).
            Fill in <em>App name</em> (e.g. "Estormi"), your support
            email, and your developer email. Skip every optional field.
            Under <strong>Scopes</strong> you can leave the default — the
            scope Estormi actually requests
            (<code style={codeStyle}>auth/calendar.readonly</code>) is
            granted at OAuth time, not at registration. Save and continue.
          </div>
          <div
            style={{
              marginTop: 10,
              padding: '10px 12px',
              background: 'rgba(160,72,72,0.10)',
              border: '1px solid var(--pourpre-doux)',
              color: 'var(--parchemin)',
              fontSize: 14,
              lineHeight: 1.55,
            }}
          >
            <div style={{ fontWeight: 700, marginBottom: 4 }}>
              ⚠ Critical sub-step — read this before you close the page:
            </div>
            After the consent screen is saved, Google parks the app in{' '}
            <strong>Testing</strong> status by default. In that state, ONLY
            Gmail addresses you've explicitly listed as test users can
            authenticate; everyone else (including you) hits the
            <code style={codeStyle}>403 access_denied</code> screen the
            moment they click "Allow".
            <br />
            <strong>Do one of these now:</strong>
            <ul style={{ margin: '6px 0 0 16px', padding: 0 }}>
              <li>
                Scroll to <em>Test users</em>, click{' '}
                <strong>+ Add users</strong>, paste the Gmail address
                you'll connect with, save. Up to 100 testers; never
                expires.
              </li>
              <li>
                <em>Or</em> click <strong>Publish app</strong> at the top
                of the consent screen — Estormi only requests the read-only
                Calendar scope, which Google does NOT consider sensitive,
                so verification is skipped and anyone on your own Google
                account can connect immediately.
              </li>
            </ul>
          </div>
        </li>

        <li>
          <strong style={{ color: 'var(--parchemin)' }}>
            Create the OAuth client ID — Desktop app.
          </strong>
          <div>
            Open{' '}
            <a
              href="https://console.cloud.google.com/apis/credentials"
              target="_blank"
              rel="noopener noreferrer"
              style={linkStyle}
            >
              APIs &amp; Services → Credentials
            </a>{' '}
            → <strong>Create Credentials</strong> →{' '}
            <strong>OAuth client ID</strong>. For{' '}
            <strong>Application type</strong> pick{' '}
            <em>Desktop app</em> (NOT "Web application" — Estormi uses a
            loopback redirect to localhost which only the Desktop flow
            supports without extra registration). Give it any name. Click{' '}
            <strong>Create</strong>.
          </div>
        </li>

        <li>
          <strong style={{ color: 'var(--parchemin)' }}>
            Download the JSON and drop it above.
          </strong>
          <div>
            The dialog that appears will offer a <strong>Download
            JSON</strong> button (also: the row for the new client in
            Credentials has a ⤓ icon on the far right). Save it
            anywhere on disk, then drag it into the dashed target above
            — or click the target and pick the file. Estormi validates
            it has the right shape (must be a Desktop app), writes it to{' '}
            <code style={codeStyle}>
              ~/Library/Application Support/Estormi/google_client_secrets.json
            </code>
            , and unlocks the "Connect with Google" button.
          </div>
        </li>
      </ol>

      <div
        style={{
          marginTop: 14,
          padding: '10px 12px',
          background: 'rgba(196,154,58,0.06)',
          border: '1px solid var(--gilt-line)',
          fontSize: 13,
          color: 'var(--ink-dim)',
          lineHeight: 1.55,
        }}
      >
        <strong style={{ color: 'var(--or-clair)' }}>Privacy &amp; cost.</strong>{' '}
        Your OAuth client is a tiny credentials file you stay in control
        of — it doesn't grant Google any visibility into Estormi. The
        Calendar API has a free quota of 1,000,000 requests/day; even an
        enthusiastic daily ingest uses a fraction of that. Estormi reads
        your calendars; it never writes events.
      </div>
    </div>
  )
}
