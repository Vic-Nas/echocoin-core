"""HTTP API and browser UI for Echocoin nodes.

Peer communication is handled separately over UDP. This module only serves
human-facing browser UI and a JSON API for wallets and block explorers.

Two Flask apps are created by the factory functions at the bottom of this file:

Public app  (default port 8333, externally reachable):
  UI:
    GET  /                            dashboard
    GET  /explorer                    recent block list
    GET  /explorer/block/<height>     block detail
    GET  /explorer/tx/<hash>          transaction detail
    GET  /address?addr=<addr>         address balance and history
    GET  /whitepaper                  protocol whitepaper
    GET  /stats                       emission and burn chart
    GET  /send                        403 — local interface only
    GET  /burn                        403 — local interface only

  JSON API (Content-Type: application/json):
    GET  /api/info
         {"height", "tip_hash", "genesis_hash", "fee_rate", "mempool_size",
          "address", "peer_count", "total_minted", "total_burnt", "can_mint"}

    GET  /api/fee_rate
         {"fee_rate": <rings/byte>, "height": <n>}

    GET  /api/block/<height>          full block object or {"error": "not found"}
    GET  /api/tx/<hash>               transaction object (confirmed or mempool)

    GET  /api/address/<addr>/balance
         {"address", "balance_rings", "balance_ech"}

    GET  /api/address/<addr>/history
         [{"height", "tx_hash", "direction": "sent"|"received", "tx"}, ...]

    GET  /api/mempool
         {"size": <n>, "transactions": [{"hash", "from", "outputs", "fee"}, ...]}

    GET  /api/stats
         {"points": [...], "totals": {"minted", "burned_fees", "circulating",
          "can_mint", "supply_cap", "net_emission_last", "rings_per_ech"}}

    POST /api/tx/send                 rate-limited: 20 requests/second
         Request body (JSON):
           {"from": <address>, "pubkey": <hex>, "outputs": [...],
            "nonce": <int>, "fee_height": <int>, "fee": <int>,
            "signature": <hex>}
         Response:
           {"ok": true,  "tx_hash": <hex>}
           {"ok": false, "error": <string>}

Private app  (default port 8334, 127.0.0.1 only):
  All public UI and JSON API endpoints, plus:
    GET/POST /send                    build and sign a send transaction
    GET/POST /burn                    build and sign a burn transaction
    POST     /api/peers/add           {"host": <str>, "port": <int>}
"""

import logging
import os
import sys

import markdown
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import crypto as crypto_mod
import pob as pob_mod
import tx as tx_mod
from params import POB_WINDOW, RINGS_PER_ECH, SUPPLY_CAP
from pob import BURN_ADDRESS

log = logging.getLogger("ec.api")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_balance(rings):
    ech = rings // RINGS_PER_ECH
    rem = rings % RINGS_PER_ECH
    return f"{ech} ECH {rem:,} rings"


def fmt_score(score):
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_address_history(addr, node):
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


def _parse_csv_outputs(outputs_raw):
    outputs, errors = [], []
    for i, line in enumerate(outputs_raw.strip().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) != 2:
            errors.append(f"Line {i}: expected 'address,amount'")
            continue
        addr, amt_str = parts[0].strip(), parts[1].strip()
        if not crypto_mod.is_valid_address(addr):
            errors.append(f"Line {i}: invalid address")
            continue
        try:
            amt = int(amt_str)
            if amt <= 0:
                raise ValueError("must be positive")
        except ValueError:
            errors.append(f"Line {i}: invalid amount '{amt_str}'")
            continue
        outputs.append({"to": addr, "amount": amt})
    return outputs, errors


def _submit_and_alert(node, outputs, passphrase, ctx):
    if not passphrase:
        ctx["alert_err"] = "Passphrase required."
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


