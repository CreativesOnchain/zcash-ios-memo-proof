"""Drive the stock and patched native apps. Compute the verdict from retained evidence."""
import argparse
import errno
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from fixture_services import Chain, MINED, RECEIVE_TIP, REFRESH_TIP

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "org.example.zcash.memoproof"
MODES = ("STOCK", "PRIVATE", "PIR OFFLINE")
STATUS_PROBE = "5a" * 32


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sim(udid, command, *args, **kwargs):
    return subprocess.run(["xcrun", "simctl", command, udid, *map(str, args)], timeout=30, **kwargs)


def await_file(path, process, timeout=330):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            raise RuntimeError(f"Native app exited with code {process.returncode}; see its console log")
        time.sleep(0.25)
    raise TimeoutError("Native app did not produce its report; see its console log")


def run_app(mode, udid, fixture, ports, app, artifacts):
    label = mode.lower().replace(" ", "-")
    sim(udid, "uninstall", BUNDLE, check=False, capture_output=True)
    sim(udid, "install", app, check=True)
    root = Path(sim(udid, "get_app_container", BUNDLE, "data", check=True,
                    capture_output=True, text=True).stdout.strip())
    documents = root / "Documents"
    lwd_port, control_port, pir_port = ports
    env = os.environ | {
        "SIMCTL_CHILD_PROOF_MODE": mode,
        "SIMCTL_CHILD_PROOF_LWD": str(lwd_port),
        "SIMCTL_CHILD_PROOF_CONTROL": f"http://127.0.0.1:{control_port}",
        "SIMCTL_CHILD_PROOF_PIR": f"http://127.0.0.1:{pir_port}",
        "SIMCTL_CHILD_PROOF_TXID": fixture["txid"],
        "SIMCTL_CHILD_PROOF_UFVK": fixture["ufvk"],
        "SIMCTL_CHILD_NSUnbufferedIO": "YES",
    }
    log_path = artifacts / f"{label}-simulator.log"
    with log_path.open("w", encoding="utf-8") as log:
        native = subprocess.Popen(
            ["xcrun", "simctl", "launch", "--console", udid, BUNDLE],
            stdout=log, stderr=subprocess.STDOUT, env=env,
        )
        try:
            result = await_file(documents / "report.json", native)
            write_json(artifacts / f"{label}-native.json", result)
            if result.get("simulator") and result.get("mode") == mode and result.get("receive"):
                wallet = Path(result["database"]).resolve()
                if not wallet.is_relative_to(documents.resolve()) or not wallet.is_file():
                    raise ValueError("Native result did not refer to a wallet database in its own container")
                with sqlite3.connect(wallet.as_uri() + "?mode=ro", uri=True) as db:
                    row = db.execute(
                        """SELECT COUNT(*) FROM tx_retrieval_queue
                           WHERE txid = ? AND query_type = 1""", (bytes.fromhex(fixture["txid"]),)
                    ).fetchone()
                    result["pending_enhancement_requests"] = row[0]
            return result
        finally:
            receive = documents / "receive.json"
            if receive.exists():
                (artifacts / f"{label}-receive.json").write_bytes(receive.read_bytes())
            sim(udid, "terminate", BUNDLE, check=False, capture_output=True)
            try:
                native.wait(timeout=5)
            except subprocess.TimeoutExpired:
                native.terminate()
                native.wait(timeout=5)


def names(value, targets):
    if isinstance(value, str):
        return any(target in value.lower() for target in targets)
    if isinstance(value, dict):
        return any(names(item, targets) for item in value.values())
    if isinstance(value, list):
        return any(names(item, targets) for item in value)
    return False


