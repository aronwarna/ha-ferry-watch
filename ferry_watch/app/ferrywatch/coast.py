"""The Finnish sea area with a strip of coast: which weather stations and harbours are 'at sea'.

FMI's bbox query and an Overpass search of Finland also return inland stations and lake
marinas; this rough outline (Gulf of Finland, Archipelago Sea, Åland, the Gulf of Bothnia,
and the Estonian shore across the Gulf of Finland) keeps the sea ones. Its land side runs
about 10 km inland, so city and harbour stations near the shore stay in.
"""

from __future__ import annotations

from .geo import point_in_polygon

# [lat, lon], clockwise from the Russian border along the Finnish shore.
COAST = [
    [60.60, 28.00], [60.65, 27.75], [60.62, 27.20], [60.57, 26.90], [60.52, 26.25], [60.45, 25.65],
    [60.33, 25.30], [60.27, 25.00], [60.25, 24.70], [60.20, 24.45], [60.12, 24.00], [60.12, 23.60],
    [60.35, 23.55], [60.45, 23.25], [60.55, 22.45], [60.65, 22.05], [60.85, 21.65], [61.10, 21.75],
    [61.45, 21.95], [61.60, 21.95], [61.86, 21.75], [62.27, 21.60], [62.50, 21.65], [63.10, 21.95],
    [63.35, 22.60], [63.60, 22.95], [63.85, 23.40], [64.20, 24.20], [64.45, 24.50], [64.60, 24.75],
    [64.80, 25.40], [65.00, 25.75], [65.35, 25.60], [65.85, 24.90], [66.00, 24.40],
    # across the Bothnian Bay and down the middle of the Gulf of Bothnia, clear of Sweden
    [66.00, 24.10], [65.80, 24.10], [65.45, 23.50], [65.00, 22.90], [64.30, 22.20], [63.70, 21.20],
    [63.30, 20.70], [62.50, 20.00], [61.50, 19.80], [60.60, 19.10],
    # west of Åland, then east along the Estonian shore
    [59.30, 19.10], [59.30, 22.50], [59.40, 22.50], [59.40, 28.00],
]


def near_sea(lat: float, lon: float) -> bool:
    return point_in_polygon(lat, lon, COAST)


def overpass_poly() -> str:
    """The outline as an Overpass `poly:` filter ("lat lon lat lon ...")."""
    return " ".join(f"{lat} {lon}" for lat, lon in COAST)
