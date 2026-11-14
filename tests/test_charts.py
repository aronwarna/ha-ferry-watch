from ferrywatch.charts import parse_capabilities

CAPS = """<?xml version="1.0"?>
<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1"
  xmlns:xlink="http://www.w3.org/1999/xlink" version="1.0.0">
  <ows:OperationsMetadata>
    <ows:Operation name="GetTile"><ows:DCP><ows:HTTP>
      <ows:Get xlink:href="https://example.test/wmts?"/>
    </ows:HTTP></ows:DCP></ows:Operation>
  </ows:OperationsMetadata>
  <Contents>
    <Layer>
      <ows:Title>Taustakartta</ows:Title><ows:Identifier>background</ows:Identifier>
      <Style isDefault="true"><ows:Identifier>default</ows:Identifier></Style>
      <Format>image/png</Format>
      <TileMatrixSetLink><TileMatrixSet>WGS84_Pseudo-Mercator</TileMatrixSet></TileMatrixSetLink>
    </Layer>
    <Layer>
      <ows:Title>Merikartat</ows:Title><ows:Identifier>merikartta</ows:Identifier>
      <Style isDefault="true"><ows:Identifier>default</ows:Identifier></Style>
      <Format>image/png</Format>
      <TileMatrixSetLink><TileMatrixSet>ETRS-TM35FIN</TileMatrixSet></TileMatrixSetLink>
      <TileMatrixSetLink><TileMatrixSet>WGS84_Pseudo-Mercator</TileMatrixSet></TileMatrixSetLink>
      <ResourceURL format="image/png" resourceType="tile"
        template="https://example.test/wmts/{Style}/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.png"/>
    </Layer>
    <TileMatrixSet>
      <ows:Identifier>ETRS-TM35FIN</ows:Identifier><ows:SupportedCRS>urn:ogc:def:crs:EPSG::3067</ows:SupportedCRS>
      <TileMatrix><ows:Identifier>0</ows:Identifier><MatrixWidth>1</MatrixWidth></TileMatrix>
    </TileMatrixSet>
    <TileMatrixSet>
      <ows:Identifier>WGS84_Pseudo-Mercator</ows:Identifier><ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>
      <TileMatrix><ows:Identifier>WGS84_Pseudo-Mercator:2</ows:Identifier><MatrixWidth>4</MatrixWidth></TileMatrix>
      <TileMatrix><ows:Identifier>WGS84_Pseudo-Mercator:3</ows:Identifier><MatrixWidth>8</MatrixWidth></TileMatrix>
      <TileMatrix><ows:Identifier>WGS84_Pseudo-Mercator:4</ows:Identifier><MatrixWidth>16</MatrixWidth></TileMatrix>
    </TileMatrixSet>
  </Contents>
</Capabilities>"""


def test_picks_chart_layer_in_web_mercator():
    chart = parse_capabilities(CAPS)
    [layer] = chart["layers"]
    assert layer["layer"] == "merikartta" and layer["id"] == "merikartta"
    assert layer["url"] == "https://example.test/wmts/default/WGS84_Pseudo-Mercator/{TileMatrix}/{y}/{x}.png"
    assert layer["matrix_ids"] == ["", "", "WGS84_Pseudo-Mercator:2", "WGS84_Pseudo-Mercator:3",
                                   "WGS84_Pseudo-Mercator:4"]
    assert layer["min_zoom"] == 2 and layer["max_zoom"] == 4


def test_falls_back_to_kvp_get_tile():
    caps = CAPS.replace('<ResourceURL format="image/png" resourceType="tile"', '<Ignored')
    [layer] = parse_capabilities(caps)["layers"]
    assert layer["url"].startswith("https://example.test/wmts?SERVICE=WMTS&REQUEST=GetTile")
    assert "LAYER=merikartta" in layer["url"] and "TILEROW={y}" in layer["url"]


def test_no_mercator_set_returns_none():
    caps = CAPS.replace("EPSG::3857", "EPSG::4326")
    assert parse_capabilities(caps) is None


def test_stacks_overview_charts_and_all_series():
    def layer(ident, title):
        return f"""<Layer><ows:Title>{title}</ows:Title><ows:Identifier>{ident}</ows:Identifier>
          <Format>image/png</Format>
          <TileMatrixSetLink><TileMatrixSet>WGS84_Pseudo-Mercator</TileMatrixSet></TileMatrixSetLink></Layer>"""
    extra = "".join([
        layer("Traficom:Merikarttasarja A public", "Merikarttasarja A"),
        layer("Traficom:Merikarttasarjat public", "Merikarttasarjat"),
        layer("Traficom:Yleiskartat 250k public", "Yleiskartat 250k public"),
    ])
    caps = CAPS.replace("<Contents>", "<Contents>" + extra)
    chart = parse_capabilities(caps)
    assert [l["id"] for l in chart["layers"]] == ["yleiskartat-250k-public", "merikarttasarjat-public"]
    assert "LAYER=Traficom%3AMerikarttasarjat%20public" in chart["layers"][1]["url"]


def test_finnish_waters_mask():
    import math
    from ferrywatch.charts import in_finnish_waters

    def tile(lat, lon, z):
        n = 2**z
        return z, int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

    # Traficom's chart draws these marks itself...
    for lat, lon in [(59.98, 24.4), (60.1, 19.93), (63.1, 21.5), (59.82, 22.95)]:
        assert in_finnish_waters(*tile(lat, lon, 12))
    # ...but not Swedish, Estonian or Russian waters.
    for lat, lon in [(59.4, 18.8), (59.5, 24.75), (60.05, 27.0), (63.8, 20.3)]:
        assert not in_finnish_waters(*tile(lat, lon, 12))
    # A tile big enough to cross the boundary keeps its marks.
    assert not in_finnish_waters(*tile(59.98, 24.4, 5))


def test_s57_service_capabilities():
    from ferrywatch.charts import choose_enc, enc_tile_url, parse_wms_capabilities
    xml = b"""<?xml version="1.0"?>
<WMS_Capabilities xmlns="http://www.opengis.net/wms" version="1.3.0">
 <Capability>
  <Request><GetMap><Format>image/png</Format><Format>image/jpeg</Format></GetMap></Request>
  <Layer><Title>S-57</Title><CRS>EPSG:3067</CRS><CRS>EPSG:3857</CRS>
   <Layer><Name>background</Name><Title>Background</Title></Layer>
   <Layer><Name>coverage.1</Name><Title>Overview</Title></Layer>
   <Layer><Name>cells</Name><Title>ENC Layer</Title></Layer>
  </Layer>
 </Capability>
</WMS_Capabilities>"""
    caps = parse_wms_capabilities(xml)
    assert [l["name"] for l in caps["layers"]] == ["background", "coverage.1", "cells"]
    enc = choose_enc(caps)
    assert enc == {"layers": "cells", "crs": "EPSG:3857", "format": "image/png"}
    url = enc_tile_url(enc, 1, 0, 0)
    assert "BBOX=-20037508.34,0.00,0.00,20037508.34" in url and "LAYERS=cells" in url and "WIDTH=512" in url
