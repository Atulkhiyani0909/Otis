from duckduckgo_search import DDGS
from langchain_core.tools import tool
import trafilatura



@tool
def browse_url(url: str, max_chars: int = 6000) -> str:
    """Fetches and reads the textual content of any public webpage or article URL.

    Use this tool whenever the user provides an HTTP/HTTPS link and asks to
    summarize, analyze, answer questions about it, or read documentation.
    Args:
        url: The full web URL to fetch (must begin with http:// or https://).
        max_chars: Maximum characters to return to prevent overflowing context
        (default 6000).
    """
    try:
        # Download raw HTML with realistic desktop user-agent
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return f"Error: Could not retrieve content from '{url}'. The site may be blocking automated requests or is offline."

        # Extract main content and clean boilerplate/ads/footers
        extracted_text = trafilatura.extract(
            downloaded,
            include_links=True,
            include_tables=True,
            output_format="txt",
        )

        if not extracted_text or len(extracted_text.strip()) == 0:
            return f"Page at '{url}' was fetched, but no readable body text could be extracted."

        # Truncate to avoid context window flooding
        trimmed_content = extracted_text.strip()[:max_chars]
        if len(extracted_text) > max_chars:
            trimmed_content += (
                f"\n\n... [Content truncated at {max_chars} characters]"
            )

        return f"=== Extracted Webpage Content from {url} ===\n\n{trimmed_content}"

    except Exception as e:
        return f"Failed to browse URL {url}: {str(e)}"

    
@tool
def web_search(query: str, max_results: int = 4) -> str:
    """Performs a live web search using DuckDuckGo to look up recent news, facts, or public info."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return f"No web results found for query: '{query}'."

        formatted_results = []
        for i, res in enumerate(results, 1):
            title = res.get("title", "No Title")
            snippet = res.get("body", "No description available")
            url = res.get("href", "")
            formatted_results.append(f"{i}. **{title}**\n   {snippet}\n   *Source:* {url}")

        return "\n\n".join(formatted_results)
    except Exception as e:
        return f"Web search failed: {str(e)}"


@tool
def search_image(query: str) -> str:
    """
    Searches the web for an image matching the query and returns a direct image URL.
    Use this whenever the user asks to see a picture, photo, diagram, or visual representation of something.
    Args:
        query: Subject or item to look for (e.g., 'Eiffel Tower night view', 'diagram of human heart').
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.images(query, max_results=1))

        if not results or not results[0].get("image"):
            return f"No images found for query: '{query}'."

        img_url = results[0]["image"]
        title = results[0].get("title", query)
        # Format with a distinct tag so Node.js can detect and send it as a native photo
        return f"[IMAGE_URL: {img_url}]\nFound image for '{title}'."
    except Exception as e:
        return f"Image search failed: {str(e)}"


@tool
def generate_image(prompt: str) -> str:
    """Generates a new image from a text description using an AI image model.
    Use this when the user asks to create, draw, generate, or make an image
    of something, as opposed to finding an existing photo (use search_image for that)."""
    # call Gemini Imagen / OpenAI images / etc., get back a URL or base64
    ...
    return f"[IMAGE_URL: {generated_url}]\nGenerated image for '{prompt}'."
    