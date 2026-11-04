"""
Tests for WebReader (Trafilatura & Firecrawl Anti-Corruption Layer).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from relay.web_reader import WebDocument, WebReader


@pytest.mark.parametrize('mime', ['text/plain', 'text/markdown; charset=utf-8', 'application/json'])
def test_plain_sources_keep_literal_markup_and_parameters_instead_of_losing_evidence(mime):
    from types import SimpleNamespace
    source = '.. note::\ninclude_links=True preserves links.\nLiteral <table> and <body> are source text.\n'
    response = SimpleNamespace(html=source, url='https://example.org/source.rst', headers={'content-type': mime})
    with patch('trafilatura.fetch_response', return_value=response), patch('trafilatura.extract') as extract:
        doc = WebReader.fetch_clean_markdown(response.url)
    assert doc.status == 'OK' and doc.method == 'http_text'
    assert doc.markdown == source and 'include_links=True' in doc.markdown
    extract.assert_not_called()


def test_web_document_dataclass():
    doc = WebDocument(
        url="https://example.com",
        title="Example Title",
        markdown="# Header\n\nContent",
        raw_size_bytes=1000,
        clean_size_bytes=200,
        reduction_pct=80.0,
        method="trafilatura_local",
        status="OK",
    )
    d = doc.to_dict()
    assert d["url"] == "https://example.com"
    assert d["reduction_pct"] == 80.0
    assert d["status"] == "OK"


def test_trafilatura_extraction_mock():
    html_mock = """
    <html>
      <head><title>Test Article</title></head>
      <body>
        <nav><a href="/">Home</a></nav>
        <article>
          <h1>Test Article Header</h1>
          <p>This is the essential text content that should be extracted cleanly without navigation noise.</p>
        </article>
        <footer><p>Copyright 2026</p></footer>
      </body>
    </html>
    """
    mock_response = MagicMock()
    mock_response.html = html_mock
    mock_response.url = "https://test.local/article"
    with patch("trafilatura.fetch_response", return_value=mock_response):
        doc = WebReader.fetch_clean_markdown("https://test.local/article")
        assert doc.status == "OK"
        assert doc.method == "trafilatura_local"
        assert "essential text content" in doc.markdown
        assert doc.clean_size_bytes < doc.raw_size_bytes
        assert doc.reduction_pct > 0


def test_firecrawl_fallback_when_forced():
    with patch("firecrawl.FirecrawlApp") as mock_app_cls:
        mock_instance = MagicMock()
        mock_instance.scrape_url.return_value = {
            "markdown": "# Firecrawl Rendered\n\nDynamic React Content",
            "metadata": {"title": "SPA App"}
        }
        mock_app_cls.return_value = mock_instance

        doc = WebReader.fetch_clean_markdown("https://spa.local/app", force_firecrawl=True)
        assert doc.status == "OK"
        assert doc.method == "firecrawl_api"
        assert "Dynamic React Content" in doc.markdown
        assert doc.title == "SPA App"
