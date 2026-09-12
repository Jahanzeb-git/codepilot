from codepilot.engine.hooks import EventType
import re
import httpx
import logging

logger = logging.getLogger(__name__)

PAGE_CHAR_THRESHOLD = 2000
MAX_SEARCH_CHARS = 3000

# -------------------------------------------------------------------------
# Security: The Tavily API key is NEVER read from os.environ at call time.
# agent_server.py captures it from os.environ at startup (before any
# subprocess can inherit it) and injects it here via set_tavily_key().
# This means search_web() can always access the key at call time without
# the key ever being present in the subprocess-inheritable environment.
# -------------------------------------------------------------------------
_TAVILY_API_KEY: str = ""

def set_tavily_key(key: str) -> None:
    """Called once by agent_server.py after it pops TAVILY_API_KEY from os.environ."""
    global _TAVILY_API_KEY
    _TAVILY_API_KEY = key


class SearchWebTool:
    def __init__(self, runtime):
        # Extract underlying AsyncRuntime whether given Runtime or AsyncRuntime
        self.async_rt = getattr(runtime, "_async", runtime)
        self.hooks = runtime.hooks

    def search_web(self, query: str = "", domain: str = "", url: str = "", section: str = "") -> str:
        """
        Search web or extract page content. Returns markdown with citations.
        Pass query/domain to search, or url/section to read specific web page sections.
        """
        # 1. Emit TOOL_CALL event for UI/hooks
        self.hooks.emit(EventType.TOOL_CALL, tool="search_web", args={"query": query, "domain": domain, "url": url, "section": section})

        clean_url = url.strip()
        clean_query = query.strip()

        if clean_url:
            result = _extract_page(clean_url, section.strip())
        elif clean_query:
            result = _perform_search(clean_query, domain.strip())
        else:
            result = "Error: Please provide a 'query' to search or a 'url' to extract content."

        # 3. Push output to execution buffer (NO print() needed by LLM!)
        if hasattr(self.async_rt, "_append_execution"):
            self.async_rt._append_execution(f"[search_web]\n{result}")

        # 4. Emit TOOL_RESULT event for UI/hooks
        self.hooks.emit(EventType.TOOL_RESULT, tool="search_web", result=result)

        return result


def _perform_search(query: str, domain: str) -> str:
    if not _TAVILY_API_KEY:
        return "Error: Web search is not configured (missing API key)."

    payload = {
        "api_key": _TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "include_answer": True,
        "max_results": 4,
    }
    if domain:
        clean_domain = domain.replace("https://", "").replace("http://", "").split("/")[0]
        payload["include_domains"] = [clean_domain]

    try:
        response = httpx.post("https://api.tavily.com/search", json=payload, timeout=15.0)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"[search_web] Tavily search error: {e}")
        return f"Error executing web search: {e}"

    output_parts = []

    answer = data.get("answer")
    if answer:
        output_parts.append(f"### Direct Answer\n{answer}")

    results = data.get("results", [])
    if results:
        output_parts.append("### Search Results")
        for res in results:
            title = res.get("title", "Untitled")
            link = res.get("url", "")
            content = res.get("content", "").strip()
            output_parts.append(f"- [{title}]({link})\n  {content}")

    if not output_parts:
        return "No relevant search results found."

    result_text = "\n\n".join(output_parts)
    if len(result_text) > MAX_SEARCH_CHARS:
        result_text = result_text[:MAX_SEARCH_CHARS] + "\n\n... [Results truncated for token efficiency]"

    return result_text


def _parse_sections(markdown_text: str) -> list[tuple[str, str]]:
    """Parses markdown headers and splits content into (heading_title, section_text) tuples."""
    pattern = re.compile(r'^(#{1,4})\s+(.+)$', re.MULTILINE)
    matches = list(pattern.finditer(markdown_text))

    if not matches:
        return [("Full Content", markdown_text)]

    sections = []
    first_start = matches[0].start()
    if first_start > 0:
        intro_text = markdown_text[:first_start].strip()
        if intro_text:
            sections.append(("Overview", intro_text))

    for i, match in enumerate(matches):
        heading_title = match.group(2).strip()
        start_pos = match.start()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(markdown_text)
        section_body = markdown_text[start_pos:end_pos].strip()
        sections.append((heading_title, section_body))

    return sections


def _extract_page(url: str, target_section: str) -> str:
    if not _TAVILY_API_KEY:
        return "Error: Web search is not configured (missing API key)."

    payload = {
        "api_key": _TAVILY_API_KEY,
        "urls": [url],
        "include_images": False,
    }

    try:
        response = httpx.post("https://api.tavily.com/extract", json=payload, timeout=20.0)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"[search_web] Tavily extract error for {url}: {e}")
        return f"Error extracting content from {url}: {e}"

    results = data.get("results", [])
    failed  = data.get("failed_results", [])

    if not results:
        if failed:
            reason = failed[0].get("error", "unknown error")
            return (
                f"Could not extract content from {url} (Tavily: {reason}).\n"
                f"Try using search_web(query=\"...\") with a descriptive query instead — "
                f"URL extraction does not work for pages that block crawlers."
            )
        return f"Could not extract content from {url}."

    raw_content = results[0].get("raw_content", "").strip()
    title       = results[0].get("title", url)

    if not raw_content:
        return f"No readable text content extracted from [{title}]({url})."

    total_length = len(raw_content)

    # Small page: return in full
    if total_length <= PAGE_CHAR_THRESHOLD:
        return f"## [{title}]({url})\n\n{raw_content}"

    parsed_sections = _parse_sections(raw_content)

    # Specific section requested
    if target_section:
        target_lower = target_section.lower()
        for heading, body in parsed_sections:
            if target_lower in heading.lower():
                return f"## [{title}]({url})\n### Section: {heading}\n\n{body}"
        available = ", ".join(f"'{h[0]}'" for h in parsed_sections)
        return (
            f"Error: Section '{target_section}' not found on [{title}]({url}).\n"
            f"Available sections are: {available}"
        )

    # Large page — return ToC + preview + navigation hint
    toc_lines  = [f"- {h}" for h, _ in parsed_sections]
    toc_md     = "\n".join(toc_lines)
    preview    = raw_content[:1000]

    return (
        f"## [{title}]({url})\n\n"
        f"### 📋 Table of Contents ({total_length:,} total characters)\n"
        f"{toc_md}\n\n"
        f"### 🔍 Page Preview (First 1,000 chars)\n"
        f"{preview}\n\n"
        f"---\n"
        f"💡 **PAGE NAVIGATION FEEDBACK:**\n"
        f"This document is large ({total_length:,} characters).\n"
        f"To read a specific section, call `search_web(url=\"{url}\", section=\"<Section Title>\")`."
    )


def register_search_web_tool(runtime) -> None:
    """Register search_web into a codepilot Runtime instance."""
    web_tool = SearchWebTool(runtime)
    if hasattr(runtime, "register_tool"):
        runtime.register_tool(name="search_web", func=web_tool.search_web, replace=True)
    elif hasattr(runtime, "tools"):
        runtime.tools["search_web"] = web_tool.search_web
