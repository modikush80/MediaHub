#!/bin/bash
# Double-click this file in Finder to launch MediaHub.
# It starts the local server and opens the app in your browser.
cd "$(dirname "$0")"
echo "Starting MediaHub..."
exec python3 -m mediahub
