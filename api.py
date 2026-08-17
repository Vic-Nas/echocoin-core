"""HTTP API + node UI. Thin wrapper over node + pool. No HTML here.

All endpoints are read-only except /api/submit_tx, /api/receive_tx, and
/api/receive_block. Flask runs in multiple threads; all reads go through
node.view (a GIL-atomic snapshot) or node.pool (thread-safe). Writes use
node.submit_tx_from_api() which serialises through the node loop via a queue.

Endpoint map:
  GET  /                         -- dashboard (HTML)
  GET  /explorer                 -- block explorer (HTML)
  GET  /address/<addr>           -- address detail (HTML)
  GET  /block/<hash_or_height>   -- block detail (HTML)
  GET  /tx/<tx_hash>             -- tx detail (HTML)
  GET  /whitepaper               -- whitepaper (HTML)
  GET  /burn                     -- burn leaderboard + form (HTML)
  GET  /send                     -- send form (HTML)
  GET  /stats                    -- stats page (HTML)
  GET  /api/info                 -- node info JSON (used by peers for genesis check)
  GET  /api/chain                -- paginated chain JSON
  GET  /api/peers                -- peer list JSON
  GET  /api/mempool              -- pending txs JSON
  POST /api/submit_tx            -- submit a signed tx
  POST /api/receive_tx           -- inbound tx relay from a peer
  POST /api/receive_block        -- inbound block from a peer
"""

import logging
import os

import markdown
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import crypto as crypto_mod
import pob as pob_mod
import tx as tx_mod
from params import POB_WINDOW, RINGS_PER_ECH, SUPPLY_CAP
from pob import BURN_ADDRESS


def fmt_balance(rings):
    ech = rings // RINGS_PER_ECH
    rem = rings % RINGS_PER_ECH
    return f"{ech} ECH {rem:,} rings"


def fmt_score(score):
    """Abbreviate large PoB scores for display: 3.01e26, 1.2e18, etc."""
    if score == 0:
        return "0"
    import math
    exp = int(math.log10(score))
    if exp < 6:
        return f"{score:,}"
    mantissa = score / (10 ** exp)
    return f"{mantissa:.2f}e{exp}"


def fmt_fee_rate(rings_per_byte):
    ech = rings_per_byte / RINGS_PER_ECH
    if ech >= 0.001:
        return f"{ech:.6f} ECH/byte"
    return f"{ech:.2e} ECH/byte"


from pydantic import BaseModel, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

log = logging.getLogger("ec.api")


class _BlockIn(BaseModel):
    model_config = {"extra": "allow"}
    height: int
    previous_hash: str
    hash: str
    timestamp: float
    transactions: list
    builder: str | None = None
    fee_rate: int
    vdf_output: str | None = None
    vdf_proof: str | None = None

    @field_validator("height")
    @classmethod
    def height_non_negative(cls, v):
        if v < 0:
            raise ValueError("height must be non-negative")
        return v

    @model_validator(mode="after")
    def validate_transactions(self):
        for t in self.transactions:
            if not isinstance(t, dict) or not _TX_REQUIRED_FIELDS.issubset(t):
                raise ValueError("malformed transaction in block")
        return self

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TX_REQUIRED_FIELDS = {"from", "pubkey", "outputs", "nonce", "fee_height", "fee", "signature"}
_LOCALHOST = ("127.0.0.1", "::1")


def _get_address_history(addr, node):
    """Return list of (height, tx_hash, direction, tx_dict) for addr."""
    v = node.view
    history = []
    for h_height, h in node.storage.get_tx_heights_for_addr(addr):
        chain = v.chain
        if 0 <= h_height < len(chain):
            for t in chain[h_height]["transactions"]:
                if tx_mod.tx_hash(t) == h:
                    direction = "sent" if t["from"] == addr else "received"
                    history.append((h_height, h, direction, t))
                    break
    return history


def _localhost_only():
    """Return a 403 response if request is not from localhost, else None."""
    if request.remote_addr not in _LOCALHOST:
        return jsonify({"ok": False, "error": "localhost only"}), 403
    return None


def _parse_csv_outputs(outputs_raw):
    """Parse 'address,amount' CSV lines. Returns (outputs, errors)."""
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
    return outputs, errors


