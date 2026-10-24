#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Rebuild the app and QEMU with this fix. Reuse only the official 3.0 PS4
# module and optional Android resources, which ci_build.sh does not produce.
# Usage: bash scripts/build_ios16_fixed.sh /absolute/path/to/Husk.tipa
set -euo pipefail
HUSK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HUSK_ROOT"
if [ "$(uname -s)" != Darwin ]; then
    echo "ERROR: macOS with Xcode and the iPhoneOS SDK is required; no TIPA was built." >&2
    exit 2
fi
for tool in xcodebuild xcrun xcodegen python3 meson ninja pkg-config qemu-img cargo rustup; do
    command -v "$tool" >/dev/null || { echo "ERROR: missing $tool" >&2; exit 2; }
done
xcodebuild -version
xcrun --sdk iphoneos --show-sdk-path
xcrun swift --version
DONOR="${1:-}"
[ -f "$DONOR" ] || { echo "ERROR: pass the official Husk 3.0 .tipa as the first argument" >&2; exit 2; }
DONOR="$(cd "$(dirname "$DONOR")" && pwd)/$(basename "$DONOR")"

python3 tests/ios-jit/test_prewarm.py
rustup target add aarch64-apple-ios

# Do not silently omit PS4, fonts or Java: the upstream clean build script
# needs the first but does not build it, and only warns for the latter two.
# The changed app executable and QEMU library are always rebuilt from source.
python3 - "$DONOR" "$HUSK_ROOT" <<'PY'
import hashlib
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import sys
import zipfile

donor, root = Path(sys.argv[1]), Path(sys.argv[2])
# Pin the official 3.0 asset recorded by GitHub's release API on 2026-10-24.
# A different donor must be reviewed explicitly, not accepted by its filename.
with donor.open('rb') as stream:
    sha = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        sha.update(chunk)
digest = sha.hexdigest()
if digest != 'b301366bb8bdef420d45f75817aba87b815dba3d55f5315c5385515f0330d0db':
    raise SystemExit('ERROR: donor does not match the pinned official Husk.tipa from release 3.0')
with zipfile.ZipFile(donor) as archive:
    infos = [n for n in archive.namelist()
             if n.startswith('Payload/') and n.count('/') == 2
             and n.endswith('.app/Info.plist')]
    if len(infos) != 1:
        raise SystemExit('ERROR: donor must contain exactly one top-level app')
    info = plistlib.loads(archive.read(infos[0]))
    if (info.get('CFBundleIdentifier') != 'com.husk.app'
            or info.get('CFBundleShortVersionString') != '3.0.0'
            or str(info.get('CFBundleVersion')) != '129'):
        raise SystemExit('ERROR: donor must be the official Husk 3.0.0 build 129')
    prefix = infos[0][:-len('Info.plist')]
    def copy_member(member, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, target.open('wb') as dest:
            shutil.copyfileobj(src, dest)
    copy_member(prefix + 'Frameworks/libhuskps4.dylib',
                root / 'build/ios-arm64/lib/libhuskps4.dylib')
    for source_dir, target_dir in [('java-runtime', 'build/java-runtime'),
                                    ('android-system', 'build/android-system')]:
        selected = prefix + source_dir + '/'
        for name in archive.namelist():
            if not name.startswith(selected) or name.endswith('/'):
                continue
            relative = PurePosixPath(name[len(selected):])
            if relative.is_absolute() or '..' in relative.parts:
                raise SystemExit('ERROR: unsafe donor resource path')
            copy_member(name, root / target_dir / str(relative))
    if not (root / 'build/java-runtime/husk-framework.dex').is_file():
        raise SystemExit('ERROR: donor is missing the Java framework')
(root / 'build').mkdir(exist_ok=True)
(root / 'build/ios16-donor-sha256.txt').write_text(digest + '  ' + donor.name + '\n')
print('Staged official 3.0 resources; donor SHA-256:', digest)
PY

mkdir -p build/logs
set +e
bash scripts/ci_build.sh "$HUSK_ROOT/build/Husk-iOS16-JIT-fixed.ipa" 2>&1 | tee build/logs/ios16-fixed-build.log
build_status=${PIPESTATUS[0]}
set -e
[ "$build_status" -eq 0 ] || exit "$build_status"

# Packaging a stale QEMU would leave the new Swift call unresolved. Check the
# exact packaged dylib and the ad-hoc signature, rather than only the build log.
VERIFY="$(mktemp -d)"
trap 'rm -rf "$VERIFY"' EXIT
unzip -q build/Husk-iOS16-JIT-fixed.tipa -d "$VERIFY"
APP="$VERIFY/Payload/Husk.app"
xcrun nm -gU "$APP/Frameworks/libqemu-aarch64-softmmu.dylib" > "$VERIFY/exports.txt"
grep -q ' _husk_ios_jit_prewarm_self$' "$VERIFY/exports.txt" || {
    echo "ERROR: packaged QEMU does not export the self-only allocator" >&2; exit 1;
}
codesign --verify --deep --strict "$APP"
shasum -a 256 build/Husk-iOS16-JIT-fixed.tipa > build/Husk-iOS16-JIT-fixed.tipa.sha256
echo "BUILT: $HUSK_ROOT/build/Husk-iOS16-JIT-fixed.tipa"
echo "Device execution and Android boot still require testing on the phone."
