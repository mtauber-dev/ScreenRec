"""python -m screenrec                    -> tray app
python -m screenrec process <folder>    -> (re)process a recording
python -m screenrec record --seconds N  -> record N seconds headless, then process
Both take --output video|document|both (default: the "output" setting)."""
import argparse
import time
from datetime import datetime
from pathlib import Path

from . import config, winutil


def main():
    winutil.set_dpi_aware()
    ap = argparse.ArgumentParser(prog="screenrec")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("process")
    p.add_argument("folder")
    r = sub.add_parser("record")
    r.add_argument("--seconds", type=float, default=10)
    r.add_argument("--region", help="x,y,w,h (default: monitor under the cursor)")
    r.add_argument("--no-mic", action="store_true")
    for sp in (p, r):
        sp.add_argument("--output", choices=("video", "document", "both"))
    args = ap.parse_args()

    if args.cmd is None:
        from .tray import main as tray_main
        tray_main()
        return

    cfg = config.load()
    output = args.output or cfg.output or "video"

    def make(session):
        result = {}
        if output in ("video", "both"):
            from .process import process_session
            result = process_session(session, cfg)
        if output in ("document", "both"):
            from .document import make_guide
            result.update(make_guide(session, cfg))
        print(result)

    if args.cmd == "process":
        make(Path(args.folder))
        return

    from .recorder import Recorder
    if args.no_mic:
        cfg["mic"] = None
    region = tuple(map(int, args.region.split(","))) if args.region else winutil.monitor_at(*winutil.cursor_pos())
    session = Path(cfg.output_dir) / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    rec = Recorder(cfg, region, session)
    rec.start()
    print(f"recording {region} with {rec.encoder}, audio={rec.has_audio} for {args.seconds}s…")
    time.sleep(args.seconds)
    rec.stop()
    make(session)


if __name__ == "__main__":
    main()
