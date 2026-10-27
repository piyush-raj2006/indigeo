from __future__ import annotations

"""Multi-hazard assessors.

Each assessor reads only the real, already-gathered evidence in an
:class:`~app.services.evidence.EvidenceContext` and turns measured values into weighted
:class:`~app.services.risk.Factor` contributions. A hazard is therefore a transparent,
rule-based read of observed data - never a prediction and never a filled-in number.

The eight assessed hazards are the ones that can be grounded in a source available to
this build. Where a required layer genuinely does not exist here (official seismic
zoning, river/floodplain polygons, cyclone tracks, land cover), the dependent factor is
marked unavailable so the hazard's confidence drops and the gap is stated in warnings.
"""

from typing import Any, Callable

from .evidence import COAST_SEARCH_MAX_M, EvidenceContext
from .risk import Factor, HazardSignal, saturate

COASTAL_APPLICABLE_M = 100_000.0  # within 100 km of the sea, coastal hazards apply
HILLY_SLOPE_DEG = 7.0  # above this mean slope, landslide becomes relevant


# -- small evidence accessors -------------------------------------------------
def _terrain(ctx: EvidenceContext) -> dict[str, Any]:
    # terrain_at() carries summary=None when no elevation tile could be retrieved.
    return (ctx.terrain or {}).get("summary") or {}


def _recent_series(ctx: EvidenceContext) -> list[dict[str, Any]]:
    return ctx.weather_recent.get("series", []) if ctx.weather_recent else []


def _recent_total_mm(ctx: EvidenceContext, days: int) -> float | None:
    series = _recent_series(ctx)
    if not series:
        return None
    window = series[-days:]
    values = [d.get("precipitation_mm") for d in window if d.get("precipitation_mm") is not None]
    return float(sum(values)) if values else None


def _recent_max(ctx: EvidenceContext, key: str, days: int) -> float | None:
    series = _recent_series(ctx)
    values = [d.get(key) for d in series[-days:] if d.get(key) is not None]
    return float(max(values)) if values else None


def _latest_soil(ctx: EvidenceContext) -> float | None:
    for entry in reversed(_recent_series(ctx)):
        value = entry.get("soil_moisture_m3m3")
        if value is not None:
            return float(value)
    return None


def _forecast(ctx: EvidenceContext) -> dict[str, Any]:
    return ctx.weather_forecast.get("daily", {}) if ctx.weather_forecast else {}


def _forecast_sum(ctx: EvidenceContext, key: str) -> float | None:
    values = [v for v in (_forecast(ctx).get(key) or []) if v is not None]
    return float(sum(values)) if values else None


def _forecast_max(ctx: EvidenceContext, key: str) -> float | None:
    values = [v for v in (_forecast(ctx).get(key) or []) if v is not None]
    return float(max(values)) if values else None


def _unavailable(name: str, source: str, note: str, weight: float = 1.0) -> Factor:
    return Factor(name=name, source=source, weight=weight, available=False, note=note)


# -- live Indian feed accessors ----------------------------------------------
def _feed_entry(ctx: EvidenceContext, key: str) -> dict[str, Any]:
    return ((ctx.alerts or {}).get("feeds") or {}).get(key) or {}


def _live_factor(
    ctx: EvidenceContext,
    feed_key: str,
    name: str,
    weight: float,
    *,
    label: str,
) -> Factor | None:
    """A factor that exists only when the operator configured its feed.

    An unconfigured feed returns ``None``: it is a deployment choice, not an observation
    that was missed at this point, and adding it as an unavailable factor would depress
    every hazard's confidence three times over. A configured feed that failed *does* add
    an unavailable factor, because then the data was expected and did not arrive.
    """
    entry = _feed_entry(ctx, feed_key)
    # The bundle entry normally carries its own provider key; the fallback keeps the
    # citation a real provider name even for a context built without an alerts block.
    provider = entry.get("provider") or {
        "imd": "imd_alerts",
        "cwc": "cwc_gauges",
        "bhuvan": "bhuvan_hazards",
    }.get(feed_key, feed_key)
    status = entry.get("status")
    # No entry at all means the feed block was never gathered (an offline fixture or an
    # older stored context), which is not the same as a configured feed that failed.
    if status in (None, "unconfigured", "available"):
        return None
    reason = entry.get("reason") or "feed did not answer"
    return _unavailable(name, provider, f"{label} is configured but unavailable: {reason}", weight)


