#!/usr/bin/env python3
"""Hourly uptime-reward payout for peers that can't mine yet.

Not a node feature -- a standalone script meant to run from cron against
your own already-running node, driving its existing HTTP APIs. No protocol
or node code changes, and it isn't part of the PyInstaller build.

The budget is tracked explicitly in a small local state file, separate from
whatever the reward wallet's real on-chain balance happens to be (mining
income, other transfers, etc. don't inflate or shrink it). It starts at
BUDGET_LAPSE the first time this runs and is decremented only once a payout
is confirmed to have actually landed on chain -- see the pending-tx check
below, which exists because a tx accepted into the local mempool can still
silently fail to propagate (Dandelion's stem hop is fire-and-forget with no
retry) and expire unconfirmed 30 minutes later with nothing to show for it.

Each run:
  1. Loads state (remaining_ticks, and an optional pending {tx_hash, amount}
     left over from the previous run).
  2. If there's a pending tx from last run: by now (>= 1h later, well past
     the mempool's 30-minute TTL) it must be either confirmed or gone.
     Checks /api/tx/<hash> -- 404 means it never confirmed, so its amount is
     refunded back into remaining_ticks; 200 means it landed, so the earlier
     debit stands. Either way the pending marker is cleared. If this check
     itself fails (e.g. node briefly unreachable), the run stops here rather
     than risking a second payout stacking on an unresolved one.
  3. Snapshots peers currently active (seen within the last hour).
  4. Drops the builder of the current tip block -- it just mined successfully,
     so it isn't one of the non-mining nodes this exists for -- and any peer
     whose balance is not lower than the reward wallet's own current balance,
     so the budget goes to nodes that actually need it rather than topping
     up ones already doing fine on their own.
  5. Splits pool_amount = remaining_ticks * (1 - 0.5 ** (1 / HALFLIFE_HOURS))
     evenly across the survivors, so the payout decays with the tracked
     budget and approaches zero as it's spent, with no hard cutoff to manage.
  6. Clamps pool_amount to the reward wallet's actual current balance too,
     as a safety net in case the two ever drift.
  7. Sends each its share at fee 0 via the private wallet's /send form,
     debits remaining_ticks, and records the new tx as pending for next
     run's confirmation check.

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
ACTIVE_WINDOW_S   = 3600      # "active" = seen within the last hour

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "uptime_rewards_state.json")

CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')
TX_HASH_RE = re.compile(r'class="hash">([0-9a-f]+)</span>')


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        state.setdefault("pending", None)
        return state
    state = {"remaining_ticks": int(BUDGET_LAPSE * TICKS_PER_LAPSE), "pending": None}
    save_state(state)
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def tx_confirmed(tx_hash):
    """True if the tx is known to the node (mempool or chain), False if it's
    gone (never confirmed, pruned after TTL), None if the check itself failed."""
    try:
        r = requests.get(f"{PUBLIC_URL}/api/tx/{tx_hash}", timeout=10)
    except requests.RequestException as e:
        print(f"could not check pending tx {tx_hash}: {e}", file=sys.stderr)
        return None
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    print(f"unexpected status {r.status_code} checking pending tx {tx_hash}", file=sys.stderr)
    return None


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
    """None if addr isn't a well-formed address, rather than raising -- addr
    ultimately traces back to a peer's self-reported, unvalidated wallet
    string (see get_active_peer_wallets), so a malformed one here is
    expected input to handle, not a crash."""
    r = requests.get(f"{PUBLIC_URL}/api/address/{addr}/balance", timeout=10)
    if r.status_code == 400:
        return None
    r.raise_for_status()
    return r.json()["balance_ticks"]


def send_payout(outputs_csv):
    """outputs_csv: 'addr,ticks\\n...'. Uses the private /send form (fee 0).
    Returns the tx hash on success, None on failure."""
    get = requests.get(f"{PRIVATE_URL}/send", timeout=10)
    get.raise_for_status()
    m = CSRF_RE.search(get.text)
    if not m:
        print("could not find csrf token on /send page", file=sys.stderr)
        return None
    resp = requests.post(f"{PRIVATE_URL}/send", data={
        "csrf_token": m.group(1),
        "outputs": outputs_csv,
        "fee": "0",
        "passphrase": PASSPHRASE,
    }, timeout=15)
    resp.raise_for_status()
    if 'class="alert alert-err"' in resp.text:
        print("send failed, response did not confirm success", file=sys.stderr)
        return None
    m = TX_HASH_RE.search(resp.text)
    if not m:
        print("send appeared to succeed but no tx hash found in response", file=sys.stderr)
        return None
    return m.group(1)


def main():
    if not PASSPHRASE:
        print("LAPSECOIN_PASSPHRASE not set", file=sys.stderr)
        return 1

    state = load_state()

    if state["pending"]:
        pending = state["pending"]
        confirmed = tx_confirmed(pending["tx_hash"])
        if confirmed is None:
            print("could not resolve previous run's pending tx, skipping this run")
            return 1
        if confirmed:
            print(f"previous payout {pending['tx_hash']} confirmed, "
                  f"debit of {pending['amount'] / TICKS_PER_LAPSE:.4f} LAPSE stands")
        else:
            state["remaining_ticks"] += pending["amount"]
            print(f"previous payout {pending['tx_hash']} never confirmed, "
                  f"refunded {pending['amount'] / TICKS_PER_LAPSE:.4f} LAPSE to budget")
        state["pending"] = None
        save_state(state)

    remaining_ticks = state["remaining_ticks"]
    if remaining_ticks <= 0:
        print("budget exhausted, nothing to pay out")
        return 0

    info = requests.get(f"{PUBLIC_URL}/api/info", timeout=10).json()
    reward_addr = info["address"]
    wallet_balance = get_balance_ticks(reward_addr)

    tip = requests.get(f"{PUBLIC_URL}/api/block/{info['height']}", timeout=10).json()
    last_builder = tip.get("builder") or None

    candidates = get_active_peer_wallets() - {reward_addr, last_builder}
    eligible = []
    for addr in candidates:
        balance = get_balance_ticks(addr)
        if balance is None:
            print(f"skipping malformed peer-reported wallet: {addr!r}", file=sys.stderr)
            continue
        if balance < wallet_balance:
            eligible.append(addr)
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
    tx_hash = send_payout(outputs_csv)
    if tx_hash:
        sent = share * len(eligible)
        state["remaining_ticks"] = remaining_ticks - sent
        state["pending"] = {"tx_hash": tx_hash, "amount": sent}
        save_state(state)
        print(f"sent {share / TICKS_PER_LAPSE:.4f} LAPSE to {len(eligible)} peer(s) "
              f"(tx {tx_hash}, pending confirmation next run), "
              f"budget remaining {state['remaining_ticks'] / TICKS_PER_LAPSE:.4f} LAPSE")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
