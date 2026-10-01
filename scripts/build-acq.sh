#!/usr/bin/env bash
# Build the optional Rust acquisition extension into src/hfbridge/.
# Requires rustc/cargo (https://rustup.rs). Python still runs without it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi
export PYO3_PYTHON="$PY"

cd "$ROOT/native/hfbridge-acq"
export CARGO_TARGET_DIR="$ROOT/native/hfbridge-acq/target"
cargo build --release

EXT="$("$PY" -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX') or '.so')")"
DEST="$ROOT/src/hfbridge/_acq$EXT"
if [[ -f target/release/lib_acq.dylib ]]; then
  SRC=target/release/lib_acq.dylib
elif [[ -f target/release/lib_acq.so ]]; then
  SRC=target/release/lib_acq.so
else
  echo "built library not found under native/hfbridge-acq/target/release" >&2
  exit 1
fi
cp "$SRC" "$DEST"
if command -v install_name_tool >/dev/null 2>&1; then
  install_name_tool -id "$DEST" "$DEST" 2>/dev/null || true
fi
# Copy / install_name_tool break the linker-signed hash. Unsigned pages
# make macOS SIGKILL Python on dlopen (CODESIGNING / Invalid Page).
if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - --timestamp=none "$DEST"
fi
echo "installed $DEST"
