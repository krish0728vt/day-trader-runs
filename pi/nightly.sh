#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
git pull -q --ff-only --autostash || true
/usr/bin/python3 nightly.py --log logs/nightly.log
bash pi/push.sh "nightly"
