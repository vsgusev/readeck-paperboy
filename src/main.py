#!/usr/bin/env python3
"""readeck-paperboy: send tagged Readeck bookmarks to Kindle as proper EPUBs.

Polls a Readeck instance for bookmarks tagged 'kindle' or 'kindle-{name}',
patches each EPUB's metadata so Kindle displays a proper title, and sends
them via SMTP. State is tracked via Readeck labels (sent-to-{name}) so the
process is idempotent across restarts and survives partial failures.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import smtplib
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from typing import Optional

import requests
import urllib3
from PIL import Image, ImageDraw, ImageFont

VERSION = "0.1.0"


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    from_addr: str


@dataclass
class Config:
    readeck_url: str
    readeck_token: str
    destinations: dict[str, str]
    default_destination: str
    smtp: SmtpConfig
    poll_interval_seconds: int
    verify_ssl: bool
    healthcheck_port: int
    log_level: str


def env(key: str, default: Optional[str] = None) -> str:
    """Read env var; fall back to default; raise if required and empty.

    An empty value (e.g. SMTP_PASS=) is treated like unset, so required vars
    fail loudly at startup instead of surfacing as a cryptic 401 later.
    """
    value = os.environ.get(key) or default
    if not value:
        raise ValueError(f"Required environment variable {key} is not set")
    return value


def parse_destinations(raw: str) -> dict[str, str]:
    """Parse 'vlad:vlad_xxx@kindle.com,anya:anya_yyy@kindle.com'."""
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"Bad DESTINATIONS entry: {pair!r}, expected 'name:email'"
            )
        name, email = pair.split(":", 1)
        name = name.strip()
        email = email.strip()
        if not name or not email:
            raise ValueError(f"Empty name or email in destination: {pair!r}")
        if not re.match(r"^[a-z0-9_-]+$", name):
            raise ValueError(
                f"Destination name must be lowercase letters/digits/_/-: {name!r}"
            )
        result[name] = email
    if not result:
        raise ValueError("DESTINATIONS is empty")
    return result


def parse_duration(raw: str) -> int:
    """Parse duration like '1h', '30m', '24h', '90s' to seconds."""
    raw = raw.strip().lower()
    if not raw:
        raise ValueError("Empty duration")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    unit = multipliers.get(raw[-1], 1)
    number = raw[:-1] if raw[-1] in multipliers else raw
    try:
        return int(number) * unit
    except ValueError:
        raise ValueError(f"Bad duration format: {raw!r}")


def load_config() -> Config:
    smtp_user = env("SMTP_USER")
    smtp = SmtpConfig(
        host=env("SMTP_HOST"),
        port=int(env("SMTP_PORT", "465")),
        user=smtp_user,
        password=env("SMTP_PASS"),
        from_addr=env("SMTP_FROM", smtp_user),
    )
    destinations = parse_destinations(env("DESTINATIONS"))
    default_dest = env("DEFAULT_DESTINATION")
    if default_dest not in destinations:
        raise ValueError(
            f"DEFAULT_DESTINATION={default_dest!r} not found in DESTINATIONS"
        )
    return Config(
        readeck_url=env("READECK_URL").rstrip("/"),
        readeck_token=env("READECK_TOKEN"),
        destinations=destinations,
        default_destination=default_dest,
        smtp=smtp,
        poll_interval_seconds=parse_duration(env("POLL_INTERVAL", "1h")),
        verify_ssl=env("VERIFY_SSL", "true").lower() != "false",
        healthcheck_port=int(env("HEALTHCHECK_PORT", "8080")),
        log_level=env("LOG_LEVEL", "INFO").upper(),
    )


# ============================================================================
# Healthcheck HTTP endpoint
# ============================================================================

_STATE = {
    "last_successful_cycle": None,
    "started_at": datetime.now(timezone.utc),
}


def make_healthcheck_handler(poll_interval_seconds: int):
    """Build an HTTP handler with the configured staleness threshold."""
    stale_threshold = poll_interval_seconds * 2
    startup_grace = poll_interval_seconds

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return

            last = _STATE["last_successful_cycle"]
            now = datetime.now(timezone.utc)
            if last is None:
                healthy = (now - _STATE["started_at"]).total_seconds() <= startup_grace
            else:
                healthy = (now - last).total_seconds() <= stale_threshold

            body = {
                "status": "ok" if healthy else "stale",
                "version": VERSION,
                "started_at": _STATE["started_at"].isoformat(),
                "last_successful_cycle": last.isoformat() if last else None,
            }
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, fmt, *args):
            pass

    return HealthHandler


def start_healthcheck_server(port: int, poll_interval_seconds: int) -> None:
    handler = make_healthcheck_handler(poll_interval_seconds)
    server = HTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="healthcheck"
    )
    thread.start()
    logging.getLogger("paperboy").info(
        "healthcheck listening on :%d/healthz", port
    )


# ============================================================================
# Readeck API client
# ============================================================================

class ReadeckClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.verify = verify_ssl

    def list_bookmarks(self, label: str, limit: int = 50) -> list[dict]:
        """List non-archived bookmarks with the given label."""
        r = self.session.get(
            f"{self.base_url}/api/bookmarks",
            params={"labels": label, "is_archived": "false", "limit": limit},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected API response shape: {type(data).__name__}"
            )
        return data

    def fetch_article_epub(self, bookmark_id: str) -> bytes:
        """Download the article as a single EPUB."""
        r = self.session.get(
            f"{self.base_url}/api/bookmarks/{bookmark_id}/article.epub",
            timeout=120,
        )
        r.raise_for_status()
        return r.content

    def update_labels(self, bookmark_id: str, labels: list[str]) -> None:
        """Replace the bookmark's full label set."""
        r = self.session.patch(
            f"{self.base_url}/api/bookmarks/{bookmark_id}",
            json={"labels": labels},
            timeout=15,
        )
        r.raise_for_status()


