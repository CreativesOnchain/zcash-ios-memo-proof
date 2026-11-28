"""Keep a valid BLOCKED result even when infrastructure fails before native code starts."""
import json
import sys
from pathlib import Path

path = Path("artifacts/results.json")
mode = sys.argv[1]
if mode == "init":
    result = {
        "status": "BLOCKED",
        "blocker": "No native Simulator run completed",
        "pins": {
            "zignal": "1341797872c97c21a7d9f83e1936f89d876fbcec",
            "wallet-pir": "09bcd8c8b79b14f3dd6599a07fc9ba72017616a3",
        },
        "runs": {
            name: {
                "status": "BLOCKED", "blocker": "Not executed", "expected_memo_sha256": None,
                "receive": None, "refresh": None, "private_query_attempts": None, "ordinary_txid_lookups": None,
            }
            for name in ("STOCK", "PRIVATE", "PIR OFFLINE")
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        sys.exit("A verdict already exists. Use a fresh checkout for another run.")
    try:
        (path.parent / "requests.jsonl").touch(exist_ok=False)
    except FileExistsError:
        sys.exit("A request log already exists. Use a fresh checkout for another run.")
elif mode == "block":
    result = json.loads(path.read_text())
    if result["status"] != "BLOCKED":
        sys.exit(0)
    if result["blocker"] == "No native Simulator run completed":
        result["blocker"] = sys.argv[2]
    for run in result["runs"].values():
        if run["blocker"] == "Not executed":
            run["blocker"] = result["blocker"]
else:
    sys.exit("Expected init or block")
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
temporary.replace(path)

