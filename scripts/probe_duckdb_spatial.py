import json

import duckdb

con = duckdb.connect("data/_probe.duckdb")
con.execute("LOAD spatial")

poly = {
    "type": "Polygon",
    "coordinates": [[[77.5, 22.5], [78.5, 22.5], [78.5, 23.5], [77.5, 23.5], [77.5, 22.5]]],
}
gj = json.dumps(poly)

con.execute("DROP TABLE IF EXISTS t")
con.execute("CREATE TABLE t(geom GEOMETRY, name VARCHAR)")
con.execute("INSERT INTO t VALUES (ST_GeomFromGeoJSON(?), 'dist')", [gj])

checks: list[tuple[str, str, list]] = [
    ("ST_Intersects param", "SELECT ST_Intersects(geom, ST_GeomFromGeoJSON(?)) FROM t", [gj]),
    ("ST_Distance_Spheroid", "SELECT ST_Distance_Spheroid(ST_Point(78,23), ST_Point(79,24))", []),
    ("ST_Buffer", "SELECT ST_AsGeoJSON(ST_Buffer(geom, 0.1)) FROM t", []),
    ("spatial join", "SELECT count(*) FROM t, (SELECT ST_Point(78,23) AS p) q WHERE ST_Contains(t.geom, q.p)", []),
    ("ST_Union_Agg", "SELECT ST_AsGeoJSON(ST_Union_Agg(geom)) FROM t", []),
    ("ST_Transform 4326->32643", "SELECT ST_AsText(ST_Transform(ST_GeomFromText('POINT(78 23)'),4326,32643))", []),
    ("ST_Length", "SELECT ST_Length(ST_GeomFromText('LINESTRING(78 23, 79 23)'))", []),
    ("ST_Length_WGS84", "SELECT ST_Length(ST_GeomFromText('LINESTRING(78 23, 79 23)',true))", []),
    ("ST_MakeValid", "SELECT ST_AsText(ST_MakeValid(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))')))", []),
    ("ST_IsValid", "SELECT ST_IsValid(ST_GeomFromText('POLYGON((0 0,2 2,2 0,0 2,0 0))'))", []),
    ("ST_Within", "SELECT ST_Within(ST_Point(78,23), geom) FROM t", []),
    ("ST_Intersection", "SELECT ST_AsGeoJSON(ST_Intersection(geom, ST_GeomFromText('POLYGON((77 22,79 22,79 24,77 24,77 22))'))) FROM t", []),
    ("COPY TO GEOJSON", "SELECT 1", []),
    ("ST_AsWKB roundtrip", "SELECT ST_AsWKB(geom) FROM t", []),
    ("RTREE re-create", "CREATE INDEX IF NOT EXISTS rt2 ON t USING RTREE (geom)", []),
    ("ST_ClosestPoint", "SELECT ST_AsText(ST_ClosestPoint(geom, ST_Point(80,25))) FROM t", []),
    ("ST_Perimeter", "SELECT ST_Perimeter(geom) FROM t", []),
    ("bbox filter", "SELECT count(*) FROM t WHERE geom && ST_MakeEnvelope(77,22,79,24)", []),
]

for label, sql, args in checks:
    try:
        res = con.execute(sql, args).fetchall()
        print(f"OK  {label}: {str(res)[:100]}")
    except Exception as exc:
        print(f"NO  {label}: {type(exc).__name__}: {str(exc)[:120]}")

try:
    con.execute("COPY t TO 'data/_probe_out.geojson' WITH (FORMAT GDAL, DRIVER 'GeoJSON')")
    print("OK  COPY TO GDAL GeoJSON")
except Exception as exc:
    print("NO  COPY GDAL driver:", str(exc)[:120])

try:
    df = con.execute("SELECT * FROM st_read('data/_probe_out.geojson')").df()
    print("OK  st_read:", df.shape, list(df.columns))
except Exception as exc:
    print("NO  st_read:", str(exc)[:120])
