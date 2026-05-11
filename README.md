# World Labs Flask App v2

Generate Marble worlds from text, a 360° pano, or multiple images. View results as in-browser panoramas.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Three input modes

| Mode | Use when | Files |
|------|----------|-------|
| **Text** | Fictional / generic scenes | none |
| **360° Pano** | Reconstructing a real place with max fidelity | 1 equirectangular image (2:1 ratio) |
| **Multi-image** | Several normal photos of the same space | 2–8 images |

### Multi-image sub-modes

- **Auto Layout** (default checkbox): images must be of the same space, similar aspect ratio + resolution, with some overlap between views.
- **Direction control** (uncheck): tag each image with an azimuth (0°/90°/180°/270° → Front/Right/Back/Left). Best with ≤4 images.

## How many panos / images do I need?

For a real place (PG, café, venue):

1. **One 360 pano = best accuracy.** A single equirectangular gives the model complete spatial info for that spot.
2. **The API does NOT accept multiple panos in one call.** If you have an exterior pano + interior pano, generate two separate worlds, then stitch in Marble Studio Compose.
3. **Normal photos** work well in multi-image Auto Layout mode IF they're from the same space with overlap. Don't mix exterior + interior in one multi-image call.
4. **Use the text prompt** alongside any image mode to nudge style or fill in missing context.

## API endpoints used

- `POST /marble/v1/media-assets:prepare_upload` — get a signed upload URL
- `PUT <signed_url>` — upload the image bytes directly to Google Cloud Storage
- `POST /marble/v1/worlds:generate` — start generation, returns an Operation
- `GET /marble/v1/operations/{id}` — polled every 15s in a background thread
- The completed operation's `response.assets.imagery.pano_url` is what the viewer renders

## Files

```
worldlabs_app_v2/
├── app.py                  # Flask app
├── requirements.txt
├── README.md
├── templates/
│   ├── index.html          # form with mode tabs + worlds list
│   └── viewer.html         # A-Frame pano viewer
└── uploads/                # local copy of user-uploaded files
```

## Notes

- API key is read from `WORLDLABS_API_KEY` env var, with a hardcoded fallback. Move it out of source before sharing.
- Generation takes 5–10 min per world.
- The response also includes Gaussian splat URLs (`assets.splats.spz_urls`) and a collider mesh (`assets.mesh.collider_mesh_url`) — useful if you want to render an actual 3D walkthrough later instead of just the flat pano.