def _feed_status(ctx: EvidenceContext, feed_key: str) -> str | None:
    return _feed_entry(ctx, feed_key).get("status")


def _imd_alert_factor(
    ctx: EvidenceContext,
    name: str,
    tags: set[str],
    weight: float,
) -> Factor | None:
    """Blend the strongest IMD bulletin whose text tags make it relevant to a hazard."""
    base = _live_factor(ctx, "imd", name, weight, label="IMD warning feed")
    if base is not None or _feed_status(ctx, "imd") != "available":
        return base
    entry = _feed_entry(ctx, "imd")
    matched = entry.get("matched") or []
    relevant = [alert for alert in matched if tags & set(alert.get("tags") or [])]
    scored = [alert for alert in relevant if alert.get("severity_score") is not None]
    severity = max((float(alert["severity_score"]) for alert in scored), default=0.0)
    titles = [alert.get("title") for alert in relevant if alert.get("title")][:5]
    note = None
    if not relevant:
        note = "No IMD alert matched this radius, by geometry or by resolved state/district name."
    elif not scored:
        note = (
            "Matched IMD alert(s) did not state a CAP severity level, so they are reported but "
            "contribute 0 to the score rather than an assumed severity."
        )
    return Factor(
        name,
        "imd_alerts",
        severity=severity,
        weight=weight,
        observed={
            "matched_alerts": len(relevant),
            "titles": titles,
            "matched_by": sorted({alert.get("matched_by") for alert in relevant if alert.get("matched_by")}),
        },
        note=note,
    )


def _cwc_gauge_factor(ctx: EvidenceContext, name: str, weight: float) -> Factor | None:
    """CWC gauge level relative to its own warning/danger thresholds."""
    base = _live_factor(ctx, "cwc", name, weight, label="CWC river-gauge feed")
    if base is not None or _feed_status(ctx, "cwc") != "available":
        return base
    entry = _feed_entry(ctx, "cwc")
    severity = entry.get("max_severity")
    nearest = entry.get("nearest") or {}
    note = None
    if not entry.get("count"):
        note = (
            "No CWC gauge was found within the search radius; that is a coverage gap, not a "
            "statement that the river is below flood level."
        )
    elif severity is None:
        note = (
            "The nearest CWC gauge does not carry warning/danger thresholds, so its level is "
            "reported but not classified as a flood state."
        )
    return Factor(
        name,
        "cwc_gauges",
        severity=float(severity) if severity is not None else 0.0,
        weight=weight,
        observed={
            "gauges_within_radius": entry.get("count"),
            "nearest_station": nearest.get("station"),
            "nearest_river": nearest.get("river"),
            "nearest_level_m": nearest.get("level_m"),
            "warning_level_m": nearest.get("warning_level_m"),
            "danger_level_m": nearest.get("danger_level_m"),
            "distance_km": round(nearest["distance_m"] / 1000.0, 1) if nearest.get("distance_m") is not None else None,
        },
        note=note,
    )


def _bhuvan_factor(
    ctx: EvidenceContext,
    hazard: str,
    name: str,
    weight: float,
) -> Factor | None:
    """Bhuvan/NDMA susceptibility or inundation polygons intersecting this radius."""
    base = _live_factor(ctx, "bhuvan", name, weight, label="Bhuvan/NDMA hazard feed")
    if base is not None or _feed_status(ctx, "bhuvan") != "available":
        return base
    entry = _feed_entry(ctx, "bhuvan")
    group = (entry.get("hazards") or {}).get(hazard)
    note = None
    if group is None:
        note = (
            f"No mapped {hazard} polygon intersected this radius; absence from the hazard map "
            "is not evidence of low susceptibility."
        )
    return Factor(
        name,
        "bhuvan_hazards",
        severity=float(group["max_severity"]) if group else 0.0,
        weight=weight,
        observed={
            "mapped_features": (group or {}).get("features", 0),
            "classes": sorted({str(value) for value in (group or {}).get("classes", [])})[:5],
            "features_in_feed": entry.get("features"),
        },
        note=note,
    )