def evaluate(mode, native, events, log, expected, txid):
    targets = (txid, bytes.fromhex(txid)[::-1].hex())
    rpc = [e for e in events if e["service"] == "lightwalletd"]
    named = [e for e in rpc if names(e["request"], targets)]
    probe_lookups = [e for e in rpc if names(e["request"], (STATUS_PROBE,))]
    pir = [e for e in events if e["service"] == "pir"]
    def route(event):
        return urlsplit(event["request"]["path"]).path

    query = [e for e in pir if e["method"] == "POST" and route(e) == "/enhance/v1/query"]
    answered = [e for e in query if e["http_status"] == 200 and e["response_bytes"] > 0]
    refused = [e for e in pir if e["method"] == "GET" and route(e) == "/enhance/v1/init"
               and e.get("backend_connection_refused") and e["http_status"] == 503]
    direct_refused = any(e["service"] == "control" and e["method"] == "direct_pir_refused"
                         and e["phase"] == "refresh" for e in events)
    receive_log, marker, refresh_log = log.partition("MemoProof begin refresh")
    phases = {"receive": (RECEIVE_TIP, receive_log), "refresh": (REFRESH_TIP, refresh_log)}
    scans = {phase: sum(e["method"] == "GetBlockRange" and e["phase"] == phase for e in rpc)
             for phase in phases}
    report = {
        "status": "BLOCKED",
        "blocker": native.get("error") or "Native receive and refresh evidence is incomplete",
        "expected_memo_sha256": expected,
        "receive": native.get("receive"),
        "refresh": native.get("refresh"),
        "private_query_attempts": len(query),
        "encrypted_query_posts": len(query),
        "encrypted_query_successes": len(answered),
        "witnessed_offline_init_requests": len(refused),
        "direct_pir_port_refused_before_refresh": direct_refused,
        "compact_block_requests": scans,
        "ordinary_txid_lookups": len(named),
        "ordinary_txid_lookups_receive": sum(e["phase"] == "receive" for e in named),
        "ordinary_txid_lookups_refresh": sum(e["phase"] == "refresh" for e in named),
        "ordinary_get_transaction": sum(e["method"] == "GetTransaction" for e in named),
        "status_probe_ordinary_lookups": len(probe_lookups),
        "sdk_enhancement_attempts": log.count("type=enhancement attempt="),
        "sdk_status_requests": log.count("type=getStatus attempt="),
        "sdk_status_deferred": log.count("Ironwood PIR status deferred"),
        "pending_enhancement_requests": native.get("pending_enhancement_requests"),
        "database": native.get("database"),
        "checks": {},
    }
    checks = report["checks"]

    def fail_if_violated():
        violated = [name for name, passed in checks.items() if not passed]
        if violated:
            report.update(status="FAIL", blocker=None, failed_criteria=violated)
        return bool(violated)

    # An observed disclosure remains evidence even if the app later exits early.
    if mode != "STOCK":
        checks["no ordinary request names the memo transaction"] = not named
        checks["no ordinary request names the status probe"] = not probe_lookups
        checks["no PIR request names the transaction in the body or path"] = not any(e["names_fixture"] for e in pir)
        if mode == "PIR OFFLINE":
            checks["no encrypted query succeeded during outage"] = not answered
    if fail_if_violated():
        return report

    if (native.get("simulator") is not True or native.get("mode") != mode
            or not native.get("database") or native.get("error")
            or not all(native.get(phase) for phase in phases)):
        return report
    if not all(scans.values()):
        report["blocker"] = "Missing decoded compact-block traffic for receive or refresh"
        return report
    if mode != "STOCK" and (not marker or "MemoProof begin receive" not in receive_log):
        report["blocker"] = "Missing native console phase markers"
        return report
    if mode == "PIR OFFLINE" and (not direct_refused or native.get("pending_enhancement_requests") is None):
        report["blocker"] = "Missing direct-refusal control or pending-queue measurement"
        return report

    for phase, (tip, phase_log) in phases.items():
        snapshot = native[phase]
        checks[f"{phase} scanned required tip"] = snapshot.get("scannedHeight", -1) >= tip
        checks[f"{phase} found the mined Ironwood output"] = (
            snapshot.get("minedHeight") == MINED and snapshot.get("ironwoodOutputCount") == 1
        )
        checks[f"{phase} memo bytes"] = (
            snapshot.get("memoHashes") == ([] if mode == "PIR OFFLINE" else [expected])
            and snapshot.get("memoLengths") == ([] if mode == "PIR OFFLINE" else [512])
        )
        if mode != "STOCK":
            statuses = phase_log.count("type=getStatus attempt=")
            checks[f"{phase} exercised and deferred each SDK status lookup"] = (
                statuses > 0 and phase_log.count("Ironwood PIR status deferred") == statuses
            )
    if mode == "STOCK":
        checks["stock receive fetched by transaction ID"] = any(
            e["method"] == "GetTransaction" and e["phase"] == "receive" for e in named
        )
    elif mode == "PRIVATE":
        checks["receive received an encrypted Valar answer"] = any(e["phase"] == "receive" for e in answered)
    else:
        checks["receive reached the tap while its Valar backend refused connections"] = any(
            e["phase"] == "receive" for e in refused
        )
        checks["refresh retried private transport after direct port closure"] = (
            "type=enhancement attempt=" in refresh_log and
            "Ironwood PIR memo pending: PIR transport attempt failed" in refresh_log
        )
        checks["wallet retained pending enhancement"] = native["pending_enhancement_requests"] > 0

    if not fail_if_violated():
        report.update(status="PASS", blocker=None)
    return report


