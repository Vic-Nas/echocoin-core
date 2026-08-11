"""HTTP API + node UI + whitepaper renderer. Thin wrapper over node + pool."""

import os
import json
import time
import logging
import html
from collections import defaultdict, OrderedDict

import threading

import markdown
from flask import Flask, request, jsonify, render_template_string

import tx as tx_mod
import crypto as crypto_mod
import mining as mining_mod
from params import SEEDS_PER_PC


def fmt_balance(seeds):
    """Format a raw seed integer as 'X PC Y seeds' for display."""
    pc   = seeds // SEEDS_PER_PC
    rem  = seeds % SEEDS_PER_PC
    return f"{pc} PC {rem:,} seeds"

log = logging.getLogger("pc.api")

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
# Simple per-source-IP token bucket, in-process/in-memory (fine for a
# single-node deployment; would need a shared store like Redis behind a
# load balancer). Protects endpoints that trigger expensive crypto work
# (FALCON-512 verification, fee recomputation) from being flooded by an
# unauthenticated caller: without this, POST /api/receive_tx and
# /api/receive_solution have no cost to the caller but real CPU cost to
# us, and that CPU contends with the same process's mining/validation loop.

class _TokenBucket:
    def __init__(self, capacity, refill_per_second):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def take(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


# Maximum number of distinct IPs tracked by the rate limiter. Beyond this,
# new IPs are denied immediately. Prevents unbounded memory growth from
# scanners cycling through addresses.
_RATE_LIMITER_MAX_BUCKETS = 10_000


class RateLimiter:
    """capacity: burst size. refill_per_second: sustained rate after burst.
    Uses LRU eviction so that inactive IPs age out and new IPs are always
    admitted, even when the table is at capacity.
    """
    def __init__(self, capacity=20, refill_per_second=5):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key):
        with self._lock:
            if key in self._buckets:
                # Move to end (most recently used).
                self._buckets.move_to_end(key)
            else:
                if len(self._buckets) >= _RATE_LIMITER_MAX_BUCKETS:
                    # Evict the least recently used bucket.
                    self._buckets.popitem(last=False)
                self._buckets[key] = _TokenBucket(self.capacity, self.refill_per_second)
            bucket = self._buckets[key]
        return bucket.take()


def _rate_limited(limiter):
    """Decorator: 429s the request before the view function (and whatever
    expensive validation it does) ever runs."""
    def decorator(fn):
        def wrapped(*args, **kwargs):
            key = request.remote_addr or "unknown"
            if not limiter.allow(key):
                return jsonify({"ok": False, "error": "rate limited"}), 429
            return fn(*args, **kwargs)
        wrapped.__name__ = fn.__name__
        return wrapped
    return decorator

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

from templates import _BASE, _STATS_BODY

def _parse_sender(data):
    """Extract sender addr from inbound message data."""
    port = data.get("sender_port")
    if port is None:
        return None
    try:
        return f"{request.remote_addr}:{int(port)}"
    except (TypeError, ValueError):
        return None


def _register_sender(pool, data):
    """Register the sender as a peer (one-liner, no side effects)."""
    addr = _parse_sender(data)
    if addr:
        pool.add(addr)


def _strike_sender(pool, data):
    """Strike the sender for sending invalid data."""
    addr = _parse_sender(data)
    if addr:
        pool.strike(addr)


_TX_REQUIRED_FIELDS = {"from", "pubkey", "outputs", "nonce", "fee_height", "fee", "signature"}


