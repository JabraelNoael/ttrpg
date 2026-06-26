#!/bin/zsh
# Relaunch the TTRPG UI cleanly.
#   ./start.sh            reopen your NEWEST saved world
#   ./start.sh 1024       reopen a specific session (id or name)
#   ./start.sh new        start a fresh empty world
# Kills any running/suspended ui.py servers first so the port is free, then runs in THIS
# terminal (so it's also a live command shell, just like before).
cd "$(dirname "$0")" || exit 1

echo "stopping any running UI servers…"
pkill -9 -f "[u]i\.py" 2>/dev/null
sleep 0.6   # let the TCP port release

echo "launching…"
exec python3 ui.py "$@"
