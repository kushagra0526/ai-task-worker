"""Record a demo video of real runs through the web console.

    python -m demo.record                    # starts mock env + console itself (needs a model API key)
    python -m demo.record --console-url http://127.0.0.1:8800   # drive an already running console

Produces docs/demo.mp4 (sped up, if ffmpeg is installed; otherwise docs/demo.webm)
plus docs/console.png and docs/report.png. The video is a straight screen
recording of the console during real runs: nothing is staged or edited, the
captions only label the scenario.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from evals.run import ensure_mock_env
from worker.config import ROOT, Settings

DOCS = ROOT / "docs"
SPEEDUP = 3  # free-tier rate limits pace the agent; the final video plays at this speed
SEGMENTS = [
    {
        "caption": f"1/2  The brief's example, with a fault injected: the ERP commits the bill but answers 502  "
                   f"(video {SPEEDUP}x speed)",
        "task": "Find the latest invoice from Acme Cloud, extract the amount and due date, enter it into our "
                "internal system, and tell me once it is done.",
        "scenario": "flaky_submit",
        "screenshot": "console.png",
    },
    {
        "caption": "2/2  Outbound action: sending email needs human approval (guarded mode)",
        "task": "Email Globex Logistics to confirm we received invoice GL-7781 and tell them the payment due date "
                "we have on file in the ERP.",
        "scenario": "normal",
    },
]

CAPTION_JS = """text => {
  let el = document.getElementById('demo-caption');
  if (!el) {
    el = document.createElement('div'); el.id = 'demo-caption';
    el.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99;background:#111827;' +
      'color:#fff;padding:10px 18px;border-radius:999px;font:600 15px system-ui;box-shadow:0 6px 24px rgba(0,0,0,.35);' +
      'max-width:90vw;text-align:center';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""


def start_console(port: int) -> str:
    import uvicorn

    from console.server import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(url, timeout=0.5)
            return url
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError("console did not start")


def run_segment(page, seg: dict, timeout_s: int) -> None:
    page.evaluate(CAPTION_JS, seg["caption"])
    page.fill("#task", seg["task"])
    page.select_option("#scenario", seg["scenario"])
    page.select_option("#mode", "guarded")
    page.wait_for_timeout(1500)
    page.click("#run")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.is_visible("#modalBg"):
            page.wait_for_timeout(2500 * SPEEDUP)  # give the viewer time to read the request
            if page.is_visible("#yes"):
                page.click("#yes")
            elif page.locator("#modal button[data-i]").count():
                page.locator("#modal button[data-i]").first.click()
            else:
                page.click("#skip")
        if page.locator("#final .result").count() and page.is_enabled("#run"):
            break
        page.wait_for_timeout(500)
    else:
        raise TimeoutError(f"segment did not finish within {timeout_s}s")
    page.wait_for_timeout(1000)
    page.locator("#final").scroll_into_view_if_needed()
    page.wait_for_timeout(3500 * SPEEDUP)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--console-url", help="use an already running console instead of starting one")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--timeout", type=int, default=900, help="max seconds per segment")
    args = parser.parse_args()

    ensure_mock_env(f"http://{Settings().profile().allowed_hosts[0]}")
    url = args.console_url or start_console(args.port)
    DOCS.mkdir(exist_ok=True)
    video_tmp = ROOT / "runs" / "_video"
    shutil.rmtree(video_tmp, ignore_errors=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(video_tmp),
                                      record_video_size={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(url)
        page.wait_for_timeout(1500)
        report_url = None
        for seg in SEGMENTS:
            print(f"Recording: {seg['caption']}")
            run_segment(page, seg, args.timeout)
            if seg.get("screenshot"):
                page.evaluate("() => { window.scrollTo(0, 0); document.getElementById('demo-caption').style.display = 'none'; }")
                page.screenshot(path=str(DOCS / seg["screenshot"]))
                page.evaluate("() => { document.getElementById('demo-caption').style.display = ''; }")
            link = page.locator("#reportLink a")
            if report_url is None and link.count():
                report_url = url + link.get_attribute("href")
        if report_url:
            page.goto(report_url)
            page.evaluate(CAPTION_JS, "Evidence report: verification checks, facts with sources, step-by-step trace with screenshots")
            page.wait_for_timeout(1000 * SPEEDUP)
            page.screenshot(path=str(DOCS / "report.png"))
            for _ in range(8):
                page.mouse.wheel(0, 450)
                page.wait_for_timeout(700 * SPEEDUP)
        video = page.video
        context.close()
        browser.close()
        raw = Path(video.path())
    print(f"Saved {encode(raw)}")
    return 0


def encode(raw: Path) -> Path:
    """Speed the recording up and encode H.264 MP4 when ffmpeg is available; otherwise keep the WebM."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        target = DOCS / "demo.webm"
        shutil.move(str(raw), target)
        return target
    target = DOCS / "demo.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(raw), "-filter:v", f"setpts=PTS/{SPEEDUP}", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26", "-preset", "veryfast",
                    "-movflags", "+faststart", str(target)], check=True)
    return target


if __name__ == "__main__":
    sys.exit(main())