def _shared_read_only_routes(app, node, pool, limiter,
                              private_port, public_port, is_private):
    """Register all read-only UI and API routes on app."""
    # Use a prefix so public and private apps don't collide on endpoint names
    pfx = "priv_" if is_private else "pub_"

    @app.context_processor
    def inject_ctx():
        return {"is_private": is_private,
                "private_port": private_port,
                "public_port": public_port}

    # ---- UI pages --------------------------------------------------------

    @app.route("/", endpoint=pfx+"dashboard")
    def dashboard():
        info = node.get_info()
        chain = node.view.chain
        return render_template("dashboard.html", title="Dashboard",
            info=info, tip=chain[-1], recent_blocks=chain[-10:][::-1])

    @app.route("/explorer", endpoint=pfx+"explorer")
    def explorer():
        return render_template("explorer.html", title="Explorer",
            recent=node.view.chain[-20:][::-1])

    @app.route("/explorer/block/<int:height>", endpoint=pfx+"block_detail")
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

    @app.route("/explorer/tx/<tx_hash>", endpoint=pfx+"tx_detail")
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
        location = (f"Block {found_height}"
                    if found_height is not None else "Mempool (unconfirmed)")
        return render_template("tx_detail.html", title="Transaction",
            tx_hash=tx_hash, tx=found, location=location)

    @app.route("/address", methods=["GET", "POST"], endpoint=pfx+"address_lookup")
    def address_lookup():
        addr = request.args.get("addr", "").strip()
        ctx = dict(title="Address", addr=addr, alert_err="",
                   history=None, balance=0, nonce=0)
        if addr and not crypto_mod.is_valid_address(addr):
            ctx["alert_err"] = "Invalid address format."
            ctx["addr"] = ""
        elif addr:
            v = node.view
            ctx["balance"] = v.state.get_balance(addr)
            ctx["nonce"]   = v.state.get_nonce(addr)
            ctx["history"] = _get_address_history(addr, node)
        return render_template("address.html", **ctx)

    @app.route("/whitepaper", endpoint=pfx+"whitepaper")
    def whitepaper():
        import sys
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        # Also try one level up in case api.py is in a subdirectory
        for candidate in [
            os.path.join(base, "docs", "whitepaper.md"),
            os.path.join(os.path.dirname(base), "docs", "whitepaper.md"),
            os.path.join(os.getcwd(), "docs", "whitepaper.md"),
        ]:
            if os.path.isfile(candidate):
                try:
                    with open(candidate) as f:
                        rendered = markdown.markdown(
                            f.read(), extensions=["fenced_code", "tables"])
                    return render_template("whitepaper.html", title="Whitepaper",
                                           rendered=rendered)
                except Exception:
                    break
        rendered = "<p>whitepaper.md not found.</p>"
        return render_template("whitepaper.html", title="Whitepaper",
                               rendered=rendered)

    @app.route("/stats", endpoint=pfx+"stats")
    def stats():
        return render_template("stats.html", title="Stats")

    # ---- JSON API (read-only) --------------------------------------------

    @app.route("/api/info", endpoint=pfx+"api_info")
    def api_info():
        return jsonify(node.get_info())

    @app.route("/api/fee_rate", endpoint=pfx+"api_fee_rate")
    def api_fee_rate():
        v = node.view
        return jsonify({"fee_rate": v.tip["fee_rate"], "height": v.height})

    @app.route("/api/block/<int:height>", endpoint=pfx+"api_block")
    def api_block(height):
        chain = node.view.chain
        if 0 <= height < len(chain):
            return jsonify(chain[height])
        return jsonify({"error": "not found"}), 404

    @app.route("/api/tx/<tx_hash_val>", endpoint=pfx+"api_get_tx")
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

    @app.route("/api/address/<addr>/balance", endpoint=pfx+"api_balance")
    def api_balance(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        balance = node.view.state.get_balance(addr)
        return jsonify({"address": addr, "balance_rings": balance,
                        "balance_ech": balance / RINGS_PER_ECH})

    @app.route("/api/address/<addr>/history", endpoint=pfx+"api_history")
    def api_history(addr):
        if not crypto_mod.is_valid_address(addr):
            return jsonify({"error": "invalid address"}), 400
        return jsonify([
            {"height": h, "tx_hash": th, "direction": d, "tx": t}
            for h, th, d, t in _get_address_history(addr, node)
        ])

    @app.route("/api/mempool", endpoint=pfx+"api_mempool")
    def api_mempool():
        txs = node.mempool.all_txs()
        return jsonify({"size": len(txs), "transactions": [
            {"hash": tx_mod.tx_hash(t), "from": t["from"],
             "outputs": t["outputs"], "fee": t["fee"]} for t in txs]})

    @app.route("/api/stats", endpoint=pfx+"api_stats")
    def api_stats():
        v  = node.view
        sv = v.state
        pts = node.stats.points
        net_last = pts[-1]["net_emission"] if pts else 0
        return jsonify({"points": pts, "totals": {
            "minted":           sv.total_minted,
            "burned_fees":      sv.total_burnt,
            "circulating":      sv.total_minted - sv.total_burnt,
            "can_mint":         max(0, SUPPLY_CAP - sv.total_minted + sv.total_burnt),
            "supply_cap":       SUPPLY_CAP,
            "net_emission_last": net_last,
            "rings_per_ech":    RINGS_PER_ECH,
        }})

    @app.route("/api/tx/send", methods=["POST"], endpoint=pfx+"api_send_tx")
    @limiter.limit("20 per second")
    def api_send_tx():
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "no JSON body"}), 400
        ok, result = node.submit_tx_from_api(data)
        if ok:
            return jsonify({"ok": True, "tx_hash": result})
        return jsonify({"ok": False, "error": result}), 400


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# PyInstaller-aware base path
# ---------------------------------------------------------------------------