# ============================================================================
# Cover rendering + EPUB metadata patching
# ============================================================================

# Paths from the fonts-dejavu-core apt package (see Dockerfile); DejaVu has Cyrillic.
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# DejaVu has no emoji glyphs; strip them so they don't render as tofu boxes.
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff\U0000fe00-\U0000fe0f\U0000200d]+"
)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", text)).strip()


def _load_font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if not line or draw.textlength(trial, font=font) <= max_width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def render_title_cover(title: str, source: str) -> Optional[bytes]:
    """Render a text-only cover (title + source) as a book-shaped JPEG.

    Kindle's grid view shows only the cover image for personal documents, so a
    text card keeps the article identifiable. Returns None when no font is
    found, in which case the caller just sends without a cover.
    """
    title_font = _load_font(_FONT_BOLD, 84) or _load_font(_FONT_REGULAR, 84)
    if title_font is None:
        return None
    source_font = _load_font(_FONT_REGULAR, 44)

    width, height, margin = 1200, 1600, 96
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    lines = _wrap(draw, _clean_text(title) or "Untitled", title_font, width - 2 * margin)
    max_lines = 9
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] += "…"

    title_line_h = int(sum(title_font.getmetrics()) * 1.15)
    source = _clean_text(source)
    has_source = bool(source) and source_font is not None
    gap = source_h = 0
    if has_source:
        source_h = sum(source_font.getmetrics())
        gap = int(title_line_h * 0.6)

    # Centre vertically so Kindle's corner chrome (ribbon, select tick, ⋯) doesn't clip it.
    block_h = len(lines) * title_line_h + gap + source_h
    y = max(margin, (height - block_h) // 2)
    for line in lines:
        draw.text((margin, y), line, font=title_font, fill=(0, 0, 0))
        y += title_line_h
    if has_source:
        y += gap
        draw.text((margin, y), source, font=source_font, fill=(110, 110, 110))

    out = BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _inject_cover_refs(opf_text: str, href: str) -> str:
    """Add a manifest item for the cover image and declare it as the cover."""
    item = (
        f'<item id="paperboy-cover" href="{href}" '
        f'media-type="image/jpeg" properties="cover-image"/>'
    )
    text = opf_text.replace("</manifest>", item + "</manifest>", 1)
    # <meta name="cover"> is the EPUB2 hint Amazon's converter actually reads.
    if 'name="cover"' not in text:
        text = text.replace(
            "</metadata>",
            '<meta name="cover" content="paperboy-cover"/></metadata>',
            1,
        )
    return text


def patch_epub_metadata(
    epub_bytes: bytes,
    title: str,
    author: str,
    cover_jpeg: Optional[bytes] = None,
) -> bytes:
    """Rewrite dc:title/dc:creator in the OPF, and optionally inject a cover.

    `cover_jpeg` is a JPEG image. When given, it's added next to the OPF and
    declared as the EPUB cover, so Kindle shows it in the library. If the OPF
    can't be located the cover is silently skipped.
    """
    buf_in = BytesIO(epub_bytes)
    buf_out = BytesIO()
    title_esc = escape(title)
    author_esc = escape(author)

    with zipfile.ZipFile(buf_in, "r") as zin, \
         zipfile.ZipFile(buf_out, "w") as zout:
        opf_name = next((n for n in zin.namelist() if n.endswith(".opf")), None)

        cover_href = cover_zip_path = None
        if cover_jpeg and opf_name:
            cover_href = "paperboy-cover.jpg"
            cover_zip_path = posixpath.join(
                posixpath.dirname(opf_name), cover_href
            )

        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == opf_name:
                text = data.decode("utf-8")
                text = re.sub(
                    r"<dc:title[^>]*>.*?</dc:title>",
                    f"<dc:title>{title_esc}</dc:title>",
                    text, count=1, flags=re.DOTALL,
                )
                text = re.sub(
                    r"<dc:creator[^>]*>.*?</dc:creator>",
                    f"<dc:creator>{author_esc}</dc:creator>",
                    text, count=1, flags=re.DOTALL,
                )
                if cover_href:
                    text = _inject_cover_refs(text, cover_href)
                data = text.encode("utf-8")
            # Reuse the original ZipInfo to preserve per-entry compression: the
            # OCF spec needs 'mimetype' first and STORED. infolist() keeps order.
            zout.writestr(item, data)

        if cover_zip_path:
            zout.writestr(cover_zip_path, cover_jpeg)
    return buf_out.getvalue()


# ============================================================================
# Email
# ============================================================================

_FILENAME_BAD_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str) -> str:
    """Make string safe for use as a filename."""
    s = _FILENAME_BAD_CHARS.sub("-", name)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120] or "article"


