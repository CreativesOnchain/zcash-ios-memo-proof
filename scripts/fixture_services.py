"""Local fixture only. Parse every lightwalletd RPC as its declared protobuf message."""
import hashlib
import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
from google.protobuf import message_factory

import compact_formats_pb2 as compact
import service_pb2 as rpc

MINED = 3_428_143
START = MINED - 1
RECEIVE_TIP = MINED + 1
REFRESH_TIP = RECEIVE_TIP + 1
EPOCH = 1_787_000_000


def http_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, body=b"", status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def tagged(value):
    """Convert decoded protobuf field values, preserving byte fields as wire-order hex."""
    if hasattr(value, "ListFields"):
        return {
            field.name: [tagged(item) for item in field_value]
            if field.is_repeated else tagged(field_value)
            for field, field_value in value.ListFields()
        }
    return value.hex() if isinstance(value, bytes) else value


def digest256(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


class Chain:
    def __init__(self, fixture, destination, coinbase):
        self.fixture = fixture
        self.txid = bytes.fromhex(fixture["txid"])
        self.raw = bytes.fromhex(fixture["tx"])
        self.events = []
        self.before_refresh = None
        self.lock = threading.RLock()
        self.destination = destination
        self.blocks = {}
        self.headers = {}
        self.transactions = {}
        previous = bytes(32)
        for height in range(START - 1, REFRESH_TIP + 1):
            header = bytearray(140)
            header[:4] = (4).to_bytes(4, "little")
            header[4:36] = previous
            payout = coinbase(height)
            payout_id = digest256(payout)
            if height == MINED:
                header[36:68] = digest256(payout_id + self.txid)
                self.transactions[height] = b"\x02" + payout + self.raw
            else:
                header[36:68] = payout_id
                self.transactions[height] = b"\x01" + payout
            header[100:104] = (EPOCH + height - START).to_bytes(4, "little")
            header[108:112] = height.to_bytes(4, "little")
            header += b"\xfd\x40\x05" + bytes(1344)
            digest = digest256(header)
            block = compact.CompactBlock(height=height, hash=digest, prevHash=previous,
                                         time=EPOCH + height - START)
            block.chainMetadata.ironwoodCommitmentTreeSize = self.tree_size(height)
            if height == MINED:
                tx = block.vtx.add(index=1, txid=self.txid)
                for action in fixture["actions"]:
                    tx.ironwoodActions.add(
                        nullifier=bytes.fromhex(action["nullifier"]),
                        cmx=bytes.fromhex(action["cmx"]),
                        ephemeralKey=bytes.fromhex(action["epk"]),
                        ciphertext=bytes.fromhex(action["prefix"]),
                    )
            self.blocks[height] = block
            self.headers[height] = header
            previous = digest
        self.set_run("SETUP")

    def tree_size(self, height):
        return len(self.fixture["actions"]) if height >= MINED else 0

    def set_run(self, mode):
        with self.lock:
            self.mode, self.phase, self.tip = mode, "receive", RECEIVE_TIP

    def record(self, service, method, request, **fields):
        with self.lock:
            event = {"mode": self.mode, "phase": self.phase, "service": service,
                     "method": method, "request": request, "monotonic": time.monotonic(), **fields}
            self.events.append(event)
            with self.destination.open("a", encoding="utf-8") as log:
                log.write(json.dumps(event, separators=(",", ":")) + "\n")

    def state(self, height):
        block = self.blocks[height]
        return rpc.TreeState(
            network="main", height=height, hash=block.hash[::-1].hex(), time=block.time,
            saplingTree="000000", orchardTree="000000",
            ironwoodTree=self.fixture["ironwood_tree"] if height >= MINED else "000000",
        )

    def block_range(self, request, context):
        if request.start.hash or request.end.hash:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Block ranges require heights")
        if request.poolTypes and set(request.poolTypes) != {rpc.SAPLING, rpc.ORCHARD, rpc.IRONWOOD}:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "This fixture serves the default shielded pool range")
        start, end = request.start.height, request.end.height
        low, high = sorted((start, end))
        tip = self.tip
        if low < START - 1 or low > tip:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, "Block range is outside the current fixture chain")
        high = min(high, tip)
        heights = range(low, high + 1) if start <= end else range(high, low - 1, -1)
        self.record("fixture", "selected_block_range", {
            "first_height": heights[0], "last_height": heights[-1], "advertised_tip": tip,
        })
        return (self.blocks[height] for height in heights)

    def answer(self, method, request, context):
        if method == "GetLightdInfo":
            return rpc.LightdInfo(version="fixture", vendor="memo-proof", chainName="main",
                                  saplingActivationHeight=START, consensusBranchId="37a5165b",
                                  blockHeight=self.tip, estimatedHeight=self.tip)
        if method == "GetLatestBlock":
            return rpc.BlockID(height=self.tip, hash=self.blocks[self.tip].hash)
        if method == "GetLatestTreeState":
            return self.state(self.tip)
        if method in ("GetTreeState", "GetBlock") and request.height > self.tip:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, "Height is above the current fixture tip")
        if method == "GetTreeState":
            return self.state(request.height)
        if method == "GetBlock":
            return self.blocks[request.height]
        if method == "GetBlockRange":
            return self.block_range(request, context)
        if method == "GetTransaction":
            if request.hash in (self.txid, self.txid[::-1]):
                return rpc.RawTransaction(data=self.raw, height=MINED)
            context.abort(grpc.StatusCode.NOT_FOUND, "Transaction not found in fixture")
        if method in {"GetSubtreeRoots", "GetAddressUtxosStream", "GetTaddressTxids",
                      "GetTaddressTransactions", "GetMempoolStream", "GetMempoolTx"}:
            return iter(())
        if method == "GetAddressUtxos":
            return rpc.GetAddressUtxosReplyList()
        context.abort(grpc.StatusCode.UNIMPLEMENTED, f"Unsupported fixture RPC {method}")

    def serve_wallet(self):
        descriptor = rpc.DESCRIPTOR.services_by_name["CompactTxStreamer"]
        handlers = {}

        def bind(method):
            def call(request, context):
                if method.client_streaming:
                    request = list(request)
                    decoded = [tagged(message) for message in request]
                else:
                    decoded = tagged(request)
                self.record("lightwalletd", method.name, decoded)
                try:
                    return self.answer(method.name, request, context)
                except KeyError as error:
                    context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Missing fixture height: {error}")
            return call

        for method in descriptor.methods:
            request_type = message_factory.GetMessageClass(method.input_type)
            shape = ("stream" if method.client_streaming else "unary") + "_" + (
                "stream" if method.server_streaming else "unary"
            )
            build = getattr(grpc, f"{shape}_rpc_method_handler")
            handlers[method.name] = build(
                bind(method), request_deserializer=request_type.FromString,
                response_serializer=lambda message: message.SerializeToString(),
            )
        server = grpc.server(ThreadPoolExecutor(max_workers=8))
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(
            descriptor.full_name, handlers,
        ),))
        port = server.add_insecure_port("127.0.0.1:0")
        if not port:
            raise RuntimeError("lightwalletd fixture could not bind")
        server.start()
        return server, port

    def serve_control(self):
        chain = self
        refresh_lock = threading.Lock()

        class Control(QuietHandler):
            def do_POST(self):
                with refresh_lock:
                    if self.path != "/refresh" or chain.phase != "receive":
                        return self.reply(status=409)
                    refused_port = None
                    if chain.before_refresh is not None:
                        try:
                            refused_port = chain.before_refresh()
                        except (OSError, RuntimeError) as error:
                            chain.record("control", "outage_setup_error", {"error": str(error)})
                            return self.reply(status=503)
                    with chain.lock:
                        chain.phase, chain.tip = "refresh", REFRESH_TIP
                        if refused_port is not None:
                            chain.record("control", "direct_pir_refused", {"port": refused_port})
                        chain.record("control", "refresh", {})
                self.reply()

        return http_server(Control)

    def serve_node(self):
        chain = self

        class Node(QuietHandler):
            def do_POST(self):
                try:
                    assert self.headers["Authorization"] == "Basic Zml4dHVyZTpmaXh0dXJl"
                    call = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    # Valar keeps the same confirmed publication while the wallet advances.
                    method, args = call["method"], call.get("params", [])
                    if method == "getblockcount":
                        result = RECEIVE_TIP
                    elif method == "getblockhash" and args == [0]:
                        result = "0" * 64
                    else:
                        value = args[0]
                        heights = {b.hash[::-1].hex(): h for h, b in chain.blocks.items()}
                        height = heights[value] if value in heights else int(value)
                        if method == "getblockhash":
                            result = chain.blocks[height].hash[::-1].hex()
                        elif method == "getblock" and args[1] == 0:
                            result = (chain.headers[height] + chain.transactions[height]).hex()
                        elif method == "getblock" and args[1] == 2:
                            result = {"trees": {"ironwood": {"size": chain.tree_size(height)}}}
                        else:
                            raise ValueError(f"Unimplemented node RPC {method}")
                    self.reply(json.dumps({"id": call["id"], "result": result, "error": None}).encode())
                except (AssertionError, ValueError, TypeError, KeyError, IndexError) as error:
                    print(f"Fixture node: {error}", flush=True)
                    self.reply(status=400)

        return http_server(Node)

    def serve_pir_tap(self, target):
        chain = self

        class Tap(QuietHandler):
            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = self.path.lower()
                names_fixture = any(
                    value in body or value.hex().encode() in body.lower() or value.hex() in path
                    for value in (chain.txid, chain.txid[::-1])
                )
                upstream = http.client.HTTPConnection("127.0.0.1", target, timeout=60)
                try:
                    try:
                        upstream.request(self.command, self.path, body,
                                         {"Content-Type": "application/octet-stream"})
                        response = upstream.getresponse()
                        data = response.read()
                    except ConnectionRefusedError:
                        chain.record("pir", self.command, {"path": self.path, "body_bytes": len(body)},
                                     http_status=503, backend_connection_refused=True, names_fixture=names_fixture,
                                     response_bytes=0, request_sha256=hashlib.sha256(body).hexdigest())
                        return self.reply(status=503)
                    chain.record("pir", self.command, {"path": self.path, "body_bytes": len(body)},
                                 http_status=response.status, names_fixture=names_fixture,
                                 backend_connection_refused=False, response_bytes=len(data),
                                 request_sha256=hashlib.sha256(body).hexdigest())
                    self.reply(data, response.status, response.getheader("Content-Type", "application/octet-stream"))
                finally:
                    upstream.close()

            do_GET = forward
            do_POST = forward

        return http_server(Tap)

