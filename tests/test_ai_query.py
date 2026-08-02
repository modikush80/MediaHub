"""Natural-language query parsing (heuristic, no model)."""
from mediahub.ai import parse_query


def test_extracts_device_and_year():
    p = parse_query("drone sunset 2024")
    assert p["filters"].get("device") == "Drone"
    assert p["filters"].get("year") == 2024
    assert "sunset" in p["text"] and "drone" not in p["text"].lower()


def test_extracts_orientation():
    p = parse_query("vertical iphone portraits")
    assert p["filters"].get("orientation") == "Vertical"
    assert p["filters"].get("device") == "iPhone"


def test_plain_query_has_no_filters():
    p = parse_query("waterfall")
    assert p["filters"] == {}
    assert p["text"] == "waterfall"


def test_month_recognized():
    p = parse_query("beach photos july")
    assert p["filters"].get("month") == 7
