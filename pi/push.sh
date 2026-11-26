#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
git add -A trader/month trader/month-dry trader/sip/stats.json trader/sip/pools logs 2>/dev/null
git diff --cached --quiet || git commit -q -m "${1:-update} $(TZ=America/New_York date '+%Y-%m-%d %H:%M ET')"
git pull -q --rebase --autostash || git rebase --abort
git push -q || echo "push failed (will try again next time)"
