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

### The yard: grass only

`lawn_sqft` is the grass inside the lot and nothing else. It is the lot minus every one of these:

| Removed | How it's found |
|---|---|
| House, garage, sheds, detached garages | 2022 county building footprints. These include small outbuildings; about 35,000 of the county's footprints are under 300 sq ft. |
| Driveway, walks, patios, pool decks, rock beds | 2025 imagery: concrete and asphalt are neutral grey, while lawn is tan or green |
| **Decks, play sets, trampolines, gazebos, above-ground pools, sheds not in the footprints** | **2022 QL1 LiDAR point cloud.** Anything over 0.8 ft tall that isn't a tree is cut out. Ring-shaped objects like trampolines and pool walls are filled in. Thin things like fences and posts are ignored, so lawn mowed along a fence isn't lost. |
| **Landscape beds & shrubs** | LiDAR shrubs (0.8–8 ft vegetation) merged into bed outlines, plus a mulch edge. Beds with no shrubs that hug the house are found from LiDAR return intensity, because mulch and rock come back darker than turf. |
| Dense evergreens | Tall trees with no ground returns underneath (spruce and pine, which have no grass under them). Grass under deciduous trees still counts. |
| In-ground pools | Blue or teal water in the imagery |

`parkway_sqft` is the city-owned grass strip between the lot line or sidewalk and the curb, which owners mow. It is reported separately. Mowing and fertilizer prices use **lawn + parkway**.

**Hand corrections:** on the business dashboard, open *Measure a property* → *Fix this yard*. Outline anything the scan got wrong, such as a patio built after 2022 or a vegetable garden, or add back grass that was cut out. The correction is saved per parcel and used in customer quotes and bulk CSVs. Only areas inside the lot outline count.

LiDAR tiles (~180 MB each, about 1,500 × 1,500 ft, roughly 150 homes) are downloaded on first use. Each is reduced to ~5 MB of 1-ft height rasters, cached, and the raw file deleted. The first lookup in a new area takes 15–30 s longer.

### Other measurements

| Measurement | Source | Notes |
|---|---|---|
| Lot sq ft | County parcel layer (`Accela_Dynamic/7`) | Exact legal lot polygon |
| Building / roof | 2022 planimetric building footprints | Vector polygons |
| Street frontage | Lot lines facing public right-of-way within 60 ft of a street | Uses 2022 LiDAR edge-of-pavement. Thin gaps between neighbouring lot lines are ignored. |
| Driveway | 2025 imagery, clipped to the lot | Detected so it's kept out of the yard, and measured on its own for snow and concrete pricing. Includes the apron out to the curb. If a shadow or parked car hides part of it, the visible slab is carried on to the garage. If nothing is visible, it falls back to 16 ft × the garage-to-curb distance, flagged `estimated`. |
| Public sidewalk | Imagery, sampled every 2 ft along the frontage | Detected length × 4 ft (county config) |
| Patios / other paving | Imagery | Paved areas on the lot that aren't the driveway |
| Structures | LiDAR + pool detector | Decks, sheds, play sets, etc. |
| Beds | LiDAR | Shrub beds, foundation beds, evergreens |

**Why imagery and LiDAR?** DOGIS publishes street pavement, but no public layer maps driveways, sidewalks, decks or beds for most of the county. Their 2013 impervious layer covers only eastern Omaha. The imagery is March 2025, leaf-off, at about 0.37 ft per pixel. The LiDAR is April 2022, at about 2.6 points per sq ft.

**Known limits:**
- Anything built or removed between April 2022 and March 2025 can be wrong in the LiDAR-based layers. Use *Fix this yard* for those.
- Flat mulch beds away from the house, with no shrubs, read as grass. Dormant grass and mulch are the same colour in March imagery.
- Pool covers that aren't blue or green aren't detected.

## Run it

```bash
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
| `ESTIMATOR_CACHE` | Directory for the GIS and imagery tile cache (default `.cache`). |

| `ESTIMATOR_DATA` | Where leads, settings, corrections and bulk-job CSVs are saved (default `data/`). |

## Deploy

**Render, one click.** In the Render dashboard, choose **New → Blueprint** and pick this repository. `render.yaml` sets up:
- a Docker web service on the **Standard** plan (2 GB RAM, about $25/mo). LiDAR tile processing peaks around 0.5 GB per tile, so the 512 MB plans are too small.
- a **20 GB persistent disk** at `/var/data`. It holds leads, pricing, yard corrections, bulk CSVs and the county map and LiDAR cache. A ZIP-code bulk run caches about 5 MB per LiDAR tile.
- a generated `ESTIMATOR_ADMIN_TOKEN`. Copy it from the service's *Environment* tab and paste it into the dashboard's Pricing tab to unlock `/business`.
- an optional `OPENAI_API_KEY` for AI renderings.
- a `/healthz` health check and auto-deploy on every push to `main`.

Then add your domain under **Settings → Custom Domains**. One option is `estimate.yourcompany.com` for customers, with `/business` on the same domain for your staff.

**Railway.**
1. Go to **New Project → Deploy from GitHub repo → Land-maps**. Railway builds the `Dockerfile`, and `railway.toml` sets the `/healthz` check and restart policy.
2. Right-click the service → **Attach Volume**, with mount path **`/var/data`**. Without it, leads and settings are wiped on every deploy.
3. Under **Variables**, add `ESTIMATOR_ADMIN_TOKEN` (any long random string) and, optionally, `OPENAI_API_KEY`.
4. Under **Settings → Networking**, click **Generate Domain** for a public URL, or add your own domain there.

Railway sets `PORT` itself. Keep one replica, because data is stored in files on the volume.

**Anywhere else with Docker** (Railway, Fly.io, a VPS):

```bash
docker build -t land-maps .
docker run -p 8000:8000 -v land-maps-data:/var/data -e ESTIMATOR_ADMIN_TOKEN=change-me land-maps
```

Keep `/var/data` on a persistent volume, or leads and settings are lost on every redeploy. Run a single instance: the data is stored in files on that disk.

**CI:** `.github/workflows/tests.yml` runs the tests, builds the image, and checks that the container boots, serves both pages and enforces the admin token. It runs on every PR and every push to `main`.

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
python -m pytest -q
```
