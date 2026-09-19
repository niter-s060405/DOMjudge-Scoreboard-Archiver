#!/usr/bin/env python3
"""
DOMjudge Scoreboard Archiver – Headless-Browser version (fixed)

This version uses Playwright (Chromium) to render the page, captures all network
responses, inlines assets, and adds extensive diagnostics to verify that all
required resources (Bootstrap CSS, team images, CSS order) are captured before
writing the final self-contained HTML.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Response

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        def __init__(self, iterable=None, total=None, desc="", unit="", **kwargs):
            self.iterable = iterable
            self.total = total or (len(iterable) if iterable else 0)
            self.desc = desc
            self.unit = unit
            self.n = 0

        def __iter__(self):
            for item in self.iterable:
                yield item
                self.update(1)

        def update(self, n=1):
            self.n += n
            if self.total:
                pct = int(self.n * 100 / self.total)
                sys.stdout.write(f"\r{self.desc}: {pct}% ({self.n}/{self.total} {self.unit})")
                sys.stdout.flush()
                if self.n >= self.total:
                    sys.stdout.write("\n")
                    sys.stdout.flush()

        def close(self):
            sys.stdout.write("\n")
            sys.stdout.flush()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        @staticmethod
        def write(s, file=sys.stdout):
            file.write(f"\r\033[K{s}\n")
            file.flush()

# ---------------------------------------------------------------------------
# Helper utilities (same as the pure-requests version)
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


class RateLimiter:
    """Thread-safe rate limiter (requests per second) using virtual scheduling."""
    def __init__(self, max_rps: float | None = None):
        self.max_rps = max_rps if (max_rps and max_rps > 0) else None
        self.min_interval = 1.0 / self.max_rps if self.max_rps else 0.0
        self.last_req_time = 0.0
        self.lock = threading.Lock()

    def acquire(self) -> None:
        if not self.max_rps:
            return
        with self.lock:
            now = time.monotonic()
            scheduled_time = max(now, self.last_req_time + self.min_interval)
            wait = scheduled_time - now
            self.last_req_time = scheduled_time

        if wait > 0:
            time.sleep(wait)


class RateLimitedSession(requests.Session):
    """A requests.Session that enforces a RateLimiter and handles timeout retries with backoff."""
    def __init__(
        self,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 10.0,
        retry_delay: float = 30.0,
        max_retries: int = 3,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.rate_limiter = rate_limiter
        self.default_timeout = timeout
        self.retry_delay = retry_delay
        self.max_retries = max_retries

    def send(self, request, **kwargs):
        if "timeout" not in kwargs or kwargs["timeout"] is None:
            kwargs["timeout"] = self.default_timeout

        attempts = 0
        while True:
            if self.rate_limiter:
                self.rate_limiter.acquire()
            try:
                resp = super().send(request, **kwargs)
                if resp.status_code in (429, 503) and attempts < self.max_retries:
                    attempts += 1
                    tqdm.write(
                        f"⚠️ [Retry {attempts}/{self.max_retries}] {request.url} returned HTTP {resp.status_code}. "
                        f"Waiting {self.retry_delay}s until requesting again..."
                    )
                    time.sleep(self.retry_delay)
                    continue
                return resp
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                attempts += 1
                if attempts > self.max_retries:
                    raise
                reason = "timed out (>10s)" if isinstance(exc, requests.exceptions.Timeout) else f"connection failed ({exc})"
                tqdm.write(
                    f"⚠️ [Retry {attempts}/{self.max_retries}] {request.url} {reason}. "
                    f"Waiting {self.retry_delay}s until requesting again..."
                )
                time.sleep(self.retry_delay)


def create_session(
    concurrency: int = 32,
    verify_ssl: bool = True,
    rate_limiter: RateLimiter | None = None,
    timeout: float = 10.0,
    retry_delay: float = 30.0,
    max_retries: int = 3,
) -> requests.Session:
    """Create a requests.Session with connection pooling, retry strategy, and optional rate limiting and timeout backoff."""
    session = RateLimitedSession(
        rate_limiter=rate_limiter,
        timeout=timeout,
        retry_delay=retry_delay,
        max_retries=max_retries,
    )
    session.headers.update(HEADERS)
    session.verify = verify_ssl
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=concurrency,
        pool_maxsize=concurrency,
        max_retries=retry_strategy,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_content_type(url: str, server_content_type: str | None = None) -> str:
    """Return a best-guess MIME type for *url*.

    The function first checks the file extension, then falls back to the server's
    ``Content-Type`` header and finally ``mimetypes.guess_type``.
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
# Main archiver class – uses Playwright to capture the rendered page
# ---------------------------------------------------------------------------
class BrowserHTMLArchiver:
    def __init__(
        self,
        start_url: str,
        output_path: str,
        verify_ssl: bool = True,
        generate_diagnostics: bool = False,
        concurrency: int = 32,
        rate_limit: float | None = None,
        timeout: float = 10.0,
        retry_delay: float = 30.0,
        max_retries: int = 3,
    ):
        self.start_url = start_url
        self.output_path = output_path
        self.verify_ssl = verify_ssl
        self.assets: dict[str, tuple[bytes, str]] = {}
        # Track if Bootstrap JS is available at runtime
        self.bootstrap_js_present: bool = False
        self.generate_diagnostics = generate_diagnostics
        self.concurrency = concurrency
        self.rate_limit = rate_limit
        self.rate_limiter = RateLimiter(rate_limit)
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.max_retries = max_retries

    # ---------------------------------------------------------------------
    # Playwright helpers – capture every network response
    # ---------------------------------------------------------------------
    def _capture_assets(self, page) -> None:
        """Intercept every network request and store the response body.

        In addition to populating ``self.assets`` we log each captured URL and its
        MIME type to a diagnostic file so we can later verify that Bootstrap CSS
        and team pictures were retrieved.
        """
        def handle_route(route):
            response: Response = route.fetch()
            url = response.url
            try:
                body = response.body()
                ct = response.headers.get("content-type")
                mime = get_content_type(url, ct)
                self.assets[url] = (body, mime)
                # Log the captured asset for diagnostics
                self._log_asset(url, f"{mime} (Status: {response.status})")
            except Exception as exc:
                print(f"⚠️ Could not store asset {url}: {exc}")
            route.fulfill(
                status=response.status,
                headers=response.headers,
                body=body,
            )
        page.route("**/*", handle_route)

    def _log_asset(self, url: str, info: str) -> None:
        """Log captured asset info (mime and status) for diagnostics.
        """
        if not self.generate_diagnostics:
            return
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"CAPTURED|{url}|{info}\n")

    def _log_script_issue(self, url: str, issue: str) -> None:
        if not self.generate_diagnostics:
            return
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"SCRIPT_ISSUE|{url}|{issue}\n")

    # ---------------------------------------------------------------------
    # CSS inlining – resolve @import and url(...)
    # ---------------------------------------------------------------------
    def _inline_css(self, css: str, base_url: str) -> str:
        import_pattern = re.compile(r"@import\s+(?:url\()?['\"]?([^'\"]+)['\"]?\)?;?")
        while True:
            m = import_pattern.search(css)
            if not m:
                break
            import_url = urljoin(base_url, m.group(1))
            content, _ = self.assets.get(import_url, (None, None))
            replacement = ""
            if content:
                replacement = self._inline_css(content.decode("utf-8", errors="ignore"), import_url)
            css = css[: m.start()] + replacement + css[m.end():]
        url_pat = re.compile(r"url\(\s*['\"]?([^'\"()]+?)['\"]?\s*\)")
        def repl(match: re.Match) -> str:
            raw = match.group(1).strip()
            if raw.lower().startswith("data:"):
                return match.group(0)
            asset_url = urljoin(base_url, raw)
            lookup_url = asset_url.split('#')[0]
            data = self.assets.get(lookup_url)
            if not data:
                return match.group(0)
            content, ct = data
            return f"url('{to_data_uri(content, ct)}')"
        return url_pat.sub(repl, css)

    # ---------------------------------------------------------------------
    # HTML transformation – replace external links with inlined equivalents
    # ---------------------------------------------------------------------
    def _inline_assets(self, soup: BeautifulSoup, page_url: str) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Collect unique missing image/icon URLs
        missing_img_urls = set()
        for img in soup.find_all("img", src=True):
            src = img.get("src")
            if not src or src.lower().startswith("data:"):
                continue
            img_url = urljoin(page_url, src)
            if img_url not in self.assets:
                missing_img_urls.add(img_url)

        missing_favicon_urls = set()
        for link in soup.find_all("link", rel=lambda x: x and any(r in x.lower() for r in ["icon", "shortcut"])):
            href = link.get("href")
            if not href or href.lower().startswith("data:"):
                continue
            icon_url = urljoin(page_url, href)
            if icon_url not in self.assets:
                missing_favicon_urls.add(icon_url)

        all_to_fetch = list(missing_img_urls.union(missing_favicon_urls))

        if all_to_fetch:
            max_workers = min(self.concurrency, len(all_to_fetch))
            rate_info = f", rate_limit={self.rate_limit} req/s" if self.rate_limit else ""
            print(f"Prefetching {len(all_to_fetch)} missing images/icons in parallel (concurrency={max_workers}{rate_info})...")
            session = create_session(
                max_workers,
                self.verify_ssl,
                self.rate_limiter,
                timeout=self.timeout,
                retry_delay=self.retry_delay,
                max_retries=self.max_retries,
            )

            def fetch_one(url):
                try:
                    r = session.get(url, timeout=self.timeout)
                    if r.status_code == 200:
                        mime, _ = mimetypes.guess_type(url)
                        if not mime:
                            mime = "image/x-icon" if url in missing_favicon_urls else "image/jpeg"
                        return url, r.content, mime
                    else:
                        return url, None, f"Status {r.status_code}"
                except Exception as exc:
                    return url, None, str(exc)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(fetch_one, url): url for url in all_to_fetch}
                with tqdm(total=len(all_to_fetch), desc="Prefetching missing assets", unit="img") as pbar:
                    for fut in as_completed(futures):
                        url, content, info = fut.result()
                        if content is not None:
                            self.assets[url] = (content, info)
                            self._log_asset(url, f"{info} (Status: 200)")
                        else:
                            tqdm.write(f"⚠️ Failed to fetch missing asset {url}: {info}")
                        pbar.update(1)

        # Stylesheets (preserve order)
        for link in soup.find_all("link", rel=lambda x: x and "stylesheet" in x):
            href = link.get("href")
            if not href:
                continue
            css_url = urljoin(page_url, href)
            content, _ = self.assets.get(css_url, (None, None))
            if not content:
                continue
            inlined = self._inline_css(content.decode("utf-8", errors="ignore"), css_url)
            style_tag = soup.new_tag("style")
            style_tag.string = inlined
            link.replace_with(style_tag)
        # Scripts – preserve defer/async/module attributes and log issues
        for script in soup.find_all("script", src=True):
            src = script.get("src")
            if not src:
                continue
            js_url = urljoin(page_url, src)
            content, mime = self.assets.get(js_url, (None, None))
            if not content:
                # Asset missing – log and keep original reference
                self._log_script_issue(js_url, "missing")
                continue
            # Detect dynamic import or import.meta URL patterns that may fail offline
            if b"import(" in content or b"import.meta" in content:
                self._log_script_issue(js_url, "dynamic_import")
            # Preserve attributes
            defer = script.has_attr('defer')
            async_attr = script.has_attr('async')
            module_type = script.get('type')
            # Inline content
            script.string = content.decode("utf-8", errors="ignore")
            # Remove src attribute
            del script["src"]
            # Re-apply attributes if they were present
            if defer:
                script['defer'] = ''
            if async_attr:
                script['async'] = ''
            if module_type:
                script['type'] = module_type
        # Images (including team pictures)
        for img in soup.find_all("img", src=True):
            src = img.get("src")
            img_url = urljoin(page_url, src)
            data = self.assets.get(img_url)
            if not data:
                continue
            content, ct = data
            img["src"] = to_data_uri(content, ct)
            img["loading"] = "eager"
        # Icons / favicons
        for link in soup.find_all("link", href=True):
            rel = link.get("rel", [])
            if any(r in ("icon", "shortcut", "apple-touch-icon") for r in rel):
                href = link.get("href")
                icon_url = urljoin(page_url, href)
                data = self.assets.get(icon_url)
                if not data:
                    continue
                content, ct = data
                link["href"] = to_data_uri(content, ct)
        # Inline CSS URLs inside inline style attributes
        for tag in soup.find_all(style=True):
            tag["style"] = self._inline_css(tag["style"], page_url)

    # ---------------------------------------------------------------------
    # Site-specific post-processing hook
    # ---------------------------------------------------------------------
    def post_process_html(self, soup: BeautifulSoup, page_url: str) -> None:
        """Hook for subclasses to apply site-specific post-processing to the serialized DOM."""
        pass

    # ---------------------------------------------------------------------
    # Simple modal shim – identical to the previous version
    # ---------------------------------------------------------------------
    def _inject_modal_helper(self, soup: BeautifulSoup) -> None:
        modal_js = """
        // Simple bootstrap-like modal shim (no external CSS/JS needed)
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

    # ---------------------------------------------------------------------
    # Post-processing fixes (modal CSS, etc.)
    # ---------------------------------------------------------------------
    def _apply_fixups(self, soup: BeautifulSoup) -> None:
        # 1. Ensure the modal is hidden by default – add minimal CSS rules.
        hide_css = """
        .modal { display: none; }
        .modal.show { display: block; }
        """
        style_tag = soup.new_tag("style")
        style_tag.string = hide_css
        if soup.head:
            soup.head.append(style_tag)
        else:
            soup.insert(0, style_tag)

    def _get_computed_styles(self, page) -> dict:
        """Capture computed style and geometry for a broad set of selectors from page.
        Returns a dictionary mapping keys to list of styling data dicts.
        """
        selectors = {
            "body": "body",
            "container": ".container",
            "container_fluid": ".container-fluid",
            "row": ".row",
            "col": "[class*='col-']",
            "scoreboard_wrapper": ".scoreboard, .scoreboard-container, .scoreboard-wrapper",
            "table": "table",
            "thead": "thead",
            "tbody": "tbody",
            "nav": "nav",
            "modal": ".modal",
        }
        props = [
            "display",
            "position",
            "font-family",
            "font-size",
            "line-height",
            "box-sizing",
            "margin",
            "padding",
        ]
        script = f"""(() => {{
  const selectors = {json.dumps(selectors)};
  const props = {json.dumps(props)};
  const result = {{}};
  for (const [key, sel] of Object.entries(selectors)) {{
    const els = document.querySelectorAll(sel);
    if (!els.length) continue;
    result[key] = [];
    els.forEach(el => {{
      const cs = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const data = {{
        width: rect.width,
        height: rect.height,
        display: cs.getPropertyValue('display'),
        position: cs.getPropertyValue('position'),
        fontFamily: cs.getPropertyValue('font-family'),
        fontSize: cs.getPropertyValue('font-size'),
        lineHeight: cs.getPropertyValue('line-height'),
        boxSizing: cs.getPropertyValue('box-sizing'),
        margin: cs.getPropertyValue('margin'),
        padding: cs.getPropertyValue('padding')
      }};
      result[key].push(data);
    }});
  }}
  return result;
}})()"""
        try:
            return page.evaluate(script)
        except Exception:
            return {}

    def _log_computed_styles(self, page) -> None:
        """Capture computed styles and write them to the diagnostics log under COMPUTED_STYLE."""
        data = self._get_computed_styles(page)
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            for key, entries in data.items():
                f.write(f"COMPUTED_STYLE|{key}|{json.dumps(entries)}\n")

    def _compare_and_log_styles(self, live_data: dict, offline_data: dict) -> None:
        """Compare live and offline computed styles and write diffs to the diagnostics log."""
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            for key in set(live_data.keys()).union(offline_data.keys()):
                live_entries = live_data.get(key, [])
                offline_entries = offline_data.get(key, [])
                if len(live_entries) != len(offline_entries):
                    f.write(f"STYLE_DIFF|{key}|element_count|LIVE: {len(live_entries)}|OFFLINE: {len(offline_entries)}\n")
                for idx in range(min(len(live_entries), len(offline_entries))):
                    le = live_entries[idx]
                    oe = offline_entries[idx]
                    for prop in le.keys():
                        val_l = le[prop]
                        val_o = oe[prop]
                        if val_l != val_o:
                            # For width and height, allow small floating point margin of error (e.g. 1px)
                            if prop in ('width', 'height'):
                                try:
                                    if abs(float(val_l) - float(val_o)) < 1.0:
                                        continue
                                except (ValueError, TypeError):
                                    pass
                            f.write(f"STYLE_DIFF|{key}[{idx}]|{prop}|LIVE: {val_l}|OFFLINE: {val_o}\n")

    def _capture_css_dependencies(self, page_url: str) -> None:
        """Crawl all text/css assets currently in self.assets, find all url(...)
        and @import references, and fetch any that are missing so they can be inlined.
        """
        url_pat = re.compile(r"url\(\s*['\"]?([^'\"()]+?)['\"]?\s*\)")
        import_pat = re.compile(r"@import\s+(?:url\()?['\"]?([^'\"]+)['\"]?\)?;?")
        urls_to_fetch = set()
        
        for asset_url, (content, mime) in list(self.assets.items()):
            if mime == "text/css":
                css_text = content.decode("utf-8", errors="ignore")
                # Find url(...)
                for match in url_pat.finditer(css_text):
                    raw = match.group(1).strip()
                    if raw.lower().startswith("data:"):
                        continue
                    resolved_url = urljoin(asset_url, raw)
                    resolved_url = resolved_url.split('#')[0]
                    if resolved_url not in self.assets and resolved_url not in urls_to_fetch:
                        urls_to_fetch.add(resolved_url)
                # Find @import
                for match in import_pat.finditer(css_text):
                    raw = match.group(1).strip()
                    if raw.lower().startswith("data:"):
                        continue
                    resolved_url = urljoin(asset_url, raw)
                    resolved_url = resolved_url.split('#')[0]
                    if resolved_url not in self.assets and resolved_url not in urls_to_fetch:
                        urls_to_fetch.add(resolved_url)
        
        # Now fetch the missing assets using requests
        if urls_to_fetch:
            max_workers = min(self.concurrency, len(urls_to_fetch))
            rate_info = f", rate_limit={self.rate_limit} req/s" if self.rate_limit else ""
            print(f"Fetching {len(urls_to_fetch)} CSS dependencies in parallel (concurrency={max_workers}{rate_info})...")
            session = create_session(
                max_workers,
                self.verify_ssl,
                self.rate_limiter,
                timeout=self.timeout,
                retry_delay=self.retry_delay,
                max_retries=self.max_retries,
            )

            def fetch_css_dep(url):
                try:
                    r = session.get(url, timeout=self.timeout)
                    if r.status_code == 200:
                        mime = get_content_type(url, r.headers.get("content-type"))
                        return url, r.content, mime, None
                    else:
                        return url, None, None, f"Status {r.status_code}"
                except Exception as e:
                    return url, None, None, str(e)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(fetch_css_dep, url): url for url in urls_to_fetch}
                with tqdm(total=len(urls_to_fetch), desc="Fetching CSS dependencies", unit="file") as pbar:
                    for fut in as_completed(futures):
                        url, content, mime, err = fut.result()
                        if content is not None:
                            self.assets[url] = (content, mime)
                            self._log_asset(url, f"{mime} (Status: 200)")
                        else:
                            tqdm.write(f"⚠️ Failed to fetch CSS dependency {url}: {err}")
                        pbar.update(1)

    def _log_missing_css_assets(self, soup: BeautifulSoup, page_url: str) -> None:
        """Inspect all inlined CSS for url(...) references that were not captured.
        Logs any missing resources to the diagnostics file.
        """
        url_pat = re.compile(r"url\((['\"]?)([^'\"]+)\1\)")
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            # check <style> tags (including those we generated)
            for style in soup.find_all('style'):
                css = style.string or ''
                for m in url_pat.finditer(css):
                    raw = m.group(2).strip()
                    if raw.lower().startswith('data:'):
                        continue
                    asset_url = urljoin(page_url, raw)
                    if asset_url not in self.assets:
                        f.write(f"MISSING_CSS_ASSET|{asset_url}\n")

    def _log_icon_assets(self, soup: BeautifulSoup, page_url: str) -> None:
        """Detect common icon resources (favicons, apple-touch-icons, medal images, Font Awesome) that are missing.
        Logs each missing URL to the diagnostics file.
        """
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            # Link icons (favicon, apple-touch-icon)
            for link in soup.find_all('link', href=True):
                rel = link.get('rel', [])
                if any(r in ('icon', 'shortcut', 'apple-touch-icon') for r in rel):
                    href = link.get('href')
                    icon_url = urljoin(page_url, href)
                    if icon_url not in self.assets:
                        f.write(f"MISSING_ICON|{icon_url}\n")
            # Image icons (e.g., medal images)
            for img in soup.find_all('img', src=True):
                src = img['src']
                if 'medal' in src.lower() or 'icon' in src.lower():
                    img_url = urljoin(page_url, src)
                    if img_url not in self.assets:
                        f.write(f"MISSING_ICON|{img_url}\n")
            # Font Awesome icons via <i> classes (font files)
            # Detect if Font Awesome CSS was inlined; if so, check its @font-face sources
            for style in soup.find_all('style'):
                css = style.string or ''
                # Look for url(...) inside @font-face rules
                for match in re.finditer(r"url\(\s*['\"]?([^'\"()]+?)['\"]?\s*\)", css):
                    raw = match.group(1).strip()
                    if raw.lower().startswith('data:'):
                        continue
                    font_url = urljoin(page_url, raw)
                    if font_url not in self.assets:
                        f.write(f"MISSING_FONT|{font_url}\n")


    def _verify_modal(self, page) -> None:
        is_modal = page.evaluate("document.querySelector('.modal') !== null")
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"MODAL_DETECTED|{'YES' if is_modal else 'NO'}\n")

    def _run_diagnostics(self, soup: BeautifulSoup, page_url: str) -> None:
        """Perform requested diagnostics and write them to the log file.

        Checks performed:
        1. Presence of Bootstrap CSS (bootstrap.min.css) in captured assets.
        2. Whether any modal element already has the ``show`` class.
        3. Which team picture URLs are missing from ``self.assets``.
        4. Whether the order of CSS ``<link>`` tags matches the order of the
           inlined ``<style>`` tags that will be inserted.
        """
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        with open(log_path, 'a', encoding='utf-8') as log:
            # 1. Bootstrap CSS detection
            bootstrap_urls = [url for url in self.assets if 'bootstrap.min.css' in url.lower()]
            if bootstrap_urls:
                log.write(f"BOOTSTRAP_PRESENT|YES|{bootstrap_urls[0]}\n")
            else:
                log.write("BOOTSTRAP_PRESENT|NO|\n")
            # 2. Modal show class check
            modal = soup.find(class_=re.compile(r'\bmodal\b'))
            has_show = False
            if modal and modal.has_attr('class'):
                has_show = any(cls == 'show' for cls in modal['class'])
            log.write(f"MODAL_HAS_SHOW|{'YES' if has_show else 'NO'}\n")
            # 3. Missing team pictures
            missing_images = []
            for img in soup.find_all('img', src=True):
                src = img['src']
                img_url = urljoin(page_url, src)
                if img_url not in self.assets:
                    missing_images.append(img_url)
            if missing_images:
                for mi in missing_images:
                    log.write(f"MISSING_IMAGE|{mi}\n")
            else:
                log.write("MISSING_IMAGE|NONE\n")
            # 4. CSS order verification
            original_links = []
            for link in soup.find_all('link', rel=lambda x: x and 'stylesheet' in x):
                href = link.get('href')
                if href:
                    original_links.append(urljoin(page_url, href))
            log.write("CSS_ORDER|ORIGINAL|" + '|'.join(original_links) + "\n")
            inlined_links = [url for url in original_links if url in self.assets]
            log.write("CSS_ORDER|INLINE|" + '|'.join(inlined_links) + "\n")
        print(f"[V] Diagnostics written to {log_path}")

    # ---------------------------------------------------------------------
    # Public entry point – orchestrates everything
    # ---------------------------------------------------------------------
    def archive(self) -> None:
        print(f"Launching headless Chromium to fetch: {self.start_url}")
        log_path = Path(self.output_path).with_suffix('.diagnostics.log')
        if self.generate_diagnostics and log_path.exists():
            log_path.unlink()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=not self.verify_ssl)
            page = context.new_page()
            self._capture_assets(page)
            page.goto(self.start_url, wait_until="networkidle")
            # Detect presence of Bootstrap JS (e.g., bootstrap.Modal constructor)
            try:
                self.bootstrap_js_present = bool(page.evaluate("typeof bootstrap !== 'undefined' && typeof bootstrap.Modal !== 'undefined'"))
            except Exception:
                self.bootstrap_js_present = False
            # Capture computed styles for layout debugging
            if self.generate_diagnostics:
                live_data = self._get_computed_styles(page)
                # Log them raw
                with open(log_path, 'a', encoding='utf-8') as f:
                    for key, entries in live_data.items():
                        f.write(f"COMPUTED_STYLE|{key}|{json.dumps(entries)}\n")
            # Capture rendered HTML while page is still alive
            rendered_html = page.evaluate("document.documentElement.outerHTML")
            final_url = page.url
            # Run modal verification while Playwright context is still active
            if self.generate_diagnostics:
                self._verify_modal(page)
            # Close browser after all page-dependent work is done
            browser.close()

        # Capture any CSS url(...) or @import dependencies that the browser did not request
        self._capture_css_dependencies(final_url)

        # Parse HTML and run post-capture diagnostics
        soup = BeautifulSoup(rendered_html, "html.parser")
        if self.generate_diagnostics:
            self._run_diagnostics(soup, final_url)
        # Inline assets and apply optional fixes
        self._inline_assets(soup, final_url)
        # Additional asset diagnostics for CSS url(...) and icons run on the inlined structure
        if self.generate_diagnostics:
            self._log_missing_css_assets(soup, final_url)
            self._log_icon_assets(soup, final_url)
        self.post_process_html(soup, final_url)
        # Conditionally inject modal shim and CSS fixups only if Bootstrap JS is missing
        if not self.bootstrap_js_present:
            self._inject_modal_helper(soup)
            self._apply_fixups(soup)

        final_html = str(soup)
        if not final_html.lstrip().startswith("<!DOCTYPE"):
            final_html = "<!DOCTYPE html>\n" + final_html
        out_path = Path(self.output_path)
        try:
            out_path.write_text(final_html, encoding="utf-8")
            print(f"[V] Inline HTML written to: {out_path.resolve()}")
        except Exception as exc:
            print(f"[ERROR] Could not write output file: {exc}", file=sys.stderr)
            sys.exit(1)

        # Now load the offline file in Playwright and compare styles!
        if self.generate_diagnostics:
            print("Verifying offline page layout in Playwright...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                offline_url = out_path.resolve().as_uri()
                page.goto(offline_url, wait_until="networkidle")
                offline_data = self._get_computed_styles(page)
                # Check offline modal presence generic selector
                is_modal = page.evaluate("document.querySelector('.modal') !== null")
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(f"OFFLINE_MODAL_DETECTED|{'YES' if is_modal else 'NO'}\n")
                # Compare and log
                self._compare_and_log_styles(live_data, offline_data)
                browser.close()

class DOMjudgeScoreboardArchiver(BrowserHTMLArchiver):
    """Subclass of BrowserHTMLArchiver specialized for DOMjudge scoreboards,
    adding submission dynamic caching and team category filter handling.
    """
    def post_process_html(self, soup: BeautifulSoup, page_url: str) -> None:
        # Remove data-ajax-refresh-target to disable background live polling offline
        for tag in soup.find_all(attrs={"data-ajax-refresh-target": True}):
            del tag["data-ajax-refresh-target"]
        self._inject_offline_ajax_cache(soup, page_url)
        self._inline_problem_statements(soup, page_url)

    def _inline_problem_statements(self, soup: BeautifulSoup, page_url: str) -> None:
        """Find all problem statement links in the HTML and inline them as data URIs."""
        statement_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"/problems/\d+/statement", href):
                statement_links.append(a)
                
        if not statement_links:
            return

        # Inject the javascript helper to handle base64-to-blob-URL rendering
        helper_script = soup.new_tag("script")
        helper_script.string = """
        function openOfflineBlob(base64, mimeType) {
            try {
                const bin = atob(base64);
                const len = bin.length;
                const bytes = new Uint8Array(len);
                for (let i = 0; i < len; i++) {
                    bytes[i] = bin.charCodeAt(i);
                }
                const blob = new Blob([bytes], {type: mimeType});
                const url = URL.createObjectURL(blob);
                window.open(url, '_blank');
            } catch (e) {
                console.error("Failed to open offline document:", e);
                alert("Failed to open document offline.");
            }
        }
        """
        if soup.body:
            soup.body.append(helper_script)
        else:
            soup.append(helper_script)

        if statement_links:
            max_workers = min(getattr(self, "concurrency", 16), len(statement_links))
            rate_info = f", rate_limit={self.rate_limit} req/s" if getattr(self, "rate_limit", None) else ""
            print(f"Found {len(statement_links)} problem statement link(s) to inline (concurrency={max_workers}{rate_info}).")
            session = create_session(
                max_workers,
                self.verify_ssl,
                getattr(self, "rate_limiter", None),
                timeout=getattr(self, "timeout", 10.0),
                retry_delay=getattr(self, "retry_delay", 30.0),
                max_retries=getattr(self, "max_retries", 3),
            )

            def fetch_statement(a_tag):
                href = a_tag["href"]
                abs_url = urljoin(page_url, href)
                try:
                    r = session.get(abs_url, timeout=getattr(self, "timeout", 10.0))
                    if r.status_code == 200:
                        content_type = r.headers.get("content-type", "application/pdf").split(";")[0].strip()
                        encoded = base64.b64encode(r.content).decode("utf-8")
                        return a_tag, abs_url, encoded, content_type, None
                    else:
                        return a_tag, abs_url, None, None, f"Status {r.status_code}"
                except Exception as e:
                    return a_tag, abs_url, None, None, str(e)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(fetch_statement, a): a for a in statement_links}
                with tqdm(total=len(statement_links), desc="Inlining problem statements", unit="doc") as pbar:
                    for fut in as_completed(futures):
                        a_tag, abs_url, encoded, content_type, err = fut.result()
                        if encoded:
                            a_tag["href"] = "javascript:void(0)"
                            a_tag["onclick"] = f"openOfflineBlob('{encoded}', '{content_type}'); return false;"
                            a_tag["target"] = "_blank"
                            tqdm.write(f"[V] Inlined statement for {abs_url} ({content_type})")
                        else:
                            tqdm.write(f"[W] Failed to download statement from {abs_url}: {err}")
                        pbar.update(1)

    def _inject_offline_ajax_cache(self, soup: BeautifulSoup, page_url: str) -> None:
        """Prefetch all submission JSON data and team modal pages,
        and inject them as a static cache to mock window.fetch and jQuery.ajax.
        """
        ajax_cache = {}
        max_workers = getattr(self, "concurrency", 32)
        rate_info = f", rate_limit={self.rate_limit} req/s" if getattr(self, "rate_limit", None) else ""
        cur_timeout = getattr(self, "timeout", 10.0)
        session = create_session(
            max_workers,
            self.verify_ssl,
            getattr(self, "rate_limiter", None),
            timeout=cur_timeout,
            retry_delay=getattr(self, "retry_delay", 30.0),
            max_retries=getattr(self, "max_retries", 3),
        )
        
        # 1. Fetch submission data URLs
        sub_tags = soup.find_all(attrs={"data-submissions-url": True})
        sub_urls = sorted(list(set(tag.get("data-submissions-url") for tag in sub_tags if tag.get("data-submissions-url"))))
        
        if sub_urls:
            cur_workers = min(max_workers, len(sub_urls))
            print(f"Prefetching {len(sub_urls)} submissions data URLs (concurrency={cur_workers}{rate_info})...")

            def fetch_sub(sub_url: str):
                abs_url = urljoin(page_url, sub_url)
                try:
                    r = session.get(abs_url, timeout=cur_timeout)
                    if r.status_code == 200:
                        return sub_url, r.text
                    else:
                        tqdm.write(f"⚠️ Submissions endpoint {sub_url} returned status {r.status_code}")
                except Exception as exc:
                    tqdm.write(f"⚠️ Error fetching submissions data {sub_url}: {exc}")
                return sub_url, None

            with ThreadPoolExecutor(max_workers=cur_workers) as executor:
                futures = {executor.submit(fetch_sub, u): u for u in sub_urls}
                with tqdm(total=len(sub_urls), desc="Prefetching submissions data", unit="req") as pbar:
                    for fut in as_completed(futures):
                        sub_url, res = fut.result()
                        if res is not None:
                            ajax_cache[sub_url] = res
                        pbar.update(1)
        
        # 2. Fetch team modal pages
        team_tags = soup.find_all('a', attrs={"data-ajax-modal": True})
        team_urls = sorted(list(set(tag.get("href") for tag in team_tags if tag.get("href"))))
        
        if team_urls:
            cur_workers = min(max_workers, len(team_urls))
            print(f"Prefetching {len(team_urls)} team modal pages (concurrency={cur_workers}{rate_info})...")

            def fetch_team(team_url: str):
                abs_url = urljoin(page_url, team_url)
                try:
                    headers = {"X-Requested-With": "XMLHttpRequest"}
                    r = session.get(abs_url, headers=headers, timeout=cur_timeout)
                    if r.status_code == 200:
                        modal_soup = BeautifulSoup(r.text, "html.parser")
                        for s_tag in modal_soup.find_all("script"):
                            s_tag.decompose()
                        # Fetch and inline any images inside the modal HTML
                        for img_tag in modal_soup.find_all("img", src=True):
                            img_src = img_tag["src"]
                            if img_src.lower().startswith("data:"):
                                continue
                            abs_img_url = urljoin(abs_url, img_src)
                            try:
                                r_img = session.get(abs_img_url, timeout=cur_timeout)
                                if r_img.status_code == 200:
                                    mime, _ = mimetypes.guess_type(abs_img_url)
                                    if not mime:
                                        mime = "image/jpeg"
                                    img_data = base64.b64encode(r_img.content).decode("utf-8")
                                    img_tag["src"] = f"data:{mime};base64,{img_data}"
                                else:
                                    tqdm.write(f"⚠️ Failed to fetch modal image {abs_img_url}: Status {r_img.status_code}")
                            except Exception as exc:
                                tqdm.write(f"⚠️ Error fetching modal image {abs_img_url}: {exc}")
                        # Extract only the main modal div (or root div) to avoid leading/trailing whitespace text nodes
                        modal_div = modal_soup.find("div", class_="modal") or modal_soup.find("div")
                        if modal_div:
                            return team_url, str(modal_div).strip()
                        else:
                            return team_url, r.text.strip()
                    else:
                        tqdm.write(f"⚠️ Team modal endpoint {team_url} returned status {r.status_code}")
                except Exception as exc:
                    tqdm.write(f"⚠️ Error fetching team page {team_url}: {exc}")
                return team_url, None

            with ThreadPoolExecutor(max_workers=cur_workers) as executor:
                futures = {executor.submit(fetch_team, u): u for u in team_urls}
                with tqdm(total=len(team_urls), desc="Prefetching team modals", unit="modal") as pbar:
                    for fut in as_completed(futures):
                        team_url, res = fut.result()
                        if res is not None:
                            ajax_cache[team_url] = res
                        pbar.update(1)
                    
        # 3. Inject JS mockup script
        if not ajax_cache:
            return
            
        js_literal = json.dumps(ajax_cache).replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT")
        js = f"""
        (function() {{
            const cache = {js_literal};
            
            // Normalize URLs (supporting relative, absolute path, and query matching)
            function findCachedContent(url) {{
                if (!url) return null;
                // Stringify if it is a Request object
                let urlStr = typeof url === 'string' ? url : url.url;
                if (!urlStr) return null;
                
                // Try exact match
                if (cache[urlStr]) return cache[urlStr];
                
                // Try matching paths or relative URLs
                try {{
                    const parsedUrl = new URL(urlStr, window.location.href);
                    const pathAndQuery = parsedUrl.pathname + parsedUrl.search;
                    if (cache[pathAndQuery]) return cache[pathAndQuery];
                    
                    // Try exact match with protocol/host stripped
                    for (const key of Object.keys(cache)) {{
                        if (urlStr.endsWith(key) || pathAndQuery.endsWith(key)) {{
                            return cache[key];
                        }}
                    }}
                }} catch(e) {{}}
                
                return null;
            }}

            // 1. Mock window.fetch
            const originalFetch = window.fetch;
            window.fetch = function(url, options) {{
                const content = findCachedContent(url);
                if (content !== null) {{
                    return Promise.resolve(new Response(content, {{
                        status: 200,
                        headers: {{ 'Content-Type': 'application/json' }}
                    }}));
                }}
                return originalFetch.apply(this, arguments);
            }};

            // 2. Mock jQuery.ajax if jQuery loads
            function setupJQueryMock() {{
                if (typeof jQuery !== 'undefined') {{
                    const originalAjax = jQuery.ajax;
                    jQuery.ajax = function(options) {{
                        let url = options.url || options;
                        const content = findCachedContent(url);
                        if (content !== null) {{
                            const mockXHR = {{
                                status: 200,
                                responseText: content,
                                getResponseHeader: function(header) {{
                                    return null;
                                }},
                                getAllResponseHeaders: function() {{
                                    return "";
                                }}
                            }};
                            let d = jQuery.Deferred();
                            d.resolve(content, "success", mockXHR);
                            if (options.success) {{
                                options.success(content, "success", mockXHR);
                            }}
                            return d.promise();
                        }}
                        return originalAjax.apply(this, arguments);
                    }};
                    console.log("DOMjudge scoreboard offline AJAX cache initialized.");
                }} else {{
                    // Try again in 50ms if jQuery is still loading
                    setTimeout(setupJQueryMock, 50);
                }}
            }}
            setupJQueryMock();
        }})();
        """
        
        script = soup.new_tag("script")
        script.string = js
        if soup.body:
            soup.body.append(script)
        else:
            soup.append(script)

        # Set up the offline search/filter logic
        self._setup_offline_filter(soup, page_url, ajax_cache)

    def _setup_offline_filter(self, soup: BeautifulSoup, page_url: str, dynamic_team_pages: dict) -> None:
        """Parse category/affiliation mappings for teams and inject the offline filter JS.
        """
        category_options = {}
        affiliation_options = {}
        for select in soup.find_all("select"):
            s_id = (select.get("id") or "").lower()
            s_name = (select.get("name") or "").lower()
            target_map = None
            if "category" in s_id or "categor" in s_name or "category" in s_name:
                target_map = category_options
            elif "affil" in s_id or "affil" in s_name:
                target_map = affiliation_options
            
            if target_map is not None:
                for option in select.find_all("option"):
                    val = option.get("value")
                    text = option.get_text(strip=True)
                    if val and text:
                        target_map[text] = str(val)

        team_filter_metadata = {}

        def parse_modal_details(modal_soup):
            cat_name = None
            aff_name = None
            for row in modal_soup.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if th and td:
                    label = th.get_text(strip=True).lower()
                    val = td.get_text(strip=True)
                    if "category" in label or "類別" in label:
                        cat_name = val
                    elif "affiliation" in label or "機構" in label or "學校" in label:
                        aff_name = val
            return cat_name, aff_name

        # 1. Extract from dynamically fetched team modal pages
        for team_url, html_content in dynamic_team_pages.items():
            if "/submissions-data/" in team_url:
                continue
            match = re.search(r"/team/(\d+)(?:[/?#]|$)", team_url)
            if not match or not html_content or html_content.startswith("{"):
                continue
            team_id = match.group(1)
            modal_soup = BeautifulSoup(html_content, "html.parser")
            cat_name, aff_name = parse_modal_details(modal_soup)
            
            meta = {}
            if cat_name and cat_name in category_options:
                meta["category"] = category_options[cat_name]
            if aff_name and aff_name in affiliation_options:
                meta["affiliation"] = affiliation_options[aff_name]
            if meta:
                team_filter_metadata[team_id] = meta

        # 2. Extract from statically inlined modals in the main HTML
        for modal in soup.find_all(id=lambda x: x and x.startswith("team-modal-")):
            modal_id = modal.get("id")
            team_id = modal_id.split("-")[-1]
            cat_name, aff_name = parse_modal_details(modal)
            
            meta = {}
            if cat_name and cat_name in category_options:
                meta["category"] = category_options[cat_name]
            if aff_name and aff_name in affiliation_options:
                meta["affiliation"] = affiliation_options[aff_name]
            if meta:
                if team_id not in team_filter_metadata:
                    team_filter_metadata[team_id] = {}
                team_filter_metadata[team_id].update(meta)

        # 3. Fallback extraction directly from scoreboard table rows
        for tr in soup.find_all("tr", attrs={"data-team-id": True}):
            team_id = tr.get("data-team-id")
            if not team_id:
                continue
            meta = team_filter_metadata.setdefault(team_id, {})
            if "affiliation" not in meta:
                univ_span = tr.find(class_="univ")
                if univ_span:
                    aff_text = univ_span.get_text(strip=True)
                    if aff_text in affiliation_options:
                        meta["affiliation"] = affiliation_options[aff_text]

        if team_filter_metadata or category_options or affiliation_options:
            js = f"""
            (function() {{
                const teamFilterMetadata = {json.dumps(team_filter_metadata)};
                
                function updateScoreboardSummary() {{
                    const scoreboardTables = document.querySelectorAll("table.scoreboard");
                    scoreboardTables.forEach(scoreboardTable => {{
                        const tbody = scoreboardTable.querySelector("tbody");
                        if (!tbody) return;
                        
                        const rows = Array.from(tbody.querySelectorAll("tr")).filter(r => r.hasAttribute("data-team-id"));
                        const visibleRows = rows.filter(r => r.style.display !== "none");
                        
                        const summaryRow = Array.from(tbody.querySelectorAll("tr")).find(r => r.querySelector(".scoresummary"));
                        if (!summaryRow) return;
                        
                        const totalSolvedCell = summaryRow.querySelector(".scorenc");
                        let globalTotalSolved = 0;
                        visibleRows.forEach(row => {{
                            const solvedCell = row.querySelector(".scorenc");
                            if (solvedCell) {{
                                globalTotalSolved += parseInt(solvedCell.textContent.trim()) || 0;
                            }}
                        }});
                        if (totalSolvedCell) {{
                            totalSolvedCell.textContent = globalTotalSolved;
                        }}
                        
                        const firstTeamRow = rows[0];
                        if (!firstTeamRow) return;
                        
                        const problemCellIndices = [];
                        Array.from(firstTeamRow.cells).forEach((cell, idx) => {{
                            if (cell.classList.contains("score_cell")) {{
                                problemCellIndices.push(idx);
                            }}
                        }});
                        
                        const summaryCells = Array.from(summaryRow.cells);
                        const numProblems = problemCellIndices.length;
                        const summaryCellsCount = summaryCells.length;
                        
                        for (let p = 0; p < numProblems; p++) {{
                            const summaryCell = summaryCells[summaryCellsCount - numProblems + p];
                            const teamCellIndex = problemCellIndices[p];
                            
                            let accepted = 0;
                            let rejected = 0;
                            let pending = 0;
                            let firstSolveTime = Infinity;
                            
                            visibleRows.forEach(row => {{
                                const cell = row.cells[teamCellIndex];
                                if (!cell) return;
                                
                                const correctDiv = cell.querySelector(".score_correct");
                                if (correctDiv) {{
                                    accepted += 1;
                                    const span = correctDiv.querySelector("span");
                                    if (span) {{
                                        const triesMatch = span.textContent.match(/(\\d+)\\s+tr/);
                                        if (triesMatch) {{
                                            const totalTries = parseInt(triesMatch[1]) || 1;
                                            rejected += (totalTries - 1);
                                        }}
                                    }}
                                    let timeStr = "";
                                    correctDiv.childNodes.forEach(node => {{
                                        if (node.nodeType === Node.TEXT_NODE) {{
                                            timeStr += node.textContent;
                                        }}
                                    }});
                                    const time = parseInt(timeStr.trim());
                                    if (!isNaN(time) && time < firstSolveTime) {{
                                        firstSolveTime = time;
                                    }}
                                }} else {{
                                    const incorrectDiv = cell.querySelector(".score_incorrect");
                                    if (incorrectDiv) {{
                                        const span = incorrectDiv.querySelector("span");
                                        if (span) {{
                                            const triesMatch = span.textContent.match(/(\\d+)\\s+tr/);
                                            if (triesMatch) {{
                                                rejected += parseInt(triesMatch[1]) || 0;
                                            }}
                                        }}
                                    }} else {{
                                        const pendingDiv = cell.querySelector(".score_pending");
                                        if (pendingDiv) {{
                                            const span = pendingDiv.querySelector("span");
                                            if (span) {{
                                                const triesMatch = span.textContent.match(/(\\d+)\\s+tr/);
                                                if (triesMatch) {{
                                                    pending += parseInt(triesMatch[1]) || 0;
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }});
                            
                            const correctSpan = summaryCell.querySelector(".submcorrect");
                            const rejectSpan = summaryCell.querySelector(".submreject");
                            const pendSpan = summaryCell.querySelector(".submpend");
                            
                            const anchor = summaryCell.querySelector("a");
                            let timeSpan = null;
                            if (anchor) {{
                                const spans = anchor.querySelectorAll("span");
                                if (spans.length >= 4) {{
                                    timeSpan = spans[3];
                                }} else {{
                                    timeSpan = Array.from(spans).find(s => s.getAttribute("title") === "first solved" || s.textContent.includes("min") || s.textContent === "n/a");
                                }}
                            }}
                            
                            if (correctSpan) correctSpan.textContent = accepted;
                            if (rejectSpan) rejectSpan.textContent = rejected;
                            if (pendSpan) pendSpan.textContent = pending;
                            
                            if (timeSpan) {{
                                if (accepted > 0 && firstSolveTime !== Infinity) {{
                                    timeSpan.textContent = firstSolveTime + "min";
                                }} else {{
                                    timeSpan.textContent = "n/a";
                                }}
                            }}
                        }}
                    }});
                }}

                function initOfflineFilter() {{
                    const filterForm = document.querySelector(".filterbox")?.closest("form") || 
                                       document.querySelector("form:has(.filterbox)") || 
                                       document.querySelector(".filterbox") || 
                                       document.querySelector("form");
                    if (!filterForm) return;

                    const categorySelect = document.getElementById("scoreboard-filter-category") || 
                                           document.querySelector("select[name*='categor']");
                    const affiliationSelect = document.getElementById("scoreboard-filter-affil") || 
                                              document.getElementById("scoreboard-filter-affiliation") || 
                                              document.querySelector("select[name*='affil']");

                    let lastClickedAction = "filter";
                    filterForm.querySelectorAll("input[type='submit'], button[type='submit']").forEach(btn => {{
                        btn.addEventListener("click", function(e) {{
                            lastClickedAction = (this.value || this.name || "").toLowerCase();
                        }});
                    }});

                    function applyFilter(isClear) {{
                        if (isClear) {{
                            if (categorySelect) {{
                                Array.from(categorySelect.options).forEach(o => o.selected = false);
                            }}
                            if (affiliationSelect) {{
                                Array.from(affiliationSelect.options).forEach(o => o.selected = false);
                            }}
                        }}

                        const selectedCategories = (categorySelect && !isClear)
                            ? Array.from(categorySelect.selectedOptions).map(o => String(o.value))
                            : [];
                        const selectedAffiliations = (affiliationSelect && !isClear)
                            ? Array.from(affiliationSelect.selectedOptions).map(o => String(o.value))
                            : [];

                        const rows = document.querySelectorAll("table.scoreboard tbody tr[data-team-id]");
                        rows.forEach(row => {{
                            const teamId = row.getAttribute("data-team-id");
                            const meta = teamFilterMetadata[teamId];

                            let show = true;
                            if (selectedCategories.length > 0) {{
                                if (!meta || !selectedCategories.includes(String(meta.category))) {{
                                    show = false;
                                }}
                            }}
                            if (selectedAffiliations.length > 0) {{
                                if (!meta || !selectedAffiliations.includes(String(meta.affiliation))) {{
                                    show = false;
                                }}
                            }}

                            row.style.display = show ? "" : "none";
                        }});

                        updateScoreboardSummary();

                        // Close dropdown
                        const dropdownToggle = document.getElementById("filter-toggle");
                        if (dropdownToggle && typeof bootstrap !== 'undefined' && bootstrap.Dropdown) {{
                            try {{
                                const dropdown = bootstrap.Dropdown.getOrCreateInstance(dropdownToggle);
                                dropdown.hide();
                            }} catch(e) {{}}
                        }}
                        const dropdownMenu = filterForm.closest(".dropdown-menu") || document.querySelector(".dropdown-menu.show");
                        if (dropdownMenu) {{
                            dropdownMenu.classList.remove("show");
                        }}
                        if (dropdownToggle) {{
                            dropdownToggle.classList.remove("show");
                            dropdownToggle.setAttribute("aria-expanded", "false");
                        }}
                    }}

                    filterForm.addEventListener("submit", function(e) {{
                        e.preventDefault();
                        e.stopPropagation();
                        const submitter = e.submitter || document.activeElement;
                        const action = (submitter && (submitter.value || submitter.name || "").toLowerCase()) || lastClickedAction;
                        const isClear = action === "clear";
                        applyFilter(isClear);
                        return false;
                    }});

                    const clearBtn = filterForm.querySelector("input[value='clear'], button[value='clear'], input[name='clear'], button[name='clear']");
                    if (clearBtn) {{
                        clearBtn.addEventListener("click", function(e) {{
                            e.preventDefault();
                            e.stopPropagation();
                            applyFilter(true);
                            return false;
                        }});
                    }}
                }}

                if (document.readyState === "loading") {{
                    document.addEventListener("DOMContentLoaded", initOfflineFilter);
                }} else {{
                    initOfflineFilter();
                }}
            }})();
            """
            script = soup.new_tag("script")
            script.string = js
            if soup.body:
                soup.body.append(script)
            else:
                soup.append(script)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive a DOMjudge scoreboard using a headless browser, producing a single fully-inlined HTML file."
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
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Generate a diagnostics log and verify the offline layout in Playwright",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=32,
        help="Number of concurrent threads for network prefetching (default: 32)",
    )
    parser.add_argument(
        "--rate-limit",
        "--rps",
        type=float,
        default=None,
        help="Maximum requests per second limit (e.g. 20 for 20 req/s, default: unlimited)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Request timeout in seconds before waiting to retry (default: 5.0)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Seconds to wait before retrying a timed-out request (default: 5.0)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=999999,
        help="Maximum number of retries for timed-out requests (default: 999999)",
    )
    args = parser.parse_args()

    if not args.output:
        parsed = urlparse(args.url)
        domain = parsed.netloc or "scoreboard"
        safe = re.sub(r"[^\w\.-]", "_", domain)
        args.output = f"{safe}_scoreboard.html"

    archiver = DOMjudgeScoreboardArchiver(
        args.url,
        args.output,
        verify_ssl=not args.insecure,
        generate_diagnostics=args.diagnostics,
        concurrency=args.concurrency,
        rate_limit=args.rate_limit,
        timeout=args.timeout,
        retry_delay=args.retry_delay,
        max_retries=args.max_retries,
    )
    archiver.archive()

if __name__ == "__main__":
    main()
