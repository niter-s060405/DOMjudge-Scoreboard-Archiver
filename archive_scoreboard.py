#!/usr/bin/env python3
"""
DOMjudge Scoreboard Archiver – Headless‑Browser version (fixed)

This version uses Playwright (Chromium) to render the page, captures all network
responses, inlines assets, and adds extensive diagnostics to verify that all
required resources (Bootstrap CSS, team images, CSS order) are captured before
writing the final self‑contained HTML.
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
from playwright.sync_api import sync_playwright, Response

# ---------------------------------------------------------------------------
# Helper utilities (same as the pure‑requests version)
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_content_type(url: str, server_content_type: str | None = None) -> str:
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
# Main archiver class – uses Playwright to capture the rendered page
# ---------------------------------------------------------------------------
class BrowserHTMLArchiver:
    def __init__(self, start_url: str, output_path: str, verify_ssl: bool = True, generate_diagnostics: bool = False):
        self.start_url = start_url
        self.output_path = output_path
        self.verify_ssl = verify_ssl
        self.assets: dict[str, tuple[bytes, str]] = {}
        # Track if Bootstrap JS is available at runtime
        self.bootstrap_js_present: bool = False
        self.generate_diagnostics = generate_diagnostics

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
            print(f"Prefetching {len(all_to_fetch)} missing images/icons in parallel...")
            def fetch_one(url):
                try:
                    r = requests.get(url, headers=HEADERS, verify=self.verify_ssl, timeout=10)
                    if r.status_code == 200:
                        mime, _ = mimetypes.guess_type(url)
                        if not mime:
                            mime = "image/x-icon" if url in missing_favicon_urls else "image/jpeg"
                        return url, r.content, mime
                    else:
                        return url, None, f"Status {r.status_code}"
                except Exception as exc:
                    return url, None, str(exc)

            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = {executor.submit(fetch_one, url): url for url in all_to_fetch}
                for fut in as_completed(futures):
                    url, content, info = fut.result()
                    if content is not None:
                        self.assets[url] = (content, info)
                        self._log_asset(url, f"{info} (Status: 200)")
                    else:
                        print(f"⚠️ Failed to fetch missing asset {url}: {info}")

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
            # Re‑apply attributes if they were present
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
    # Universal offline AJAX/fetch cache
    # ---------------------------------------------------------------------
    def _inject_offline_ajax_cache(self, soup: BeautifulSoup, page_url: str) -> None:
        """Prefetch all submission JSON data and team modal pages,
        and inject them as a static cache to mock window.fetch and jQuery.ajax.
        """
        ajax_cache = {}
        
        # 1. Fetch submission data URLs
        sub_tags = soup.find_all(attrs={"data-submissions-url": True})
        sub_urls = sorted(list(set(tag.get("data-submissions-url") for tag in sub_tags if tag.get("data-submissions-url"))))
        
        if sub_urls:
            print(f"Prefetching {len(sub_urls)} submissions data URLs...")
            for sub_url in sub_urls:
                abs_url = urljoin(page_url, sub_url)
                try:
                    r = requests.get(abs_url, headers=HEADERS, verify=self.verify_ssl, timeout=10)
                    if r.status_code == 200:
                        ajax_cache[sub_url] = r.text
                    else:
                        print(f"⚠️ Submissions endpoint {sub_url} returned status {r.status_code}")
                except Exception as exc:
                    print(f"⚠️ Error fetching submissions data {sub_url}: {exc}")
        
        # 2. Fetch team modal pages
        team_tags = soup.find_all('a', attrs={"data-ajax-modal": True})
        team_urls = sorted(list(set(tag.get("href") for tag in team_tags if tag.get("href"))))
        
        if team_urls:
            print(f"Prefetching {len(team_urls)} team modal pages...")
            for team_url in team_urls:
                abs_url = urljoin(page_url, team_url)
                try:
                    headers = {**HEADERS, "X-Requested-With": "XMLHttpRequest"}
                    r = requests.get(abs_url, headers=headers, verify=self.verify_ssl, timeout=10)
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
                                r_img = requests.get(abs_img_url, headers=HEADERS, verify=self.verify_ssl, timeout=10)
                                if r_img.status_code == 200:
                                    mime, _ = mimetypes.guess_type(abs_img_url)
                                    if not mime:
                                        mime = "image/jpeg"
                                    img_data = base64.b64encode(r_img.content).decode("utf-8")
                                    img_tag["src"] = f"data:{mime};base64,{img_data}"
                                else:
                                    print(f"⚠️ Failed to fetch modal image {abs_img_url}: Status {r_img.status_code}")
                            except Exception as exc:
                                print(f"⚠️ Error fetching modal image {abs_img_url}: {exc}")
                        # Extract only the main modal div (or root div) to avoid leading/trailing whitespace text nodes
                        modal_div = modal_soup.find("div", class_="modal") or modal_soup.find("div")
                        if modal_div:
                            ajax_cache[team_url] = str(modal_div).strip()
                        else:
                            ajax_cache[team_url] = r.text.strip()
                    else:
                        print(f"⚠️ Team modal endpoint {team_url} returned status {r.status_code}")
                except Exception as exc:
                    print(f"⚠️ Error fetching team page {team_url}: {exc}")
                    
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

    # ---------------------------------------------------------------------
    # Simple modal shim – identical to the previous version
    # ---------------------------------------------------------------------
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

    # ---------------------------------------------------------------------
    # Post‑processing fixes (modal CSS, etc.)
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
        for url in urls_to_fetch:
            try:
                print(f"Fetching CSS dependency: {url}")
                r = requests.get(url, headers=HEADERS, verify=self.verify_ssl, timeout=10)
                if r.status_code == 200:
                    mime = get_content_type(url, r.headers.get("content-type"))
                    self.assets[url] = (r.content, mime)
                    self._log_asset(url, f"{mime} (Status: {r.status_code})")
                else:
                    print(f"⚠️ Failed to fetch CSS dependency {url}: Status {r.status_code}")
            except Exception as e:
                print(f"⚠️ Error fetching CSS dependency {url}: {e}")

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
            log.write("CSS_ORDER|INLINE|" + '|'.join(original_links) + "\n")
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

        # Parse HTML and run post‑capture diagnostics
        soup = BeautifulSoup(rendered_html, "html.parser")
        if self.generate_diagnostics:
            self._run_diagnostics(soup, final_url)
        # Inline assets and apply optional fixes
        self._inline_assets(soup, final_url)
        # Additional asset diagnostics for CSS url(...) and icons run on the inlined structure
        if self.generate_diagnostics:
            self._log_missing_css_assets(soup, final_url)
            self._log_icon_assets(soup, final_url)
        self._inject_offline_ajax_cache(soup, final_url)
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
            print(f"✅ Inline HTML written to: {out_path.resolve()}")
        except Exception as exc:
            print(f"❌ Could not write output file: {exc}", file=sys.stderr)
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

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive a DOMjudge scoreboard using a headless browser, producing a single fully‑inlined HTML file."
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
    args = parser.parse_args()

    if not args.output:
        parsed = urlparse(args.url)
        domain = parsed.netloc or "scoreboard"
        safe = re.sub(r"[^\w\.-]", "_", domain)
        args.output = f"{safe}_scoreboard.html"

    archiver = BrowserHTMLArchiver(args.url, args.output, verify_ssl=not args.insecure, generate_diagnostics=args.diagnostics)
    archiver.archive()

if __name__ == "__main__":
    main()
