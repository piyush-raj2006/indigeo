from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from app.core.errors import ArchiveUnsafeError, UnsupportedUploadError, ValidationError
from app.gis.upload import export_layer, import_inspection, inspect_upload, save_upload_bytes


@pytest.fixture()
def uploads(tmp_root: Path) -> Path:
    directory = tmp_root / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def make_geojson() -> bytes:
    feature = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [88.36, 22.57]},
                "properties": {"name": "Kolkata", "population": 4500000, "hazard": "flood"},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[88.0, 22.0], [88.5, 22.0], [88.5, 22.6], [88.0, 22.6], [88.0, 22.0]]],
                },
                "properties": {"name": "Basirhat block", "population": None, "hazard": "cyclone"},
            },
        ],
    }
    return json.dumps(feature).encode()


def test_geojson_upload_is_described(uploads):
    path = uploads / "sites.geojson"
    path.write_bytes(make_geojson())
    inspection = inspect_upload(path)
    assert inspection.status == "ready"
    assert inspection.layers[0].feature_count == 2
    assert inspection.layers[0].geometry_type == "Point|Polygon"
    assert "population" in inspection.layers[0].columns
    assert inspection.layers[0].missing_values == {"population": 1}
    assert inspection.preview["type"] == "FeatureCollection"
    assert len(inspection.preview["features"]) == 2


def test_geojson_import_writes_queryable_layer(uploads, store, monkeypatch):
    from app.gis import spatial as spatial_module
    from app.core.database import init_schema

    init_schema()
    path = uploads / "sites.geojson"
    path.write_bytes(make_geojson())
    inspection = inspect_upload(path)
    monkeypatch.setattr(spatial_module, "get_spatial_store", lambda: store)
    result = import_inspection(
        inspection, path, dataset_code="user_sites", table_name="user_sites", title="Test sites", actor="tester"
    )
    assert result["features"] == 2
    assert result["rtree_index"] is True
    hits = store.features_within_radius("user_sites", 88.36, 22.57, 1_000)
    assert any(h["attributes"]["name"] == "Kolkata" for h in hits)


def test_malformed_geojson_is_rejected(uploads):
    path = uploads / "broken.geojson"
    path.write_bytes(b'{"type": "FeatureCollection", "features": [')
    inspection = inspect_upload(path)
    assert inspection.status == "rejected"
    assert inspection.errors


def test_unsupported_extension_rejected(uploads):
    path = uploads / "notes.txt"
    path.write_text("not gis data")
    inspection = inspect_upload(path)
    assert inspection.status == "rejected"
    assert "Unsupported file type" in inspection.errors[0]


def test_oversized_upload_rejected(uploads, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    with pytest.raises(Exception) as excinfo:
        save_upload_bytes(b"x" * 500, "big.geojson", staging_dir=uploads)
    assert excinfo.value.code.value == "upload_too_large"


def test_shapefile_zip_roundtrip_and_crs_reprojection(uploads, store, monkeypatch):
    from app.core.database import init_schema
    from app.gis import spatial as spatial_module

    init_schema()
    workdir = uploads / "shpsrc"
    workdir.mkdir(exist_ok=True)
    frame = gpd.GeoDataFrame(
        {"block": ["Contai I", "Deshaprana"], "risk": [3, 5]},
        geometry=[
            Polygon([(88.0, 21.8), (88.1, 21.8), (88.1, 21.9), (88.0, 21.9)]),
            Polygon([(88.2, 21.8), (88.3, 21.8), (88.3, 21.9), (88.2, 21.9)]),
        ],
        crs="EPSG:4326",
    )
    # Export as UTM 45N so the importer has to reproject.
    frame_utm = frame.to_crs("EPSG:32645")
    shp_path = workdir / "blocks.shp"
    frame_utm.to_file(shp_path, driver="ESRI Shapefile")

    zip_path = uploads / "blocks.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for sibling in workdir.glob("blocks.*"):
            archive.write(sibling, sibling.name)

    inspection = inspect_upload(zip_path)
    assert inspection.status == "ready", inspection.errors
    assert inspection.container == "shapefile_zip"
    assert inspection.layers[0].crs_input == "EPSG:32645"
    assert inspection.layers[0].reprojected is True
    assert any("Reprojected" in w for w in inspection.warnings)

    monkeypatch.setattr(spatial_module, "get_spatial_store", lambda: store)
    result = import_inspection(inspection, zip_path, dataset_code="user_blocks", table_name="user_blocks", title="Blocks")
    assert result["features"] == 2
    # Coordinates must have landed back in degrees.
    assert 87.9 < result["bbox"][0] < 88.4
    hits = store.features_containing("user_blocks", 88.05, 21.85)
    assert hits and hits[0]["attributes"]["block"] == "Contai I"


def test_zip_with_path_traversal_rejected(uploads):
    path = uploads / "evil.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../etc/blocks.shp", b"junk")
        archive.writestr("blocks.shp", b"fake shapefile body")
    inspection = inspect_upload(path)
    assert inspection.status == "rejected"
    assert "archive_unsafe" in inspection.codes
    assert any("escapes" in err.lower() for err in inspection.errors)
    assert not (uploads / "etc").exists()