def create_app(node, pool, net_in_q):
    app = Flask(__name__)
    tx_limiter       = RateLimiter(capacity=20, refill_per_second=5)   # ~5/sec sustained, bursts to 20
    solution_limiter = RateLimiter(capacity=200, refill_per_second=50) # solutions arrive much more often legitimately
    app.logger.setLevel(logging.WARNING)

    # ---- Dashboard -------------------------------------------------------

    @app.route("/")
    def dashboard():
        info = node.get_info()
        chain = node.view.chain   # snapshot: avoids RuntimeError if mining thread appends mid-iteration
        tip  = chain[-1]
        recent_blocks = chain[-10:][::-1]

        blocks_html = ""
        for b in recent_blocks:
            tx_count  = len(b["transactions"])
            sol_count = sum(s["count"] for s in b.get("solver_summaries", []))
            blocks_html += f"""
            <tr>
              <td><a href="/explorer/block/{b['height']}">{b['height']}</a></td>
              <td class="hash-short">{b['hash'][:20]}…</td>
              <td>{tx_count}</td>
              <td>{sol_count}</td>
              <td>{b['fee_rate']}</td>
            </tr>"""

        body = f"""
        <div class="stats" style="margin-bottom:1rem">
          <div class="stat"><div class="stat-label">Height</div><div class="stat-value">{info['height']}</div></div>
          <div class="stat"><div class="stat-label">Mempool</div><div class="stat-value">{info['mempool_size']}</div></div>
          <div class="stat"><div class="stat-label">Peers</div><div class="stat-value">{info['peer_count']}</div></div>
          <div class="stat"><div class="stat-label">Fee Rate</div><div class="stat-value">{info['fee_rate']}</div></div>
        </div>
        <div class="card">
          <div class="card-title">Your address</div>
          <div class="hash">{info['address']}</div>
        </div>
        <div class="card">
          <div class="card-title">Tip hash</div>
          <div class="hash">{tip['hash']}</div>
        </div>
        <div class="card">
          <div class="card-title">Recent blocks</div>
          <table>
            <thead><tr><th>Height</th><th>Hash</th><th>Txs</th><th>Solutions</th><th>Fee rate</th></tr></thead>
            <tbody>{blocks_html}</tbody>
          </table>
        </div>"""
        return render_template_string(_BASE.format(title="Dashboard", body=body))

    # ---- Send ------------------------------------------------------------

    @app.route("/send", methods=["GET", "POST"])
    def send():
        # Signing key handling: restrict to localhost. Remote access requires an SSH tunnel.
        # The passphrase is only needed when the mining loop is not running (KEK not cached).
        # Any node can send -- mining is not required.
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify({"ok": False, "error": "localhost only"}), 403
        alert = ""
        from_addr = node.addr
        v          = node.view
        tip_height = v.height
        fee_rate   = v.tip["fee_rate"]
        state      = v.state
        nonce      = state.get_nonce(from_addr) + 1
        balance    = state.get_balance(from_addr)

        if request.method == "POST":
            outputs_raw = request.form.get("outputs", "").strip()
            passphrase  = request.form.get("passphrase", "").strip()
            csv_file    = request.files.get("csv_file")
            if csv_file and csv_file.filename:
                outputs_raw = csv_file.read().decode()

            outputs = []
            errors  = []
            for i, line in enumerate(outputs_raw.splitlines()):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    errors.append(f"line {i+1}: expected 'address,amount'")
                    continue
                try:
                    amount = int(parts[1])
                except ValueError:
                    errors.append(f"line {i+1}: amount must be an integer")
                    continue
                outputs.append({"to": parts[0], "amount": amount})

            if errors:
                alert = f'<div class="alert alert-err">{"<br>".join(errors)}</div>'
            elif not outputs:
                alert = '<div class="alert alert-err">No valid outputs.</div>'
            elif not passphrase and node._kek is None:
                alert = '<div class="alert alert-err">Passphrase required to sign (leave blank if the mining loop is already running).</div>'
            else:
                try:
                    t, fee = node.build_and_sign_tx(outputs, passphrase or None)
                    ok, result = node.submit_tx_from_api(t)
                    if ok:
                        alert = f'<div class="alert alert-ok">Sent. tx hash: <span class="hash">{result}</span></div>'
                    else:
                        alert = f'<div class="alert alert-err">Error: {result}</div>'
                except Exception as e:
                    alert = f'<div class="alert alert-err">Error: {e}</div>'

        body = f"""
        <h2>Send</h2>
        {alert}
        <div class="card">
          <div class="card-title">Your address</div>
          <div class="hash" style="margin-bottom:.5rem">{from_addr}</div>
          <div class="stat-label">Balance: <strong style="color:var(--green)">{fmt_balance(balance)}</strong> &nbsp;|&nbsp; Nonce: {nonce} &nbsp;|&nbsp; Fee rate: {fee_rate}/byte</div>
        </div>
        <div class="card">
          <div class="card-title">Outputs — paste CSV (address,amount) or upload file</div>
          <form method="POST" enctype="multipart/form-data">
            <div class="form-group">
              <label>CSV outputs (one per line: address,amount)</label>
              <textarea name="outputs" rows="6" placeholder="word1.word2...word12,1000&#10;word1.word2...word12,500"></textarea>
            </div>
            <div class="form-group">
              <label>Or upload CSV file</label>
              <input type="file" name="csv_file" accept=".csv,.txt">
            </div>
            <div class="form-group">
              <label>Your signing passphrase (leave blank if mining loop is active)</label>
              <input type="password" name="passphrase" placeholder="Your passphrase to sign the transaction">
            </div>
            <button type="submit">Sign &amp; Send</button>
          </form>
        </div>"""
        return render_template_string(_BASE.format(title="Send", body=body))

    # ---- Block explorer --------------------------------------------------

    @app.route("/explorer")
    def explorer():
        recent = node.view.chain[-20:][::-1]
        rows = ""
        for b in recent:
            rows += f"""<tr>
              <td><a href="/explorer/block/{b['height']}">{b['height']}</a></td>
              <td class="hash"><a href="/explorer/block/{b['height']}">{b['hash'][:32]}…</a></td>
              <td>{len(b['transactions'])}</td>
              <td>{sum(s["count"] for s in b.get("solver_summaries", []))}</td>
              <td>{b['fee_rate']}</td>
            </tr>"""

        body = f"""
        <h2>Block Explorer</h2>
        <div class="card">
          <table>
            <thead><tr><th>Height</th><th>Hash</th><th>Txs</th><th>Solutions</th><th>Fee rate</th></tr></thead>
            <tbody>{rows or '<tr><td colspan="5" style="color:var(--muted);text-align:center">No blocks yet</td></tr>'}</tbody>
          </table>
        </div>"""
        return render_template_string(_BASE.format(title="Explorer", body=body))

    @app.route("/explorer/block/<int:height>")
    def block_detail(height):
        chain = node.view.chain
        if height < 0 or height >= len(chain):
            return render_template_string(_BASE.format(title="Not found",
                body='<div class="alert alert-err">Block not found.</div>')), 404

        b   = chain[height]
        msg = html.escape(b.get("message", ""))

        tx_rows = ""
        for t in b["transactions"]:
            h = tx_mod.tx_hash(t)
            total = sum(o["amount"] for o in t["outputs"])
            tx_rows += f"""<tr>
              <td class="hash-short"><a href="/explorer/tx/{h}">{h[:20]}…</a></td>
              <td class="hash-short">{t['from'][:24]}…</td>
              <td>{total}</td>
              <td>{t['fee']}</td>
            </tr>"""

        summaries = b.get("solver_summaries", [])
        total_count = sum(s["count"] for s in summaries)
        sol_rows = ""
        for s in summaries:
            reward = (mining_mod.BLOCK_REWARD * s["count"]) // total_count if total_count else 0
            sol_rows += (
                f"<tr><td class='hash-short'>{s['address']}</td>"
                f"<td>{s['count']} solution{'s' if s['count'] != 1 else ''}</td>"
                f"<td>{reward} seeds</td></tr>"
            )

        body = f"""
        <h2>Block {height}</h2>
        {f'<div class="alert alert-ok">{msg}</div>' if msg else ''}
        <div class="card">
          <div class="card-title">Block details</div>
          <table>
            <tr><td style="color:var(--muted);width:140px">Hash</td><td class="hash">{b['hash']}</td></tr>
            <tr><td style="color:var(--muted)">Previous</td><td class="hash">{b['previous_hash']}</td></tr>
            <tr><td style="color:var(--muted)">Height</td><td>{b['height']}</td></tr>
            <tr><td style="color:var(--muted)">Transactions</td><td>{len(b['transactions'])}</td></tr>
            <tr><td style="color:var(--muted)">Solvers</td><td>{len(b.get("solver_summaries", []))}</td></tr><tr><td style="color:var(--muted)">Solutions</td><td>{sum(s["count"] for s in b.get("solver_summaries", []))}</td></tr>
            <tr><td style="color:var(--muted)">Fee rate</td><td>{b['fee_rate']}</td></tr>
            <tr><td style="color:var(--muted)">Difficulty</td><td>{b['difficulty_target']}</td></tr>
          </table>
        </div>
        <div class="card">
          <div class="card-title">Transactions</div>
          <table>
            <thead><tr><th>Hash</th><th>From</th><th>Total sent</th><th>Fee</th></tr></thead>
            <tbody>{tx_rows or '<tr><td colspan="4" style="color:var(--muted);text-align:center">No transactions</td></tr>'}</tbody>
          </table>
        </div>
        <div class="card">
          <div class="card-title">Solvers rewarded</div>
          <table>
            <thead><tr><th>Address</th><th>Solutions</th><th>Reward</th></tr></thead>
            <tbody>{sol_rows or '<tr><td colspan="3" style="color:var(--muted);text-align:center">No solvers</td></tr>'}</tbody>
          </table>
        </div>
        <a href="/explorer/block/{height-1}" style="margin-right:1rem">&larr; prev</a>
        {'<a href="/explorer/block/'+str(height+1)+'">&rarr; next</a>' if height+1 < len(chain) else ''}
        """
        return render_template_string(_BASE.format(title=f"Block {height}", body=body))

    @app.route("/explorer/tx/<tx_hash>")
    def tx_detail(tx_hash):
        found = None
        found_height = None
        # Use the tx_index for O(1) lookup.
        height = node.storage.get_tx_height(tx_hash)
        if height is not None:
            chain = node.view.chain
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == tx_hash:
                        found = t
                        found_height = height
                        break
        if not found:
            found = node.mempool.get(tx_hash)

        if not found:
            return render_template_string(_BASE.format(title="Not found",
                body='<div class="alert alert-err">Transaction not found.</div>')), 404

        out_rows = "".join(
            f"<tr><td class='hash-short'>{o['to']}</td><td>{o['amount']}</td></tr>"
            for o in found["outputs"]
        )
        location = f"Block {found_height}" if found_height is not None else "Mempool (unconfirmed)"

        body = f"""
        <h2>Transaction</h2>
        <div class="card">
          <table>
            <tr><td style="color:var(--muted);width:120px">Hash</td><td class="hash">{tx_hash}</td></tr>
            <tr><td style="color:var(--muted)">Status</td><td>{location}</td></tr>
            <tr><td style="color:var(--muted)">From</td><td class="hash-short">{found['from']}</td></tr>
            <tr><td style="color:var(--muted)">Nonce</td><td>{found['nonce']}</td></tr>
            <tr><td style="color:var(--muted)">Fee</td><td>{found['fee']}</td></tr>
            <tr><td style="color:var(--muted)">Fee height</td><td>{found['fee_height']}</td></tr>
          </table>
        </div>
        <div class="card">
          <div class="card-title">Outputs</div>
          <table>
            <thead><tr><th>To</th><th>Amount</th></tr></thead>
            <tbody>{out_rows}</tbody>
          </table>
        </div>"""
        return render_template_string(_BASE.format(title="Transaction", body=body))

    # ---- Address lookup --------------------------------------------------

    @app.route("/address", methods=["GET", "POST"])
    def address_lookup():
        addr  = request.args.get("addr", "").strip()
        addr_html = html.escape(addr)
        alert = ""
        content = ""

        if addr:
            if not crypto_mod.is_valid_address(addr):
                alert = '<div class="alert alert-err">Invalid address format.</div>'
                addr = ""
        if addr:
            v       = node.view
            state   = v.state
            balance = state.get_balance(addr)
            nonce   = state.get_nonce(addr)
            # O(1) lookup via addr_index, then fetch each tx from the chain.
            history = []
            for height, h in node.storage.get_tx_heights_for_addr(addr):
                chain = v.chain
                if 0 <= height < len(chain):
                    for t in chain[height]["transactions"]:
                        if tx_mod.tx_hash(t) == h:
                            direction = "sent" if t["from"] == addr else "received"
                            history.append((height, h, direction, t))
                            break

            h_rows = ""
            for height, h, direction, t in reversed(history):
                color = "var(--red)" if direction == "sent" else "var(--green)"
                h_rows += f"""<tr>
                  <td>{height}</td>
                  <td><a href="/explorer/tx/{h}">{h[:20]}…</a></td>
                  <td style="color:{color}">{direction}</td>
                  <td>{sum(o['amount'] for o in t['outputs'])}</td>
                </tr>"""

            content = f"""
            <div class="stats" style="margin-bottom:1rem">
              <div class="stat"><div class="stat-label">Balance</div><div class="stat-value" style="color:var(--green)">{fmt_balance(balance)}</div></div>
              <div class="stat"><div class="stat-label">Nonce</div><div class="stat-value">{nonce}</div></div>
              <div class="stat"><div class="stat-label">Transactions</div><div class="stat-value">{len(history)}</div></div>
            </div>
            <div class="card">
              <div class="card-title">History</div>
              <table>
                <thead><tr><th>Height</th><th>Tx hash</th><th>Direction</th><th>Amount</th></tr></thead>
                <tbody>{h_rows or '<tr><td colspan="4" style="color:var(--muted);text-align:center">No transactions</td></tr>'}</tbody>
              </table>
            </div>"""

        body = f"""
        <h2>Address Lookup</h2>
        {alert}
        <div class="card">
          <form method="GET">
            <div class="form-group">
              <label>Address (twelve dot-separated words)</label>
              <input name="addr" value="{addr_html}" placeholder="word1.word2.word3...">
            </div>
            <button type="submit">Look up</button>
          </form>
        </div>
        {content}"""
        return render_template_string(_BASE.format(title="Address", body=body))

    # ---- Whitepaper ------------------------------------------------------

    @app.route("/whitepaper")
    def whitepaper():
        base    = getattr(__import__("sys"), "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        wp_path = os.path.join(base, "whitepaper.md")
        try:
            with open(wp_path) as f:
                md_text = f.read()
            rendered = markdown.markdown(md_text, extensions=["fenced_code", "tables"])
        except FileNotFoundError:
            rendered = "<p>whitepaper.md not found.</p>"

        body = f'<div class="wp">{rendered}</div>'
        return render_template_string(_BASE.format(title="Whitepaper", body=body))


    # ---- Stats -----------------------------------------------------------

    @app.route("/api/stats")
    def api_stats():
        """Return per-block economics data for charting.
        One pass over the chain; sampled to at most 500 points so the
        response stays small regardless of chain length.
        """
        chain = node.view.chain
        if len(chain) <= 1:
            return jsonify({"points": [], "totals": {
                "minted": 0, "burned_fees": 0, "burned_remainder": 0,
                "circulating": 0,
            }})

        from params import BLOCK_REWARD

        # Build per-block series, then sample
        points = []
        cum_minted = 0
        cum_burned_fees = 0
        cum_burned_remainder = 0

        for blk in chain[1:]:   # skip genesis (no PoW, no rewards)
            summaries = blk.get("solver_summaries", [])
            total_count = sum(s["count"] for s in summaries)

            # Actual minted = sum of floor-division shares (remainder burned)
            block_minted = 0
            if total_count > 0:
                for s in summaries:
                    block_minted += (BLOCK_REWARD * s["count"]) // total_count
            burned_remainder = BLOCK_REWARD - block_minted if summaries else 0

            burned_fees = sum(t["fee"] for t in blk.get("transactions", []))

            cum_minted += block_minted
            cum_burned_fees += burned_fees
            cum_burned_remainder += burned_remainder

            points.append({
                "height": blk["height"],
                "minted": cum_minted,
                "burned_fees": cum_burned_fees,
                "burned_remainder": cum_burned_remainder,
                "circulating": cum_minted - cum_burned_fees - cum_burned_remainder,
                # net_emission: coins actually entering circulation this block
                # (positive = supply expanding, negative = supply contracting)
                "net_emission": block_minted - burned_fees - burned_remainder,
            })

        # Sample to max 500 points evenly, always include last
        if len(points) > 500:
            step = len(points) / 500
            sampled = [points[int(i * step)] for i in range(500)]
            if sampled[-1] != points[-1]:
                sampled[-1] = points[-1]
            points = sampled

        totals = points[-1] if points else {
            "minted": 0, "burned_fees": 0, "burned_remainder": 0, "circulating": 0,
        }

        # Compute net emission rate over the last block for display.
        last = points[-1] if len(points) >= 1 else None
        prev = points[-2] if len(points) >= 2 else None
        if last and prev:
            net_emission_last = last["net_emission"]
        elif last:
            net_emission_last = last["net_emission"]
        else:
            net_emission_last = 0

        return jsonify({
            "points": points,
            "totals": {
                "minted":              totals["minted"],
                "burned_fees":         totals["burned_fees"],
                "burned_remainder":    totals["burned_remainder"],
                "circulating":         totals["circulating"],
                "net_emission_last":   net_emission_last,
                "seeds_per_pc":        SEEDS_PER_PC,
            },
        })

    @app.route("/stats")
    def stats():
        body = (
            "<h2>Economics</h2>"
            + _STATS_BODY
        )
        return render_template_string(_BASE.format(title="Stats", body=body))

    # ---- JSON API --------------------------------------------------------

    @app.route("/api/info")
    def api_info():
        return jsonify(node.get_info())

    @app.route("/api/fee_rate")
    def api_fee_rate():
        v = node.view
        return jsonify({"fee_rate": v.tip["fee_rate"], "height": v.height})

    @app.route("/api/block/<int:height>")
    def api_block(height):
        chain = node.view.chain
        if 0 <= height < len(chain):
            return jsonify(chain[height])
        return jsonify({"error": "not found"}), 404

    @app.route("/api/chain")
    def api_chain():
        # Paginated to prevent a single request from serializing the entire
        # chain (which grows without bound) into memory. Syncing peers call
        # this in a loop with from_height advancing each time.
        try:
            from_height = int(request.args.get("from", 0))
            to_height   = int(request.args.get("to", from_height + 500))
        except (TypeError, ValueError):
            return jsonify({"error": "from and to must be integers"}), 400
        to_height = min(to_height, from_height + 500)   # hard page cap
        chain = node.view.chain
        slice_ = [b for b in chain if from_height <= b["height"] <= to_height]
        return jsonify(slice_)

    @app.route("/api/chain/tip")
    def api_chain_tip():
        return jsonify(node.view.chain[-1])

    @app.route("/api/tx/send", methods=["POST"])
    @_rate_limited(tx_limiter)
    def api_send_tx():
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "no JSON body"}), 400
        ok, result = node.submit_tx_from_api(data)
        if ok:
            return jsonify({"ok": True, "tx_hash": result})
        return jsonify({"ok": False, "error": result}), 400

    @app.route("/api/tx/<tx_hash_val>")
    def api_get_tx(tx_hash_val):
        t = node.mempool.get(tx_hash_val)
        if t:
            return jsonify(t)
        # Use the tx_index for O(1) lookup instead of scanning the chain.
        height = node.storage.get_tx_height(tx_hash_val)
        if height is not None:
            chain = node.view.chain
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == tx_hash_val:
                        return jsonify(t)
        return jsonify({"error": "not found"}), 404

    @app.route("/api/address/<addr>/balance")
    def api_balance(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        balance = node.view.state.get_balance(addr)
        return jsonify({
            "address":      addr,
            "balance_seeds": balance,
            "balance_pc":    balance / SEEDS_PER_PC,
        })

    @app.route("/api/address/<addr>/history")
    def api_history(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        chain   = node.view.chain
        history = []
        for height, h in node.storage.get_tx_heights_for_addr(addr):
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == h:
                        history.append({
                            "height":    height,
                            "tx_hash":   h,
                            "direction": "sent" if t["from"] == addr else "received",
                            "tx":        t,
                        })
                        break
        return jsonify(history)

    @app.route("/api/mempool")
    def api_mempool():
        txs = node.mempool.all_txs()
        return jsonify({
            "size": len(txs),
            "transactions": [{"hash": tx_mod.tx_hash(t), "from": t["from"],
                               "outputs": t["outputs"], "fee": t["fee"]} for t in txs],
        })

    @app.route("/api/receive_solution", methods=["POST"])
    @_rate_limited(solution_limiter)
    def api_recv_solution():
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "no JSON body"}), 400
        sol = data.get("solution", {})
        # Cheap structural pre-check before any crypto work: FALCON-512 public
        # keys are 897 bytes (1794 hex chars); nonce must be a non-negative int.
        pk = sol.get("pubkey", "")
        nonce = sol.get("nonce")
        if (not isinstance(pk, str) or len(pk) != 1794
                or not all(c in "0123456789abcdefABCDEF" for c in pk)
                or not isinstance(nonce, int) or nonce < 0):
            _strike_sender(pool, data)
            return jsonify({"ok": False, "error": "malformed solution"}), 400
        net_in_q.put({
            "type":     "solution",
            "solution": sol,
        })
        _register_sender(pool, data)
        return jsonify({"ok": True})

    @app.route("/api/receive_block", methods=["POST"])
    @_rate_limited(tx_limiter)
    def api_recv_block():
        data = request.get_json()
        if not data or "block" not in data:
            return jsonify({"ok": False, "error": "missing block"}), 400
        blk = data["block"]
        # Cheap structural pre-check before any validation work.
        required_fields = {"height", "previous_hash", "hash", "timestamp",
                           "transactions", "solver_summaries", "difficulty_target", "fee_rate"}
        if not isinstance(blk, dict) or not required_fields.issubset(blk):
            return jsonify({"ok": False, "error": "malformed block"}), 400
        if not isinstance(blk["height"], int) or blk["height"] < 0:
            return jsonify({"ok": False, "error": "malformed block"}), 400
        # Reject blocks whose transactions list contains non-dict entries or
        # entries missing the fields required for ordering. This prevents an
        # unauthenticated caller from crashing block.validate() via sort_key()
        # and DoS-ing the assembler round.
        if not isinstance(blk["transactions"], list):
            return jsonify({"ok": False, "error": "malformed block"}), 400
        for t in blk["transactions"]:
            if not isinstance(t, dict) or not _TX_REQUIRED_FIELDS.issubset(t):
                _strike_sender(pool, data)
                return jsonify({"ok": False, "error": "malformed block"}), 400
        net_in_q.put({
            "type":  "block",
            "block": blk,
        })
        _register_sender(pool, data)
        return jsonify({"ok": True})

    @app.route("/api/receive_tx", methods=["POST"])
    @_rate_limited(tx_limiter)
    def api_recv_tx():
        data = request.get_json()
        if not data or "tx" not in data:
            return jsonify({"ok": False, "error": "missing tx"}), 400
        tx_dict = data.get("tx")
        if not isinstance(tx_dict, dict):
            return jsonify({"ok": False, "error": "invalid tx"}), 400
        h = tx_mod.tx_hash(tx_dict)
        # Dedup at the API boundary so concurrent Flask threads
        # don't double-enqueue the same tx.
        if node.gossip.mark_seen(h):
            return jsonify({"ok": True})
        net_in_q.put({
            "type":           "tx",
            "tx":             tx_dict,
            "relay_type":     data.get("type", "tx_fluff"),
            "remaining_hops": data.get("remaining_hops", 0),
        })
        return jsonify({"ok": True})

    @app.route("/api/peers")
    def api_peers():
        addrs = pool.all_addrs()
        return jsonify({"count": len(addrs), "peers": addrs})

    @app.route("/api/peers/add", methods=["POST"])
    def api_add_peer():
        # Node-operator tooling, not part of the gossip/consensus protocol
        # (peers self-discover via the DHT). Restricted to localhost: it
        # triggers an outbound HTTP GET to an arbitrary caller-supplied
        # host:port with no other validation, which is a straightforward
        # SSRF if reachable remotely -- an unauthenticated caller could use
        # any publicly reachable node to probe internal-network or
        # cloud-metadata addresses.
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify({"ok": False, "error": "localhost only"}), 403
        data = request.get_json()
        if data and "host" in data and "port" in data:
            pool.add(f"{data['host']}:{data['port']}")
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "need host and port"}), 400

    return app
