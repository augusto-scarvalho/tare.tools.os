"""
Web Ingestion Service (Anti-Corruption Layer for Web & Documentation Extraction).
Implements a layered fallback:
1. Tier 1: Local Trafilatura (Ultra-fast, zero-cost, noise-free Markdown extraction).
2. Tier 2: Firecrawl Engine (Headless browser rendering, JS execution & Cloudflare bypass).
3. Tier 3: Resilient HTTP Stream with standard DOM parser fallback.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

# Force UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


@dataclass
class WebDocument:
    url: str
    title: str
    markdown: str
    raw_size_bytes: int
    clean_size_bytes: int
    reduction_pct: float
    method: str
    status: str
    error: Optional[str] = None
    final_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class WebReader:
    """Universal web content extractor with layered fallback."""

    MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024  # the fallback fetch reads at most this much; the caller bounds the text again

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    @classmethod
    def _plain_document(cls, url, text, final_url, content_type):
        """Raw docs, schemas and source files must not be interpreted as HTML."""
        if not isinstance(content_type, str):
            return None
        mime = content_type.split(';', 1)[0].strip().lower()
        if not ((mime.startswith('text/') and mime not in {'text/html'})
                or mime in {'application/json', 'application/yaml', 'application/toml', 'application/xml'}
                or mime.endswith('+json')):
            return None
        size = len(text.encode('utf-8'))
        return WebDocument(url=url, title=url, markdown=text, raw_size_bytes=size, clean_size_bytes=size,
            reduction_pct=0, method='http_text', status='OK', final_url=final_url)

    @classmethod
    def fetch_clean_markdown(cls, url: str, force_firecrawl: bool = False) -> WebDocument:
        """Extract clean markdown from a URL using layered fallback."""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        # 1. Tier 2 Direct (if requested)
        if force_firecrawl:
            return cls._fetch_via_firecrawl(url)

        # 2. Tier 1: Trafilatura (Local, Fast, Zero-Cost)
        try:
            import trafilatura

            response = trafilatura.fetch_response(url, decode=True, with_headers=True)
            if response and response.html and urlsplit(response.url or '').scheme in {'http', 'https'}:
                headers = response.headers or {}
                plain = cls._plain_document(url, response.html, response.url, headers.get('content-type', ''))
                if plain:
                    return plain
            # urllib3-backed Trafilatura can lose the effective host in geturl().
            # Do not invent it from the seed: a redirect may have changed hosts.
            # The urllib fallback below retains the absolute effective URL.
            if (response and response.html and len(response.html) > 100
                    and urlsplit(response.url or '').scheme in {'http', 'https'}):
                downloaded = response.html
                final_url = response.url
                extracted = trafilatura.extract(
                    downloaded,
                    output_format="markdown",
                    include_links=True,
                    include_tables=True,
                    include_images=False,
                    favor_recall=True,
                )
                metadata = trafilatura.extract_metadata(downloaded)
                title = metadata.title if (metadata and metadata.title) else cls._extract_fallback_title(downloaded, url)

                if extracted and len(extracted.strip()) > 50:
                    raw_len = len(downloaded)
                    clean_len = len(extracted)
                    reduction = round((1 - (clean_len / max(raw_len, 1))) * 100, 1)

                    return WebDocument(
                        url=url,
                        title=title or "Sem Título",
                        markdown=extracted.strip(),
                        raw_size_bytes=raw_len,
                        clean_size_bytes=clean_len,
                        reduction_pct=reduction,
                        method="trafilatura_local",
                        status="OK",
                        final_url=final_url,
                    )
        except Exception as e:
            # Trafilatura failure, proceed to Tier 2
            pass

        # 3. Tier 2: Firecrawl API (Headless JS / Bot Bypass)
        firecrawl_key = os.environ.get("FIRECRAWL_API_KEY")
        if firecrawl_key:
            doc = cls._fetch_via_firecrawl(url, api_key=firecrawl_key)
            if doc.status == "OK":
                return doc

        # 4. Tier 3: Resilient HTTP Fetch + Trafilatura Parsing
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": cls.USER_AGENT, "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_html = resp.read(cls.MAX_DOWNLOAD_BYTES).decode("utf-8", errors="replace")
                final_url = resp.geturl()
                headers = getattr(resp, 'headers', None)
                content_type = headers.get('Content-Type', '') if headers is not None else ''
            plain = cls._plain_document(url, raw_html, final_url, content_type)
            if plain:
                return plain

            import trafilatura

            extracted = trafilatura.extract(
                raw_html,
                output_format="markdown",
                include_links=True,
                include_tables=True,
                favor_recall=True,
            )
            if extracted and len(extracted.strip()) > 30:
                raw_len = len(raw_html)
                clean_len = len(extracted)
                reduction = round((1 - (clean_len / max(raw_len, 1))) * 100, 1)
                return WebDocument(
                    url=url,
                    title=cls._extract_fallback_title(raw_html, url),
                    markdown=extracted.strip(),
                    raw_size_bytes=raw_len,
                    clean_size_bytes=clean_len,
                    reduction_pct=reduction,
                    method="urllib_trafilatura_fallback",
                    status="OK",
                    final_url=final_url,
                )
        except Exception as err:
            return WebDocument(
                url=url,
                title="Erro de Ingestão",
                markdown="",
                raw_size_bytes=0,
                clean_size_bytes=0,
                reduction_pct=0.0,
                method="failed",
                status="ERROR",
                error=f"Falha ao extrair URL via Trafilatura e Firecrawl: {err}",
            )

        return WebDocument(
            url=url,
            title="Conteúdo Vazio",
            markdown="",
            raw_size_bytes=0,
            clean_size_bytes=0,
            reduction_pct=0.0,
            method="empty",
            status="EMPTY",
            error="Nenhum conteúdo textual pôde ser extraído da página.",
        )

    @classmethod
    def _fetch_via_firecrawl(cls, url: str, api_key: Optional[str] = None) -> WebDocument:
        """Call Firecrawl SDK for headless rendering."""
        try:
            from firecrawl import FirecrawlApp

            app = FirecrawlApp(api_key=api_key or os.environ.get("FIRECRAWL_API_KEY"))
            scrape_result = app.scrape_url(url, params={"formats": ["markdown"]})

            if scrape_result and "markdown" in scrape_result:
                md = scrape_result["markdown"]
                title = scrape_result.get("metadata", {}).get("title", url)
                raw_len = len(md) * 3  # Estimated raw DOM
                clean_len = len(md)
                return WebDocument(
                    url=url,
                    title=title,
                    markdown=md.strip(),
                    raw_size_bytes=raw_len,
                    clean_size_bytes=clean_len,
                    reduction_pct=66.7,
                    method="firecrawl_api",
                    status="OK",
                )
        except Exception as e:
            return WebDocument(
                url=url,
                title="Erro Firecrawl",
                markdown="",
                raw_size_bytes=0,
                clean_size_bytes=0,
                reduction_pct=0.0,
                method="firecrawl_failed",
                status="ERROR",
                error=str(e),
            )
        return WebDocument(
            url=url,
            title="Erro Firecrawl",
            markdown="",
            raw_size_bytes=0,
            clean_size_bytes=0,
            reduction_pct=0.0,
            method="firecrawl_empty",
            status="EMPTY",
        )

    @staticmethod
    def _extract_fallback_title(html: str, default: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return default
