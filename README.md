# iOS memo proof

The native outcome is **BLOCKED** on the Windows host because Xcode and the iOS Simulator are unavailable. The workflow runs the native proof on a GitHub-hosted Mac.

## Run from Windows

1. Extract the ZIP into a GitHub repository you control. Commit the files at the repository root, including the hidden `.github` directory. Put the workflow on the default branch.
2. In **Actions**, select **iOS memo proof**, then **Run workflow**. The job selects an installed iPhone Simulator at runtime.
3. Download **ios-memo-proof** from the run. Read `results.json` and `requests.jsonl`. The artifact also contains native app reports, Valar logs, Xcode build logs and available `.xcresult` bundles. The job exits nonzero on FAIL or BLOCKED.

On a Mac with Xcode, an installed iPhone Simulator, Rustup, XcodeGen, `protoc`, OpenSSL and Python 3.11 or 3.12, run `bash scripts/run-macos.sh` from a fresh extraction. Setup fetches the pinned dependencies. All wallet and fixture traffic uses loopback.

## Fixture and acceptance

The fixture generator derives a disposable viewing key and encrypts 512 known bytes into one action of the pinned Valar V6 sample. This generates a new note commitment, ciphertext and transaction ID. The fixture chain provides matching compact blocks, full transaction bytes and Ironwood tree state. It has a confirmation block and a new block for refresh. The chain is synthetic and serves the wire encoding without validating proofs or signatures. Ordinary traffic goes to a local lightwalletd-compatible gRPC fixture service that decodes requests. Private traffic goes to the pinned Valar publisher and query binaries.

Each run imports the view-only key into a fresh wallet. The native app obtains memo bytes with `SDKSynchronizer.getMemos` after normal sync. The expected memo remains with the host verifier. PRIVATE and PIR OFFLINE also enqueue a disposable status-only ID. The SQLite backend emits status requests for IDs with unknown mined height, so the probe drives the actual SDK status branch alongside the confirmed memo's enhancement request.

| Run | Passing evidence |
| --- | --- |
| STOCK | The untouched vendored SDK reads the expected memo and the logger records a matching `GetTransaction` during receive. |
| PRIVATE | A real encrypted Valar query returns, the patched SDK reads the expected memo and receive and refresh yield zero ordinary requests naming either ID. The status probe exercises the SDK's status branch in both phases. |
| PIR OFFLINE | The private port refuses connections while the same gRPC service stays up. The SDK attempts private retrieval, detects the confirmed output, leaves the memo and enhancement request pending and sends zero ordinary requests naming either ID. |

The verifier records expected and observed hashes, decoded lookup counts, encrypted posts, offline attempts, status deferrals and database identifiers. PASS requires all three native runs. FAIL names observed contradictions. BLOCKED names evidence or infrastructure that was unavailable.

## SDK change and shipping

`patches/zignal-sdk.diff` is the exact SDK change, including its Rust dependency lockfile. The initializer opts into private incoming Ironwood memos. The Rust layer retains compact action data from the regular scan, validates the Valar manifest against the wallet's scanned chain, authenticates the note using the wallet's viewing key, and writes the memo atomically after rechecking the note and anchor. Other enhancement and status requests are deferred in this mode.

An app can build and link the patched vendored SDK without an upstream merge. The proof remains unverified on iOS. Release work includes recovery for existing wallets whose compact context was already pruned, qualification of reorg and retry behavior, and device measurements.

- [Pinned Zignal source](https://github.com/Zkignal/zignal/tree/1341797872c97c21a7d9f83e1936f89d876fbcec)
- [Pinned Valar source](https://github.com/valargroup/wallet-pir/tree/09bcd8c8b79b14f3dd6599a07fc9ba72017616a3)
- [Valar integration contract](https://github.com/valargroup/wallet-pir/blob/09bcd8c8b79b14f3dd6599a07fc9ba72017616a3/docs/enhance/integration.md)
- [Protocol schemas](https://github.com/zakura-core/wallet-libraries/tree/248fb191a91d1260e50f95b3a14a12f67085829c/librustzcash/zcash_client_backend/lightwallet-protocol/walletrpc)

