-- Export Apple Notes modified within the given day window (default 7 if no arg;
-- the wrapper passes a watermark-derived window seeded from NOTES_DAYS_WINDOW)
-- to the staging folder.
-- Run via: osascript export_notes.applescript [days [timeout_secs [output_folder]]]
-- The wrapper passes its resolved $STAGING as output_folder so the export
-- lands where the wrapper polls; absent, it falls back to the default below.
-- Emits one JSON sidecar per note alongside an HTML body file.

on run argv
    set dayWindow to 7
    set timeoutSecs to 3600
    set outputFolder to (POSIX path of (path to home folder)) & "estormi-staging/notes/"
    if (count of argv) > 0 then
        try
            set dayWindow to (item 1 of argv) as integer
        end try
    end if
    if (count of argv) > 1 then
        try
            set timeoutSecs to (item 2 of argv) as integer
        end try
    end if
    if (count of argv) > 2 then
        set outputFolder to (item 3 of argv) as text
        if outputFolder does not end with "/" then set outputFolder to outputFolder & "/"
    end if

    do shell script "mkdir -p " & quoted form of outputFolder

    set cutoffDate to (current date) - (dayWindow * days)
    set exportedCount to 0

    with timeout of timeoutSecs seconds
        tell application "Notes"
            repeat with aNote in (every note whose modification date > cutoffDate)
                try
                    set noteTitle to name of aNote
                    set noteBody to body of aNote
                    set noteDate to modification date of aNote
                    set noteId to id of aNote
                    set safeId to my sanitizeId(noteId)

                    set tmpFile to outputFolder & safeId & ".html"
                    my writeText(tmpFile, noteBody)

                    set metaFile to outputFolder & safeId & ".meta.json"
                    set isoDate to my isoOf(noteDate)
                    set jsonStr to "{\"title\":" & my jsonEscape(noteTitle) & ",\"date\":\"" & isoDate & "\",\"id\":\"" & safeId & "\"}"
                    my writeText(metaFile, jsonStr)

                    set exportedCount to exportedCount + 1
                end try
            end repeat
        end tell
    end timeout

    return (exportedCount as string) & " notes exported to " & outputFolder
end run

on sanitizeId(s)
    set s to s as string
    set AppleScript's text item delimiters to "/"
    set parts to text items of s
    set AppleScript's text item delimiters to "_"
    set s to parts as string
    set AppleScript's text item delimiters to ""
    return s
end sanitizeId

on isoOf(d)
    -- AppleScript gives local time; convert to UTC. In Paris summer time,
    -- time to GMT is +7200, so 19:00 local must become 17:00Z.
    set utcDate to d - (time to GMT)
    set y to year of utcDate as string
    set m to text -2 thru -1 of ("0" & ((month of utcDate) as integer))
    set dd to text -2 thru -1 of ("0" & (day of utcDate))
    set hh to text -2 thru -1 of ("0" & (hours of utcDate))
    set mm to text -2 thru -1 of ("0" & (minutes of utcDate))
    set ss to text -2 thru -1 of ("0" & (seconds of utcDate))
    return y & "-" & m & "-" & dd & "T" & hh & ":" & mm & ":" & ss & "Z"
end isoOf

on jsonEscape(s)
    set s to s as string
    set out to ""
    repeat with i from 1 to length of s
        set ch to character i of s
        if ch is "\"" then
            set out to out & "\\\""
        else if ch is "\\" then
            set out to out & "\\\\"
        else if ch is return or ch is linefeed then
            set out to out & "\\n"
        else if ch is tab then
            set out to out & "\\t"
        else if (id of ch) < 32 then
            -- Any other C0 control char: emit a strict-JSON \u00XX escape so
            -- downstream json.loads never chokes and poison-pills the watermark.
            set cp to id of ch
            set hexDigits to "0123456789abcdef"
            set out to out & "\\u00" & (character ((cp div 16) + 1) of hexDigits) & (character ((cp mod 16) + 1) of hexDigits)
        else
            set out to out & ch
        end if
    end repeat
    return "\"" & out & "\""
end jsonEscape

on writeText(filePath, txt)
    set fileRef to open for access (POSIX file filePath) with write permission
    try
        set eof of fileRef to 0
        write txt to fileRef as «class utf8»
    on error errMsg
        close access fileRef
        error errMsg
    end try
    close access fileRef
end writeText