def require_refusal(port):
    with socket.socket() as probe:
        probe.settimeout(2)
        observed = probe.connect_ex(("127.0.0.1", port))
    if observed != errno.ECONNREFUSED:
        raise RuntimeError(f"Expected ECONNREFUSED on the isolated PIR port, got {observed}")


def start_valar(chain, artifacts, state, binaries, node_port):
    from local_fleet import Fleet
    import urllib.request

    fleet = Fleet(binaries, state, artifacts, profile="smoke", block_interval=3600)
    try:
        deferred = []
        start_role = fleet.role
        fleet.role = lambda *args, **kwargs: deferred.append((args, kwargs))
        fleet.start_roles()
        cookie = state / "cookie"
        cookie.write_text("fixture:fixture", encoding="ascii")
        management = f"127.0.0.1:{fleet.port()}"
        cfg = state / "publisher.json"
        write_json(cfg, {
            "management_listen": management,
            "chain": {"rpc_url": f"http://127.0.0.1:{node_port}", "rpc_cookie": str(cookie), "poll_ms": 1000},
            "enhance": {"enabled": True, "settings": {
                "artifact_listen": fleet.artifacts, "listen": fleet.operator,
                "data_dir": str(state / "publisher"), "prevout_cache_outputs": 1000,
            }},
        })
        publisher = fleet.start("publisher", [str(fleet.publisher), "--config", str(cfg)])
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://{management}/.well-known/pir-service-discovery", timeout=2,
                ) as response:
                    discovery = response.read().decode()
                break
            except OSError:
                if publisher.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Pinned Valar publisher did not expose discovery; see publisher.log")
                time.sleep(0.2)
        (state / "fault").write_text("discovery-body:" + discovery, encoding="utf-8")
        for args, kwargs in deferred:
            start_role(*args, **kwargs)
        manifest = fleet.wait_ready(timeout=220)
        if manifest["protocol_revision"] != "ironwood-enhance-pir-v9-native-two-mask-m29":
            raise ValueError("Valar did not serve the pinned native v9 protocol")
        anchor = chain.blocks[manifest["anchor_height"]]
        if (manifest["anchor_block_hash"] != anchor.hash[::-1].hex()
                or manifest["coverage"]["records"] != len(chain.fixture["actions"])):
            raise ValueError("Valar publication does not cover the fixture chain")
        write_json(artifacts / "valar-manifest.json", manifest)
        return fleet
    except BaseException:
        fleet.close()
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("udid")
    args = p.parse_args()
    state = ROOT / ".run"
    artifacts = ROOT / "artifacts"
    output = artifacts / "results.json"
    result = json.loads(output.read_text())
    run_results = result["runs"]
    fixture = json.loads((state / "fixture.json").read_text())
    memo = bytes([255]) + bytes(i % 251 for i in range(1, 512))
    assert bytes.fromhex(fixture["memo"]) == memo
    expected = hashlib.sha256(memo).hexdigest()
    for pending in run_results.values():
        pending["expected_memo_sha256"] = expected
    result.update(fixture_txid=fixture["txid"], fixture_mined_height=MINED,
                  expected_memo_sha256=expected, simulator_udid=args.udid, status_probe_txid=STATUS_PROBE)
    write_json(output, result)

    from fixture_node import confirmation_transaction
    chain = Chain(fixture, artifacts / "requests.jsonl", confirmation_transaction)
    lwd, lwd_port = chain.serve_wallet()
    control = chain.serve_control()
    node = chain.serve_node()
    fleet = tap = None
    stage = "stock build"
    try:
        for mode in MODES:
            label = mode.lower().replace(" ", "-")
            chain.set_run(mode)
            if mode == "STOCK":
                subprocess.run(["bash", str(ROOT / "scripts/build-native.sh"), "stock", args.udid], check=True)
                app = state / "stock/Build/Products/Debug-iphonesimulator/MemoProof.app"
                pir_port = 0
            elif mode == "PRIVATE":
                stage = "candidate build"
                subprocess.run(["bash", str(ROOT / "scripts/build-native.sh"), "private", args.udid], check=True)
                app = state / "private/Build/Products/Debug-iphonesimulator/MemoProof.app"
                stage = "build pinned Valar publisher and query server"
                with (artifacts / "valar-build.log").open("w", encoding="utf-8") as log:
                    subprocess.run([
                        "cargo", "+1.97.1", "build", "--locked", "--profile", "release-fast",
                        "-p", "pir-publisher", "-p", "pir-query-server", "--jobs", "4",
                        "--features", "pir-publisher/enhance,pir-publisher/native-reinspiring,"
                        "pir-query-server/enhance,pir-query-server/native-reinspiring",
                    ], cwd=state / "wallet-pir", stdout=log, stderr=subprocess.STDOUT, check=True)
                stage = "Valar publisher and query service setup"
                fleet_state = state / "valar-fleet"
                fleet_state.mkdir()
                fleet = start_valar(chain, artifacts, fleet_state, state / "wallet-pir/target/release-fast",
                                    node.server_address[1])
                tap = chain.serve_pir_tap(urlsplit(fleet.origin).port)
                pir_port = tap.server_address[1]
            else:
                stage = "isolating the PIR outage"
                backend_port = urlsplit(fleet.origin).port
                fleet.close()
                fleet = None
                require_refusal(backend_port)
                result["offline_isolation"] = {
                    "valar_backend_port": backend_port, "valar_backend_refused": True,
                    "app_pir_port": pir_port, "lightwalletd_port": lwd_port,
                }

                def isolate_refresh():
                    nonlocal tap
                    tap.shutdown()
                    tap.server_close()
                    tap = None
                    require_refusal(pir_port)
                    chain.record("control", "direct_pir_refused", {"port": pir_port})

                chain.before_refresh = isolate_refresh
            stage = f"{mode} native Simulator run"
            try:
                native = run_app(mode, args.udid, fixture,
                                 (lwd_port, control.server_address[1], pir_port), app, artifacts)
            except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
                native = {"error": f"{stage}: {error}"}
            log_path = artifacts / f"{label}-simulator.log"
            log = log_path.read_text(errors="replace") if log_path.exists() else ""
            run_results[mode] = evaluate(
                mode, native, [event for event in chain.events if event["mode"] == mode],
                log, expected, fixture["txid"],
            )
            write_json(output, result)
            if mode == "STOCK" and run_results[mode]["status"] != "PASS":
                break
    except Exception as error:
        result["blocker"] = f"{stage}: {error}"
        for run in run_results.values():
            if run.get("blocker") == "Not executed":
                run["blocker"] = result["blocker"]
        print(result["blocker"], file=sys.stderr)
    finally:
        if tap is not None:
            tap.shutdown()
            tap.server_close()
        if fleet is not None:
            fleet.close()
        node.shutdown()
        control.shutdown()
        lwd.stop(grace=1).wait()
        statuses = [run.get("status") for run in run_results.values()]
        if "FAIL" in statuses:
            result.update(status="FAIL", blocker=None)
        elif statuses == ["PASS"] * 3:
            dbs = [run["database"] for run in run_results.values()]
            if len(set(dbs)) != 3:
                result.update(status="FAIL", blocker=None, failed_criteria=["Distinct wallet databases"])
            else:
                result.update(status="PASS", blocker=None)
        elif result.get("blocker") in (None, "No native Simulator run completed"):
            result["blocker"] = "; ".join(
                f"{mode}: {run.get('blocker')}" for mode, run in run_results.items() if run["status"] != "PASS"
            )
        write_json(output, result)
        print(result["status"], flush=True)
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

