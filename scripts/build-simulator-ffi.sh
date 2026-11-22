#!/usr/bin/env bash
# Build the single simulator slice exercised here. The module, framework metadata
# and Swift package declaration come from the pinned SDK.
set -euo pipefail
cd "${1:?SDK path}"
[[ "$(uname -s)" == Darwin ]]
case "$(uname -m)" in
  arm64) arch=arm64; target=aarch64-apple-ios-sim ;;
  x86_64) arch=x86_64; target=x86_64-apple-ios ;;
  *) echo "Unsupported simulator host architecture" >&2; exit 2 ;;
esac
export IPHONEOS_DEPLOYMENT_TARGET=17.0
export CARGO_ENCODED_RUSTFLAGS
CARGO_ENCODED_RUSTFLAGS="$(printf '%s\037%s\037%s' "--remap-path-prefix=$HOME=/build" \
    "--remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo" "--remap-path-prefix=$PWD=/sdk")"
export CFLAGS="-ffile-prefix-map=$HOME=/build"
header=target/Headers/zcashlc.h
rm -f "$header"
touch rust/build.rs
cargo build --lib --locked --release --target "$target"
test -s "$header"
xcrun --sdk iphonesimulator clang -target "${arch}-apple-ios17.0-simulator" -isysroot "$(xcrun --sdk iphonesimulator --show-sdk-path)" -fsyntax-only -x c "$header"

slice="ios-$arch-simulator"
xcf=LocalPackages/libzcashlc.xcframework
framework="$xcf/$slice/libzcashlc.framework"
mkdir -p "$framework/Headers" "$framework/Modules"
cp "target/$target/release/libzcashlc.a" "$framework/libzcashlc"
cp "$header" "$framework/Headers/"
cp BuildSupport/module.modulemap "$framework/Modules/"
cp BuildSupport/platform-Info.plist "$framework/Info.plist"
cp BuildSupport/LocalPackages-Package.swift LocalPackages/Package.swift
python3 - "$xcf" "$slice" "$arch" <<'PY'
import plistlib
import sys
from pathlib import Path

output, identifier, arch = sys.argv[1:]
framework_info = Path(output) / identifier / "libzcashlc.framework" / "Info.plist"
with framework_info.open("rb") as f:
    info = plistlib.load(f)
info.update(CFBundlePackageType="FMWK", CFBundleSupportedPlatforms=["iPhoneSimulator"],
            MinimumOSVersion="17.0")
with framework_info.open("wb") as f:
    plistlib.dump(info, f)
library = {
    "LibraryIdentifier": identifier,
    "LibraryPath": "libzcashlc.framework",
    "SupportedArchitectures": [arch],
    "SupportedPlatform": "ios",
    "SupportedPlatformVariant": "simulator",
}
with (Path(output) / "Info.plist").open("wb") as f:
    plistlib.dump({"AvailableLibraries": [library], "CFBundlePackageType": "XFWK",
                   "XCFrameworkFormatVersion": "1.0"}, f)
PY
plutil -lint "$xcf/Info.plist"
xcrun lipo -verify_arch "$arch" "$framework/libzcashlc"
shasum -a 256 "$framework/libzcashlc" "$header"
