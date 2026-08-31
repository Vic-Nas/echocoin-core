#!/usr/bin/env python3
"""Hourly uptime-reward payout for peers that can't mine yet.

Not a node feature -- a standalone script meant to run from cron against
your own already-running node, driving its existing HTTP APIs. No protocol
or node code changes, and it isn't part of the PyInstaller build.

The budget is tracked explicitly in a small local state file, separate from
whatever the reward wallet's real on-chain balance happens to be (mining
income, other transfers, etc. don't inflate or shrink it). It starts at
BUDGET_LAPSE the first time this runs and is decremented by whatever it
actually pays out each hour.

Each run:
  1. Loads remaining_ticks from the state file (seeds it at BUDGET_LAPSE if
     the file doesn't exist yet).
  2. Snapshots peers currently active (seen within the last hour).
  3. Drops any peer already holding >= BALANCE_CAP_LAPSE, so the budget goes
     to nodes that actually need it rather than topping up existing holders.
  4. Splits pool_amount = remaining_ticks * (1 - 0.5 ** (1 / HALFLIFE_HOURS))
     evenly across the survivors, so the payout decays with the tracked
     budget and approaches zero as it's spent, with no hard cutoff to manage.
  5. Clamps pool_amount to the reward wallet's actual current balance too,
     as a safety net in case the two ever drift.
  6. Sends each its share at fee 0 via the private wallet's /send form, then
     subtracts what was actually sent from remaining_ticks and saves it.

Requires LAPSECOIN_PASSPHRASE set to the same passphrase the node itself
uses (needed to sign the payout transaction).
"""

import json
import os
import re
import sys
import time

import requests

PUBLIC_URL  = os.environ.get("LAPSECOIN_PUBLIC_URL", "http://127.0.0.1:8333")
PRIVATE_URL = os.environ.get("LAPSECOIN_PRIVATE_URL", "http://127.0.0.1:8335")
PASSPHRASE  = os.environ.get("LAPSECOIN_PASSPHRASE", "")

TICKS_PER_LAPSE   = 100_000_000
BUDGET_LAPSE      = float(os.environ.get("LAPSECOIN_REWARD_BUDGET_LAPSE", "1430"))
HALFLIFE_HOURS    = int(os.environ.get("LAPSECOIN_REWARD_HALFLIFE_HOURS", str(24 * 30)))
BALANCE_CAP_LAPSE = float(os.environ.get("LAPSECOIN_REWARD_BALANCE_CAP_LAPSE", "20"))
ACTIVE_WINDOW_S   = 3600      # "active" = seen within the last hour

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "uptime_rewards_state.json")

CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')


def load_remaining_ticks():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)["remaining_ticks"]
    remaining = int(BUDGET_LAPSE * TICKS_PER_LAPSE)
    save_remaining_ticks(remaining)
    return remaining


def save_remaining_ticks(remaining_ticks):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"remaining_ticks": remaining_ticks}, f)
    os.replace(tmp, STATE_FILE)


def get_active_peer_wallets():
    """Wallet addresses of peers seen within ACTIVE_WINDOW_S, deduped."""
    now = time.time()
    wallets = set()
    page = 1
    while True:
        r = requests.get(f"{PUBLIC_URL}/api/peers", params={"page": page}, timeout=10)
        r.raise_for_status()
        data = r.json()
        for p in data["peers"]:
            wallet = p["wallet"] or p["inferred_wallet"]
            if wallet and now - p["last_seen"] <= ACTIVE_WINDOW_S:
                wallets.add(wallet)
        total_pages = -(-data["peer_count"] // 8) or 1
        if page >= total_pages:
            break
        page += 1
    return wallets


def get_balance_ticks(addr):
    r = requests.get(f"{PUBLIC_URL}/api/address/{addr}/balance", timeout=10)
    r.raise_for_status()
    return r.json()["balance_ticks"]


def send_payout(outputs_csv):
    """outputs_csv: 'addr,ticks\\n...'. Uses the private /send form (fee 0)."""
    get = requests.get(f"{PRIVATE_URL}/send", timeout=10)
    get.raise_for_status()
    m = CSRF_RE.search(get.text)
    if not m:
        print("could not find csrf token on /send page", file=sys.stderr)
        return False
    resp = requests.post(f"{PRIVATE_URL}/send", data={
        "csrf_token": m.group(1),
        "outputs": outputs_csv,
        "fee": "0",
        "passphrase": PASSPHRASE,
    }, timeout=15)
    resp.raise_for_status()
    ok = 'class="alert alert-err"' not in resp.text
    if not ok:
        print("send failed, response did not confirm success", file=sys.stderr)
    return ok


def main():
    if not PASSPHRASE:
        print("LAPSECOIN_PASSPHRASE not set", file=sys.stderr)
        return 1

    remaining_ticks = load_remaining_ticks()
    if remaining_ticks <= 0:
        print("budget exhausted, nothing to pay out")
        return 0

    reward_addr = requests.get(f"{PUBLIC_URL}/api/info", timeout=10).json()["address"]
    wallet_balance = get_balance_ticks(reward_addr)

    candidates = get_active_peer_wallets() - {reward_addr}
    eligible = [
        addr for addr in candidates
        if get_balance_ticks(addr) < BALANCE_CAP_LAPSE * TICKS_PER_LAPSE
    ]
    if not eligible:
        print("no eligible peers this hour")
        return 0

    pool_amount = int(remaining_ticks * (1 - 0.5 ** (1 / HALFLIFE_HOURS)))
    pool_amount = min(pool_amount, wallet_balance)
    if pool_amount <= 0:
        print("pool amount rounded to zero (budget nearly spent, or wallet balance too low)")
        return 0

    share = pool_amount // len(eligible)
    if share <= 0:
        print("per-peer share rounded to zero")
        return 0

    outputs_csv = "\n".join(f"{addr},{share}" for addr in eligible)
    if send_payout(outputs_csv):
        sent = share * len(eligible)
        remaining_ticks -= sent
        save_remaining_ticks(remaining_ticks)
        print(f"paid {share / TICKS_PER_LAPSE:.4f} LAPSE to {len(eligible)} peer(s), "
              f"budget remaining {remaining_ticks / TICKS_PER_LAPSE:.4f} LAPSE")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
