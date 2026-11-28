"""Select an available iPhone that meets MemoProof's iOS 17 deployment target."""
import json
import sys
from pathlib import Path

devices = json.loads(Path(sys.argv[1]).read_text())["devices"]
runtimes = [key for key in devices if key.startswith("com.apple.CoreSimulator.SimRuntime.iOS-")]
runtimes.sort(key=lambda key: tuple(map(int, key.split(".iOS-")[1].split("-"))), reverse=True)
for runtime in runtimes:
    if int(runtime.split(".iOS-")[1].split("-")[0]) < 17:
        continue
    phones = [device for device in devices[runtime]
              if device["isAvailable"] and device["name"].startswith("iPhone")]
    if phones:
        print(next((d for d in phones if d["state"] == "Booted"), phones[0])["udid"])
        break
else:
    sys.exit("No available iPhone Simulator running iOS 17 or later")

