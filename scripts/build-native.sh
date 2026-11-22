#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:?stock or private}"
simulator="${2:?Simulator UDID}"
root="$PWD"
sdk=".run/zignal/vendor/zcash-swift-wallet-sdk"
xcargs=(-configuration Debug)
if [[ "$mode" == private ]]; then
  git -C .run/zignal apply --check "$root/patches/zignal-sdk.diff"
  git -C .run/zignal apply "$root/patches/zignal-sdk.diff"
  cp "$sdk/Cargo.lock" artifacts/private-Cargo.lock
  xcargs+=('SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) PRIVATE_MEMO')
elif [[ "$mode" != stock ]]; then
  echo "Unknown build mode '$mode'" >&2
  exit 2
fi

export RUSTUP_TOOLCHAIN=1.97.1
export CARGO_PROFILE_RELEASE_LTO=false
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-4}"
export LIBCLANG_PATH="$(xcode-select -p)/Toolchains/XcodeDefault.xctoolchain/usr/lib"
bash scripts/build-simulator-ffi.sh "$sdk" 2>&1 | tee "artifacts/$mode-ffi.log"

(cd native && xcodegen generate --spec project.yml)
lockdir=native/MemoProof.xcodeproj/project.xcworkspace/xcshareddata/swiftpm
mkdir -p "$lockdir"
cp .run/zignal/ios/Zignal/Zignal.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved "$lockdir/"
xcodebuild -resolvePackageDependencies -project native/MemoProof.xcodeproj -scheme MemoProof \
  -clonedSourcePackagesDirPath .run/swift-packages 2>&1 | tee "artifacts/$mode-swift-packages.log"
cp "$lockdir/Package.resolved" "artifacts/$mode-Package.resolved"
xcodebuild build -project native/MemoProof.xcodeproj -scheme MemoProof \
  -destination "id=$simulator" -derivedDataPath ".run/$mode" \
  -clonedSourcePackagesDirPath .run/swift-packages -disableAutomaticPackageResolution \
  -resultBundlePath "artifacts/$mode.xcresult" "${xcargs[@]}" 2>&1 | tee "artifacts/$mode-xcodebuild.log"

app=".run/$mode/Build/Products/Debug-iphonesimulator/MemoProof.app"
if ! codesign --verify "$app" 2>/dev/null; then
  codesign --force --sign - --timestamp=none "$app"
fi

