#!/usr/bin/env python3
"""
DOMjudge Scoreboard Archiver
Downloads a DOMjudge scoreboard from a URL and packages it along with all
referenced stylesheets, team flags (images), and FontAwesome webfonts (for medals,
thumbs-up, clock, heart, and question-circle icons) into a single self-contained MHTML file.
"""

import argparse
import base64
import email
import email.utils
import mimetypes
import os
import re
import sys
import uuid
import quopri
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# Standard browser headers to prevent blocks
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

def get_content_type(url: str, server_content_type: str = None) -> str:
    """
    Determine the Content-Type of an asset from its URL extension,
    falling back to the server-reported Content-Type or a generic fallback.
    """
    path = url.split('?')[0].split('#')[0].lower()
    
    # Hardcoded mapping for web standards that are frequently misconfigured
    if path.endswith('.svg'):
        return 'image/svg+xml'
    elif path.endswith('.woff2'):
        return 'font/woff2'
    elif path.endswith('.woff'):
        return 'font/woff'
    elif path.endswith('.ttf'):
        return 'font/ttf'
    elif path.endswith('.otf'):
        return 'font/otf'
    elif path.endswith('.eot'):
        return 'application/vnd.ms-fontobject'
    elif path.endswith('.css'):
        return 'text/css'
    elif path.endswith('.js'):
        return 'application/javascript'
    elif path.endswith('.png'):
        return 'image/png'
    elif path.endswith('.jpg') or path.endswith('.jpeg'):
        return 'image/jpeg'
    elif path.endswith('.gif'):
        return 'image/gif'
    elif path.endswith('.ico'):
        return 'image/x-icon'
    elif path.endswith('.html') or path.endswith('.htm'):
        return 'text/html'
        
    # Use server's content type if it exists and isn't generic
    if server_content_type:
        clean_server_ct = server_content_type.split(';')[0].strip().lower()
        if clean_server_ct not in ('', 'application/octet-stream', 'text/plain'):
            return server_content_type
            
    # Fall back using standard mimetypes guess
    guessed, _ = mimetypes.guess_type(path)
    return guessed or 'application/octet-stream'

