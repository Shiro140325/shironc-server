#!/bin/sh
# Runs one app for launch.py, as that app's user. Render's container doesn't
# let root signal another user's processes, so the launcher asks for a stop
# by closing this script's stdin instead; this script (same user as the app)
# then stops the app: SIGTERM, and SIGKILL if it's still running 20s later.
exec 3<&0
"$@" </dev/null 3<&- &
app=$!
{ cat <&3 >/dev/null; kill -TERM "$app" 2>/dev/null; sleep 20; kill -KILL "$app" 2>/dev/null; } &
watcher=$!
exec 3<&-
wait "$app"
code=$?
kill "$watcher" 2>/dev/null
exit $code
