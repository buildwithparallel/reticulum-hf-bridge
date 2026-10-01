"""RTL ingress: decode an HF frame and inject LXMF to the dest hash."""

from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from hfbridge.fieldreport import ChannelStatus, FieldReporter
from hfbridge.frame import Frame
from hfbridge.listen import main as listen_main
from hfbridge.lxmfconv import via_title
from hfbridge.node import LxmfNode, start_node


def _stats_line(
    *,
    heard: int,
    forwarded: int,
    decode_failed: int,
    inject_failed: int,
    last_origin: str = "",
) -> str:
    line = (
        f"ingress-stats heard={heard} forwarded={forwarded} "
        f"decode_failed={decode_failed} inject_failed={inject_failed}"
    )
    if last_origin:
        line += f" last={last_origin}"
    return line


def _make_handlers(node: LxmfNode):
    stats = {
        "heard": 0,
        "forwarded": 0,
        "decode_failed": 0,
        "inject_failed": 0,
        "last_origin": "",
    }

    def _report() -> None:
        print(_stats_line(**stats), flush=True)

    def _inject(frame: Frame) -> None:
        stats["heard"] += 1
        stats["last_origin"] = frame.origin
        if not any(frame.dest):
            # Light / unaddressed test frames have no mailbox.
            _report()
            return
        try:
            node.send(
                frame.dest.hex(),
                frame.payload.decode("utf-8", errors="replace"),
                title=via_title(frame.origin),
            )
            stats["forwarded"] += 1
        except TimeoutError:
            stats["inject_failed"] += 1
        _report()

    def _decode_failed(_reason: str) -> None:
        stats["decode_failed"] += 1
        _report()

    _report()
    return _inject, _decode_failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--report-url",
        help="POST payload-free decode reports to this controller URL",
    )
    parser.add_argument(
        "--report-queue",
        type=Path,
        default=Path("field-reports.jsonl"),
        help="durable JSONL queue for controller reports",
    )
    parser.add_argument(
        "--report-gps-csv",
        type=Path,
        help="gpslog CSV used for the latest fix in each report",
    )
    parser.add_argument(
        "--report-token-file",
        type=Path,
        help="file containing the controller's private Pi report token",
    )
    parser.add_argument(
        "--report-timeout",
        type=float,
        default=5.0,
        help="controller POST timeout in seconds",
    )
    args, leftover = parser.parse_known_args(argv)

    node = start_node(args.config, name="ingress", display_name="hf-ingress")
    print(f"ingress {node.hash_hex}", flush=True)
    node.announce()

    listen_argv = leftover + ["--quiet"] if args.quiet else leftover
    on_frame, on_decode_failed = _make_handlers(node)
    report_token = (
        args.report_token_file.read_text().strip()
        if args.report_token_file is not None
        else ""
    )
    channel_status = ChannelStatus()
    reporter = (
        FieldReporter(
            args.report_url,
            args.report_queue,
            gps_csv=args.report_gps_csv,
            token=report_token,
            timeout=args.report_timeout,
            heartbeat_interval=2.0,
            status=channel_status,
        )
        if args.report_url
        else None
    )

    def _keep_announcing() -> None:
        while True:
            time.sleep(15)
            node.announce()

    threading.Thread(target=_keep_announcing, daemon=True).start()
    try:
        return listen_main(
            listen_argv,
            on_frame=on_frame,
            on_decode_failed=on_decode_failed,
            on_decode_event=reporter.submit if reporter is not None else None,
            channel_status=channel_status,
            gain_override=reporter.rx_gain_override if reporter is not None else None,
        )
    finally:
        if reporter is not None:
            reporter.close()


if __name__ == "__main__":
    raise SystemExit(main())
