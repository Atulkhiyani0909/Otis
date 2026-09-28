import re
from youtube_transcript_api import YouTubeTranscriptApi
from langchain_core.tools import tool

def extract_video_id(url_or_id: str) -> str:
    """Extracts the 11-character video ID from YouTube share URLs or IDs."""
    match = re.search(r"(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})", url_or_id)
    return match.group(1) if match else url_or_id

@tool
def get_youtube_transcript(video_url: str) -> str:
    """
    Fetches the text transcript of a YouTube video given its URL or ID.
    Use this to extract key takeaways, summarize conference talks, or answer questions about a video.
    """
    video_id = extract_video_id(video_url)
    try:
        ytt = YouTubeTranscriptApi()
        transcript_data = ytt.fetch(video_id, languages=['en', 'hi'])
        full_text = " ".join([snippet.text for snippet in transcript_data])
        
        # Guard prompt window from blowing up on multi-hour talks
        return full_text[:12000]
    except Exception as e:
        return f"Could not retrieve transcript for video {video_id}: {str(e)}"