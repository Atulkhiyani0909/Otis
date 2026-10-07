import os
import random
import re
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from duckduckgo_search import DDGS
from langchain_core.tools import tool
import trafilatura

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Create local storage for generated images
MEDIA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "generated_images"))
os.makedirs(MEDIA_DIR, exist_ok=True)


@tool
def browse_url(url: str, max_chars: int = 6000) -> str:
    """Fetches and reads the textual content of any public webpage or article URL."""
    try:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return f"Error: URL '{url}' is invalid. It must start with http:// or https://."

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, timeout=12) as response:
                downloaded = response.read().decode("utf-8", errors="ignore")

        if not downloaded:
            return f"Error: Could not retrieve content from '{url}'."

        extracted_text = trafilatura.extract(
            downloaded,
            include_links=True,
            include_tables=True,
            output_format="txt",
        )

        if not extracted_text or not extracted_text.strip():
            return f"Page at '{url}' was fetched, but no readable body text could be extracted."

        trimmed_content = extracted_text.strip()[:max_chars]
        if len(extracted_text) > max_chars:
            trimmed_content += f"\n\n... [Content truncated at {max_chars} characters]"

        return f"=== Extracted Webpage Content from {url} ===\n\n{trimmed_content}"
    except Exception as e:
        return f"Failed to browse URL {url}: {str(e)}"


@tool
def web_search(query: str, max_results: int = 4) -> str:
    """Performs a live web search to look up current news, facts, documentation, or public info."""
    cleaned_query = re.sub(r'["\']', '', query).strip()[:200]
    if not cleaned_query:
        return "Error: Empty search query."

    try:
        results = []
        with DDGS(timeout=10) as ddgs:
            raw_results = ddgs.text(cleaned_query, max_results=max_results)
            if raw_results:
                results = list(raw_results)

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
    """Searches the web for an image matching the query and returns a direct image URL."""
    cleaned_query = re.sub(r'["\']', '', query).strip()[:150]
    if not cleaned_query:
        return "Error: Empty image query."

    try:
        candidates = []
        with DDGS(timeout=10) as ddgs:
            raw = ddgs.images(cleaned_query, max_results=4)
            if raw:
                candidates = list(raw)

        if not candidates:
            return f"No images found for query: '{query}'."

        selected_url = None
        selected_title = query
        for item in candidates:
            img_url = item.get("image") or item.get("thumbnail")
            if img_url and img_url.startswith(("http://", "https://")):
                selected_url = img_url
                selected_title = item.get("title", query)
                break

        if not selected_url:
            return f"No accessible image URLs found for query: '{query}'."

        return f"[IMAGE_URL: {selected_url}]\nFound image for '{selected_title}'."
    except Exception as e:
        return f"Image search failed: {str(e)}"


@tool
def generate_image(prompt: str) -> str:
    """Generates a high-quality visual or graphic from a text description.
    Use this when the user asks to create, draw, generate, or make an image of something.
    Args:
        prompt: Detailed description of the image to generate.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "Error: Prompt cannot be empty."

    try:
        encoded_prompt = urllib.parse.quote(prompt)
        seed = random.randint(1000, 99999)
        
        # enhance=false removes the 8s LLM delay; 768x768 turbo renders in ~2 seconds
        engine_url = (
            f"https://image.pollinations.ai/prompt/{encoded_prompt}"
            f"?width=768&height=768&nologo=true&model=turbo&enhance=false&seed={seed}"
        )

        req = urllib.request.Request(engine_url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            image_data = resp.read()

        if not image_data or len(image_data) < 1000:
            return "Failed to generate image: empty stream received."

        filename = f"gen_{uuid.uuid4().hex[:8]}.jpg"
        file_path = os.path.join(MEDIA_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(image_data)

        # Output the exact tag with the local path
        return f"[IMAGE_PATH: {file_path}]\nVisual generated successfully."

    except Exception as e:
        return f"Failed to generate image: {str(e)}"