#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
npm run check
npx wrangler deploy --dry-run
# Only invoke this script when deployment to the configured account is authorized.
exec npx wrangler deploy "$@"
