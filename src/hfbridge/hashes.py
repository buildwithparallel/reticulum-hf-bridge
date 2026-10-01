"""Print LXMF delivery hashes from isolated instance identity files."""

from __future__ import annotations

import argparse
from pathlib import Path

import RNS

ROOT = Path("rns-instances")
ROLES = ("origin", "txbridge", "ingress", "dest")


def lxmf_hash(identity_path: Path) -> str:
    identity = RNS.Identity.from_file(str(identity_path))
    if identity is None:
        raise FileNotFoundError(identity_path)
    return RNS.Destination.hash(identity, "lxmf", "delivery").hex()


def collect(root: Path) -> dict[str, str]:
    hashes = {}
    for role in ROLES:
        path = root / role / "identity"
        if path.exists():
            hashes[role] = lxmf_hash(path)
    return hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    hashes = collect(args.root)
    if not hashes:
        print(f"no identities under {args.root}; run python -m hfbridge.rnssetup first")
        return 1
    for role in ROLES:
        if role in hashes:
            print(f"{role:8} {hashes[role]}")
    if "txbridge" in hashes and "dest" in hashes:
        print()
        print("Crosstalk origin: TCP client 127.0.0.1:3742")
        print(f"  compose to  {hashes['txbridge']}")
        print(f"  HF dest     {hashes['dest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
