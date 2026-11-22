#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:?stock or private}"
simulator="${2:?Simulator UDID}"
root="$PWD"
sdk=".run/zignal/vendor/zcash-swift-wallet-sdk"
xcargs=()
if [[ "$mode" == private ]]; then
  git -C .run/zignal apply --check "$root/patches/zignal-sdk.diff"
  git -C .run/zignal apply "$root/patches/zignal-sdk.diff"
  cp "$sdk/Cargo.lock" artifacts/private-Cargo.lock
  xcargs+=(SWIFT_ACTIVE_COMPILATION_CONDITIONS=PRIVATE_MEMO)
elif [[ "$mode" != stock ]]; then
  echo "Unknown build mode '$mode'" >&2
  exit 2
fi

# The upstream script builds the current Mac's Simulator slice and a device slice.
# Disabling whole-program LTO keeps the cold proof build within the runner budget.
export RUSTUP_TOOLCHAIN=1.97.1
export CARGO_PROFILE_RELEASE_LTO=false
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}"
export LIBCLANG_PATH="$(xcode-select -p)/Toolchains/XcodeDefault.xctoolchain/usr/lib"
bash "$sdk/Scripts/build-ios-ffi.sh" 2>&1 | tee "artifacts/$mode-ffi.log"

xcodegen generate --spec native/project.yml --project native
lockdir=native/MemoProof.xcodeproj/project.xcworkspace/xcshareddata/swiftpm
mkdir -p "$lockdir"
cp .run/zignal/ios/Zignal/Zignal.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved "$lockdir/"
xcodebuild -resolvePackageDependencies -project native/MemoProof.xcodeproj -scheme MemoProof \
  -clonedSourcePackagesDirPath .run/swift-packages 2>&1 | tee "artifacts/$mode-swift-packages.log"
xcodebuild build -project native/MemoProof.xcodeproj -scheme MemoProof \
  -configuration Debug -destination "id=$simulator" -derivedDataPath ".run/$mode" \
  -clonedSourcePackagesDirPath .run/swift-packages -disableAutomaticPackageResolution \
  -resultBundlePath "artifacts/$mode.xcresult" "${xcargs[@]}" 2>&1 | tee "artifacts/$mode-xcodebuild.log"

app=".run/$mode/Build/Products/Debug-iphonesimulator/MemoProof.app"
if ! codesign --verify "$app" 2>/dev/null; then
  codesign --force --sign - --timestamp=none "$app"
fi

