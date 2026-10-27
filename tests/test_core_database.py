from __future__ import annotations

"""The relational store has no migration runner, so ``init_schema`` performs the one
additive upgrade this build needs. These tests pin that it is additive and idempotent:
a store written by an older build keeps its rows and gains the drawn-area columns.
"""

from sqlalchemy import create_engine, inspect, text

from app.core.database import _upgrade_hazard_analyses


def test_an_existing_store_gains_the_drawn_area_columns_without_losing_rows(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE hazard_analyses (id INTEGER PRIMARY KEY, analysis_uuid VARCHAR(36))")
        )
        connection.execute(text("INSERT INTO hazard_analyses (analysis_uuid) VALUES ('legacy-run')"))

    _upgrade_hazard_analyses(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("hazard_analyses")}
    assert {"geometry_kind", "wkt"} <= columns
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT analysis_uuid, geometry_kind, wkt FROM hazard_analyses"
        ).one()
    assert row.analysis_uuid == "legacy-run"
    assert row.geometry_kind == "point", "a row written before the columns existed is a point run"
    assert row.wkt is None

    _upgrade_hazard_analyses(engine)  # running it again must be a no-op
