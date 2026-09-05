#!/usr/bin/env bash
# Serve the route map. The page fetches its bundle from /worlds/<seed>/, so the server has to
# be rooted at the repo, not at routemap/. fetch() is blocked on file:// -- opening index.html
# directly will not work.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-8502}
echo "route map: http://localhost:$PORT/routemap/"
exec python3 -m http.server "$PORT"