def _coastal_factor(name: str, ctx: EvidenceContext, reach_m: float, weight: float) -> Factor:
    """Proximity to the sea, measured against the loaded national outline.

    A point with no coastline inside the search radius is a measurement (it is far from
    the sea), so it scores zero exposure. Only a coastline layer that never answered is
    a gap, and that is what lowers the hazard's confidence.
    """
    if not ctx.source_available("coastline"):
        return _unavailable(
            name, "coastline", "The national coastline outline is not loaded, so distance to the sea is unknown.", weight=weight
        )
    coast_m = ctx.coast_distance_m
    if coast_m is None:
        return Factor(
            name,
            "coastline",
            severity=0.0,
            weight=weight,
            observed={"distance_to_india_coastline_km": None, "at_least_km": COAST_SEARCH_MAX_M / 1000.0},
            note=f"No coastline within {COAST_SEARCH_MAX_M / 1000:.0f} km.",
        )
    return Factor(
        name,
        "coastline",
        severity=1.0 - saturate(coast_m, 0, reach_m),
        weight=weight,
        observed={"distance_to_india_coastline_km": round(coast_m / 1000.0, 1)},
    )


def _shoreline_note(terrain: dict[str, Any]) -> str | None:
    """Flag a point whose own DEM pixel is at or below sea level inside a partly-land window.

    ``bathymetry_only`` only catches windows that are wholly submerged. At a shoreline the
    sampled window reaches land while the exact coordinate is intertidal water, and a 0 m
    read there is not a land elevation even though the severity it produces is defensible.
    """
    point_m = terrain.get("elevation_at_point_m")
    if point_m is None or terrain.get("bathymetry_only") or float(point_m) > 0.0:
        return None
    return (
        f"The DEM reads {point_m} m at this exact coordinate while the sampled window reaches "
        f"{terrain.get('elevation_max_m')} m, so the point lies on the shoreline or in intertidal "
        "water. The elevation in this factor's observations is that bathymetric/zero reading, not "
        "a measured land height."
    )


# -- assessors ---------------------------------------------------------------
def assess_earthquake(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="earthquake", evidence_kind="historical")
    signal.provenance = list(ctx.provenance)
    seismic = ctx.seismicity
    if not ctx.source_available("usgs_earthquakes") or not seismic:
        # Same factor name as the measured branch above: the availability flag, not a
        # renamed placeholder, is what tells the reader this driver was never observed.
        signal.factors.append(_unavailable("regional_seismicity_density", "usgs_earthquakes", "USGS ComCat did not answer."))
        signal.unavailable_sources.append("usgs_earthquakes")
    else:
        density = seismic.get("events_within_150km") or 0
        signal.factors.append(
            Factor(
                "regional_seismicity_density",
                "usgs_earthquakes",
                severity=saturate(density, 0, 15),
                weight=1.0,
                observed={"events_within_150km": density, "window_years": seismic.get("window_years")},
            )
        )
        nearest_km = seismic.get("nearest_event_km")
        signal.factors.append(
            Factor(
                "proximity_to_epicentre",
                "usgs_earthquakes",
                severity=1.0 - saturate(nearest_km, 0, 500) if nearest_km is not None else 0.0,
                weight=1.1,
                observed={"nearest_event_km": nearest_km},
            )
        )
        max_mag = seismic.get("max_magnitude_within_150km") or seismic.get("max_magnitude")
        signal.factors.append(
            Factor(
                "maximum_historical_magnitude",
                "usgs_earthquakes",
                severity=saturate(max_mag, 4.5, 8.5) if max_mag else 0.0,
                weight=1.3,
                observed={"max_magnitude": max_mag},
            )
        )
        signal.details = {
            "events_within_150km": density,
            "nearest_event_km": nearest_km,
            "max_magnitude": seismic.get("max_magnitude"),
        }
    signal.factors.append(
        _unavailable(
            "official_seismic_zone",
            "not_loaded",
            "The BIS seismic zoning map is not ingested in this build; epicenter climatology "
            "is not a substitute for official site zoning.",
            weight=1.5,
        )
    )
    signal.unavailable_sources.append("bis_seismic_zoning")
    signal.warnings.append("Earthquake score is a historical seismicity climatology, not a forecast.")
    return signal


