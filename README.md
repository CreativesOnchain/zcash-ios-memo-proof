# Private Ironwood memo retrieval on iOS

This test follows one incoming Zcash payment through the Swift wallet SDK. It records which requests the wallet makes to read a 512-byte Ironwood memo, then refreshes the wallet and checks the memo again. The payment, viewing key and chain are synthetic. The wallet connects to local test services.

## What the native proof measures

[Simulator run #5](https://github.com/CreativesOnchain/zcash-ios-memo-proof/actions/runs/45073979280) passed all three cases on November 22, 2026. It used Xcode 16.4 and an iPhone 17 Pro simulator. Each case started with a fresh wallet database.

| SDK and service | What the wallet read on receive and refresh | What the servers observed |
| --- | --- | --- |
| STOCK | The expected 512-byte memo both times. | One `GetTransaction` naming the payment on receive. No repeat on refresh. |
| PRIVATE | The same memo both times. | One Valar query on receive, with a 39,028-byte request and a 33,908-byte response. No lightwalletd request named the payment or the separate status probe. |
| PIR OFFLINE | The confirmed output was visible both times. The memo stayed absent and one enhancement remained queued. | On receive, the recorder returned HTTP 503 after Valar refused the connection. On refresh, the recorder's own port was closed and the SDK logged a failed retry. Neither ID appeared in the lightwalletd requests. |

The app reads the memo through `SDKSynchronizer.getMemos`. The verifier checks its length and SHA-256 digest against the expected fixture bytes. In the two private cases, a second test ID exercises the SDK path for transactions whose mined height is unknown. A lightwalletd request naming either ID fails either private case, even if the app later exits. Missing app reports or missing block-request traces leave the case blocked. The [verifier](scripts/run_proof.py) checks both scan heights and requires a different wallet database for each mode.

The [verdict](evidence/ios-run-5/results.json), [decoded requests](evidence/ios-run-5/requests.jsonl), and [app reports and console logs](evidence/ios-run-5/) are copies from run #5. The queue counts came from the runner's read-only SQLite queries.

Run #5 recorded scan heights 3,428,144 on receive and 3,428,145 on refresh. Replaying its receive request against the original fixture exposed a server error. The fixture could send the next block early and then end the stream with a gRPC error. The corrected fixture stops at the tip advertised for each phase. A fresh simulator run is needed to verify the app with that correction.

## Scope of the SDK change

This independent test uses the [Swift SDK revision vendored by Zignal](https://github.com/Zkignal/zignal/tree/1341797872c97c21a7d9f83e1936f89d876fbcec/vendor/zcash-swift-wallet-sdk) and [Valar's Enhance service](https://github.com/valargroup/wallet-pir/blob/09bcd8c8b79b14f3dd6599a07fc9ba72017616a3/docs/enhance/integration.md). The [SDK patch](patches/zignal-sdk.diff) retains the compact data needed to authenticate the returned note, checks the publication anchor against the scanned chain, and writes the decrypted memo to the wallet database.

Enabling the patch also defers ordinary status and full-transaction requests, including unrelated ones. Even after PRIVATE recovered the memo, the full-transaction enhancement remained queued. The outage test ended while Valar was offline. Recovery after service returns, app restart, wallet upgrades, Tor routing and physical iPhone behavior are outside this run.

## Run from Windows

In a GitHub repository where you have write access, open **Actions → iOS memo proof → Run workflow**. To use your own account, fork this repository and enable Actions in the fork. The job runs on a hosted Mac. Download the **ios-memo-proof** artifact for the verdict, requests and build logs.

On a Mac, use a fresh clone with Xcode, an iPhone Simulator running iOS 17 or later, Python 3.11 or 3.12, Rustup, XcodeGen, protoc and OpenSSL 3 on your path.

```sh
bash scripts/run-macos.sh
```

## Replay the verifier without Xcode

From the repository root, use Python 3.11 or 3.12 on Linux or macOS. These tests replay run #5 and exercise the verifier's failure cases.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r scripts/requirements.txt
mkdir -p .venv/proto
python -m grpc_tools.protoc -I proto --python_out=.venv/proto proto/compact_formats.proto proto/service.proto
PYTHONPATH="$PWD/.venv/proto" python -m unittest discover -s scripts -p test_verdict.py -v
```

## What has already run on Linux

The earlier [Rust wallet result](evidence/linux-wallet-core.json) and [request log](evidence/linux-requests.jsonl) came from a host driver that chose the requests itself. Its native-iOS status records the state before the first simulator run. To repeat that comparison, build separate stock and patched SDK libraries with `cargo +1.97.1 rustc --lib --locked --crate-type cdylib` in each SDK crate. Generate the fixture and protocol modules using [run-macos.sh](scripts/run-macos.sh). Build Valar's publisher and query server with the features in [run_proof.py](scripts/run_proof.py). Add the generated modules and Valar's `tools/enhance/scripts` directory to `PYTHONPATH`, then run the host driver with paths to those builds and a new output directory.

```sh
python scripts/check-rust-wallet.py \
  --stock-library /path/to/stock/libzcashlc.so \
  --private-library /path/to/patched/libzcashlc.so \
  --fixture /path/to/fixture.json \
  --binaries /path/to/valar/bin \
  --artifacts ./host-comparison
```

The harness uses the [MIT license](LICENSE). The [SDK notice](patches/SDK-LICENSE) and [protocol license](proto/LICENSE) accompany the vendored files. The [wallet protocol schemas](https://github.com/zakura-core/wallet-libraries/tree/248fb191a91d1260e50f95b3a14a12f67085829c/librustzcash/zcash_client_backend/lightwallet-protocol/walletrpc) are pinned to the revision used by this fixture.
