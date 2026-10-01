"""Vanilla LXMF origin: send a message to the Hermes TX bridge."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from hfbridge.frame import check_payload
from hfbridge.lxmfconv import bridge_title, parse_dest_hash
from hfbridge.node import start_node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bridge", required=True, help="TX bridge LXMF hash, 32 hex")
    parser.add_argument("--to", required=True, help="final dest LXMF hash, 32 hex")
    parser.add_argument("--text", default="hello hf")
    parser.add_argument("--wait", type=float, default=8.0, help="seconds to announce and send")
    args = parser.parse_args(argv)
    parse_dest_hash(args.to)
    parse_dest_hash(args.bridge)
    check_payload(args.text.encode("utf-8"))

    node = start_node(args.config, name="origin", display_name="hf-origin")
    print(f"origin {node.hash_hex}", flush=True)
    node.announce()
    node.send(args.bridge, args.text, title=bridge_title(bytes.fromhex(args.to)))
    print(
        f"sent to bridge {args.bridge} for dest {args.to}: {args.text!r}",
        flush=True,
    )
    time.sleep(args.wait)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
