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
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from peer_udp import MT_TX, UDPTransport


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
