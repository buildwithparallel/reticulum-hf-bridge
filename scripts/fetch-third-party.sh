#!/usr/bin/env bash
# Clone and (where practical) build the radio stacks used for HL2 TX + RTL-SDR RX tests.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TP="$ROOT/third_party"
PREFIX="$TP/prefix"
mkdir -p "$TP" "$PREFIX"

clone_shallow() {
    local url="$1"
    local dest="$2"
    if [[ -d "$dest/.git" ]]; then
        echo "updating $dest"
        git -C "$dest" fetch --depth 1 origin
        git -C "$dest" reset --hard FETCH_HEAD
    else
        echo "cloning $url -> $dest"
        git clone --depth 1 "$url" "$dest"
    fi
}

clone_sparse() {
    local url="$1"
    local dest="$2"
    shift 2
    if [[ -d "$dest/.git" ]]; then
        echo "updating sparse $dest"
        git -C "$dest" fetch --depth 1 origin
        git -C "$dest" reset --hard FETCH_HEAD
    else
        echo "sparse-cloning $url -> $dest ($*)"
        git clone --depth 1 --filter=blob:none --sparse "$url" "$dest"
        git -C "$dest" sparse-checkout set "$@"
    fi
}

clone_shallow https://github.com/osmocom/rtl-sdr.git "$TP/rtl-sdr"
clone_shallow https://github.com/pyrtlsdr/pyrtlsdr.git "$TP/pyrtlsdr"
clone_shallow https://github.com/jimahlstrom/quisk.git "$TP/quisk"
clone_sparse https://github.com/softerhardware/Hermes-Lite2.git "$TP/Hermes-Lite2" software/hermeslite

echo "building librtlsdr into $PREFIX"
cmake -S "$TP/rtl-sdr" -B "$TP/rtl-sdr/build" \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$TP/rtl-sdr/build" --parallel
cmake --install "$TP/rtl-sdr/build"

echo "done. sources in $TP, tools in $PREFIX/bin"