class ScoreboardArchiver:
    def __init__(self, start_url: str, output_path: str, verify_ssl: bool = True):
        self.start_url = start_url
        self.output_path = output_path
        self.verify_ssl = verify_ssl
        
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.verify = verify_ssl
        
        if not verify_ssl:
            # Suppress SSL warnings if insecure is chosen
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
            
        self.assets = {}  # maps absolute URL -> (content_bytes, content_type)
        self.fetched_urls = set()

    def parse_css_for_assets(self, css_content: str, css_url: str):
        """
        Scan CSS text to locate font and image assets referenced in url(...) rules
        and queue them for downloading.
        """
        # Find all url(...) declarations
        pattern = r"url\((['\"]?)([^'\")]+)\1\)"
        matches = re.findall(pattern, css_content)
        for _, rel_url in matches:
            rel_url = rel_url.strip()
            # Skip data URIs
            if rel_url.lower().startswith('data:'):
                continue
            
            # Resolve to absolute URL
            abs_url = urljoin(css_url, rel_url)
            self.fetch_asset(abs_url)

    def fetch_asset(self, url: str):
        """
        Download an asset from the URL and store it.
        If it's a CSS file, parse it recursively for nested assets.
        """
        # Remove fragment/hash from URL for downloading and storage
        parsed = urlparse(url)
        normalized_url = parsed._replace(fragment='').geturl()
        
        if normalized_url in self.fetched_urls:
            return
            
        self.fetched_urls.add(normalized_url)
        print(f"Downloading asset: {normalized_url}")
        
        try:
            r = self.session.get(normalized_url, timeout=15)
            if r.status_code == 200:
                content_type = get_content_type(normalized_url, r.headers.get('Content-Type'))
                
                # If it's a CSS file, parse it recursively for fonts/images
                if content_type.startswith('text/css') or normalized_url.split('?')[0].endswith('.css'):
                    css_text = r.text
                    self.assets[normalized_url] = (r.content, 'text/css')
                    self.parse_css_for_assets(css_text, normalized_url)
                else:
                    self.assets[normalized_url] = (r.content, content_type)
            else:
                print(f"Warning: Failed to download asset {normalized_url} (HTTP {r.status_code})")
        except Exception as e:
            print(f"Warning: Error downloading asset {normalized_url}: {e}")

    def archive(self):
        """
        Main runner: fetches the page, discovers and downloads all dependencies,
        compiles them into MHTML, and saves the output file.
        """
        print(f"Fetching scoreboard page: {self.start_url}")
        try:
            response = self.session.get(self.start_url, timeout=20)
            response.raise_for_status()
        except Exception as e:
            print(f"Error fetching scoreboard page {self.start_url}: {e}", file=sys.stderr)
            sys.exit(1)
            
        actual_url = response.url
        html_content = response.text
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Determine contest title
        title = "DOMjudge Scoreboard"
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        print(f"Detected title: {title}")
        
        # Modify HTML image loading to eagerly load everything (prevents issues with lazy flags offline)
        for img in soup.find_all('img'):
            img['loading'] = 'eager'
            
        # Discover and fetch stylesheets
        for link in soup.find_all('link', rel=lambda x: x and 'stylesheet' in x):
            href = link.get('href')
            if href:
                abs_url = urljoin(actual_url, href)
                self.fetch_asset(abs_url)
                
        # Discover and fetch images
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                abs_url = urljoin(actual_url, src)
                self.fetch_asset(abs_url)
                
        # Discover and fetch favicon
        for link in soup.find_all('link', rel=lambda x: x and ('icon' in x or 'shortcut' in x)):
            href = link.get('href')
            if href:
                abs_url = urljoin(actual_url, href)
                self.fetch_asset(abs_url)
                
        # Discover and fetch scripts
        for script in soup.find_all('script'):
            src = script.get('src')
            if src:
                abs_url = urljoin(actual_url, src)
                self.fetch_asset(abs_url)
                
        # Get the modified HTML string
        final_html = str(soup)
        
        # Build MHTML structure manually to prevent library line-folding from corrupting boundary fields in Chrome
        print("\nCompiling assets into MHTML...")
        boundary = f"----MultipartBoundary--{uuid.uuid4().hex.upper()}----"
        
        mhtml_parts = []
        mhtml_parts.append(b"From: <Saved by Blink>")
        mhtml_parts.append(f"Snapshot-Content-Location: {actual_url}".encode('utf-8'))
        mhtml_parts.append(f"Subject: {title}".encode('utf-8'))
        mhtml_parts.append(f"Date: {email.utils.formatdate(localtime=True)}".encode('utf-8'))
        mhtml_parts.append(b"MIME-Version: 1.0")
        mhtml_parts.append(f'Content-Type: multipart/related; type="text/html"; boundary="{boundary}"'.encode('utf-8'))
        mhtml_parts.append(b"") # Empty line before first boundary
        
        # 1. HTML Part
        mhtml_parts.append(f"--{boundary}".encode('utf-8'))
        mhtml_parts.append(b'Content-Type: text/html; charset="utf-8"')
        mhtml_parts.append(b"Content-Transfer-Encoding: quoted-printable")
        mhtml_parts.append(f"Content-Location: {actual_url}".encode('utf-8'))
        mhtml_parts.append(b"")
        
        qp_html = quopri.encodestring(final_html.encode('utf-8'))
        # Normalize line breaks to CRLF
        qp_html = qp_html.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        mhtml_parts.append(qp_html)
        
        # 2. Assets (CSS, JS, Fonts, Images)
        for asset_url, (content_bytes, content_type) in self.assets.items():
            mhtml_parts.append(f"--{boundary}".encode('utf-8'))
            mhtml_parts.append(f"Content-Type: {content_type}".encode('utf-8'))
            
            # Clean Content-Type to check main type
            clean_ct = content_type.split(';')[0].strip().lower()
            maintype = clean_ct.split('/')[0].strip() if '/' in clean_ct else 'application'
            
            if maintype == 'text':
                mhtml_parts.append(b"Content-Transfer-Encoding: quoted-printable")
                mhtml_parts.append(f"Content-Location: {asset_url}".encode('utf-8'))
                mhtml_parts.append(b"")
                qp_asset = quopri.encodestring(content_bytes)
                qp_asset = qp_asset.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                mhtml_parts.append(qp_asset)
            else:
                mhtml_parts.append(b"Content-Transfer-Encoding: base64")
                mhtml_parts.append(f"Content-Location: {asset_url}".encode('utf-8'))
                mhtml_parts.append(b"")
                b64_str = base64.b64encode(content_bytes).decode('ascii')
                wrapped_b64 = '\r\n'.join(b64_str[i:i+76] for i in range(0, len(b64_str), 76))
                mhtml_parts.append(wrapped_b64.encode('ascii'))
                
        # End Boundary
        mhtml_parts.append(f"--{boundary}--".encode('utf-8'))
        mhtml_parts.append(b"")
        
        mhtml_bytes = b"\r\n".join(mhtml_parts)
        
        # Write out the file
        print(f"Writing MHTML output to: {self.output_path}")
        try:
            with open(self.output_path, 'wb') as f:
                f.write(mhtml_bytes)
            print(f"\nSuccess! Archive completed and saved to: {os.path.abspath(self.output_path)}")
        except Exception as e:
            print(f"Error saving MHTML file: {e}", file=sys.stderr)
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="Archive a DOMjudge scoreboard into a single, offline-ready MHTML file."
    )
    parser.add_argument(
        '--url',
        required=True,
        help="The scoreboard URL (e.g., https://taichung2025.icpc.tw/)"
    )
    parser.add_argument(
        '--output',
        help="Path to the output MHTML file (defaults to sanitized_domain_scoreboard.mhtml)"
    )
    parser.add_argument(
        '--insecure',
        action='store_true',
        help="Disable SSL certificate validation (useful for internal contest server setups)"
    )
    
    args = parser.parse_args()
    
    # Generate default output filename if not specified
    if not args.output:
        parsed_url = urlparse(args.url)
        domain = parsed_url.netloc or "scoreboard"
        # Sanitize domain name for Windows/Unix filenames
        domain_clean = re.sub(r'[^\w\.-]', '_', domain)
        args.output = f"{domain_clean}_scoreboard.mhtml"
        
    # Run the archiver
    archiver = ScoreboardArchiver(args.url, args.output, verify_ssl=not args.insecure)
    archiver.archive()

if __name__ == '__main__':
    main()
