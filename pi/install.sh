#!/bin/bash
set -e
cd "$(dirname "$0")/.."
REPO="$(pwd)"
[ "$REPO" = "$HOME/day-trader-runs" ] || { echo "Clone the repo to ~/day-trader-runs first."; exit 1; }
python3 -c "import sys; assert sys.version_info >= (3, 9)" || { echo "Needs Python 3.9 or newer."; exit 1; }
git config user.name "pi day-trader"
git config user.email "pi-day-trader@users.noreply.github.com"
mkdir -p trader/history logs
if [ -f "$HOME/pi_history.tar.gz" ]; then
  echo "Unpacking the saved 5-minute history..."
  tar xzf "$HOME/pi_history.tar.gz" -C trader
  echo "  $(ls trader/history | wc -l) stocks"
fi
sudo cp pi/day-trader*.service pi/day-trader*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now day-trader.timer day-trader-nightly.timer day-trader-push.timer
echo
echo "Building tonight's stocks-in-play pool (10-30 minutes on a Pi 2)..."
python3 nightly.py --force --log logs/nightly.log
bash pi/push.sh "install"
echo
systemctl list-timers 'day-trader*' --no-pager
echo
echo "Installed. The day starts on its own at 9:31 AM New York time on weekdays."
