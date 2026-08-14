"""HTTP API + node UI. Thin wrapper over node + pool. No HTML here."""

import logging
import os
import threading
import time
from collections import OrderedDict

import markdown
from flask import Flask, jsonify, render_template, request

import crypto as crypto_mod
import pob as pob_mod
import tx as tx_mod
from params import POB_WINDOW, RINGS_PER_ECH, SUPPLY_CAP
from pob import BURN_ADDRESS


def fmt_balance(rings):
    ech = rings // RINGS_PER_ECH
    rem = rings % RINGS_PER_ECH
    return f"{ech} ECH {rem:,} rings"


def fmt_fee_rate(rings_per_byte):
    ech = rings_per_byte / RINGS_PER_ECH
    if ech >= 0.001:
        return f"{ech:.6f} ECH/byte"
    return f"{ech:.2e} ECH/byte"


log = logging.getLogger("ec.api")

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_RATE_LIMITER_MAX_BUCKETS = 10_000


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


class RateLimiter:
    def __init__(self, capacity=20, refill_per_second=5):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key):
        with self._lock:
            if key in self._buckets:
                self._buckets.move_to_end(key)
            else:
                if len(self._buckets) >= _RATE_LIMITER_MAX_BUCKETS:
                    self._buckets.popitem(last=False)
                self._buckets[key] = _TokenBucket(self.capacity, self.refill_per_second)
            bucket = self._buckets[key]
        return bucket.take()


def _rate_limited(limiter):
    def decorator(fn):
        def wrapped(*args, **kwargs):
            if not limiter.allow(request.remote_addr or "unknown"):
                return jsonify({"ok": False, "error": "rate limited"}), 429
            return fn(*args, **kwargs)
        wrapped.__name__ = fn.__name__
        return wrapped
    return decorator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TX_REQUIRED_FIELDS = {"from", "pubkey", "outputs", "nonce", "fee_height", "fee", "signature"}
_LOCALHOST = ("127.0.0.1", "::1")


def _parse_sender(data):
    port = data.get("sender_port")
    if port is None:
        return None
    try:
        port_int = int(port)
        if not 1 <= port_int <= 65535:
            return None
        return f"{request.remote_addr}:{port_int}"
    except (TypeError, ValueError):
        return None


def _register_sender(discovery, data):
    addr = _parse_sender(data)
    if addr:
        discovery.enqueue_candidate(addr)


def _strike_sender(pool, data):
    addr = _parse_sender(data)
    if addr:
        pool.strike(addr)



# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(node, pool, net_in_q, discovery):
    app = Flask(__name__, template_folder="templates_html")
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_fee_rate=fmt_fee_rate)
    app.logger.setLevel(logging.WARNING)

    tx_limiter    = RateLimiter(capacity=20, refill_per_second=5)
    block_limiter = RateLimiter(capacity=10, refill_per_second=2)

    # ---- UI pages --------------------------------------------------------

    @app.route("/")
    def dashboard():
        info = node.get_info()
        chain = node.view.chain
        return render_template("dashboard.html", title="Dashboard",
            info=info, tip=chain[-1], recent_blocks=chain[-10:][::-1])

    @app.route("/send", methods=["GET", "POST"])
    def send():
        if request.remote_addr not in _LOCALHOST:
            return jsonify({"ok": False, "error": "localhost only"}), 403
        v = node.view
        ctx = dict(title="Send", from_addr=node.addr, balance=v.state.get_balance(node.addr),
                   nonce=v.state.get_nonce(node.addr) + 1, fee_rate=v.tip["fee_rate"],
                   alert_ok="", alert_err="")
        if request.method == "POST":
            outputs_raw = request.form.get("outputs", "").strip()
            passphrase  = request.form.get("passphrase", "").strip()
            csv_file    = request.files.get("csv_file")
            if csv_file and csv_file.filename:
                outputs_raw = csv_file.read().decode()
            outputs, errors = [], []
            for i, line in enumerate(outputs_raw.splitlines()):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 2:
                    errors.append(f"line {i+1}: expected 'address,amount'")
                    continue
                try:
                    outputs.append({"to": parts[0], "amount": int(parts[1])})
                except ValueError:
                    errors.append(f"line {i+1}: amount must be an integer")
            if errors:
                ctx["alert_err"] = "<br>".join(errors)
            elif not outputs:
                ctx["alert_err"] = "No valid outputs."
            elif not passphrase and not node.is_signing_active():
                ctx["alert_err"] = "Passphrase required (leave blank if mining loop is active)."
            else:
                try:
                    t, _fee = node.build_and_sign_tx(outputs, passphrase or None)
                    ok, result = node.submit_tx_from_api(t)
                    if ok:
                        ctx["alert_ok"] = f'Sent. tx hash: <span class="hash">{result}</span>'
                    else:
                        ctx["alert_err"] = f"Error: {result}"
                except Exception as e:
                    ctx["alert_err"] = f"Error: {e}"
        return render_template("send.html", **ctx)

    @app.route("/burn", methods=["GET", "POST"])
    def burn():
        if request.remote_addr not in _LOCALHOST:
            return jsonify({"ok": False, "error": "localhost only"}), 403
        v = node.view
        chain = v.chain
        balance = v.state.get_balance(node.addr)
        bw          = node._burn_window
        pool_totals = bw.pool_totals()
        burn_totals = bw.sender_totals()
        burn_history = bw.history()
        tip_hash_int = pob_mod._tip_hash_int(chain)
        scores = {addr: bw.score(tip_hash_int, addr) for addr in pool_totals}
        ctx = dict(title="Burn", from_addr=node.addr, balance=balance,
                   my_burn=burn_totals.get(node.addr, 0),
                   my_score=bw.score(tip_hash_int, node.addr),
                   total_burn=sum(pool_totals.values()),
                   sorted_burners=sorted(burn_totals.items(), key=lambda x: -x[1]),
                   sorted_pools=sorted(pool_totals.items(), key=lambda x: -x[1]),
                   burn_history=burn_history, scores=scores,
                   pob_window=POB_WINDOW, alert_ok="", alert_err="")
        if request.method == "POST":
            raw        = request.form.get("amount", "").strip()
            passphrase = request.form.get("passphrase", "").strip()
            try:
                burn_rings = int(raw)
                if burn_rings <= 0:
                    raise ValueError("must be positive")
            except ValueError as e:
                ctx["alert_err"] = f"Invalid amount: {e}"
            else:
                if not passphrase and not node.is_signing_active():
                    ctx["alert_err"] = "Passphrase required (leave blank if mining loop is active)."
                elif burn_rings > balance:
                    ctx["alert_err"] = "Insufficient balance."
                else:
                    try:
                        beneficiary = request.form.get("beneficiary", "").strip() or node.addr
                        if not crypto_mod.is_valid_address(beneficiary):
                            beneficiary = node.addr
                        burn_out = {"to": BURN_ADDRESS, "amount": burn_rings}
                        if beneficiary != node.addr:
                            burn_out["beneficiary"] = beneficiary
                        t, _fee = node.build_and_sign_tx([burn_out], passphrase or None)
                        ok, result = node.submit_tx_from_api(t)
                        if ok:
                            ctx["alert_ok"] = f'Burn submitted. tx: <span class="hash">{result}</span>'
                        else:
                            ctx["alert_err"] = f"Error: {result}"
                    except Exception as e:
                        ctx["alert_err"] = f"Error: {e}"
        return render_template("burn.html", **ctx)

    @app.route("/explorer")
    def explorer():
        return render_template("explorer.html", title="Explorer",
            recent=node.view.chain[-20:][::-1])

    @app.route("/explorer/block/<int:height>")
    def block_detail(height):
        chain = node.view.chain
        if height < 0 or height >= len(chain):
            return render_template("error.html", title="Not found",
                message="Block not found."), 404
        b = chain[height]
        tx_rows = [(tx_mod.tx_hash(t), t, sum(o["amount"] for o in t["outputs"]))
                   for t in b["transactions"]]
        return render_template("block_detail.html", title=f"Block {height}",
            b=b, tx_rows=tx_rows, has_next=height + 1 < len(chain))

    @app.route("/explorer/tx/<tx_hash>")
    def tx_detail(tx_hash):
        found = found_height = None
        height = node.storage.get_tx_height(tx_hash)
        if height is not None:
            chain = node.view.chain
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == tx_hash:
                        found, found_height = t, height
                        break
        if not found:
            found = node.mempool.get(tx_hash)
        if not found:
            return render_template("error.html", title="Not found",
                message="Transaction not found."), 404
        location = f"Block {found_height}" if found_height is not None else "Mempool (unconfirmed)"
        return render_template("tx_detail.html", title="Transaction",
            tx_hash=tx_hash, tx=found, location=location)

    @app.route("/address", methods=["GET", "POST"])
    def address_lookup():
        addr = request.args.get("addr", "").strip()
        ctx = dict(title="Address", addr=addr, alert_err="", history=None,
                   balance=0, nonce=0)
        if addr and not crypto_mod.is_valid_address(addr):
            ctx["alert_err"] = "Invalid address format."
            ctx["addr"] = ""
        elif addr:
            v = node.view
            ctx["balance"] = v.state.get_balance(addr)
            ctx["nonce"]   = v.state.get_nonce(addr)
            history = []
            for h_height, h in node.storage.get_tx_heights_for_addr(addr):
                chain = v.chain
                if 0 <= h_height < len(chain):
                    for t in chain[h_height]["transactions"]:
                        if tx_mod.tx_hash(t) == h:
                            direction = "sent" if t["from"] == addr else "received"
                            history.append((h_height, h, direction, t))
                            break
            ctx["history"] = history
        return render_template("address.html", **ctx)

    @app.route("/whitepaper")
    def whitepaper():
        base    = getattr(__import__("sys"), "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        wp_path = os.path.join(base, "whitepaper.md")
        try:
            with open(wp_path) as f:
                rendered = markdown.markdown(f.read(), extensions=["fenced_code", "tables"])
        except FileNotFoundError:
            rendered = "<p>whitepaper.md not found.</p>"
        return render_template("whitepaper.html", title="Whitepaper", rendered=rendered)

    @app.route("/stats")
    def stats():
        return render_template("stats.html", title="Stats")

    # ---- JSON API --------------------------------------------------------

    @app.route("/api/stats")
    def api_stats():
        chain = node.view.chain
        if len(chain) <= 1:
            return jsonify({"points": [], "totals": {
                "minted": 0, "burned_fees": 0, "circulating": 0}})
        sv = node.view.state
        points, cum_burned = [], 0
        for blk in chain[1:]:
            burned = sum(t["fee"] for t in blk.get("transactions", []))
            cum_burned += burned
            frac = blk["height"] / max(len(chain) - 1, 1)
            approx_minted = int(sv.total_minted * frac)
            points.append({"height": blk["height"], "minted": approx_minted,
                           "burned_fees": cum_burned,
                           "circulating": approx_minted - cum_burned,
                           "net_emission": burned})
        if len(points) > 500:
            step = len(points) / 500
            points = [points[int(i * step)] for i in range(500)]
        return jsonify({"points": points, "totals": {
            "minted":      sv.total_minted,
            "burned_fees": sv.total_burnt,
            "circulating": sv.total_minted - sv.total_burnt,
            "can_mint":    max(0, SUPPLY_CAP - sv.total_minted + sv.total_burnt),
            "rings_per_ech": RINGS_PER_ECH,
        }})

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
        try:
            from_height = int(request.args.get("from", 0))
            to_height   = int(request.args.get("to", from_height + 500))
        except (TypeError, ValueError):
            return jsonify({"error": "from and to must be integers"}), 400
        to_height = min(to_height, from_height + 500)
        chain = node.view.chain
        return jsonify([b for b in chain if from_height <= b["height"] <= to_height])

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
        return jsonify({"address": addr, "balance_rings": balance,
                        "balance_ech": balance / RINGS_PER_ECH})

    @app.route("/api/address/<addr>/history")
    def api_history(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        chain = node.view.chain
        history = []
        for height, h in node.storage.get_tx_heights_for_addr(addr):
            if 0 <= height < len(chain):
                for t in chain[height]["transactions"]:
                    if tx_mod.tx_hash(t) == h:
                        history.append({"height": height, "tx_hash": h,
                            "direction": "sent" if t["from"] == addr else "received", "tx": t})
                        break
        return jsonify(history)

    @app.route("/api/mempool")
    def api_mempool():
        txs = node.mempool.all_txs()
        return jsonify({"size": len(txs), "transactions": [
            {"hash": tx_mod.tx_hash(t), "from": t["from"],
             "outputs": t["outputs"], "fee": t["fee"]} for t in txs]})

    @app.route("/api/receive_block", methods=["POST"])
    @_rate_limited(block_limiter)
    def api_recv_block():
        data = request.get_json()
        if not data or "block" not in data:
            return jsonify({"ok": False, "error": "missing block"}), 400
        blk = data["block"]
        required = {"height", "previous_hash", "hash", "timestamp",
                    "transactions", "builder", "fee_rate", "vdf_output", "vdf_proof"}
        if not isinstance(blk, dict) or not required.issubset(blk):
            return jsonify({"ok": False, "error": "malformed block"}), 400
        if not isinstance(blk["height"], int) or blk["height"] < 0:
            return jsonify({"ok": False, "error": "malformed block"}), 400
        if not isinstance(blk["transactions"], list):
            return jsonify({"ok": False, "error": "malformed block"}), 400
        for t in blk["transactions"]:
            if not isinstance(t, dict) or not _TX_REQUIRED_FIELDS.issubset(t):
                _strike_sender(pool, data)
                return jsonify({"ok": False, "error": "malformed block"}), 400
        net_in_q.put({"type": "block", "block": blk})
        _register_sender(discovery, data)
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
        if node.mark_tx_seen(h):
            return jsonify({"ok": True})
        net_in_q.put({"type": "tx", "tx": tx_dict,
                      "relay_type": data.get("type", "tx_fluff"),
                      "remaining_hops": data.get("remaining_hops", 0)})
        return jsonify({"ok": True})

    @app.route("/api/peers")
    def api_peers():
        addrs = pool.all_addrs()
        return jsonify({"count": len(addrs), "peers": addrs})

    @app.route("/api/peers/add", methods=["POST"])
    def api_add_peer():
        if request.remote_addr not in _LOCALHOST:
            return jsonify({"ok": False, "error": "localhost only"}), 403
        data = request.get_json()
        if data and "host" in data and "port" in data:
            pool.add(f"{data['host']}:{data['port']}")
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "need host and port"}), 400

    return app
