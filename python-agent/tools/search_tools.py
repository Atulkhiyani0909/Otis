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


import base64
import json
import os
import urllib.error
import urllib.request

from langchain_core.tools import tool

from tools.image_store import put_image, queue_for_delivery




def _sniff_mime(data: bytes, fallback: str = None) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback or "image/png"


def _find_image(resp: dict):
    """Returns (image_bytes, mime, text_parts). Skips the model's interim 'thought' images."""
    texts = []

    top = resp.get("output_image")
    if isinstance(top, dict) and top.get("data"):
        try:
            return base64.b64decode(top["data"]), top.get("mime_type"), texts
        except Exception:
            pass

    for step in resp.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") == "thought":
            continue
        for block in step.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image" and block.get("data"):
                try:
                    return base64.b64decode(block["data"]), block.get("mime_type"), texts
                except Exception:
                    continue
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
    return None, None, texts

@tool
def generate_image(prompt: str) -> str:
    """Generates a high-quality image from a text description and sends it to the user automatically.
    Use this when the user asks to create, draw, generate, design or make an image of something.
    Write the prompt as a rich description: subject, style (photo, 3D, watercolor, flat icon...),
    lighting, composition, colors, and any exact text that must appear in the image.
    Args:
        prompt: Detailed description of the image to generate.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "Error: Prompt cannot be empty."

    base = os.getenv("POLLINATIONS_URL", "https://image.pollinations.ai/prompt").rstrip("/")
    width = os.getenv("POLLINATIONS_WIDTH", "1024").strip() or "1024"
    height = os.getenv("POLLINATIONS_HEIGHT", "1024").strip() or "1024"
    model = os.getenv("POLLINATIONS_MODEL", "flux").strip() or "flux"

    params = {
        "width": width,
        "height": height,
        "model": model,
        "nologo": "true",
        "seed": random.randint(1, 10_000_000),
    }
    url = f"{base}/{urllib.parse.quote(prompt[:1500], safe='')}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers=DEFAULT_HEADERS, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            image_bytes = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return "Failed to generate image: Pollinations rate limit reached. Try again in a minute."
        return f"Failed to generate image: HTTP {e.code}."
    except Exception as e:
        return f"Failed to generate image: {str(e)}"

    if not content_type.startswith("image/") or len(image_bytes) < 1000:
        return "Failed to generate image: the service did not return a valid image. Suggest rephrasing or trying again."

    mime = _sniff_mime(image_bytes, content_type.split(";")[0].strip() or None)
    ref = put_image(image_bytes, mime)   # kept in memory only
    queue_for_delivery(ref)              # main.py sends it with your reply

    return (
        "Image generated. It is attached to your reply to the user automatically. "
        "Do NOT write any image tag, link, path or id in your reply: just add a short caption. "
        f"(Internal id for emailing it: gen:{ref})"
    )