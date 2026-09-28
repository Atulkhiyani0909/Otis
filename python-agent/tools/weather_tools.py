import requests
from langchain_core.tools import tool

WMO_CODES = {
    0: "Clear sky ☀️",
    1: "Mainly clear 🌤️",
    2: "Partly cloudy ⛅",
    3: "Overcast ☁️",
    45: "Foggy 🌫️",
    51: "Light drizzle 🌦️",
    53: "Moderate drizzle 🌧️",
    61: "Slight rain 🌧️",
    63: "Moderate rain 🌧️",
    65: "Heavy rain ⛈️",
    80: "Rain showers 🌦️",
    95: "Thunderstorm ⚡",
}

def resolve_coordinates(place_name: str):
    """Converts a city or locality name into (lat, lon, resolved_name)."""
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    res = requests.get(geo_url, params={"name": place_name, "count": 1, "language": "en", "format": "json"}, timeout=5).json()
    results = res.get("results")
    if not results:
        return None
    top = results[0]
    return top["latitude"], top["longitude"], f"{top.get('name')}, {top.get('admin1', '')} ({top.get('country_code', '')})"

@tool
def get_current_weather(location_or_city: str = "Bhopal", latitude: float = None, longitude: float = None) -> str:
    """
    Fetches real-time weather conditions for any city name OR explicit GPS coordinates.
    Args:
        location_or_city: Name of the city, region, or locality (e.g., 'Indore', 'Bhopal', 'Delhi').
        latitude: Optional direct latitude if already provided by device GPS.
        longitude: Optional direct longitude if already provided by device GPS.
    """
    resolved_label = location_or_city

    # 1. Resolve coordinates if not provided directly
    if latitude is None or longitude is None:
        coords = resolve_coordinates(location_or_city)
        if not coords:
            return f"Could not find coordinates for '{location_or_city}'. Please verify the city name."
        latitude, longitude, resolved_label = coords

    # 2. Query Open-Meteo with coordinates
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "timezone": "auto"
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        data = response.json()

        current = data.get("current", {})
        temp = current.get("temperature_2m", "N/A")
        feels_like = current.get("apparent_temperature", "N/A")
        humidity = current.get("relative_humidity_2m", "N/A")
        wind = current.get("wind_speed_10m", "N/A")
        code = current.get("weather_code", -1)
        condition = WMO_CODES.get(code, "Unknown conditions")

        return (
            f"📍 Weather for {resolved_label}:\n"
            f"• Condition: {condition}\n"
            f"• Temperature: {temp}°C (Feels like: {feels_like}°C)\n"
            f"• Humidity: {humidity}%\n"
            f"• Wind: {wind} km/h"
        )
    except Exception as e:
        return f"Failed to retrieve weather for {resolved_label}: {str(e)}"