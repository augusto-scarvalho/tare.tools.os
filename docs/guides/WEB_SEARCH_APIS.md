# Web search APIs

In **Set up Tare → Web search**, add a key for DeepSeek Search, SerpApi or Firecrawl. One service is enough.
Keys go to the existing operating-system credential vault. A matching environment variable takes priority:
`DEEPSEEK_API_KEY`, `SERPAPI_API_KEY` or `FIRECRAWL_API_KEY`. Setup shows where a key is configured; this is not
proof that its account has credit or access. Opening setup and saving keys send no search or model request.

Automatic tries connected, allowed services in this order: DeepSeek, SerpApi, Firecrawl. The first-service
selector changes that order. An explicit `provider` in a tool call selects that service alone. Machine-wide
choices use the existing environment settings, under `search_providers`; the CLI also accepts
`tare setup --use search` and `tare setup --no-use search:firecrawl`. No secret belongs in those settings.

New coordinator conversations expose `search_web(query, limit=5, provider="auto", refresh=false)`, including
for local and other open-weight models. The host calls the search API directly; it does not start another
model or run project code. Native CLI research remains available through `research`. For a supplied URL use
`read_url`. A saved conversation keeps its original tool definitions and prompt prefix. Its existing `research`
tool can fall back to direct API discovery when no native researcher can start, without adding tools or
rewriting history. It returns a partial result with discovery links; page reading and synthesis remain pending.
An explicit CLI route or an uncertain/paid native attempt never silently changes to an API backend.

Search results are short discovery snippets with source URLs, provider and fetch time. They are untrusted
and do not prove a page was read. The coordinator reads relevant URLs before citing page-backed claims.
Queries contain at most 128 characters (the common supported range) and results are capped at ten. Project
contents, keys and chat history must not be used as search queries.

Identical recent queries reuse their result for one hour while the service is still allowed/configured.
Refresh is explicit and preserves conversation history. Eight API requests are permitted per user message;
fallback attempts count too. Automatic fallback is limited to definite authentication, access, credit or
rate-limit refusals. Timeouts, malformed results and uncertain requests stop; recovery never repeats an
unknown HTTP request. Empty successful searches are valid results and do not trigger another service.

The existing runtime supervises HTTP workers for cancellation, deadlines and finite memory pipes. Keys are
passed through stdin in memory, never command arguments or input files. Redirects are refused. Raw provider
responses, request URLs with keys, HTML, images and full-page crawls are not retained. Search receipts keep
eight bounded result sets; older receipts keep request counts. Ordinary conversation history stays intact.
Dollar cost remains unknown when the provider
does not report it; an API request is distinct from model tokens or subscription usage.

Provider contracts: [DeepSeek Search](https://api-docs.deepseek.com/guides/search/),
[SerpApi Google Search](https://serpapi.com/search-api),
[Firecrawl v2 Search](https://docs.firecrawl.dev/api-reference/endpoint/search). Firecrawl scraping is not enabled
by the search adapter; relevant pages are read separately.