def assess_flood(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="flood")
    terrain = _terrain(ctx)
    recent_mm = _recent_total_mm(ctx, 15)
    forecast_mm = _forecast_sum(ctx, "precipitation_sum")
    if recent_mm is None:
        signal.factors.append(_unavailable("antecedent_rainfall", "open_meteo", "Recent rainfall window unavailable."))
        signal.unavailable_sources.append("open_meteo_recent")
    else:
        signal.factors.append(
            Factor("antecedent_rainfall", "open_meteo", severity=saturate(recent_mm, 0, 150), observed={"rain_15d_mm": round(recent_mm, 1)})
        )
    if forecast_mm is None:
        signal.factors.append(_unavailable("forecast_rainfall", "open_meteo", "Forward forecast unavailable."))
        signal.unavailable_sources.append("open_meteo_forecast")
    else:
        signal.factors.append(
            Factor("forecast_rainfall", "open_meteo", severity=saturate(forecast_mm, 0, 120), weight=1.2, observed={"rain_7d_mm": round(forecast_mm, 1)})
        )
    if terrain.get("data_gap") or terrain.get("elevation_mean_m") is None or not ctx.source_available("dem"):
        signal.factors.append(_unavailable("terrain_depression", "dem", "No usable terrain block for this point."))
        signal.unavailable_sources.append("dem")
    elif terrain.get("elevation_at_point_m") is None or terrain.get("elevation_min_m") is None or terrain.get("elevation_max_m") is None:
        signal.factors.append(
            _unavailable("terrain_depression", "dem", "The terrain block has no point elevation or window extent to measure against.")
        )
        signal.unavailable_sources.append("dem")
    else:
        here = float(terrain["elevation_at_point_m"])
        low = float(terrain["elevation_min_m"])
        high = float(terrain["elevation_max_m"])
        relief = high - low
        if relief <= 0.0:
            depression = 0.5
            note = (
                "The sampled DEM window is flat to within its own resolution, so where the point sits "
                "in the local drainage cannot be measured; scored neutrally rather than guessed."
            )
        else:
            # Depth below the top of the surrounding window, normalised by that window's relief.
            depression = 1.0 - saturate(here - low, 0.0, relief)
            note = None
        shore = _shoreline_note(terrain)
        signal.factors.append(
            Factor(
                "terrain_depression",
                "dem",
                severity=depression,
                observed={
                    "elevation_at_point_m": terrain.get("elevation_at_point_m"),
                    "relief_m": terrain.get("elevation_relief_m"),
                    "window_min_m": round(low, 1),
                    "window_max_m": round(high, 1),
                    "local_low_point": bool(terrain.get("local_low_point")),
                },
                note=note or shore,
            )
        )
        if shore:
            signal.warnings.append(shore)
    imd = _imd_alert_factor(ctx, "imd_heavy_rain_alert", {"rain", "thunderstorm", "flood"}, 1.2)
    if imd is not None:
        signal.factors.append(imd)
    gauge = _cwc_gauge_factor(ctx, "cwc_river_gauge_level", 1.4)
    if gauge is not None:
        signal.factors.append(gauge)
    inundation = _bhuvan_factor(ctx, "flood", "mapped_flood_inundation", 1.3)
    if inundation is not None:
        signal.factors.append(inundation)
    live = [factor for factor in (imd, gauge, inundation) if factor is not None]
    signal.details["live_alerts"] = {
        "feeds_used": [factor.source for factor in live if factor.available],
        "feeds_missing": [factor.source for factor in live if not factor.available],
    }
    signal.factors.append(
        _unavailable(
            "river_floodplain_proximity",
            "not_loaded",
            "No national river/floodplain layer is ingested, so true fluvial exposure cannot be measured.",
            weight=1.5,
        )
    )
    signal.unavailable_sources.append("river_floodplain")
    signal.evidence_kind = "forecast" if forecast_mm is not None else "model"
    signal.warnings.append("Flood score reflects rainfall and local depression only; riverine floodplain exposure is a data gap.")
    if imd is not None:
        signal.warnings.append("IMD is a warning bulletin, not an observation of flooding at this point.")
    return signal


