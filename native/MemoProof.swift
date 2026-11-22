import CryptoKit
import Foundation
import SQLite3
import UIKit
import ZcashLightClientKit

private struct Snapshot: Codable {
    let scannedHeight: Int
    let minedHeight: Int?
    let ironwoodOutputCount: Int
    let memoHashes: [String]
    let memoLengths: [Int]
}

private struct Report: Codable {
    let mode: String
    let simulator: Bool
    let database: String
    var receive: Snapshot?
    var refresh: Snapshot?
    var error: String?
}

private struct ProofError: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

private struct ProofLogger: ZcashLightClientKit.Logger {
    func maxLogLevel() -> OSLogger.LogLevel? { nil }
    func debug(_ message: String, file: StaticString, function: StaticString, line: Int) {}
    func info(_ message: String, file: StaticString, function: StaticString, line: Int) { print(message) }
    func event(_ message: String, file: StaticString, function: StaticString, line: Int) {}
    func warn(_ message: String, file: StaticString, function: StaticString, line: Int) { print(message) }
    func error(_ message: String, file: StaticString, function: StaticString, line: Int) { print(message) }
    func sync(_ message: String, file: StaticString, function: StaticString, line: Int) { print(message) }
}

@main
final class MemoProof: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        let screen = UIViewController()
        screen.view.backgroundColor = .systemBackground
        window.rootViewController = screen
        window.makeKeyAndVisible()
        self.window = window
        Task { await run() }
        return true
    }

    @MainActor
    private func run() async {
        let env = ProcessInfo.processInfo.environment
        let mode = env["PROOF_MODE"] ?? "unknown"
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let root = documents.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let db = root.appendingPathComponent("wallet.db")
        let reportURL = documents.appendingPathComponent("report.json")
        #if targetEnvironment(simulator)
        let simulator = true
        #else
        let simulator = false
        #endif
        var report = Report(mode: mode, simulator: simulator, database: db.path)
        var wallet: SDKSynchronizer?

        do {
            guard simulator else { throw ProofError("This proof requires the iOS Simulator") }
            guard let port = env["PROOF_LWD"].flatMap(Int.init),
                  let txid = env["PROOF_TXID"].flatMap(Self.hexData), txid.count == 32,
                  let ufvk = env["PROOF_UFVK"],
                  let control = env["PROOF_CONTROL"].flatMap(URL.init(string:)) else {
                throw ProofError("Missing native fixture launch settings")
            }
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
            let endpoint = LightWalletEndpoint(address: "127.0.0.1", port: port, secure: false,
                                               singleCallTimeoutInMillis: 5_000, streamingCallTimeoutInMillis: 10_000)
            let receiveTip = 3_428_144
            let birthday = 3_428_142
            var heights = NetworkActivationHeights.allActiveFromGenesis
            heights.sapling = birthday
            heights.blossom = birthday
            heights.heartwood = birthday
            heights.canopy = birthday
            heights.nu5 = birthday
            heights.nu6 = birthday
            heights.nu6_1 = birthday
            heights.nu6_2 = birthday
            heights.nu6_3 = birthday + 1
            let network = ZcashNetworkBuilder.custom(base: .mainnet, activationHeights: heights)
            #if PRIVATE_MEMO
            guard mode != "STOCK", let origin = env["PROOF_PIR"].flatMap(URL.init(string:)) else {
                throw ProofError("The private build requires a loopback PIR origin")
            }
            let initializer = Initializer(
                cacheDbURL: nil, fsBlockDbRoot: root.appendingPathComponent("blocks"),
                generalStorageURL: root.appendingPathComponent("storage"), dataDbURL: db,
                torDirURL: root.appendingPathComponent("tor"), endpoint: endpoint, network: network,
                spendParamsURL: root.appendingPathComponent("spend.params"),
                outputParamsURL: root.appendingPathComponent("output.params"),
                saplingParamsSourceURL: .default, loggingPolicy: .custom(ProofLogger()),
                isTorEnabled: false, isExchangeRateEnabled: false, ironwoodMemoPIR: origin
            )
            #else
            guard mode == "STOCK" else { throw ProofError("Expected the unmodified SDK build") }
            let initializer = Initializer(
                cacheDbURL: nil, fsBlockDbRoot: root.appendingPathComponent("blocks"),
                generalStorageURL: root.appendingPathComponent("storage"), dataDbURL: db,
                torDirURL: root.appendingPathComponent("tor"), endpoint: endpoint, network: network,
                spendParamsURL: root.appendingPathComponent("spend.params"),
                outputParamsURL: root.appendingPathComponent("output.params"),
                saplingParamsSourceURL: .default, loggingPolicy: .custom(ProofLogger()),
                isTorEnabled: false, isExchangeRateEnabled: false
            )
            #endif

            let synchronizer = SDKSynchronizer(initializer: initializer)
            wallet = synchronizer
            guard case .success = try await synchronizer.prepare(
                with: nil, walletBirthday: birthday, name: "Memo proof", keySource: nil
            ) else { throw ProofError("Fresh view-only wallet did not prepare") }
            _ = try await synchronizer.importAccount(
                ufvk: ufvk, seedFingerprint: nil, zip32AccountIndex: nil, purpose: .viewOnly,
                name: "Public fixture", keySource: nil, birthday: birthday
            )
            #if PRIVATE_MEMO
            try enqueueStatusProbe(in: db)
            #endif
            print("MemoProof begin receive")
            report.receive = try await sync(synchronizer, height: receiveTip, txid: txid)
            try write(report, to: documents.appendingPathComponent("receive.json"))
            await synchronizer.stopAndWait()

            var advance = URLRequest(url: control.appendingPathComponent("refresh"))
            advance.httpMethod = "POST"
            advance.timeoutInterval = 5
            let (_, response) = try await URLSession.shared.data(for: advance)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw ProofError("Fixture did not advance for refresh")
            }
            print("MemoProof begin refresh")
            report.refresh = try await sync(synchronizer, height: receiveTip + 1, txid: txid)
        } catch {
            report.error = String(describing: error)
        }
        await wallet?.stopAndWait()
        do { try write(report, to: reportURL) }
        catch { print("Could not write native evidence: \(error)") }
        fflush(stdout)
    }

    #if PRIVATE_MEMO
    private func enqueueStatusProbe(in database: URL) throws {
        // The backend only emits GetStatus for an ID whose mined height is unknown.
        // Queue a disposable ID to drive that real SDK path alongside the known mined memo.
        let probe = String(repeating: "5a", count: 32)
        var connection: OpaquePointer?
        defer { sqlite3_close(connection) }
        guard sqlite3_open_v2(database.path, &connection, SQLITE_OPEN_READWRITE, nil) == SQLITE_OK else {
            throw ProofError("Could not open the fixture wallet for the status probe")
        }
        let sql = "INSERT INTO tx_retrieval_queue (txid, query_type) VALUES (X'\(probe)', 0)"
        guard sqlite3_exec(connection, sql, nil, nil, nil) == SQLITE_OK else {
            throw ProofError("Could not enqueue the fixture status probe")
        }
    }
    #endif

    @MainActor
    private func sync(_ wallet: SDKSynchronizer, height: Int, txid: Data) async throws -> Snapshot {
        try await wallet.start(retry: true)
        let deadline = Date().addingTimeInterval(150)
        while Date() < deadline {
            switch wallet.latestState.syncStatus {
            case .error(let error):
                throw ProofError("SDK sync at \(height): \(error)")
            case .upToDate:
                if let scan = try await wallet.latestScannedBlock(), scan.height >= height {
                    let tx = await wallet.receivedTransactions.first(where: { $0.rawID == txid })
                    let outputs: [ZcashTransaction.Output]
                    if let tx { outputs = await wallet.getTransactionOutputs(for: tx) }
                    else { outputs = [] }
                    let memos = try await wallet.getMemos(for: txid).map { try $0.asMemoBytes().bytes }
                    return Snapshot(
                        scannedHeight: scan.height, minedHeight: tx?.minedHeight,
                        ironwoodOutputCount: outputs.filter { $0.pool == .ironwood }.count,
                        memoHashes: memos.map { SHA256.hash(data: Data($0)).map { String(format: "%02x", $0) }.joined() },
                        memoLengths: memos.map(\.count)
                    )
                }
            default:
                break
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw ProofError("SDK did not finish scanning through \(height)")
    }

    private func write(_ report: Report, to url: URL) throws {
        try JSONEncoder().encode(report).write(to: url, options: .atomic)
    }

    private static func hexData(_ hex: String) -> Data? {
        guard hex.count.isMultiple(of: 2) else { return nil }
        var result = Data()
        var cursor = hex.startIndex
        while cursor != hex.endIndex {
            let next = hex.index(cursor, offsetBy: 2)
            guard let byte = UInt8(hex[cursor..<next], radix: 16) else { return nil }
            result.append(byte)
            cursor = next
        }
        return result
    }
}

