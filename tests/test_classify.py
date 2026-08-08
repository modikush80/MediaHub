"""Classification rules — pure functions, the basis for how files get organized."""
from mediahub.classify import (bucket_for, classify_device, classify_stage,
                               classify_orientation, classify, slugify)


def test_bucket_for():
    assert bucket_for(".arw") == "raw"
    assert bucket_for(".dng") == "raw"
    assert bucket_for(".mp4") == "videos"
    assert bucket_for(".xmp") == "sidecar"
    assert bucket_for(".zzz") == "other"


def test_device_by_extension_and_name():
    assert classify_device("", "", ".insv", "VID_001.insv") == "Insta360"
    assert classify_device("", "", ".gpr", "GX010001.gpr") == "GoPro"
    assert classify_device("", "", ".arw", "DSC01234.arw") == "Sony"
    assert classify_device("", "", ".heic", "IMG_1234.heic") == "iPhone"
    assert classify_device("", "", ".mp4", "DJI_0001.mp4") == "Drone"
    assert classify_device("Canon", "EOS R5", ".cr3", "x.cr3") == "Canon"
    assert classify_device("", "", ".txt", "notes.txt") == "Unknown"


def test_device_by_altitude_means_drone():
    assert classify_device("", "", ".dng", "x.dng", has_altitude=True) == "Drone"


def test_osmo_pocket_is_not_drone():
    # Osmo Pocket handheld: DJI make but model says Osmo -> its own device
    assert classify_device("DJI", "Osmo Pocket 3", ".mp4", "DJI_0007.mp4") == "Osmo Pocket"
    assert classify_device("", "OSMO POCKET", ".jpg", "x.jpg") == "Osmo Pocket"
    # a real DJI drone (altitude / FC model) still resolves to Drone
    assert classify_device("DJI", "FC7303", ".dng", "DJI_0001.dng", has_altitude=True) == "Drone"


def test_insta360_by_make_and_model():
    assert classify_device("Arashi Vision", "Insta360 X4", ".mp4", "x.mp4") == "Insta360"
    assert classify_device("Insta360", "One RS", ".jpg", "x.jpg") == "Insta360"
    assert classify_device("", "", ".insv", "VID.insv") == "Insta360"


def test_stage_detection():
    assert classify_stage(".xmp", "x.xmp") == "sidecar"
    assert classify_stage(".jpg", "sunset-edit.jpg") == "edited"
    assert classify_stage(".jpg", "plain.jpg", edited_sw=True) == "edited"
    assert classify_stage(".jpg", "plain.jpg", folder="Exports") == "edited"
    assert classify_stage(".arw", "DSC0001.arw") == "original"


def test_orientation():
    assert classify_orientation(4000, 3000) == "Horizontal"
    assert classify_orientation(3000, 4000) == "Vertical"
    # EXIF-rotated landscape sensor -> portrait
    assert classify_orientation(4000, 3000, rotated=True) == "Vertical"
    assert classify_orientation(0, 0) == "Unknown"
    assert classify_orientation(None, None) == "Unknown"


def test_trip_keyword_rules():
    assert classify("Costa Rica 2024")[0] == "Costa Rica"
    assert classify("iceland_trip")[0] == "Iceland"
    label, category = classify("Costa Rica")
    assert isinstance(label, str) and isinstance(category, str)


def test_slugify_is_path_safe():
    s = slugify("Bora Bora / Trip!")
    assert s == "Bora-Bora-Trip"                # spaces/slashes/punctuation collapsed to -
    assert " " not in s and "/" not in s
    assert slugify("!!!") == "Untitled"         # empty result falls back
