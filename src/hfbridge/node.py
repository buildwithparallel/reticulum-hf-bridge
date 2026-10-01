"""One isolated Reticulum + LXMF instance. No radio code."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import LXMF
import RNS


@dataclass
class LxmfNode:
    reticulum: RNS.Reticulum
    identity: RNS.Identity
    router: LXMF.LXMRouter
    destination: RNS.Destination
    name: str

    @property
    def hash_hex(self) -> str:
        return self.destination.hash.hex()

    def announce(self) -> None:
        self.destination.announce()

    def wait_for_identity(self, dest_hex: str, timeout: float = 30.0) -> RNS.Identity:
        dest_hash = bytes.fromhex(dest_hex)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            identity = RNS.Identity.recall(dest_hash)
            if identity is not None:
                return identity
            RNS.Transport.request_path(dest_hash)
            time.sleep(0.4)
        raise TimeoutError(f"no announce for {dest_hex} after {timeout:.0f}s")

    def send(self, dest_hex: str, content: str, *, title: str = "") -> None:
        identity = self.wait_for_identity(dest_hex)
        dest = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        message = LXMF.LXMessage(
            dest,
            self.destination,
            content,
            title=title,
            desired_method=LXMF.LXMessage.DIRECT,
        )
        self.router.handle_outbound(message)


def start_node(
    config_dir: Path,
    *,
    name: str,
    display_name: str,
    on_delivery=None,
) -> LxmfNode:
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    identity_path = config_dir / "identity"
    if identity_path.exists():
        identity = RNS.Identity.from_file(str(identity_path))
    else:
        identity = RNS.Identity()
        identity.to_file(str(identity_path))

    reticulum = RNS.Reticulum(str(config_dir))
    router = LXMF.LXMRouter(storagepath=str(config_dir))
    destination = router.register_delivery_identity(identity, display_name=display_name)
    if destination is None:
        raise RuntimeError("LXMF router refused a second delivery identity")
    if on_delivery is not None:
        router.register_delivery_callback(on_delivery)
    return LxmfNode(
        reticulum=reticulum,
        identity=identity,
        router=router,
        destination=destination,
        name=name,
    )
