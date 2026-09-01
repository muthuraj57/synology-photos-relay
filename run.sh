#!/bin/sh
# Launch on the NAS with:  setsid ./run.sh </dev/null >/dev/null 2>&1 &
cd "$(dirname "$0")" || exit 1
exec /usr/bin/env python3 relay.py >> relay.log 2>&1
