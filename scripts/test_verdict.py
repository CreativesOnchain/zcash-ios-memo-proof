"""Check the verifier against recorded Simulator output and controlled failure cases."""
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import grpc
import compact_formats_pb2 as compact
import service_pb2 as rpc

from fixture_services import Chain, MINED, RECEIVE_TIP, REFRESH_TIP
from run_proof import MODES, ROOT, STATUS_PROBE, evaluate, run_app

TXID = bytes(range(32)).hex()
HASH = "4b" * 32
STATUS_LOG = "type=getStatus attempt=1\nIronwood PIR status deferred\n"
OUTAGE_LOG = "type=enhancement attempt=1\nIronwood PIR memo pending: PIR transport attempt failed\n"


def rpc_event(phase, method, request=None):
    return {"phase": phase, "service": "lightwalletd", "method": method, "request": request or {}}


def pir(phase, method, path, status, refused=False):
    return {"phase": phase, "service": "pir", "method": method,
            "request": {"path": path, "body_bytes": 32 if method == "POST" else 0},
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
            events.append(rpc_event(phase, "GetBlockRange"))
        if mode == "STOCK":
            events.append(rpc_event("receive", "GetTransaction", {"hash": TXID}))
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

    def test_receive_cannot_claim_a_block_from_the_refresh_phase(self):
        native, events, log = self.case("PRIVATE")
        native["receive"]["scannedHeight"] = REFRESH_TIP
        self.assertEqual(self.status("PRIVATE", native, events, log), "FAIL")

    def test_public_lookup_is_a_failure_even_after_an_app_crash(self):
        for value in (TXID, bytes.fromhex(TXID)[::-1].hex(), STATUS_PROBE):
            with self.subTest(value=value):
                native, events, log = self.case("PRIVATE")
                native["error"] = "app stopped"
                events.append(rpc_event("refresh", "GetTransaction", {"nested": [{"hash": value}]}))
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

    def test_empty_or_unmeasured_query_cannot_prove_private_retrieval(self):
        native, events, log = self.case("PRIVATE")
        event = events[-1]
        event["request"]["body_bytes"] = 0
        report = evaluate("PRIVATE", native, events, log, HASH, TXID)
        self.assertEqual((report["status"], report["encrypted_query_successes"]), ("FAIL", 0))
        del event["request"]["body_bytes"]
        self.assertEqual(self.status("PRIVATE", native, events, log), "BLOCKED")

    def test_even_an_empty_successful_reply_contradicts_the_outage(self):
        native, events, log = self.case("PIR OFFLINE")
        event = pir("receive", "POST", "/enhance/v1/query", 204)
        events.append(event)
        self.assertEqual(self.status("PIR OFFLINE", native, events, log), "FAIL")

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
            offline.close()
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

    def test_failed_outage_setup_keeps_the_receive_tip_for_a_retry(self):
        fixture = {"txid": TXID, "tx": "01", "actions": [], "ironwood_tree": "000000"}
        with tempfile.TemporaryDirectory() as tmp:
            chain = Chain(fixture, Path(tmp) / "trace.jsonl", lambda height: b"fixture")
            chain.set_run("PIR OFFLINE")
            control = chain.serve_control()
            request = urllib.request.Request(f"http://127.0.0.1:{control.server_port}/refresh", data=b"")
            def failed_setup():
                raise RuntimeError("PIR port is still open")
            try:
                chain.before_refresh = failed_setup
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(caught.exception.code, 503)
                caught.exception.close()
                self.assertEqual((chain.phase, chain.tip), ("receive", RECEIVE_TIP))
                self.assertEqual(chain.events[-1]["phase"], "receive")
                chain.before_refresh = lambda: 12345
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual((chain.phase, chain.tip), ("refresh", REFRESH_TIP))
                self.assertEqual([(e["phase"], e["method"]) for e in chain.events[-2:]],
                                 [("refresh", "direct_pir_refused"), ("refresh", "refresh")])
            finally:
                control.shutdown()
                control.server_close()

    def test_stream_stops_at_each_phase_tip_for_the_recorded_sdk_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = {"txid": TXID, "tx": "01", "actions": [], "ironwood_tree": "000000"}
            chain = Chain(fixture, Path(tmp) / "trace.jsonl", lambda height: b"fixture")
            chain.set_run("STOCK")
            server, port = chain.serve_wallet()
            control = chain.serve_control()
            service = "/" + rpc.DESCRIPTOR.services_by_name["CompactTxStreamer"].full_name
            trace = (ROOT / "evidence/ios-run-5/requests.jsonl").read_text().splitlines()
            requests = {e["phase"]: e["request"] for e in map(json.loads, trace)
                        if e["mode"] == "STOCK" and e["method"] == "GetBlockRange"}
            try:
                with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
                    stream = channel.unary_stream(service + "/GetBlockRange",
                                                  request_serializer=rpc.BlockRange.SerializeToString,
                                                  response_deserializer=compact.CompactBlock.FromString)
                    def heights(request):
                        return [block.height for block in stream(rpc.BlockRange(**request), timeout=3)]
                    self.assertEqual(heights(requests["receive"]), [MINED, RECEIVE_TIP])
                    self.assertEqual(chain.events[-1]["request"], {
                        "first_height": MINED, "last_height": RECEIVE_TIP, "advertised_tip": RECEIVE_TIP,
                    })
                    self.assertEqual(heights({"start": {"height": RECEIVE_TIP}, "end": {"height": MINED}}),
                                     [RECEIVE_TIP, MINED])
                    with self.assertRaises(grpc.RpcError) as caught:
                        heights(requests["refresh"])
                    self.assertEqual(caught.exception.code(), grpc.StatusCode.OUT_OF_RANGE)
                    request = urllib.request.Request(f"http://127.0.0.1:{control.server_port}/refresh", data=b"")
                    with urllib.request.urlopen(request, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                    self.assertEqual(heights(requests["refresh"]), [REFRESH_TIP])
            finally:
                server.stop(grace=0).wait()
                control.shutdown()
                control.server_close()


class RunFileTests(unittest.TestCase):
    def test_rerun_preserves_the_archived_verdict_and_request_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("run-macos.sh", "results.py"):
                shutil.copy(ROOT / "scripts" / name, scripts)
            init = [sys.executable, str(scripts / "results.py"), "init"]
            subprocess.run(init, cwd=root, check=True)
            results, requests = [root / "artifacts" / name for name in ("results.json", "requests.jsonl")]
            verdict = b'{"status": "PASS"}\n'
            trace = b'{"service":"lightwalletd"}\n'
            results.write_bytes(verdict)
            requests.write_bytes(trace)
            for cmd in (init, ["bash", str(scripts / "run-macos.sh")]):
                self.assertNotEqual(subprocess.run(cmd, cwd=root, capture_output=True).returncode, 0)
                self.assertEqual((results.read_bytes(), requests.read_bytes()), (verdict, trace))
            results.unlink()
            self.assertNotEqual(subprocess.run(init, cwd=root, capture_output=True).returncode, 0)
            self.assertFalse(results.exists())
            self.assertEqual(requests.read_bytes(), trace)

    def test_previous_simulator_reports_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "Documents"
            documents.mkdir()
            for name in ("report.json", "receive.json"):
                with self.subTest(name=name):
                    stale = documents / name
                    stale.write_text("{}")
                    with patch("run_proof.sim", return_value=subprocess.CompletedProcess([], 0, stdout=tmp)), \
                         patch("run_proof.subprocess.Popen") as launch:
                        with self.assertRaisesRegex(RuntimeError, "earlier launch"):
                            run_app("STOCK", "udid", {}, (), root, root)
                        launch.assert_not_called()
                    stale.unlink()

    def test_rejects_only_installed_iphone_if_its_runtime_is_too_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            devices = Path(tmp) / "devices.json"
            devices.write_text(json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-16-4": [
                {"name": "iPhone 14", "state": "Booted", "isAvailable": True, "udid": "old"}
            ]}}))
            result = subprocess.run([sys.executable, str(ROOT / "scripts/select_simulator.py"), str(devices)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("iOS 17", result.stderr)


class ArchivedRunTests(unittest.TestCase):
    def test_native_run_5(self):
        source = ROOT / "evidence/ios-run-5"
        original = json.loads((source / "results.json").read_text())
        events = [json.loads(line) for line in (source / "requests.jsonl").read_text().splitlines()]
        memo = bytes([255]) + bytes(i % 251 for i in range(1, 512))
        expected = hashlib.sha256(memo).hexdigest()
        self.assertEqual(expected, original["expected_memo_sha256"])
        databases = set()
        for mode in MODES:
            with self.subTest(mode=mode):
                prefix = mode.lower().replace(" ", "-")
                native = json.loads((source / f"{prefix}-native.json").read_text())
                # Run 5 archived the host's SQLite count in results.json.
                native["pending_enhancement_requests"] = original["runs"][mode]["pending_enhancement_requests"]
                trace = [e for e in events if e["mode"] == mode]
                console = (source / f"{prefix}-simulator.log").read_text()
                result = evaluate(mode, native, trace, console, expected, original["fixture_txid"])
                self.assertEqual(result["status"], "PASS", result)
                for key in ("ordinary_txid_lookups", "ordinary_txid_lookups_receive", "ordinary_txid_lookups_refresh",
                            "status_probe_ordinary_lookups", "encrypted_query_successes"):
                    self.assertEqual(result[key], original["runs"][mode][key])
                databases.add(result["database"])
        self.assertEqual(len(databases), 3)
