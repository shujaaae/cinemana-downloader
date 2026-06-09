"""Headless CLI — handy for testing and for users who prefer the terminal.

    python -m cinemana <url-or-id> [--quality 1080] [--dest PATH] [--list]

The GUI (``python app.py``) is the primary interface; this shares the same
service/engine, so resume and manifest behaviour are identical.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .model import Episode
from .service import DownloadService, Events, height_label


def _ep_label(ep: Episode) -> str:
    return "فيلم" if ep.is_movie else f"S{ep.season:02d}E{ep.episode:02d}"


class _Reporter:
    """Stateful console reporter that knows the current episode's label."""

    def __init__(self, labels: dict[str, str]):
        self.labels = labels
        self.current = ""

    def events(self) -> Events:
        return Events(
            on_log=self._log,
            on_status=self._status,
            on_progress=self._progress,
        )

    def _log(self, msg: str):
        print(msg)

    def _status(self, nb: str, status: str, extra: dict):
        self.current = self.labels.get(nb, nb)
        if status == "done":
            print()  # finish the progress line

    def _progress(self, nb: str, done: int, total):
        label = self.labels.get(nb, nb)
        if total:
            pct = min(100, int(done * 100 / total))
            bar = "#" * (pct // 4)
            sys.stdout.write(f"\r  {label}: [{bar:<25}] {pct:3d}%  {done/1048576:.1f}/{total/1048576:.1f} MB ")
        else:
            sys.stdout.write(f"\r  {label}: {done/1048576:.1f} MB ")
        sys.stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cinemana", description="محمّل سينمانا")
    parser.add_argument("input", help="رابط المسلسل أو معرّف الفيديو")
    parser.add_argument("--quality", "-q", type=int, default=1080, help="ارتفاع الجودة (افتراضي 1080)")
    parser.add_argument("--dest", "-d", default=".", help="مجلد الحفظ")
    parser.add_argument("--list", "-l", action="store_true", help="عرض الحلقات فقط دون تحميل")
    parser.add_argument("--segments", "-s", type=int, default=4,
                        help="عدد الاتصالات المتوازية لكل ملف (افتراضي 4)")
    parser.add_argument("--concurrency", "-c", type=int, default=1,
                        help="عدد الحلقات المتزامنة (افتراضي 1 لإبقاء عرض التقدّم واضحاً)")
    args = parser.parse_args(argv)

    dest = Path(args.dest).resolve()

    print(f"جاري جلب: {args.input}")
    plan = DownloadService(dest).prepare(args.input)
    kind = "فيلم" if plan.is_movie else f"{len(plan.episodes)} حلقة"
    print(f"\n{plan.title} — {kind}")
    print(f"الجودات المتاحة: {', '.join(height_label(h) for h in plan.available_heights) or 'غير معروف'}\n")
    for ep in plan.episodes:
        print(f"  {_ep_label(ep)}  {ep.title}")

    if args.list:
        return 0

    target = args.quality if args.quality in plan.available_heights else plan.default_height
    print(f"\nبدء التحميل بجودة {height_label(target)} إلى {dest}\n")

    reporter = _Reporter({ep.nb: _ep_label(ep) for ep in plan.episodes})
    service = DownloadService(dest, events=reporter.events())
    summary = service.run(plan, target, concurrency=args.concurrency, segments=args.segments)
    print(f"\nانتهى. مكتمل {summary.get('done', 0)}/{summary.get('total', 0)}، "
          f"أخطاء {summary.get('error', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
