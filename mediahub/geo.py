"""Tiny offline reverse-geocoder — maps GPS coordinates to a place name using a
curated, embedded gazetteer (no network, no dataset file, no dependencies).

Used to organize camera-dump / unsorted media by *where* it was shot when the
folder name gives no clue. Nearest gazetteer entry within MAX_KM wins; farther
than that returns None (caller falls back to a coarse label). The gazetteer is
intentionally compact — expand GAZETTEER for finer coverage.
"""
import math

MAX_KM = 160.0  # nearest place must be within this radius to be trusted

# (name, latitude, longitude) — major world cities + notable travel spots.
GAZETTEER = [
    # North America
    ("Reykjavík", 64.146, -21.942), ("Vík", 63.418, -19.006),
    ("San José CR", 9.928, -84.091), ("Liberia CR", 10.634, -85.437),
    ("Banff", 51.178, -115.571), ("Calgary", 51.045, -114.058),
    ("Jasper", 52.873, -118.081), ("Vancouver", 49.283, -123.121),
    ("Los Angeles", 34.052, -118.244), ("San Diego", 32.716, -117.161),
    ("San Francisco", 37.775, -122.419), ("San Jose", 37.339, -121.895),
    ("Las Vegas", 36.170, -115.140), ("Seattle", 47.606, -122.332),
    ("Portland", 45.515, -122.678), ("Phoenix", 33.448, -112.074),
    ("Albuquerque", 35.084, -106.651), ("Santa Fe", 35.687, -105.938),
    ("Denver", 39.739, -104.990), ("Salt Lake City", 40.760, -111.891),
    ("New York", 40.713, -74.006), ("Chicago", 41.878, -87.630),
    ("Miami", 25.762, -80.192), ("Austin", 30.267, -97.743),
    ("Honolulu", 21.307, -157.858), ("George Town KY", 19.286, -81.367),
    ("Cancún", 21.161, -86.851), ("Mexico City", 19.433, -99.133),
    ("Toronto", 43.651, -79.383), ("Yellowstone", 44.428, -110.588),
    ("Grand Canyon", 36.107, -112.113), ("Moab", 38.573, -109.550),
    # South America
    ("Lima", -12.046, -77.043), ("Cusco", -13.532, -71.967),
    ("Rio de Janeiro", -22.907, -43.173), ("Buenos Aires", -34.604, -58.382),
    ("Santiago", -33.449, -70.669), ("Quito", -0.181, -78.468),
    # Europe
    ("London", 51.507, -0.128), ("Paris", 48.857, 2.352),
    ("Nice", 43.710, 7.262), ("Amsterdam", 52.370, 4.895),
    ("Rome", 41.903, 12.496), ("Venice", 45.440, 12.316),
    ("Barcelona", 41.385, 2.173), ("Madrid", 40.417, -3.703),
    ("Lisbon", 38.722, -9.139), ("Berlin", 52.520, 13.405),
    ("Zurich", 47.377, 8.542), ("Interlaken", 46.686, 7.863),
    ("Vienna", 48.208, 16.373), ("Prague", 50.076, 14.438),
    ("Santorini", 36.393, 25.461), ("Athens", 37.984, 23.728),
    ("Dubrovnik", 42.650, 18.091), ("Copenhagen", 55.676, 12.568),
    ("Oslo", 59.914, 10.752), ("Tromsø", 69.649, 18.956),
    # Africa / Middle East
    ("Cape Town", -33.925, 18.424), ("Marrakesh", 31.630, -7.981),
    ("Cairo", 30.044, 31.236), ("Nairobi", -1.286, 36.817),
    ("Dubai", 25.205, 55.271), ("Istanbul", 41.008, 28.978),
    # Asia
    ("Tokyo", 35.690, 139.692), ("Kyoto", 35.012, 135.768),
    ("Bangkok", 13.756, 100.502), ("Singapore", 1.352, 103.820),
    ("Bali", -8.409, 115.189), ("Hong Kong", 22.320, 114.170),
    ("Seoul", 37.567, 126.978), ("Delhi", 28.614, 77.209),
    ("Mumbai", 19.076, 72.878), ("Kathmandu", 27.717, 85.324),
    ("Maldives", 3.202, 73.220),
    # Oceania / Pacific
    ("Papeete", -17.537, -149.566), ("Bora Bora", -16.501, -151.741),
    ("Moorea", -17.540, -149.830), ("Auckland", -36.848, 174.763),
    ("Queenstown NZ", -45.031, 168.663), ("Milford Sound", -44.671, 167.926),
    ("Sydney", -33.869, 151.209), ("Melbourne", -37.814, 144.963),
    ("Fiji", -18.124, 178.450),
]


def _haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


_CACHE = {}


def place_for(lat, lon):
    """Nearest gazetteer place name within MAX_KM, else None. Memoized by a
    coarse coordinate bucket so 15k dump files collapse to a handful of lookups."""
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return None
    if lat == 0 and lon == 0:
        return None                      # null island -> treat as no fix
    key = (round(lat, 2), round(lon, 2))
    if key in _CACHE:
        return _CACHE[key]
    best, best_d = None, MAX_KM
    for name, plat, plon in GAZETTEER:
        d = _haversine(lat, lon, plat, plon)
        if d < best_d:
            best, best_d = name, d
    _CACHE[key] = best
    return best
