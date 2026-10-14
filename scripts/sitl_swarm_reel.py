"""Turn a recorded mission into a vertical reel: an HTML page and, optionally, an MP4.

The page (1080x1920, black, one glowing map, a caption telling the story from
the log's own numbers) plays itself in any browser. ``--video`` captures it
frame by frame with a headless Chromium (Playwright) and encodes an H.264 MP4
with ffmpeg; both are presentation tools only, imported here and nowhere in
the package.

Usage:
    python scripts/sitl_swarm_reel.py docs/data/sitl/phase5/c5.jsonl --output c5-reel.html
    python scripts/sitl_swarm_reel.py lt3-run1.jsonl --video lt3-run1.mp4 [--fps 30]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from eudis_swarm.live_mission import read_jsonl
from eudis_swarm.live_reel import DEFAULT_PLAYBACK_RATE, render_reel_html

FONT_CSS_URL = (
    "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400&display=swap"
)


def inline_font_css() -> str | None:
    """Fetch Space Grotesk and return @font-face rules with the fonts inlined.

    Best effort: offline, or behind a proxy that refuses it, the page simply
    falls back to a system font.
    """

    import base64
    import re
    import urllib.request

    try:
        request = urllib.request.Request(
            FONT_CSS_URL, headers={"User-Agent": "Mozilla/5.0 Chrome/120"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            css = response.read().decode("utf-8")
        for url in sorted(set(re.findall(r"url\((https://[^)]+)\)", css))):
            with urllib.request.urlopen(url, timeout=15) as response:
                encoded = base64.b64encode(response.read()).decode("ascii")
            css = css.replace(url, f"data:font/woff2;base64,{encoded}")
    except OSError as error:
        print(f"note: font not inlined ({error}); using the linked or system font")
        return None
    return css


def _capture(page_path: Path, video: Path, fps: int, chromium: str | None) -> None:
    try:
        from playwright.sync_api import (
            sync_playwright,  # pyright: ignore[reportMissingImports]
        )
    except ModuleNotFoundError as error:
        raise SystemExit(
            "--video needs Playwright: pip install playwright (and a Chromium)"
        ) from error
    if shutil.which("ffmpeg") is None:
        raise SystemExit("--video needs ffmpeg on PATH")
    with tempfile.TemporaryDirectory() as frames_dir, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium)
        page = browser.new_page(viewport={"width": 1080, "height": 1920})
        page.goto(page_path.resolve().as_uri() + "?capture=1")
        page.wait_for_function("window.reelDuration !== undefined")
        page.evaluate("document.fonts.ready")
        duration = float(page.evaluate("window.reelDuration"))
        total = int(duration * fps) + 1
        for frame in range(total):
            page.evaluate(f"window.renderAt({frame / fps})")
            page.screenshot(path=f"{frames_dir}/f{frame:05d}.png")
            if frame % fps == 0:
                print(f"\rframe {frame}/{total}", end="", flush=True)
        browser.close()
        print()
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-framerate",
                str(fps),
                "-i",
                f"{frames_dir}/f%05d.png",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(video),
            ],
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn a recorded mission into a vertical reel (HTML, optional MP4)."
    )
    parser.add_argument("log", help="evidence log from sitl_swarm_mission.py")
    parser.add_argument("--output", help="HTML page (default: <log>-reel.html)")
    parser.add_argument("--video", help="also write an MP4 here")
    parser.add_argument("--title", default="Fail-stop")
    parser.add_argument("--eyebrow", default="Swarm coordination")
    parser.add_argument(
        "--rate",
        type=float,
        default=DEFAULT_PLAYBACK_RATE,
        help="logical seconds per reel second between story beats (default 1)",
    )
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--no-inline-font",
        action="store_true",
        help="link the web font instead of embedding it (smaller page, needs network)",
    )
    parser.add_argument("--chromium", help="path to a Chromium binary for --video")
    arguments = parser.parse_args(argv)

    log = Path(arguments.log)
    with log.open(encoding="utf-8") as stream:
        records = read_jsonl(stream)
    page = render_reel_html(
        records,
        title=arguments.title,
        eyebrow=arguments.eyebrow,
        rate=arguments.rate,
        font_css=None if arguments.no_inline_font else inline_font_css(),
    )
    output = (
        Path(arguments.output)
        if arguments.output
        else log.with_name(log.stem + "-reel.html")
    )
    output.write_text(page, encoding="utf-8")
    print(f"wrote {output}")
    if arguments.video:
        _capture(output, Path(arguments.video), arguments.fps, arguments.chromium)
        print(f"wrote {arguments.video}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
