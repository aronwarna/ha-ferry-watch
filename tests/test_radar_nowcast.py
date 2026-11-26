import io

from PIL import Image, ImageDraw

from ferrywatch import radar


def _rain(dx=0, dy=0, size=(512, 384)):
    img = Image.new("RGBA", size)
    d = ImageDraw.Draw(img)
    for cx, cy, r in ((120, 100, 30), (300, 220, 45), (420, 90, 20)):
        d.ellipse((cx + dx - r, cy + dy - r, cx + dx + r, cy + dy + r), fill=(30, 90, 220, 255))
    return img


def test_motion_is_found():
    old, new = _rain(), _rain(12, -8)
    whole, field = radar.motion(new.getchannel("A"), old.getchannel("A"))
    assert abs(whole[0] - 12) <= 2 and abs(whole[1] + 8) <= 2
    mx, my = radar.movement_at(field, 0.5, 0.5)
    assert abs(mx - 12) <= 3 and abs(my + 8) <= 3


def test_no_rain_means_no_movement():
    empty = Image.new("L", (512, 384))
    whole, field = radar.motion(empty, empty)
    assert whole == (0, 0) and radar.movement_at(field, 0.2, 0.8) == (0.0, 0.0)


def test_forecast_tile_moves_the_rain_on_and_fades():
    picture = Image.new("RGBA", (512, 512))
    ImageDraw.Draw(picture).ellipse((80, 80, 140, 140), fill=(30, 90, 220, 255))
    field = [[(20.0, 0.0)]]  # 20 px east in 900 s
    origin = (10, 20)
    # Tile (z, x, y) = BASE_Z, 10, 20 is the picture's top-left 256 px.
    now = radar.forecast_tile(picture, origin, field, 900.0, 0.0, radar.BASE_Z, 10, 20)
    later = radar.forecast_tile(picture, origin, field, 900.0, 3600.0, radar.BASE_Z, 10, 20)
    a = Image.open(io.BytesIO(now)).getchannel("A")
    b = Image.open(io.BytesIO(later)).getchannel("A")
    assert a.getbbox()[0] in range(78, 83)
    assert abs(b.getbbox()[0] - (a.getbbox()[0] + 80)) <= 8  # an hour on: 80 px further east
    assert b.getextrema()[1] < a.getextrema()[1]  # and fainter
    # Rain that has left the tile: nothing to draw.
    assert radar.forecast_tile(picture, origin, [[(200.0, 0.0)]], 900.0, 7200.0, radar.BASE_Z, 10, 20) is None


def test_times_carry_on_two_hours():
    class O:  # just what Nowcast reads
        bounds = radar.BOUNDS
        id = "rain-radar"
    n = radar.Nowcast(None, O())
    seen = [1000 * 300 + i * 300 for i in range(12)]
    times = n.extend(seen)
    assert n.base == seen[-1] and len(times) == 12 + radar.AHEAD and times[-1] == seen[-1] + 7200
    assert n.extend(seen[:1]) == seen[:1] and n.base is None
