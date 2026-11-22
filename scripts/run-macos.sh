#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."

stage="host and dependency checks"
record_blocker() {
  local status="$1"
  python3 scripts/results.py block "$stage (exit $status)"
}
trap 'record_blocker "$?"' ERR

mkdir -p artifacts
python3 scripts/results.py init
[[ "$(uname -s)" == Darwin ]]
for dependency in xcodebuild xcrun xcodegen rustup git openssl protoc; do
  command -v "$dependency"
done
xcodebuild -version | tee artifacts/xcode-version.log
xcrun simctl list -j devices available > artifacts/simulator-devices.json

stage="select and boot an installed iOS Simulator"
udid="$(python3 scripts/select_simulator.py artifacts/simulator-devices.json)"
xcrun simctl boot "$udid" 2>/dev/null || xcrun simctl list devices booted | grep -F "$udid"
xcrun simctl bootstatus "$udid" -b 2>&1 | tee artifacts/simulator-boot.log

stage="fetch the pinned sources and prepare the fixture"
mkdir .run
find_pin() {
  git init -q "$2"
  git -C "$2" -c protocol.version=2 fetch --depth 1 "$1" "$3"
  git -C "$2" checkout -q --detach FETCH_HEAD
  test "$(git -C "$2" rev-parse HEAD)" = "$3"
}
find_pin https://github.com/Zkignal/zignal.git .run/zignal 1341797872c97c21a7d9f83e1936f89d876fbcec
find_pin https://github.com/valargroup/wallet-pir.git .run/wallet-pir 09bcd8c8b79b14f3dd6599a07fc9ba72017616a3

rustup toolchain install 1.97.1 --profile minimal
if [[ "$(uname -m)" = arm64 ]]; then sim_target=aarch64-apple-ios-sim
else sim_target=x86_64-apple-ios
fi
rustup target add --toolchain 1.97.1 aarch64-apple-ios "$sim_target"
python3 -m venv .run/python
source .run/python/bin/activate
python -m pip install -r scripts/requirements.txt
mkdir -p .run/generated
python -m grpc_tools.protoc -I proto --python_out=.run/generated proto/compact_formats.proto proto/service.proto

template=.run/wallet-pir/tools/pir-e2e-tests/tests/fixtures/ironwood-fee-expiry.hex
CARGO_TARGET_DIR="$PWD/.run/fixture-target" cargo +1.97.1 run --quiet --locked \
  --manifest-path scripts/fixture-gen/Cargo.toml -- "$template" > .run/fixture.json

stage="stock, private and offline Simulator runs"
export PYTHONPATH="$PWD/.run/generated:$PWD/.run/wallet-pir/tools/enhance/scripts"
export CARGO_NET_GIT_FETCH_WITH_CLI=true
python scripts/run_proof.py "$udid"