def test_zip_with_nested_archive_and_executable_rejected(uploads):
    path = uploads / "smuggle.zip"
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as nested:
        nested.writestr("payload.shp", b"x")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("blocks.shp", b"fake")
        archive.writestr("installer.exe", b"MZ\x90\x00")
        archive.writestr("nested.zip", inner.getvalue())
    inspection = inspect_upload(path)
    assert inspection.status == "rejected"
    assert "archive_unsafe" in inspection.codes
    assert any("safety" in err.lower() for err in inspection.errors)


def test_shapefile_zip_bomb_rejected(uploads, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_archive_members", 1)
    path = uploads / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for i in range(50):
            archive.writestr(f"layer{i}.shp", b"fake")
    inspection = inspect_upload(path)
    assert inspection.status == "rejected"
    assert "archive_unsafe" in inspection.codes
    assert any("too many members" in err.lower() for err in inspection.errors)


def test_csv_points_and_bad_rows(uploads, store, monkeypatch):
    from app.core.database import init_schema
    from app.gis import spatial as spatial_module

    init_schema()
    path = uploads / "ward_points.csv"
    path.write_text(
        "ward,lat,lon,people\n1,22.57,88.36,1200\n2,22.60,88.40,800\n3,,88.9,600\n4,900,88.9,100\n",
        encoding="utf-8",
    )
    inspection = inspect_upload(path)
    assert inspection.status == "ready"
    assert inspection.layers[0].feature_count == 2
    assert any("out-of-range" in w or "skipped" in w for w in inspection.layers[0].warnings)

    monkeypatch.setattr(spatial_module, "get_spatial_store", lambda: store)
    import_inspection(inspection, path, dataset_code="user_wards", table_name="user_wards", title="Wards")
    assert store.count("user_wards") == 2


def test_csv_without_coordinates_rejected(uploads):
    path = uploads / "no_coords.csv"
    path.write_text("name,district\nA,24 Parganas\n", encoding="utf-8")
    inspection = inspect_upload(path)
    assert inspection.status == "rejected"
    assert any("latitude" in err.lower() or "coordinate" in err.lower() for err in inspection.errors)


def test_export_layer_geojson_and_gpkg(store, monkeypatch):
    import shapely

    from app.gis import spatial as spatial_module

    monkeypatch.setattr(spatial_module, "get_spatial_store", lambda: store)
    store.replace_layer(
        "export_test",
        [shapely.wkb.dumps(Polygon([(77.0, 20.0), (77.1, 20.0), (77.1, 20.1), (77.0, 20.1)]))],
        [{"name": "sample"}],
    )
    filename, payload, mime = export_layer("export_test", fmt="geojson")
    assert filename.endswith(".geojson") and mime == "application/geo+json"
    parsed = json.loads(payload)
    assert parsed["type"] == "FeatureCollection" and len(parsed["features"]) == 1

    gpkg_name, gpkg_bytes, gpkg_mime = export_layer("export_test", fmt="gpkg")
    assert gpkg_name.endswith(".gpkg") and gpkg_bytes.startswith(b"SQLite format 3")
    assert gpkg_mime == "application/geopackage+sqlite3"

    with pytest.raises(ValidationError):
        export_layer("export_test", fmt="kml")
