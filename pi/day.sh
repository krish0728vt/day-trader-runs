#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
git pull -q --ff-only --autostash || true
exec /usr/bin/python3 month.py --log logs/month.log
