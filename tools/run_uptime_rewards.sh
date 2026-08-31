#!/bin/sh
# Cron entry point for uptime_rewards.py.
#
# Sources the passphrase from uptime_rewards.env (chmod 600, gitignored) so
# it works unattended after a reboot with no manual re-entry, and takes an
# flock lock so a slow/overlapping run can't fire twice for the same hour.
#
# Crontab (crontab -e), runs hourly and survives reboots as long as cron
# itself is enabled to start at boot:
#   0 * * * * /path/to/lapsecoin/tools/run_uptime_rewards.sh >> /path/to/lapsecoin/tools/uptime_rewards.log 2>&1
set -eu
cd "$(dirname "$0")/.."
. tools/uptime_rewards.env
exec flock -n /tmp/lapsecoin_uptime_rewards.lock python3 tools/uptime_rewards.py
