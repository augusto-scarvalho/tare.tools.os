#!/usr/bin/env python3
"""
CLI Tool: Ingest and clean web URLs using Trafilatura & Firecrawl.
Usage:
    python scripts/fetch_clean_url.py <URL> [--output file.md] [--json] [--firecrawl]
"""

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from relay.web_reader import WebReader


def main():
    parser = argparse.ArgumentParser(
        description="Extract clean Markdown content from URLs using Trafilatura and Firecrawl."
    )
    parser.add_argument("url", help="URL of the article, documentation or web page to extract")
    parser.add_argument("--output", "-o", help="Path to save the extracted Markdown")
    parser.add_argument("--json", action="store_true", help="Return structured JSON output")
    parser.add_argument("--firecrawl", action="store_true", help="Force extraction through the Firecrawl engine")

    args = parser.parse_args()

    doc = WebReader.fetch_clean_markdown(args.url, force_firecrawl=args.firecrawl)

    if args.json:
        print(json.dumps(doc.to_dict(), indent=2, ensure_ascii=False))
        return

    if doc.status != "OK":
        print(f"❌ [ERROR]: {doc.error or 'Failed to extract the page'}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(doc.markdown, encoding="utf-8")
        print(f"✅ Content saved to: {out_path}")
        print(f"   • Title: {doc.title}")
        print(f"   • Method: {doc.method}")
        print(f"   • Noise reduction: {doc.reduction_pct}% ({doc.raw_size_bytes:,}B -> {doc.clean_size_bytes:,}B)")
    else:
        print("=" * 70)
        print(f"📄 TITLE: {doc.title}")
        print(f"🔗 URL: {doc.url}")
        print(f"⚙️ METHOD: {doc.method} | REDUCTION: {doc.reduction_pct}% ({doc.raw_size_bytes:,}B -> {doc.clean_size_bytes:,}B)")
        print("=" * 70)
        print()
        print(doc.markdown)


if __name__ == "__main__":
    main()
