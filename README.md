# Property Estimator: lawn, fertilizer, snow & concrete

This tool measures properties from **county GIS** and **county aerial imagery**, then turns the measurements into prices for lawn mowing, fertilizer, snow removal and concrete companies.

- **Customer page (`/`):** the customer types an address with autocomplete, **clicks their lot on the map**, and sees their lawn, driveway and sidewalk square footage, a ballpark price for each service, an AI rendering of a new driveway, and a quote-request form.
- **Business dashboard (`/business`):**
  - **Bulk measure by ZIP:** measures every parcel in a ZIP code, with the property-type and measurement columns you choose, and gives you a CSV. You can add owner mailing info for direct mail.
  - **Measure a property:** look up any lot, or draw your own polygons to get an exact sq ft.
  - **Leads:** every quote request, with its measurements attached.
  - **Pricing & settings:** rates, minimums, the ± range shown to customers, company name and brand colour.

Douglas County, NE (DOGIS) is built in. Other counties are added as config (see below).

## How each number is measured (Douglas County)

| Measurement | Source | Notes |
|---|---|---|
| Lot sq ft | County parcel layer (`Accela_Dynamic/7`) | Exact legal lot polygon |
| Building / roof | 2022 planimetric building footprints | Vector polygons |
| Street frontage | Lot lines facing public right-of-way within 60 ft of a street | Uses 2022 LiDAR edge-of-pavement |
| **Driveway** | 2025 leaf-off orthoimagery, classified per pixel and clipped to the lot | Includes the apron out to the curb. If the house's shadow or a parked car hides part of the slab, the detected slab is carried on to the garage. If nothing is visible, it falls back to 16 ft × the garage-to-curb distance and is flagged `estimated`. |
| **Public sidewalk** | Imagery, sampled every 2 ft along the frontage between the lot line and the curb | Detected length × 4 ft width (set in county config). Reports 0 where there's no walk. |
| Patios / other paving | Imagery: paved areas on the lot that aren't the driveway | Pool decks, patios, side walks |
| **Lawn / turf** | Lot − buildings − all paved surfaces | |

**Why imagery?** DOGIS publishes street pavement, but driveways and sidewalks aren't in any public layer for most of the county. Their 2013 impervious layer covers only eastern Omaha. The imagery is March 2025, leaf-off, at about 0.37 ft per pixel.

**Accuracy:** good for ballpark pricing. Lot and building numbers are exact. Driveway and sidewalk numbers are usually within about 10–20%. Deep shadows, tree cover, gravel drives and dark asphalt are the weak spots. Every result says how its driveway was measured (`imagery`, `estimated` or `none_found`), so the business can check those by hand with the draw tool.

## Run it

```bash
cd estimator
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# customer:  http://localhost:8000/
# business:  http://localhost:8000/business
```

### Environment variables

| Variable | Purpose |
|---|---|
| `ESTIMATOR_ADMIN_TOKEN` | Protects `/api/admin/*` (the dashboard). **Set this in production.** Without it, the dashboard is open. Enter the token on the dashboard's Pricing tab; it is stored in that browser. |
| `OPENAI_API_KEY` | Turns on AI driveway renderings. |
| `OPENAI_IMAGE_MODEL` | Image model to use (default `gpt-image-1`). |
| `RENDER_PROVIDER` | `openai` (default). Add other providers in `app/render.py`. |
| `ESTIMATOR_CACHE` | Directory for the GIS and imagery tile cache (default `estimator/.cache`). |

Leads, settings and bulk-job CSVs are written to `estimator/data/`.

### AI renderings

The customer uploads a photo of their driveway (or the app uses the aerial view of their lot) and picks a finish: broom, exposed aggregate, stamped, or stained. An image-editing model repaints only the driveway. Prompts are in `app/render.py` → `FINISH_PROMPTS`. Without an API key, the page says renderings aren't switched on yet.

## Bulk runs

About 0.5 s per parcel with 6 workers, once the cache is warm. A ZIP like 68135 (≈10,600 residential parcels) takes about 1–1.5 hours. The CSV columns are: PIN, address, city, ZIP, class, each chosen measurement, driveway method, sidewalk detected, lat/lon, and optionally owner and mailing address. Use **Max parcels** to try a small sample first.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/suggest?q=` | Address autocomplete (county geocoder) |
| GET | `/api/locate?q=&magicKey=` | Geocode, plus the parcel PIN at that point |
| GET | `/api/parcels?bbox=w,s,e,n` | Clickable parcel outlines (GeoJSON) |
| GET | `/api/measure/{pin}?finish=` | Measurements + overlay + price estimate |
| POST | `/api/lead` | Quote request |
| POST | `/api/render` | AI rendering (multipart: `finish`, `pin`, optional `photo`) |
| GET/PUT | `/api/admin/settings` | Pricing |
| POST | `/api/admin/bulk/preview`, `/api/admin/bulk` | Count / start a ZIP job |
| GET | `/api/admin/jobs[/{id}[/rows\|/csv]]` | Job status and results |
| GET | `/api/admin/leads` | Leads |

## Adding a county

Copy `app/counties/douglas_ne.py` and point it at the new county's ArcGIS REST services:

- parcel layer and field names
- building footprints
- edge of pavement
- a geocoder
- an orthoimagery tile cache
- the local projected CRS

Then register it in `app/counties/__init__.py`. Sarpy and Lancaster counties publish similar layers. If a county has no pavement layer, sidewalk detection turns off.

## Tests

```bash
cd estimator && python -m pytest -q
```
