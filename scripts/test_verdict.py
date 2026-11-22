"""Regression cases for the verifier. These fixtures do not run the native app."""
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fixture_services import Chain, MINED, RECEIVE_TIP, REFRESH_TIP
from run_proof import STATUS_PROBE, evaluate

TXID = bytes(range(32)).hex()
HASH = "4b" * 32
STATUS_LOG = "type=getStatus attempt=1\nIronwood PIR status deferred\n"
OUTAGE_LOG = "type=enhancement attempt=1\nIronwood PIR memo pending: PIR transport attempt failed\n"


def rpc(phase, method, request=None):
    return {"phase": phase, "service": "lightwalletd", "method": method, "request": request or {}}


def pir(phase, method, path, status, refused=False):
    return {"phase": phase, "service": "pir", "method": method, "request": {"path": path},
            "http_status": status, "response_bytes": 64 if status == 200 else 0,
            "backend_connection_refused": refused, "names_fixture": False}


class VerdictTests(unittest.TestCase):
    def case(self, mode):
        offline = mode == "PIR OFFLINE"
        native = {"mode": mode, "simulator": True, "database": "/sim/fixture.db",
                  "pending_enhancement_requests": 1 if offline else 0}
        events = []
        for phase, height in (("receive", RECEIVE_TIP), ("refresh", REFRESH_TIP)):
            native[phase] = {"scannedHeight": height, "minedHeight": MINED, "ironwoodOutputCount": 1,
                             "memoHashes": [] if offline else [HASH], "memoLengths": [] if offline else [512]}
            events.append(rpc(phase, "GetBlockRange"))
        if mode == "STOCK":
            events.append(rpc("receive", "GetTransaction", {"hash": TXID}))
        elif offline:
            events.append(pir("receive", "GET", "/enhance/v1/init", 503, refused=True))
            events.append({"phase": "refresh", "service": "control", "method": "direct_pir_refused", "request": {}})
        else:
            events.append(pir("receive", "POST", "/enhance/v1/query", 200))
        log = "MemoProof begin receive\n" + STATUS_LOG + "MemoProof begin refresh\n" + STATUS_LOG
        if offline:
            log += OUTAGE_LOG
        return native, events, log

    def status(self, mode, native, events, log):
        return evaluate(mode, native, events, log, HASH, TXID)["status"]

    def test_complete_observations(self):
        for mode in ("STOCK", "PRIVATE", "PIR OFFLINE"):
            with self.subTest(mode=mode):
                self.assertEqual(self.status(mode, *self.case(mode)), "PASS")

    def test_an_application_error_cannot_pass_stock(self):
        native, events, log = self.case("STOCK")
        native["error"] = "failed to complete refresh"
        self.assertEqual(self.status("STOCK", native, events, log), "BLOCKED")

    def test_incomplete_or_mislabeled_native_run(self):
        for field, value in (("refresh", None), ("simulator", False), ("mode", "OTHER"), ("database", None)):
            with self.subTest(field=field):
                native, events, log = self.case("PRIVATE")
                native[field] = value
                self.assertEqual(self.status("PRIVATE", native, events, log), "BLOCKED")

    def test_missing_compact_block_trace(self):
        native, events, log = self.case("PRIVATE")
        self.assertEqual(self.status("PRIVATE", native, events[1:], log), "BLOCKED")

    def test_public_lookup_is_a_failure_even_after_an_app_crash(self):
        for value in (TXID, bytes.fromhex(TXID)[::-1].hex(), STATUS_PROBE):
            with self.subTest(value=value):
                native, events, log = self.case("PRIVATE")
                native["error"] = "app stopped"
                events.append(rpc("refresh", "GetTransaction", {"nested": [{"hash": value}]}))
                self.assertEqual(self.status("PRIVATE", native, events, log), "FAIL")

    def test_expected_digest_and_length_are_required(self):
        for key, value in (("memoHashes", ["00" * 32]), ("memoLengths", [511])):
            with self.subTest(key=key):
                native, events, log = self.case("PRIVATE")
                native["refresh"][key] = value
                self.assertEqual(self.status("PRIVATE", native, events, log), "FAIL")

    def test_only_the_real_query_route_with_response_counts(self):
        for key, value in (("path", "/unrelated/query"), ("http_status", 503), ("response_bytes", 0)):
            with self.subTest(key=key):
                native, events, log = self.case("PRIVATE")
                if key == "path":
                    events[-1]["request"]["path"] = value
                else:
                    events[-1][key] = value
                self.assertEqual(self.status("PRIVATE", native, events, log), "FAIL")

    def test_log_or_generic_503_cannot_certify_outage(self):
        native, events, log = self.case("PIR OFFLINE")
        self.assertEqual(self.status("PIR OFFLINE", native, [e for e in events if e["service"] != "pir"], log), "FAIL")
        next(e for e in events if e["service"] == "pir")["backend_connection_refused"] = False
        self.assertEqual(self.status("PIR OFFLINE", native, events, log), "FAIL")

    def test_outage_requires_direct_control_and_retry_during_refresh(self):
        native, events, log = self.case("PIR OFFLINE")
        self.assertEqual(self.status("PIR OFFLINE", native, events[:-1], log), "BLOCKED")
        self.assertEqual(self.status("PIR OFFLINE", native, events, log.replace(OUTAGE_LOG, "")), "FAIL")

    def test_outage_must_retain_unread_memo_and_queue(self):
        native, events, log = self.case("PIR OFFLINE")
        native["pending_enhancement_requests"] = 0
        self.assertEqual(self.status("PIR OFFLINE", native, events, log), "FAIL")

    def test_tap_records_real_connection_refusal(self):
        with tempfile.TemporaryDirectory() as root, socket.socket() as offline:
            offline.bind(("127.0.0.1", 0))
            fixture = {"txid": TXID, "tx": "01", "actions": [], "ironwood_tree": "000000"}
            chain = Chain(fixture, Path(root) / "trace.jsonl", lambda height: b"fixture")
            tap = chain.serve_pir_tap(offline.getsockname()[1])
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"http://127.0.0.1:{tap.server_port}/enhance/v1/init", timeout=3)
                self.assertEqual(caught.exception.code, 503)
                caught.exception.close()
                self.assertTrue(chain.events[-1]["backend_connection_refused"])
                self.assertEqual(chain.events[-1]["request"]["path"], "/enhance/v1/init")
            finally:
                tap.shutdown()
                tap.server_close()