def _submit_and_alert(node, outputs, passphrase, ctx):
    """Build, sign, and submit a tx. Sets ctx alert_ok or alert_err in place."""
    if not passphrase and not node.is_signing_active():
        ctx["alert_err"] = "Passphrase required (leave blank if mining loop is active)."
        return
    try:
        t, _fee = node.build_and_sign_tx(outputs, passphrase or None)
        ok, result = node.submit_tx_from_api(t)
        if ok:
            ctx["alert_ok"] = f'Submitted. tx: <span class="hash">{result}</span>'
        else:
            ctx["alert_err"] = f"Error: {result}"
    except Exception as e:
        log.warning("[api] tx build/submit failed  err=%s", e)
        ctx["alert_err"] = f"Error: {e}"


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
    app.jinja_env.globals.update(fmt_balance=fmt_balance, fmt_fee_rate=fmt_fee_rate, fmt_score=fmt_score)
    app.logger.setLevel(logging.WARNING)

    limiter = Limiter(get_remote_address, app=app, default_limits=[],
                      storage_uri="memory://")

    # ---- UI pages --------------------------------------------------------

    @app.route("/")
    def dashboard():
        info = node.get_info()
        chain = node.view.chain
        return render_template("dashboard.html", title="Dashboard",
            info=info, tip=chain[-1], recent_blocks=chain[-10:][::-1])

    @app.route("/send", methods=["GET", "POST"])
    def send():
        denied = _localhost_only()
        if denied:
            return denied
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
            outputs, errors = _parse_csv_outputs(outputs_raw)
            if errors:
                ctx["alert_err"] = "<br>".join(errors)
            elif not outputs:
                ctx["alert_err"] = "No valid outputs."
            else:
                _submit_and_alert(node, outputs, passphrase, ctx)
                if ctx["alert_ok"]:
                    ctx["alert_ok"] = ctx["alert_ok"].replace("Submitted.", "Sent.")
        return render_template("send.html", **ctx)

    @app.route("/burn", methods=["GET", "POST"])
    def burn():
        denied = _localhost_only()
        if denied:
            return denied
        v = node.view
        balance      = v.state.get_balance(node.addr)
        bw           = v.burn_window
        pool_totals  = bw.pool_totals()
        burn_totals  = bw.sender_totals()
        tip_hash_int = pob_mod._tip_hash_int(v.chain)
        ctx = dict(title="Burn", from_addr=node.addr, balance=balance,
                   my_burn=burn_totals.get(node.addr, 0),
                   my_score=bw.score(tip_hash_int, node.addr),
                   total_burn=sum(pool_totals.values()),
                   sorted_burners=sorted(burn_totals.items(), key=lambda x: -x[1]),
                   sorted_pools=sorted(pool_totals.items(), key=lambda x: -x[1]),
                   burn_history=bw.history(),
                   scores={addr: bw.score(tip_hash_int, addr) for addr in pool_totals},
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
                if burn_rings > balance:
                    ctx["alert_err"] = "Insufficient balance."
                else:
                    beneficiary = request.form.get("beneficiary", "").strip() or node.addr
                    if not crypto_mod.is_valid_address(beneficiary):
                        beneficiary = node.addr
                    burn_out = {"to": BURN_ADDRESS, "amount": burn_rings}
                    if beneficiary != node.addr:
                        burn_out["beneficiary"] = beneficiary
                    _submit_and_alert(node, [burn_out], passphrase, ctx)
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
            ctx["history"] = _get_address_history(addr, node)
        return render_template("address.html", **ctx)

    @app.route("/whitepaper")
    def whitepaper():
        base    = getattr(__import__("sys"), "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        wp_path = os.path.join(base, "docs", "whitepaper.md")
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
        v  = node.view
        sv = v.state
        return jsonify({"points": node.stats.points, "totals": {
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
    @limiter.limit("20 per second")
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
        return jsonify([
            {"height": h, "tx_hash": th, "direction": d, "tx": t}
            for h, th, d, t in _get_address_history(addr, node)
        ])

    @app.route("/api/mempool")
    def api_mempool():
        txs = node.mempool.all_txs()
        return jsonify({"size": len(txs), "transactions": [
            {"hash": tx_mod.tx_hash(t), "from": t["from"],
             "outputs": t["outputs"], "fee": t["fee"]} for t in txs]})

    @app.route("/api/receive_block", methods=["POST"])
    @limiter.limit("10 per second")
    def api_recv_block():
        data = request.get_json()
        if not data or "block" not in data:
            return jsonify({"ok": False, "error": "missing block"}), 400
        try:
            blk = _BlockIn.model_validate(data["block"]).model_dump()
        except PydanticValidationError as e:
            log.warning("[api] malformed block from %s: %s",
                        request.remote_addr, str(e)[:120])
            _strike_sender(pool, data)
            return jsonify({"ok": False, "error": "malformed block"}), 400
        net_in_q.put({"type": "block", "block": blk})
        _register_sender(discovery, data)
        return jsonify({"ok": True})

    @app.route("/api/receive_tx", methods=["POST"])
    @limiter.limit("20 per second")
    def api_recv_tx():
        # Validated and enqueued via node loop; errors logged there.
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
        denied = _localhost_only()
        if denied:
            return denied
        data = request.get_json()
        if data and "host" in data and "port" in data:
            pool.add(f"{data['host']}:{data['port']}")
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "need host and port"}), 400

    return app
