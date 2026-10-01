"""Vanilla LXMF destination: print whatever the ingress injects."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from hfbridge.node import start_node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)

    def on_delivery(message) -> None:
        title = message.title_as_string()
        content = message.content_as_string()
        source = message.source_hash.hex() if message.source_hash else "?"
        print(
            f"DELIVERED from {source} title={title!r} content={content!r}",
            flush=True,
        )

    node = start_node(
        args.config, name="dest", display_name="hf-dest", on_delivery=on_delivery
    )
    print(f"dest {node.hash_hex}", flush=True)
    print("waiting for injected LXMF; Ctrl-C to stop", flush=True)
    try:
        while True:
            node.announce()
            time.sleep(15)
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
