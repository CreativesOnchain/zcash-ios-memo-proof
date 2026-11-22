# Private Ironwood memo proof for iOS

**Native verdict before the first Simulator run: BLOCKED.** This repository compares Zignal's pinned Swift wallet SDK with an opt-in change for reading a confirmed Ironwood memo through Valar private information retrieval (PIR). The test uses one disposable viewing key, one synthetic transaction and loopback services.

## Run from Windows

1. Extract the contents into the root of a GitHub repository you control. Commit and push them to its default branch. Check that GitHub shows `.github/workflows/ios-memo-proof.yml` at that path. GitHub Desktop can commit the extracted directory, including `.github`.
2. Open **Actions → iOS memo proof → Run workflow**. The workflow builds both SDK versions and the Valar services from pinned source on a hosted Mac. It selects an installed iPhone Simulator. You can start the job from Windows.
3. Download the **ios-memo-proof** artifact from the completed run. Open **results.json** and **requests.jsonl**. Check the overall status and the three entries under `runs`. A setup failure leaves a BLOCKED verdict with the available Xcode and Simulator diagnostics.

On a Mac, run `bash scripts/run-macos.sh` from a fresh extraction with Xcode, an iPhone Simulator runtime, Rustup, XcodeGen, protoc, OpenSSL 3 and Python 3.11 or 3.12 installed. Setup fetches dependencies. The wallet and test services communicate over loopback.

## What the native proof measures

The generator encrypts 512 known bytes into one Ironwood action, updates its commitment and reconstructs the serialized transaction. The synthetic chain serves matching compact blocks, full transaction bytes and tree state. The fixture exercises wallet serialization, scanning and decryption. It does not validate a spend or submit a transaction.

Each mode imports the disposable viewing key into a fresh database. The app uses its normal Swift synchronizer and reads memos through `SDKSynchronizer.getMemos`. Only the host verifier receives the expected digest. The private modes also insert a separate disposable status-only ID into the wallet queue. This exercises the SDK's ordinary status branch for a transaction of unknown mined height.

| Mode | Evidence required for PASS |
| --- | --- |
| STOCK | The unmodified Swift SDK detects the confirmed output and returns the expected 512 bytes at receive and refresh. A decoded `GetTransaction` names the fixture transaction during receive. |
| PRIVATE | The patched Swift SDK returns the same memo both times. During receive, a real encrypted query receives a nonempty Valar response. Neither phase sends the fixture ID or status-probe ID to the gRPC service. |
| PIR OFFLINE | For receive, the recorder observes the SDK's initialization request, receives a TCP connection refusal from the stopped Valar backend and returns HTTP 503. For refresh, the recorder shuts down too. The harness verifies direct TCP refusal at the app's PIR port and the SDK logs a retry failure. The confirmed output stays visible, its memo stays absent, the enhancement request stays queued and neither ID goes to the gRPC service. |

All three modes require recorded compact-block requests during both phases. Both private modes must exercise and defer the ordinary status lookup in each phase. A directly observed disclosure fails the privacy check even if the app crashes. Missing native evidence, a missing trace or a failed build leaves the relevant run BLOCKED. The overall PASS requires three distinct wallet databases and three passing native runs.

## What has already run on Linux

`evidence/linux-wallet-core.json` contains the result from separate builds of the pinned original Rust wallet library and the patched library. `evidence/linux-requests.jsonl` contains the corresponding decoded HTTP and gRPC traffic. The host driver called the C entry points for scanning, ordinary decryption and private recovery. It read `v_tx_outputs` and `v_transactions` in each resulting wallet database after both phases. The driver chose the requests in this comparison. Only the Simulator run can establish which requests the Swift app selects.

The ordinary path retrieved the full serialized transaction. The private path made a successful encrypted query. Both stored the same 512-byte memo. The stopped backend and then the closed wallet-facing port left one confirmed Ironwood output, a NULL memo and one pending enhancement. After *successful* private recovery, the general full-transaction enhancement also remained queued. A memo can be available before the wallet has the rest of the transaction.

To repeat the optional host comparison, build separate Linux shared libraries from the original and patched SDK crates with `cargo +1.97.1 rustc --lib --locked --crate-type cdylib`. Generate the protocol modules and fixture with the commands in `scripts/run-macos.sh`. Build Valar's `pir-publisher` and `pir-query-server` at the pin and with the features in `scripts/run_proof.py`. With the generated modules and Valar's `tools/enhance/scripts` on `PYTHONPATH`, run the following command. Use a new directory for the output.

```sh
python scripts/check-rust-wallet.py \
  --stock-library /path/to/stock/libzcashlc.so \
  --private-library /path/to/patched/libzcashlc.so \
  --fixture /path/to/fixture.json \
  --binaries /path/to/valar/bin \
  --artifacts ./host-comparison
```

## Scope of the SDK change

`patches/zignal-sdk.diff` includes the exact SDK change and Rust lockfile. The app can vendor it directly. The patch retains compact context from newly scanned notes, compares the Valar manifest anchor against the scanned chain, authenticates and decrypts the note locally, and rechecks the note and anchor before writing the memo. The proof covers receiving one confirmed note. The opt-in mode also defers ordinary status and full-transaction requests, including unrelated ones. Existing-wallet recovery, a complete policy for those deferred requests, reorg handling and physical-device measurements remain release work.

- [Zignal developer's question about Swift and iOS](https://forum.zcashcommunity.com/t/announcing-fastz-cash-rpc-by-zcash-labs/59478/3)
- [Pinned Zignal SDK source](https://github.com/Zkignal/zignal/tree/1341797872c97c21a7d9f83e1936f89d876fbcec)
- [Pinned Valar service and integration contract](https://github.com/valargroup/wallet-pir/blob/09bcd8c8b79b14f3dd6599a07fc9ba72017616a3/docs/enhance/integration.md)
- [Pinned wallet protocol schemas](https://github.com/zakura-core/wallet-libraries/tree/248fb191a91d1260e50f95b3a14a12f67085829c/librustzcash/zcash_client_backend/lightwallet-protocol/walletrpc)
- [GitHub manual workflow instructions](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)

The proof harness uses the MIT license. The bundled protocol schemas and SDK patch carry their upstream notices in `proto/LICENSE` and `patches/SDK-LICENSE`.
