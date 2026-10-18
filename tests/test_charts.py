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
    layer = parse_capabilities(CAPS)
    assert layer["layer"] == "merikartta"
    assert layer["url"] == "https://example.test/wmts/default/WGS84_Pseudo-Mercator/{TileMatrix}/{y}/{x}.png"
    assert layer["matrix_ids"] == ["", "", "WGS84_Pseudo-Mercator:2", "WGS84_Pseudo-Mercator:3",
                                   "WGS84_Pseudo-Mercator:4"]
    assert layer["min_zoom"] == 2 and layer["max_zoom"] == 4


def test_falls_back_to_kvp_get_tile():
    caps = CAPS.replace('<ResourceURL format="image/png" resourceType="tile"', '<Ignored')
    layer = parse_capabilities(caps)
    assert layer["url"].startswith("https://example.test/wmts?SERVICE=WMTS&REQUEST=GetTile")
    assert "LAYER=merikartta" in layer["url"] and "TILEROW={y}" in layer["url"]


def test_no_mercator_set_returns_none():
    caps = CAPS.replace("EPSG::3857", "EPSG::4326")
    assert parse_capabilities(caps) is None