def send_email(smtp: SmtpConfig, to: str, title: str, epub_bytes: bytes) -> None:
    """Send EPUB attachment to a single recipient.

    Port 465 uses implicit TLS (SMTPS); any other port (e.g. 587) connects
    plain and upgrades via STARTTLS.
    """
    msg = EmailMessage()
    msg["Subject"] = title[:200]
    msg["From"] = smtp.from_addr
    msg["To"] = to
    msg.set_content(title)
    msg.add_attachment(
        epub_bytes,
        maintype="application", subtype="epub+zip",
        filename=sanitize_filename(title) + ".epub",
    )
    if smtp.port == 465:
        with smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=60) as s:
            s.login(smtp.user, smtp.password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(smtp.host, smtp.port, timeout=60) as s:
            s.starttls()
            s.login(smtp.user, smtp.password)
            s.send_message(msg)


# ============================================================================
# Per-bookmark and per-destination processing
# ============================================================================

def make_author(bm: dict) -> str:
    """Build a creator string from bookmark metadata."""
    authors = bm.get("authors")
    if isinstance(authors, list) and authors:
        return ", ".join(str(a) for a in authors)
    site = bm.get("site_name") or bm.get("site")
    if site:
        return str(site)
    return "Readeck"


def compute_new_labels(
    current_labels: list[str], dest_name: str, is_default_dest: bool
) -> list[str]:
    """Remove the queue label(s) and add the sent label for this destination."""
    new = list(current_labels)
    specific_queue = f"kindle-{dest_name}"
    if specific_queue in new:
        new.remove(specific_queue)
    if is_default_dest and "kindle" in new:
        new.remove("kindle")
    sent_label = f"sent-to-{dest_name}"
    if sent_label not in new:
        new.append(sent_label)
    return new


def fetch_pending_for_destination(
    client: ReadeckClient,
    dest_name: str,
    is_default_dest: bool,
) -> list[dict]:
    """Find bookmarks queued for this destination and not yet sent there.

    Listing errors propagate: a failure here means Readeck is unreachable,
    and run_cycle must not mark the cycle healthy on that basis.
    """
    sent_label = f"sent-to-{dest_name}"
    bookmarks: dict[str, dict] = {}

    for bm in client.list_bookmarks(f"kindle-{dest_name}"):
        bookmarks[bm["id"]] = bm

    if is_default_dest:
        for bm in client.list_bookmarks("kindle"):
            bookmarks[bm["id"]] = bm

    return [
        bm for bm in bookmarks.values()
        if sent_label not in (bm.get("labels") or [])
    ]


def process_one_bookmark(
    bm: dict,
    client: ReadeckClient,
    smtp_config: SmtpConfig,
    kindle_email: str,
    dest_name: str,
    is_default_dest: bool,
    log: logging.Logger,
) -> bool:
    """Process a single bookmark; return True on success."""
    title = (bm.get("title") or "Untitled").strip()
    author = make_author(bm)

    log.info("  → %s", title)
    try:
        raw_epub = client.fetch_article_epub(bm["id"])
    except Exception as e:
        log.error("    fetch EPUB failed: %s", e)
        return False

    cover_jpeg = None
    try:
        cover_jpeg = render_title_cover(
            title, bm.get("site_name") or bm.get("site") or ""
        )
    except Exception as e:
        log.warning("    cover render failed, sending without it: %s", e)

    try:
        patched = patch_epub_metadata(raw_epub, title, author, cover_jpeg)
    except Exception as e:
        log.error("    patch metadata failed: %s", e)
        return False

    try:
        send_email(smtp_config, kindle_email, title, patched)
    except Exception as e:
        log.error("    SMTP send failed: %s", e)
        return False

    try:
        new_labels = compute_new_labels(
            bm.get("labels") or [], dest_name, is_default_dest
        )
        client.update_labels(bm["id"], new_labels)
    except Exception as e:
        # Email already delivered; a failed label update means the next cycle re-sends.
        log.error(
            "    LABEL UPDATE FAILED but email was already sent: %s "
            "(next cycle will re-send unless labels are fixed manually)", e,
        )
        return False

    return True


def process_destination(
    client: ReadeckClient,
    smtp_config: SmtpConfig,
    config: Config,
    dest_name: str,
    kindle_email: str,
) -> tuple[int, int]:
    """Process all pending bookmarks for one destination."""
    log = logging.getLogger(f"paperboy.{dest_name}")
    is_default = dest_name == config.default_destination

    pending = fetch_pending_for_destination(client, dest_name, is_default)
    if not pending:
        log.debug("no pending bookmarks")
        return 0, 0

    log.info("found %d pending bookmark(s)", len(pending))
    sent = 0
    failed = 0
    for bm in pending:
        if process_one_bookmark(
            bm, client, smtp_config, kindle_email, dest_name, is_default, log
        ):
            sent += 1
        else:
            failed += 1
    log.info("destination done: sent=%d, failed=%d", sent, failed)
    return sent, failed


def run_cycle(config: Config) -> None:
    """One full pass over all destinations."""
    log = logging.getLogger("paperboy")
    client = ReadeckClient(
        config.readeck_url, config.readeck_token, config.verify_ssl
    )
    total_sent = 0
    total_failed = 0
    reached_readeck = True
    for dest_name, kindle_email in config.destinations.items():
        try:
            sent, failed = process_destination(
                client, config.smtp, config, dest_name, kindle_email
            )
            total_sent += sent
            total_failed += failed
        except Exception as e:
            log.error("destination %s crashed: %s", dest_name, e)
            reached_readeck = False
    log.info("cycle complete: sent=%d, failed=%d", total_sent, total_failed)
    # Healthy only if Readeck was reachable for every destination; per-bookmark
    # send failures don't flip health (avoids flapping on a transient SMTP error).
    if reached_readeck:
        _STATE["last_successful_cycle"] = datetime.now(timezone.utc)


# ============================================================================
# Entrypoint
# ============================================================================

def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    try:
        config = load_config()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    setup_logging(config.log_level)
    if not config.verify_ssl:
        # Self-signed cert: mute urllib3's per-request warning so it doesn't drown the logs.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    log = logging.getLogger("paperboy")
    log.info(
        "readeck-paperboy %s starting: %d destination(s), poll_interval=%ds",
        VERSION, len(config.destinations), config.poll_interval_seconds,
    )
    for name, email in config.destinations.items():
        marker = " (default)" if name == config.default_destination else ""
        log.info("  destination: %s → %s%s", name, email, marker)

    start_healthcheck_server(
        config.healthcheck_port, config.poll_interval_seconds
    )

    while True:
        try:
            run_cycle(config)
        except Exception as e:
            log.exception("cycle crashed: %s", e)
        log.debug("sleeping for %d seconds", config.poll_interval_seconds)
        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main() or 0)