def _base_dir():
    """Return the directory that contains templates_html/, working both from
    source (repo root) and inside a PyInstaller bundle (sys._MEIPASS)."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


# Public app factory  (port 8333)
# ---------------------------------------------------------------------------

def create_app(node, pool, private_port=8334, public_port=8333):
    app = Flask(__name__,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(
        fmt_balance=fmt_balance, fmt_fee_rate=fmt_fee_rate, fmt_score=fmt_score)
    app.logger.setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    limiter = Limiter(get_remote_address, app=app, default_limits=[],
                      storage_uri="memory://")

    _shared_read_only_routes(app, node, pool, limiter,
                             private_port, public_port, is_private=False)

    # Send/Burn disabled on public port; show locked page
    @app.route("/send")
    def send_locked():
        return render_template("error.html", title="Send",
            message=f"Send is only available on the local interface "
                    f"(localhost:{private_port})."), 403

    @app.route("/burn")
    def burn_locked():
        return render_template("error.html", title="Burn",
            message=f"Burn is only available on the local interface "
                    f"(localhost:{private_port})."), 403

    return app


# ---------------------------------------------------------------------------
# Private app factory  (port 8334, 127.0.0.1 only)
# ---------------------------------------------------------------------------

def create_private_app(node, pool, private_port=8334, public_port=8333):
    """Full-featured app for local use. Never expose via Funnel or public port."""
    app = Flask(__name__,
                template_folder=os.path.join(_base_dir(), "templates_html"))
    app.jinja_env.globals.update(
        fmt_balance=fmt_balance, fmt_fee_rate=fmt_fee_rate, fmt_score=fmt_score)
    app.logger.setLevel(logging.WARNING)

    limiter = Limiter(get_remote_address, app=app, default_limits=[],
                      storage_uri="memory://")

    _shared_read_only_routes(app, node, pool, limiter,
                             private_port, public_port, is_private=True)

    @app.route("/send", methods=["GET", "POST"])
    def send():
        v = node.view
        ctx = dict(title="Send", from_addr=node.addr,
                   balance=v.state.get_balance(node.addr),
                   nonce=v.state.get_nonce(node.addr) + 1,
                   fee_rate=v.tip["fee_rate"],
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
                    beneficiary = (request.form.get("beneficiary", "").strip()
                                   or node.addr)
                    if not crypto_mod.is_valid_address(beneficiary):
                        beneficiary = node.addr
                    burn_out = {"to": BURN_ADDRESS, "amount": burn_rings}
                    if beneficiary != node.addr:
                        burn_out["beneficiary"] = beneficiary
                    _submit_and_alert(node, [burn_out], passphrase, ctx)
        return render_template("burn.html", **ctx)

    @app.route("/api/peers/add", methods=["POST"])
    def api_add_peer():
        data = request.get_json()
        if data and "host" in data and "port" in data:
            pool.add(f"{data['host']}:{data['port']}")
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "need host and port"}), 400

    return app
