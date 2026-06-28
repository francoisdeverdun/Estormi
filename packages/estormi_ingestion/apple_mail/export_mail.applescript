-- Export recent Apple Mail messages to the staging folder.
-- Run via: osascript export_mail.applescript [days [timeout_secs [output_folder]]]
-- The wrapper passes its resolved $STAGING as output_folder so the export
-- lands where the wrapper polls; absent, it falls back to the default below.
-- Emits one .meta.json + one .txt (body) per message.

on run argv
    set dayWindow to 3
    set timeoutSecs to 3600
    set outputFolder to (POSIX path of (path to home folder)) & "estormi-staging/mail/"
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
        tell application "Mail"
            set theAccounts to {}
            try
                set theAccounts to every account
            end try
            repeat with anAccount in theAccounts
                -- walk every mailbox in the account (INBOX, Archive, Sent, etc.)
                set allBoxes to {}
                try
                    set allBoxes to every mailbox of anAccount
                end try

                repeat with aBox in allBoxes
                    try
                        set boxName to name of aBox
                        -- AppleScript string comparisons are case-insensitive by default
                        if boxName is "Trash" or boxName is "Deleted Messages" or boxName is "Corbeille" or boxName is "Deleted Items" then
                            -- skip trash / deleted items
                        else
                            set recentMessages to (messages of aBox whose date received > cutoffDate)
                            repeat with aMsg in recentMessages
                                try
                                    set msgId to message id of aMsg
                                    set msgSubject to subject of aMsg
                                    set msgSender to sender of aMsg
                                    set msgDate to date received of aMsg
                                    set msgBody to content of aMsg
                                    set msgHeaders to ""
                                    try
                                        set msgHeaders to all headers of aMsg
                                    end try

                                    set safeId to my sanitizeId(msgId)
                                    set bodyFile to outputFolder & safeId & ".txt"
                                    my writeText(bodyFile, msgBody)

                                    set metaFile to outputFolder & safeId & ".meta.json"
                                    set isoDate to my isoOf(msgDate)
                                    set jsonStr to "{\"title\":" & my jsonEscape(msgSubject) & ",\"from\":" & my jsonEscape(msgSender) & ",\"date\":\"" & isoDate & "\",\"id\":\"" & safeId & "\",\"headers\":" & my jsonEscape(msgHeaders) & "}"
                                    my writeText(metaFile, jsonStr)

                                    set exportedCount to exportedCount + 1
                                end try
                            end repeat
                        end if
                    end try
                end repeat
            end repeat
        end tell
    end timeout

    return (exportedCount as string) & " messages exported to " & outputFolder
end run

on sanitizeId(s)
    set s to s as string
    set AppleScript's text item delimiters to {"/", "\\", ":", "<", ">", "@", " "}
    set parts to text items of s
    set AppleScript's text item delimiters to "_"
    set s to parts as string
    set AppleScript's text item delimiters to ""
    if (length of s) > 200 then set s to text 1 thru 200 of s
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
