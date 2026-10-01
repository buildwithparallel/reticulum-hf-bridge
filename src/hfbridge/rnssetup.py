"""Write four isolated Reticulum configs that cannot see each other."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path("rns-instances")

# Two TCP islands on localhost. Origin talks only to the Hermes bridge.
# Dest talks only to the RTL ingress. Nothing crosses except HF.
PAIRS = {
    "origin": ("hf-origin", 37501, 37502, "client", 3742),
    "txbridge": ("hf-txbridge", 37503, 37504, "server", 3742),
    "ingress": ("hf-ingress", 37505, 37506, "server", 3743),
    "dest": ("hf-dest", 37507, 37508, "client", 3743),
}


def _config(name: str, rpc: int, control: int, role: str, tcp_port: int) -> str:
    if role == "server":
        iface = f"""
  [[Island]]
    type = TCPServerInterface
    interface_enabled = True
    listen_ip = 127.0.0.1
    listen_port = {tcp_port}
"""
    else:
        iface = f"""
  [[Island]]
    type = TCPClientInterface
    interface_enabled = True
    target_host = 127.0.0.1
    target_port = {tcp_port}
"""
    return f"""[reticulum]
  enable_transport = Yes
  share_instance = No
  instance_name = {name}
  shared_instance_port = {rpc}
  instance_control_port = {control}

[logging]
  loglevel = 3

[interfaces]
{iface}
"""


def write_configs(root: Path) -> list[Path]:
    written = []
    root.mkdir(parents=True, exist_ok=True)
    for folder, (name, rpc, control, role, port) in PAIRS.items():
        path = root / folder
        path.mkdir(parents=True, exist_ok=True)
        config = path / "config"
        config.write_text(_config(name, rpc, control, role, port))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    paths = write_configs(args.root)
    print("Isolated Reticulum configs:")
    for path in paths:
        print(f"  {path}")
    print(
        "\nTwo islands: origin↔txbridge on 127.0.0.1:3742, "
        "ingress↔dest on 127.0.0.1:3743.\n"
        "Start dest, then ingress, then txbridge, then origin."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
