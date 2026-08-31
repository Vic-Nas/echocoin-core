#!/bin/sh
# Cron entry point for uptime_rewards.py.
#
# Expects LAPSECOIN_PASSPHRASE already set in the environment this runs
# under (e.g. a line in crontab itself, or Environment= in a systemd timer
# unit) -- same as how the node itself takes it non-interactively. Takes an
# flock lock so a slow or overlapping run can't fire twice for the same hour.
#
# Crontab (crontab -e), runs hourly and survives reboots as long as cron
# itself is enabled to start at boot:
#   LAPSECOIN_PASSPHRASE=your passphrase
#   0 * * * * /path/to/lapsecoin/tools/run_uptime_rewards.sh >> /path/to/lapsecoin/tools/uptime_rewards.log 2>&1
set -eu
: "${LAPSECOIN_PASSPHRASE:?LAPSECOIN_PASSPHRASE not set in the environment}"
cd "$(dirname "$0")/.."
exec flock -n /tmp/lapsecoin_uptime_rewards.lock python3 tools/uptime_rewards.py