def assess_coastal_flood(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="coastal_flood")
    coast = _coastal_factor("coastal_proximity", ctx, reach_m=50_000.0, weight=1.4)
    signal.factors.append(coast)
    if not coast.available:
        signal.unavailable_sources.append("coastline")
    terrain = _terrain(ctx)
    point_m = terrain.get("elevation_at_point_m")
    shore = _shoreline_note(terrain)
    if point_m is None or terrain.get("bathymetry_only"):
        signal.factors.append(_unavailable("low_elevation", "dem", "No land elevation for this point."))
        signal.unavailable_sources.append("dem")
    else:
        signal.factors.append(
            Factor(
                "low_elevation",
                "dem",
                severity=1.0 - saturate(point_m, 0, 10),
                weight=1.3,
                observed={"elevation_m": point_m},
                note=shore,
            )
        )
        if shore:
            signal.warnings.append(shore)
    gust = _forecast_max(ctx, "wind_gusts_10m_max")
    if gust is None:
        signal.factors.append(_unavailable("storm_wind_forcing", "open_meteo", "No forecast wind."))
        signal.unavailable_sources.append("open_meteo_forecast")
    else:
        signal.factors.append(Factor("storm_wind_forcing", "open_meteo", severity=saturate(gust, 40, 140), observed={"max_gust_kmh": round(gust, 1)}))
    inundation = _bhuvan_factor(ctx, "flood", "mapped_flood_inundation", 1.2)
    if inundation is not None:
        signal.factors.append(inundation)
    warning = _imd_alert_factor(ctx, "imd_coastal_weather_warning", {"cyclone", "rain", "wind"}, 1.2)
    if warning is not None:
        signal.factors.append(warning)
    signal.factors.append(
        _unavailable("storm_surge_model", "not_loaded", "A hydrodynamic storm-surge model is out of scope for this build.", weight=1.5)
    )
    signal.unavailable_sources.append("storm_surge_model")
    signal.evidence_kind = "forecast" if gust is not None else "model"
    signal.warnings.append("Coastal flooding here is a proximity + elevation proxy; no surge height is modelled.")
    return signal


def assess_cyclone(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="cyclone")
    coast = _coastal_factor("coastal_exposure", ctx, reach_m=COASTAL_APPLICABLE_M, weight=1.5)
    signal.factors.append(coast)
    if not coast.available:
        signal.unavailable_sources.append("coastline")
    coast_m = ctx.coast_distance_m
    wind = _forecast_max(ctx, "wind_speed_10m_max")
    gust = _forecast_max(ctx, "wind_gusts_10m_max")
    if wind is None and gust is None:
        signal.factors.append(_unavailable("forecast_wind", "open_meteo", "No forward wind forecast."))
        signal.unavailable_sources.append("open_meteo_forecast")
    else:
        best = max(filter(None, [wind, gust]), default=None)
        signal.factors.append(
            Factor("forecast_wind", "open_meteo", severity=saturate(best, 40, 180), weight=1.3, observed={"max_forecast_wind_kmh": round(best, 1) if best else None})
        )
    warning = _imd_alert_factor(ctx, "imd_cyclone_warning", {"cyclone", "wind"}, 1.5)
    if warning is not None:
        signal.factors.append(warning)
    signal.factors.append(
        _unavailable("historical_storm_tracks", "not_loaded", "No cyclone track climatology is ingested for this coast.", weight=1.4)
    )
    signal.unavailable_sources.append("storm_tracks")
    signal.evidence_kind = "forecast"
    if coast_m is not None and coast_m > COASTAL_APPLICABLE_M:
        signal.warnings.append("Inland location: cyclone wind exposure falls sharply with distance from the sea.")
    return signal


