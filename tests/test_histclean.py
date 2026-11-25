import io

from PIL import Image, ImageDraw

from ferrywatch import histclean as hc


def test_lest97_round_trip():
    x, y = hc.LEST.forward(59.437, 24.745)  # Tallinn
    assert abs(x - 542275) < 30 and abs(y - 6589031) < 30
    lat, lon = hc.LEST.inverse(x, y)
    assert abs(lat - 59.437) < 1e-6 and abs(lon - 24.745) < 1e-6


def _photo(opaque: bool) -> Image.Image:
    """A 'photo' in the middle with a black collar (or white background if opaque) round it."""
    img = Image.new("RGB", (200, 160), (255, 255, 255) if opaque else (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((40, 30, 159, 129), fill=(90, 110, 80))
    d.rectangle((60, 50, 70, 60), fill=(0, 0, 0))  # a dark roof inside the photo must stay
    return img


def test_footprint_finds_picture_not_collar():
    for opaque in (False, True):
        mask = hc.footprint(_photo(opaque), opaque)
        assert mask.getpixel((5, 5)) == 0 and mask.getpixel((100, 80)) == 255
        assert mask.getpixel((65, 55)) == 255  # enclosed black stays picture


def test_cover_rle_round_trip_and_queries():
    mbox = (0.0, 0.0, 2000.0, 1600.0)
    mask = hc.footprint(_photo(False), False).transpose(Image.FLIP_TOP_BOTTOM)  # any shape will do
    cov = hc.Cover.from_mask(mask, mbox, long=40)
    back = hc.Cover.from_rle(mbox, cov.w, cov.h, cov.rle())
    assert back.bits == cov.bits and 0.2 < cov.share() < 0.6
    assert cov.any_in((900, 700, 1100, 900))       # middle
    assert not cov.any_in((0, 0, 100, 100))        # corner collar
    assert cov.edge_zone((900, 700, 1000, 800)) is None  # wholly inside: nothing to clean


def test_clean_tile_cuts_collar_keeps_inside():
    img = _photo(False)
    out = hc.clean_tile(img, None, opaque=False)
    assert out.mode == "RGBA"
    assert out.getpixel((5, 5))[3] == 0 and out.getpixel((100, 80))[3] == 255
    # with no coverage grid every big black blob goes; the grid's edge zone protects the inside
    assert out.getpixel((65, 55))[3] == 0
    zone = Image.new("L", img.size, 255)
    ImageDraw.Draw(zone).rectangle((50, 40, 149, 119), fill=0)
    kept = hc.clean_tile(img, zone, opaque=False)
    assert kept.getpixel((65, 55))[3] == 255 and kept.getpixel((5, 5))[3] == 0
    assert hc.clean_tile(Image.new("RGB", (64, 64)), None, opaque=False) is None  # all collar


def test_encode_picks_format():
    opaque = Image.new("RGB", (32, 32), (80, 90, 70))
    assert hc.encode(opaque, photo=True)[:2] == b"\xff\xd8"  # JPEG
    see = Image.new("RGBA", (32, 32), (80, 90, 70, 0))
    data = hc.encode(see, photo=True)
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    assert Image.open(io.BytesIO(data)).mode in ("RGBA", "RGB")
