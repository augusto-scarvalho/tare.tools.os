---
name: web-ingestion
description: "Extract clean, structured Markdown from web links, articles, and technical documentations using Trafilatura (local zero-cost) and Firecrawl (headless JS/Cloudflare bypass)."
---

# Web Ingestion & Clean Extraction Skill (Trafilatura & Firecrawl)

Use this skill whenever an agent needs to inspect, read, or summarize external web URLs, technical documentation, API guides, or help center articles.

---

## 🎯 When to Use This Skill
- Reading external product documentation, API references, or pricing pages (OpenAI, Google, Anthropic, GitHub, etc.).
- When standard HTTP fetch fails with HTTP 403 / 401 or returns massive amounts of raw HTML/JS noise (>400KB).
- To reduce token consumption by >90% before ingesting documentation into LLM contexts.

---

## 🛠️ CLI Quick Reference

```bash
# 1. Extract and show in the terminal with reduction statistics
python scripts/tools/fetch_clean_url.py "https://example.com/article"

# 2. Extract and save straight to a clean Markdown file
python scripts/tools/fetch_clean_url.py "https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers" --output docs/reference.md

# 3. Extract as JSON with structured metadata
python scripts/tools/fetch_clean_url.py "https://example.com/doc" --json

# 4. Force rendering through the Firecrawl engine (complex React/Vue SPAs)
python scripts/tools/fetch_clean_url.py "https://spa-app.com" --firecrawl
```

---

## 🐍 Direct Use from Python

```python
from relay.web_reader import WebReader

doc = WebReader.fetch_clean_markdown("https://support.google.com/googleone/answer/16286513?hl=en")
if doc.status == "OK":
    print(f"Title: {doc.title}")
    print(f"Reduction: {doc.reduction_pct}% ({doc.raw_size_bytes}B -> {doc.clean_size_bytes}B)")
    print(doc.markdown)
```
