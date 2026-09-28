import requests
from langchain_core.tools import tool


def get_coordinates(place_name: str):
    """Free geocoding using Open-Meteo's public API (no key required)."""
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    res = requests.get(
        geo_url,
        params={"name": place_name, "count": 1, "language": "en", "format": "json"},
        timeout=5,
    ).json()
    results = res.get("results")
    if not results:
        return None
    top = results[0]
    return (
        top["latitude"],
        top["longitude"],
        f"{top.get('name')}, {top.get('admin1', '')}",
    )


@tool
def get_commute_and_distance(
    origin: str,
    destination: str,
    mode: str = "driving",
) -> str:
    """
    Calculates travel distance and estimated duration between two locations using free OpenStreetMap routing.
    No credit card or API key required.
    Args:
        origin: Starting address, landmark, or city (e.g., 'Bhopal Junction' or 'DB City Mall, Bhopal').
        destination: Target venue or city (e.g., 'Raja Bhoj Airport, Bhopal').
        mode: 'driving', 'walking', or 'cycling' (default: 'driving').
    """
    # 1. Geocode origin and destination
    origin_coords = get_coordinates(origin)
    if not origin_coords:
        return f"Could not find coordinates for origin: '{origin}'."

    dest_coords = get_coordinates(destination)
    if not dest_coords:
        return f"Could not find coordinates for destination: '{destination}'."

    lat1, lon1, resolved_origin = origin_coords
    lat2, lon2, resolved_dest = dest_coords

    # Map mode to OSRM profile
    profile_map = {
        "driving": "driving",
        "walking": "foot",
        "cycling": "bike",
        "bicycling": "bike",
    }
    profile = profile_map.get(mode.lower(), "driving")

    # 2. Call Free OSRM Public Routing API (Coordinates format: {lon},{lat})
    osrm_url = f"https://router.project-osrm.org/route/v1/{profile}/{lon1},{lat1};{lon2},{lat2}"
    params = {"overview": "false"}

    try:
        response = requests.get(osrm_url, params=params, timeout=7)
        data = response.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            return f"Could not calculate route between '{origin}' and '{destination}'."

        route = data["routes"][0]
        distance_meters = route.get("distance", 0)
        duration_seconds = route.get("duration", 0)

        # Convert to readable units
        distance_km = round(distance_meters / 1000, 2)
        duration_minutes = round(duration_seconds / 60)

        hours = duration_minutes // 60
        mins = duration_minutes % 60
        time_str = f"{hours} hr {mins} mins" if hours > 0 else f"{mins} mins"

        return (
            f"🚗 Route Summary ({mode.capitalize()} - OpenStreetMap):\n"
            f"• From: {resolved_origin}\n"
            f"• To: {resolved_dest}\n"
            f"• Distance: {distance_km} km\n"
            f"• Estimated Travel Time: ~{time_str}"
        )
    except Exception as e:
        return f"Failed to compute route: {str(e)}"