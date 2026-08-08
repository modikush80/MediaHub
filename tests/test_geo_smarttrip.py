"""Offline reverse-geocoder + smart_trip (camera-dump location/year labeling)."""
from mediahub import geo
from mediahub.classify import smart_trip


def test_geo_nearest_known_places():
    assert geo.place_for(64.13, -21.90) == "Reykjavík"      # Iceland
    assert geo.place_for(-16.50, -151.74) == "Bora Bora"    # French Polynesia
    assert geo.place_for(51.17, -115.57) == "Banff"


def test_geo_null_and_remote_return_none():
    assert geo.place_for(0, 0) is None                       # null island
    assert geo.place_for(5.0, -140.0) is None                # mid-Pacific, > MAX_KM
    assert geo.place_for(None, None) is None


def _f(folder, lat=None, lon=None, date=""):
    return {"top_folder": folder, "gps_latitude": lat, "gps_longitude": lon,
            "capture_date": date}


def test_named_trip_unaffected_by_gps():
    label, cat = smart_trip(_f("Iceland_Trip", lat=48.8, lon=2.3, date="2024-09-01"))
    assert label == "Iceland" and cat == "trips"


def test_camera_dump_gets_place_and_year():
    # 'sony camera 15' matches the camera-dumps rule; GPS + date refine it
    label, cat = smart_trip(_f("Sony Camera 15", lat=64.13, lon=-21.90, date="2024:06:02 10:00:00"))
    assert cat == "camera-dumps"
    assert label == "Reykjavík 2024"


def test_camera_dump_without_gps_falls_back_to_year():
    label, cat = smart_trip(_f("Sony Camera 15", date="2023-03-01"))
    assert label == "Camera Dump 2023" and cat == "camera-dumps"


def test_multiyear_dump_splits_by_file_year():
    a = smart_trip(_f("Sony Camera 15", date="2022-01-01"))[0]
    b = smart_trip(_f("Sony Camera 15", date="2024-01-01"))[0]
    assert a != b                                            # different years -> different trips
