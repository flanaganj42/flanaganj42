#!/bin/bash
set -e
CIVICR_API_KEY="$(cat /etc/civicresilience-key)"
export CIVICR_API_KEY
exec /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    -m uvicorn main:app --host 127.0.0.1 --port 8080 --app-dir /Users/jamesflanagan/api
