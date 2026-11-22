"""Run the host Rust wallet comparison with a built C library and local Valar binaries."""
import argparse
import ctypes
import hashlib
import json
import os
import platform
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import grpc
import compact_formats_pb2 as compact
import service_pb2 as rpc
from fixture_node import confirmation_transaction
from fixture_services import Chain, START, MINED, RECEIVE_TIP, REFRESH_TIP
from run_proof import require_refusal, start_valar, write_json


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def sized(value):
    return value, len(value)


class WalletCore:
    """Signatures from the pinned SDK's generated zcashlc.h."""
    def __init__(self, library, private):
        lib = ctypes.CDLL(str(library.resolve()))
        p, size, u32, i32, i64, flag = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                                        ctypes.c_int32, ctypes.c_int64, ctypes.c_bool)

        def bind(name, result, *args):
            function = getattr(lib, "zcashlc_" + name)
            function.argtypes = args
            function.restype = result
            return function

        self.error_size = bind("last_error_length", i32)
        self.error_text = bind("error_message_utf8", i32, p, i32)
        self.clear = bind("clear_last_error", None)
        self.network = bind("set_custom_network", flag, u32, *([i64] * 10))
        self.init = bind("init_data_database", i32, p, size, p, size, u32)
        self.account = bind("import_account_ufvk", p, p, size, p, p, size, i64, u32, u32, p, p, p, u32)
        self.free_id = bind("free_ffi_uuid", None, p)
        self.init_cache = bind("init_block_metadata_db", flag, p, size)
        self.tip = bind("update_chain_tip", flag, p, size, i32, u32)
        scan_args = [p, size, p, size, i32, p, size, u32, u32]
        if private:
            scan_args.append(flag)
        self.scan = bind("scan_blocks", p, *scan_args)
        self.free_scan = bind("free_scan_summary", None, p)
        self.decrypt = bind("decrypt_and_store_transaction", i32, p, size, p, size, i64, u32, p)
        if private:
            self.recover = bind("recover_ironwood_memos", i32, p, size, p, size, p, u32)

    def last_error(self):
        length = self.error_size()
        if length < 1:
            return "No last-error detail from the wallet library"
        buf = ctypes.create_string_buffer(length + 1)
        self.error_text(buf, length + 1)
        self.clear()
        return buf.value.decode(errors="replace")

    def check(self, success):
        if not success:
            raise RuntimeError(self.last_error())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("stock-library", "private-library", "fixture", "binaries", "artifacts"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    output = args.artifacts.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = json.loads(args.fixture.read_text())
    txid = bytes.fromhex(fixture["txid"])
    expected = bytes.fromhex(fixture["memo"])
    stock = WalletCore(args.stock_library, private=False)
    candidate = WalletCore(args.private_library, private=True)
    for core in (stock, candidate):
        core.check(core.network(1, 1, *([START] * 8), MINED))
    chain = Chain(fixture, output / "requests.jsonl", confirmation_transaction)
    lwd, lwd_port = chain.serve_wallet()
    node = chain.serve_node()
    fleet = tap = None
    channel = grpc.insecure_channel(f"127.0.0.1:{lwd_port}")
    service = "/" + rpc.DESCRIPTOR.services_by_name["CompactTxStreamer"].full_name + "/"
    blocks = channel.unary_stream(service + "GetBlockRange", response_deserializer=compact.CompactBlock.FromString)
    full_tx = channel.unary_unary(service + "GetTransaction", response_deserializer=rpc.RawTransaction.FromString)
    patch = Path(__file__).resolve().parents[1] / "patches/zignal-sdk.diff"
    report = {"platform": platform.system() + " " + platform.machine(), "layer": "Rust C ABI and fixture gRPC",
              "zignal_source": "1341797872c97c21a7d9f83e1936f89d876fbcec",
              "valar_source": "09bcd8c8b79b14f3dd6599a07fc9ba72017616a3",
              "candidate_patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
              "native_ios_status": "BLOCKED", "txid_wire_order": fixture["txid"],
              "expected_memo_sha256": hashlib.sha256(expected).hexdigest(), "runs": {}}
    try:
        for mode in ("ORDINARY", "PRIVATE", "PIR OFFLINE"):
            core = stock if mode == "ORDINARY" else candidate
            chain.set_run("HOST " + mode)
            if mode == "PRIVATE":
                state = output / "fleet"
                state.mkdir()
                fleet = start_valar(chain, output, state, args.binaries.resolve(), node.server_port)
                backend = urlsplit(fleet.origin).port
                tap = chain.serve_pir_tap(backend)
                pir_port = tap.server_port
                origin = f"http://127.0.0.1:{pir_port}".encode()
            elif mode == "PIR OFFLINE":
                fleet.close()
                fleet = None
                require_refusal(backend)
            wallet_dir = output / mode.lower().replace(" ", "-")
            wallet_dir.mkdir()
            cache = wallet_dir / "cache"
            cache.mkdir()
            cache_path = os.fsencode(cache)
            database = wallet_dir / "wallet.db"
            db_path = os.fsencode(database)
            core.check(core.init(*sized(db_path), None, 0, 2) == 0)
            birthday = rpc.TreeState(height=START, hash="0" * 64, saplingTree="000000").SerializeToString()
            account = core.account(*sized(db_path), fixture["ufvk"].encode(), *sized(birthday),
                                   RECEIVE_TIP, 2, 1, b"Public fixture", None, None, 2**32 - 1)
            core.check(account)
            core.free_id(account)
            core.check(core.init_cache(*sized(cache_path)))
            report["runs"][mode] = {}
            for phase, first, last in (("receive", MINED, RECEIVE_TIP), ("refresh", REFRESH_TIP, REFRESH_TIP)):
                chain.phase, chain.tip = phase, last
                if phase == "refresh" and mode == "PIR OFFLINE":
                    tap.shutdown()
                    tap.server_close()
                    tap = None
                    require_refusal(pir_port)
                block_range = rpc.BlockRange(start=rpc.BlockID(height=first), end=rpc.BlockID(height=last))
                count = 0
                with sqlite3.connect(cache / "blockmeta.sqlite") as meta:
                    for block in blocks(block_range.SerializeToString(), timeout=5):
                        path = cache / "blocks" / f"{block.height}-{block.hash[::-1].hex()}-compactblock"
                        path.write_bytes(block.SerializeToString())
                        meta.execute("""INSERT INTO compactblocks_meta
                          (height,blockhash,time,sapling_outputs_count,orchard_actions_count) VALUES(?,?,?,?,?)""",
                                     (block.height, block.hash, block.time, 0, 0))
                        count += 1
                require(count == last - first + 1, "Fixture gRPC block range incomplete")
                core.check(core.tip(*sized(db_path), last, 2))
                state = chain.state(first - 1).SerializeToString()
                scan_args = [*sized(cache_path), *sized(db_path), first, *sized(state), count, 2]
                if mode != "ORDINARY":
                    scan_args.append(True)
                scan = core.scan(*scan_args)
                core.check(scan)
                core.free_scan(scan)
                if phase == "receive" and mode == "ORDINARY":
                    reply = full_tx(rpc.TxFilter(hash=txid).SerializeToString(), timeout=5)
                    require(reply.height == MINED and reply.data.hex() == fixture["tx"], "Wrong full transaction")
                    returned_id = ctypes.create_string_buffer(32)
                    core.check(core.decrypt(*sized(db_path), *sized(reply.data), MINED, 2, returned_id) == 1)
                    require(returned_id.raw == txid, "Wallet decrypted a different transaction")
                elif phase == "receive" and mode == "PRIVATE":
                    core.check(core.recover(*sized(db_path), *sized(txid), origin, 2) == 1)
                elif mode == "PIR OFFLINE":
                    rc = core.recover(*sized(db_path), *sized(txid), origin, 2)
                    require(rc == -1 and "PIR transport attempt failed" in core.last_error(),
                            "Wallet core did not report the expected transport failure")
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
                    outputs = conn.execute("SELECT output_pool, memo FROM v_tx_outputs WHERE txid=?", (txid,)).fetchall()
                    mined = conn.execute("SELECT mined_height FROM v_transactions WHERE txid=?", (txid,)).fetchall()
                    queue = conn.execute("SELECT query_type FROM tx_retrieval_queue WHERE txid=?", (txid,)).fetchall()
                require(mined == [(MINED,)], "Wallet view did not identify the confirmed transaction")
                require(outputs == [(4, None if mode == "PIR OFFLINE" else expected)], "Unexpected memo in wallet view")
                if mode == "PIR OFFLINE":
                    require(queue == [(1,)], "The unavailable memo did not retain its enhancement request")
                memo = outputs[0][1]
                report["runs"][mode][phase] = {
                    "mined_height": MINED, "scanned_through": last, "ironwood_outputs": len(outputs),
                    "memo_bytes": None if memo is None else len(memo),
                    "memo_sha256": None if memo is None else hashlib.sha256(memo).hexdigest(),
                    "pending_full_enhancements": sum(row == (1,) for row in queue),
                }
            trace = [e for e in chain.events if e["mode"] == "HOST " + mode]
            ordinary = [e for e in trace if e["service"] == "lightwalletd" and e["method"] == "GetTransaction"]
            require(len(ordinary) == (1 if mode == "ORDINARY" else 0), "Host driver issued an unexpected lookup")
            pir = [e for e in trace if e["service"] == "pir"]
            answered = sum(e["method"] == "POST" and urlsplit(e["request"]["path"]).path == "/enhance/v1/query"
                           and e["http_status"] == 200 and e["response_bytes"] > 0 for e in pir)
            refused = sum(e.get("backend_connection_refused", False) for e in pir)
            require((answered > 0) == (mode == "PRIVATE"), "Unexpected encrypted query evidence")
            require((refused > 0) == (mode == "PIR OFFLINE"), "Unexpected witness evidence")
            report["runs"][mode]["traffic"] = {"host_requested_GetTransaction": len(ordinary),
                                               "successful_encrypted_posts": answered, "witnessed_refusals": refused}
        report["host_core_status"] = "PASS"
        write_json(output / "linux-wallet-core.json", report)
        print(json.dumps(report, indent=2))
    finally:
        channel.close()
        if tap is not None:
            tap.shutdown()
            tap.server_close()
        if fleet is not None:
            fleet.close()
        node.shutdown()
        node.server_close()
        lwd.stop(grace=0).wait()


if __name__ == "__main__":
    main()