def assess_landslide(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="landslide")
    terrain = _terrain(ctx)
    slope = terrain.get("slope_deg")
    if slope is None or terrain.get("data_gap") or terrain.get("bathymetry_only"):
        signal.factors.append(_unavailable("slope", "dem", "No usable slope for this point."))
        signal.unavailable_sources.append("dem")
    else:
        signal.factors.append(Factor("slope", "dem", severity=saturate(slope, 5, 35), weight=1.5, observed={"slope_deg": slope}))
        signal.details["mean_slope_deg"] = slope
        signal.details["applicable"] = slope >= HILLY_SLOPE_DEG
    relief = terrain.get("elevation_relief_m")
    if relief is not None and not terrain.get("data_gap"):
        signal.factors.append(Factor("local_relief", "dem", severity=saturate(relief, 0, 500), weight=1.2, observed={"relief_m": relief}))
    trigger = None
    recent_mm = _recent_total_mm(ctx, 15)
    forecast_mm = _forecast_sum(ctx, "precipitation_sum")
    if recent_mm is not None or forecast_mm is not None:
        trigger = (recent_mm or 0.0) + (forecast_mm or 0.0)
        signal.factors.append(
            Factor("rainfall_trigger", "open_meteo", severity=saturate(trigger, 0, 200), weight=1.1, observed={"rain_total_mm": round(trigger, 1)})
        )
    else:
        signal.factors.append(_unavailable("rainfall_trigger", "open_meteo", "No rainfall to assess a wetting trigger."))
        signal.unavailable_sources.append("open_meteo")
    susceptibility = _bhuvan_factor(ctx, "landslide", "bhuvan_landslide_susceptibility", 1.2)
    if susceptibility is not None:
        signal.factors.append(susceptibility)
    warning = _imd_alert_factor(ctx, "imd_heavy_rain_alert", {"rain", "landslide"}, 1.0)
    if warning is not None:
        signal.factors.append(warning)
    signal.factors.append(
        _unavailable("geology_landcover", "not_loaded", "No lithology or land-cover layer is ingested; slope + rain only.", weight=1.4)
    )
    signal.unavailable_sources.append("geology_landcover")
    signal.evidence_kind = "model"
    signal.warnings.append("Landslide score is a slope/relief/rainfall susceptibility proxy, not a stability analysis.")
    return signal


def assess_heatwave(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="heatwave")
    fc_tmax = _forecast_max(ctx, "temperature_2m_max")
    if fc_tmax is None:
        signal.factors.append(_unavailable("forecast_heat", "open_meteo", "No forward temperature forecast."))
        signal.unavailable_sources.append("open_meteo_forecast")
    else:
        signal.factors.append(
            Factor("forecast_heat", "open_meteo", severity=saturate(fc_tmax, 30, 47), weight=1.6, observed={"forecast_tmax_c": round(fc_tmax, 1)})
        )
    recent_tmax = _recent_max(ctx, "tmax_c", 30)
    if recent_tmax is not None:
        signal.factors.append(Factor("recent_heat_basis", "open_meteo", severity=saturate(recent_tmax, 28, 45), weight=0.9, observed={"recent_tmax_c": round(recent_tmax, 1)}))
    else:
        signal.factors.append(_unavailable("recent_heat_basis", "open_meteo", "No recent temperature window."))
        signal.unavailable_sources.append("open_meteo_recent")
    alert = _imd_alert_factor(ctx, "imd_heat_wave_alert", {"heat"}, 1.3)
    if alert is not None:
        signal.factors.append(alert)
    terrain = _terrain(ctx)
    elevation = terrain.get("elevation_at_point_m")
    if elevation is not None and not terrain.get("bathymetry_only"):
        # Altitude moderates heat, so this factor is scored as (1 - elevation influence).
        signal.factors.append(
            Factor(
                "altitude_mitigation",
                "dem",
                severity=1.0 - saturate(elevation, 0, 1500),
                weight=0.7,
                observed={"elevation_m": elevation},
                note=_shoreline_note(terrain),
            )
        )
    else:
        signal.factors.append(_unavailable("altitude_mitigation", "dem", "No land elevation to temper the heat estimate."))
    signal.evidence_kind = "forecast"
    signal.warnings.append("Heat is taken from ERA5/ICON reanalysis and forecast, not a weather-station observation.")
    return signal


