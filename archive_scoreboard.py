#!/usr/bin/env python3
"""
DOMjudge Scoreboard Archiver → Self‑contained HTML

Downloads a DOMjudge scoreboard page and inlines **all** external assets (CSS, JavaScript,
images, fonts, icons) into a single HTML file. The resulting file can be opened offline
and provides the same functionality as the original MHTML version, including a custom
modal handler and offline submission data.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_content_type(url: str, server_content_type: str = None) -> str:
    """Return a best‑guess MIME type for *url*.

    The function first checks the file extension, then falls back to the server's
    ``Content‑Type`` header and finally ``mimetypes.guess_type``.
    """
    path = url.split("?")[0].split("#")[0].lower()
    ext_map = {
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".eot": "application/vnd.ms-fontobject",
        ".css": "text/css",
        ".js": "application/javascript",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".ico": "image/x-icon",
        ".html": "text/html",
        ".htm": "text/html",
    }
    for ext, ct in ext_map.items():
        if path.endswith(ext):
            return ct
    if server_content_type:
        ct = server_content_type.split(";")[0].strip().lower()
        if ct and ct not in ("application/octet-stream", "text/plain"):
            return server_content_type
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def to_data_uri(content: bytes, content_type: str) -> str:
    """Encode *content* as a ``data:`` URI using base64.

    ``content_type`` must be a valid MIME type.
    """
    b64 = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{b64}"

# ---------------------------------------------------------------------------
# Main archiver class
# ---------------------------------------------------------------------------
class InlineHTMLArchiver:
    def __init__(self, start_url: str, output_path: str, verify_ssl: bool = True):
        self.start_url = start_url
        self.output_path = output_path
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.verify = verify_ssl
        if not verify_ssl:
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
        self.fetched: dict[str, tuple[bytes, str]] = {}

    # ---------------------------------------------------------------------
    # Asset fetching utilities
    # ---------------------------------------------------------------------
    def _fetch(self, url: str) -> tuple[bytes, str] | None:
        """Download *url* and return ``(content_bytes, mime_type)``.

        Results are cached in ``self.fetched`` to avoid duplicate network calls.
        """
        norm = urlparse(url)._replace(fragment="").geturl()
        if norm in self.fetched:
            return self.fetched[norm]
        try:
            print(f"Downloading: {norm}")
            r = self.session.get(norm, timeout=15)
            if r.status_code != 200:
                print(f"  ⚠️  Non‑200 status {r.status_code}")
                return None
            ct = get_content_type(norm, r.headers.get("Content-Type"))
            self.fetched[norm] = (r.content, ct)
            return r.content, ct
        except Exception as exc:
            print(f"  ⚠️  Error fetching {norm}: {exc}")
            return None

    # ---------------------------------------------------------------------
    # CSS processing – inline @import and url(...)
    # ---------------------------------------------------------------------
    def _inline_css(self, css: str, base_url: str) -> str:
        """Return CSS with all external URLs inlined as data URIs.

        Handles ``@import`` statements recursively and replaces ``url(...)`` patterns.
        """
        import_pattern = re.compile(r"@import\s+(?:url\()?['\"]?([^'\"]+)['\"]?\)?;?")
        while True:
            m = import_pattern.search(css)
            if not m:
                break
            import_url = urljoin(base_url, m.group(1))
            fetched = self._fetch(import_url)
            replacement = ""
            if fetched:
                content, _ = fetched
                replacement = self._inline_css(content.decode("utf-8", errors="ignore"), import_url)
            css = css[: m.start()] + replacement + css[m.end() :]
        url_pattern = re.compile(r"url\((['\"]?)([^'\")]+)\1\)")
        def repl(match: re.Match) -> str:
            raw = match.group(2).strip()
            if raw.lower().startswith("data:"):
                return match.group(0)
            asset_url = urljoin(base_url, raw)
            fetched = self._fetch(asset_url)
            if not fetched:
                return match.group(0)
            content, ct = fetched
            return f"url('{to_data_uri(content, ct)}')"
        return url_pattern.sub(repl, css)

    # ---------------------------------------------------------------------
    # HTML transformation – inline CSS/JS/Images and inject helpers
    # ---------------------------------------------------------------------
    def _inline_assets(self, soup: BeautifulSoup, page_url: str) -> None:
        # Stylesheets
        for link in soup.find_all("link", rel=lambda x: x and "stylesheet" in x):
            href = link.get("href")
            if not href:
                continue
            css_url = urljoin(page_url, href)
            fetched = self._fetch(css_url)
            if not fetched:
                continue
            content, _ = fetched
            inlined = self._inline_css(content.decode("utf-8", errors="ignore"), css_url)
            style_tag = soup.new_tag("style")
            style_tag.string = inlined
            link.replace_with(style_tag)
        # Scripts
        for script in soup.find_all("script", src=True):
            src = script.get("src")
            if not src:
                continue
            js_url = urljoin(page_url, src)
            fetched = self._fetch(js_url)
            if not fetched:
                continue
            content, _ = fetched
            script.string = content.decode("utf-8", errors="ignore")
            del script["src"]
        # Images
        for img in soup.find_all("img", src=True):
            src = img.get("src")
            img_url = urljoin(page_url, src)
            fetched = self._fetch(img_url)
            if not fetched:
                continue
            content, ct = fetched
            img["src"] = to_data_uri(content, ct)
        # Icons / favicons
        for link in soup.find_all("link", href=True):
            rel = link.get("rel", [])
            if any(r in ("icon", "shortcut", "apple-touch-icon") for r in rel):
                href = link.get("href")
                icon_url = urljoin(page_url, href)
                fetched = self._fetch(icon_url)
                if not fetched:
                    continue
                content, ct = fetched
                link["href"] = to_data_uri(content, ct)
        # Inline CSS URLs inside style attributes
        for tag in soup.find_all(style=True):
            tag["style"] = self._inline_css(tag["style"], page_url)

    def _inject_submissions_override(self, soup: BeautifulSoup, page_url: str) -> None:
        tag = soup.find(attrs={"data-submissions-url": True})
        if not tag:
            return
        sub_url = tag.get("data-submissions-url")
        if not sub_url:
            return
        abs_sub = urljoin(page_url, sub_url)
        try:
            r = self.session.get(abs_sub, timeout=15)
            if r.status_code != 200:
                print(f"⚠️ Submissions endpoint returned {r.status_code}")
                return
            json_literal = json.dumps(r.text)
            js = f"""
            document.addEventListener('DOMContentLoaded', function () {{
                const originalFetch = window.fetch;
                window.fetch = function(url, options) {{
                    if (url === {json.dumps(sub_url)}) {{
                        return Promise.resolve(new Response({json_literal}, {{
                            status: 200,
                            headers: {{ 'Content-Type': 'application/json' }}
                        }}));
                    }}
                    return originalFetch.apply(this, arguments);
                }};
            }});
            """
            script = soup.new_tag("script")
            script.string = js
            if soup.body:
                soup.body.append(script)
            else:
                soup.append(script)
        except Exception as exc:
            print(f"⚠️ Error fetching submissions data: {exc}")

    def _inject_modal_helper(self, soup: BeautifulSoup) -> None:
        modal_js = """
        // Simple bootstrap‑like modal shim (no external CSS/JS needed)
        document.addEventListener('DOMContentLoaded', function () {
            document.querySelectorAll('[data-bs-toggle="modal"]').forEach(function (el) {
                el.addEventListener('click', function (e) {
                    e.preventDefault();
                    var target = el.getAttribute('data-bs-target');
                    if (!target) return;
                    var modal = document.querySelector(target);
                    if (!modal) return;
                    modal.style.display = 'block';
                    modal.classList.add('show');
                    modal.style.background = 'rgba(0,0,0,0.5)';
                });
            });
            document.querySelectorAll('[data-bs-dismiss="modal"]').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var modal = btn.closest('.modal');
                    if (modal) {
                        modal.style.display = 'none';
                        modal.classList.remove('show');
                    }
                });
            });
        });
        """
        tag = soup.new_tag("script")
        tag.string = modal_js
        if soup.body:
            soup.body.append(tag)
        else:
            soup.append(tag)

    def archive(self) -> None:
        print(f"Fetching scoreboard page: {self.start_url}")
        try:
            r = self.session.get(self.start_url, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            print(f"❌ Failed to fetch page: {exc}", file=sys.stderr)
            sys.exit(1)
        page_url = r.url
        soup = BeautifulSoup(r.text, "html.parser")
        self._inline_assets(soup, page_url)
        self._inject_submissions_override(soup, page_url)
        self._inject_modal_helper(soup)
        final_html = str(soup)
        out_path = Path(self.output_path)
        try:
            out_path.write_text(final_html, encoding="utf-8")
            print(f"✅ Inline HTML written to: {out_path.resolve()}")
        except Exception as exc:
            print(f"❌ Could not write output file: {exc}", file=sys.stderr)
            sys.exit(1)

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive a DOMjudge scoreboard into a single, fully‑inlined HTML file."
    )
    parser.add_argument("--url", required=True, help="Scoreboard URL (e.g. https://taichung2025.icpc.tw/)")
    parser.add_argument(
        "--output",
        help="Path for the generated HTML file. Defaults to <domain>_scoreboard.html",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL verification (useful for internal contest servers)",
    )
    args = parser.parse_args()
    if not args.output:
        parsed = urlparse(args.url)
        domain = parsed.netloc or "scoreboard"
        safe = re.sub(r"[^\w\.-]", "_", domain)
        args.output = f"{safe}_scoreboard.html"
    archiver = InlineHTMLArchiver(args.url, args.output, verify_ssl=not args.insecure)
    archiver.archive()

if __name__ == "__main__":
    main()
