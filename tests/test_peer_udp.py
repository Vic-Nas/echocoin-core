"""
Unit tests for peer_udp.py's UDPTransport._dispatch, MT_TX path only.

Regression test for a bug where remaining_hops/relay_type were dropped on
receive, collapsing every inbound tx to an immediate fluff regardless of
what the sender actually put on the wire -- defeating Dandelion's stem
phase between processes. No sockets are opened; _dispatch is called
directly with a hand-built message.
"""

import os
import sys
import threading
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from peer_udp import MT_GETINFO, MT_INFO, MT_TX, UDPTransport


def _make_transport(on_tx):
    return UDPTransport(
        port=9999,
        genesis_hash="a" * 64,
        on_block=MagicMock(),
        on_tx=on_tx,
        on_peers=MagicMock(),
        pool=MagicMock(),
    )


def test_dispatch_forwards_stem_hop_info():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    udp._dispatch(MT_TX, 111, {"tx": tx, "remaining_hops": 3, "relay_type": "tx_stem"},
                  ("1.2.3.4", 5000))
    on_tx.assert_called_once_with(tx, "1.2.3.4:5000", 111, 3, "tx_stem")


def test_dispatch_defaults_to_fluff_when_fields_absent():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    udp._dispatch(MT_TX, 222, {"tx": tx}, ("1.2.3.4", 5000))
    on_tx.assert_called_once_with(tx, "1.2.3.4:5000", 222, 0, "tx_fluff")


def test_dispatch_dedups_by_msg_id():
    on_tx = MagicMock()
    udp = _make_transport(on_tx)
    tx = {"from": "addr"}
    msg = {"tx": tx, "remaining_hops": 0, "relay_type": "tx_fluff"}
    udp._dispatch(MT_TX, 333, msg, ("1.2.3.4", 5000))
    udp._dispatch(MT_TX, 333, msg, ("1.2.3.4", 5000))
    assert on_tx.call_count == 1


# ---------------------------------------------------------------------------
# MT_GETINFO / MT_INFO wallet/version fields -- must stay wire-compatible
# with peers not carrying them yet.
# ---------------------------------------------------------------------------

def test_getinfo_response_includes_wallet_and_version():
    """Responding to MT_GETINFO must include our wallet and version
    alongside height/tip."""
    udp = _make_transport(MagicMock())
    udp.set_tip_provider(lambda: (42, "deadbeef", "some.wallet.address", "0.1.1"))
    sent = []
    udp._send_one = lambda msg_type, msg_id, data, target: sent.append((msg_type, data, target))

    udp._dispatch(MT_GETINFO, 1, {"genesis": udp.genesis_hash}, ("1.2.3.4", 5000))

    assert len(sent) == 1
    msg_type, data, target = sent[0]
    assert msg_type == MT_INFO
    assert data == {"genesis": udp.genesis_hash, "height": 42,
                    "tip_hash": "deadbeef", "wallet": "some.wallet.address",
                    "version": "0.1.1"}


def test_info_reply_captures_wallet_and_version():
    """A well-formed MT_INFO reply's wallet/version fields land in the
    pending result."""
    udp = _make_transport(MagicMock())
    ev = threading.Event()
    with udp._info_lock:
        udp._info_events[7] = ev

    udp._dispatch(MT_INFO, 7, {"height": 10, "tip_hash": "abc",
                              "wallet": "peer.wallet", "version": "0.2.0"},
                  ("1.2.3.4", 5000))

    assert udp._info_results[7] == {"height": 10, "tip_hash": "abc",
                                    "wallet": "peer.wallet", "version": "0.2.0"}


def test_info_reply_from_older_peer_without_wallet_or_version_field():
    """An old peer's MT_INFO reply (no wallet/version keys at all) must not
    break -- both just come back empty instead of missing/erroring."""
    udp = _make_transport(MagicMock())
    ev = threading.Event()
    with udp._info_lock:
        udp._info_events[9] = ev

    udp._dispatch(MT_INFO, 9, {"height": 10, "tip_hash": "abc"}, ("1.2.3.4", 5000))

    assert udp._info_results[9] == {"height": 10, "tip_hash": "abc",
                                    "wallet": "", "version": ""}