def assess_drought(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="drought", evidence_kind="model")
    total_45 = _recent_total_mm(ctx, 45)
    if total_45 is None:
        signal.factors.append(_unavailable("precipitation_deficit", "open_meteo", "No rainfall window for a deficit estimate."))
        signal.unavailable_sources.append("open_meteo_recent")
    else:
        signal.factors.append(
            Factor("precipitation_deficit", "open_meteo", severity=1.0 - saturate(total_45, 0, 250), weight=1.4, observed={"rain_45d_mm": round(total_45, 1)})
        )
    soil = _latest_soil(ctx)
    if soil is None:
        signal.factors.append(_unavailable("root_zone_soil_moisture", "open_meteo", "No soil moisture series."))
        signal.unavailable_sources.append("soil_moisture")
    else:
        signal.factors.append(
            Factor("root_zone_soil_moisture", "open_meteo", severity=1.0 - saturate(soil, 0.05, 0.40), weight=1.3, observed={"soil_m3m3": round(soil, 3)})
        )
    heat = _forecast_max(ctx, "temperature_2m_max")
    if heat is not None:
        signal.factors.append(Factor("evaporative_demand", "open_meteo", severity=saturate(heat, 28, 45), weight=0.8, observed={"forecast_tmax_c": round(heat, 1)}))
    signal.factors.append(
        _unavailable("vegetation_stress", "not_loaded", "No NDVI/vegetation satellite layer ingested; crop stress is not observed.", weight=1.4)
    )
    signal.unavailable_sources.append("ndvi_vegetation")
    signal.factors.append(
        _unavailable("groundwater_reservoir", "not_loaded", "No groundwater or reservoir storage data ingested.", weight=1.2)
    )
    signal.unavailable_sources.append("groundwater")
    signal.warnings.append("Drought is inferred from ERA5 rainfall/soil reanalysis; it is not an IMD or ground-truth drought declaration.")
    return signal


def assess_wildfire(ctx: EvidenceContext) -> HazardSignal:
    signal = HazardSignal(hazard="wildfire")
    status = ctx.source_status.get("firms")
    if status == "available":
        count = ctx.fires.get("count", 0)
        signal.factors.append(
            Factor("active_fire_detection", "firms", severity=saturate(count, 0, 40), weight=1.6, observed={"hotspots_7d": count})
        )
        signal.evidence_kind = "observed"
    else:
        signal.factors.append(
            _unavailable(
                "active_fire_detection",
                "firms",
                "FIRMS active-fire data is " + ("not configured (set FIRMS_MAP_KEY)" if status == "unconfigured" else "unavailable")
                + "; no detections are substituted.",
                weight=1.6,
            )
        )
        signal.unavailable_sources.append("firms")
        signal.evidence_kind = "model"
    dryness = None
    soil = _latest_soil(ctx)
    heat = _forecast_max(ctx, "temperature_2m_max")
    if soil is not None and heat is not None:
        dryness = saturate(heat, 28, 45) * (1.0 - saturate(soil, 0.05, 0.40))
        signal.factors.append(
            Factor("fuel_dryness", "open_meteo", severity=dryness, weight=1.0, observed={"combined_dry_index": round(dryness, 3)})
        )
    else:
        signal.factors.append(_unavailable("fuel_dryness", "open_meteo", "No soil/temperature basis for dryness."))
        signal.unavailable_sources.append("open_meteo")
    signal.factors.append(
        _unavailable("vegetation_fuel_load", "not_loaded", "No land-cover/fuel layer ingested; fire spread cannot be characterised.", weight=1.4)
    )
    signal.unavailable_sources.append("vegetation_fuel")
    signal.warnings.append("Wildfire needs active-fire observations; without FIRMS it is a dryness-only proxy.")
    return signal


ASSESSORS: dict[str, Callable[[EvidenceContext], HazardSignal]] = {
    "earthquake": assess_earthquake,
    "flood": assess_flood,
    "coastal_flood": assess_coastal_flood,
    "cyclone": assess_cyclone,
    "landslide": assess_landslide,
    "heatwave": assess_heatwave,
    "drought": assess_drought,
    "wildfire": assess_wildfire,
}

HAZARDS: tuple[str, ...] = tuple(ASSESSORS)


def assess_context(ctx: EvidenceContext, hazards: tuple[str, ...] | list[str] | None = None) -> list[HazardSignal]:
    """Run the requested assessors against an already-gathered evidence context."""
    selected = hazards or HAZARDS
    signals: list[HazardSignal] = []
    for hazard in selected:
        assess = ASSESSORS.get(hazard)
        if assess is None:
            continue
        signals.append(assess(ctx))
    return signals


__all__ = [
    "ASSESSORS",
    "HAZARDS",
    "assess_context",
    "assess_coastal_flood",
    "assess_cyclone",
    "assess_drought",
    "assess_earthquake",
    "assess_flood",
    "assess_heatwave",
    "assess_landslide",
    "assess_wildfire",
]
