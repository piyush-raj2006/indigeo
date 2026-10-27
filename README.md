# IndiGeo — Geospatial Risk Analytics & Intelligence Engine

IndiGeo is an enterprise-grade geospatial risk assessment platform built to evaluate multi-hazard vulnerabilities across the Indian sub-region. It provides real-time spatial scoring, deterministic risk modeling, location search, interactive GIS mapping, and transparent data provenance audits for regional disaster management and urban planning.

---

## Key Features

- **Multi-Hazard Risk Analysis:** Evaluates 8 spatial hazards—Flood, Coastal Flood, Earthquake, Landslide, Cyclone, Heatwave, Drought, and Wildfire.
- **Deterministic Rule Engine:** Transparent scoring model ($Score = 100 \times \frac{\sum (weight \times severity)}{\sum weight}$) with fallback handling when upstream feeds time out or miss data bounds.
- **Interactive GIS Map:** Built with Leaflet, featuring auto-zoom (`flyTo`) on location search, administrative boundary highlights, dynamic radius fitting, and preset shortcuts (Nagpur, Chennai, Port Blair).
- **Executive Recommendations:** Summarizes action items for Moderate and High hazard zones into clean, actionable guidance.
- **Data Provenance & Audit:** Detailed reporting on data sources (Open-Meteo, USGS ComCat, NASA FIRMS, OpenStreetMap), HTTP response statuses, confidence levels, and methodological limitations.
- **Production-Ready Architecture:** Asynchronous worker pipeline for heavy spatial computations to prevent gateway timeouts, featuring built-in health checks and environment auditing.

---

## Tech Stack

- **Frontend:** Next.js (TypeScript), React, Tailwind CSS, Leaflet / React-Leaflet
- **Backend:** Python, FastAPI, Uvicorn, Spatial/Terrain Analysis (Rasterio, DuckDB, OpenStreetMap Overpass)
- **Deployment:** Docker, Docker Compose, Nginx

---

## System Architecture & Workflow
