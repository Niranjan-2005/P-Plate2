# AI Visual Quality Inspection (P-PLATE01)

> P-Plate2 - industry plate checker for defective and normal plates.

Django + DRF + Celery/Redis service for batch visual inspection of demonstration metal plates. Original model outputs are immutable. Supervisor corrections are stored in isolated review fields. Engineers are blocked from review on the server.

## Layout

```
config/                 Django project (settings, urls, celery)
inspection/             Application
  models.py             Product, InspectionBatch, InspectionRecord, ReviewEvent, UserProfile
  utils.py              Pre-inference validation (JPEG/PNG, 5 MiB, 64–4096 px, batch 1–10)
  tasks.py              Celery inference — one process_single_inspection task per image
  views.py              Upload + supervisor review (JSON 403 denials)
  permissions.py        RBAC helpers
  admin.py              Django admin: products managed here, inspection data read-only
Model/                  Official frozen classifier and weights
```

## Frozen model

The official Round 2 model files are installed in `Model/`:

- `Model/inference.py`
- `Model/classifier.npz`
- `Model/model_metadata.json`

These files are copied unchanged from the challenge pack. Keep the
classifier weights and **0.5** decision threshold unchanged. Each valid upload
dispatches `process_single_inspection.delay(record.id)` to Celery/Redis, and
that task calls `predict()` via `inspection/ml.py`, which does not
re-threshold scores.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py seed_demo
```

Start Redis, then:

```powershell
python manage.py runserver
celery -A config worker -l info --pool=solo
```

`--pool=solo` is the reliable Windows worker pool.

## Environment

| Variable | Default | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev placeholder | Set in production |
| `DJANGO_DEBUG` | `true` | |
| `DB_ENGINE` | `sqlite` | `postgres` or `mysql` |
| `DB_NAME` `DB_USER` `DB_PASSWORD` `DB_HOST` `DB_PORT` | | Used when not SQLite |
| `CELERY_BROKER_URL` | `redis://127.0.0.1:6379/0` | |

PostgreSQL example: `DB_ENGINE=postgres`. MySQL: install `mysqlclient` and set `DB_ENGINE=mysql`.

## RBAC

The web UI and the API enforce the same scopes.

| Role | API capability | Web pages |
|---|---|---|
| Administrator | CRUD `/api/products/` | Dashboard + Settings (product list, link to Django admin). No inspection data. |
| Engineer | `POST /api/inspections/batches/`, view own batches/records | Dashboard, Batch Inspection, Results, History (own records only) |
| Supervisor | list/filter all records, `POST/PATCH/GET /api/inspections/records/<id>/review/` | Dashboard, History, Results, Supervisor Review, Audit Log |

Engineers and administrators who call the review URL receive a **403 with a JSON
`detail`**, and the record is left untouched. Product administration is expected
to happen in Django admin, so administrators deliberately have no oversight of
inspection records in either layer (the demo `admin` account is still a
superuser, which is a Django-admin permission, not an application role).

Review submissions write `reviewed_class`, `review_comment`, `reviewer` and
`review_timestamp` on the record **and** append a `ReviewEvent` row
(`previous_class → reviewed_class`, comment, reviewer, source, timestamp).
`ReviewEvent` rows are never edited or deleted, so amendments keep the earlier
decision on file. Model outputs (`predicted_class`, `confidence`,
`defect_probability`, `model_version`) are write-once and cannot be edited in
Django admin either — records and review events are read-only there.

## API

Authenticate:

```http
POST /api/auth/token/
{"username":"engineer","password":"engineer-pass"}
```

Upload (multipart, field `images`, 1–10 files, `product_id` required):

```http
POST /api/inspections/batches/
Authorization: Token <engineer-token>
```

Review (supervisor only; `GET` returns the append-only review chain):

```http
POST /api/inspections/records/1/review/
{"reviewed_class":"Defective","review_comment":"scratch on left edge"}
```

Supervisor filters on `GET /api/records/`: `status`, `predicted_class`,
`reviewed_class`, `batch`, `batch__product__product_id`,
`submitted_by__username`, `created_at`, plus `search` and `ordering`.
The web History page mirrors this with a **Review state** filter
(`pending`, `reviewed`, `upheld`, `corrected`).

Demo users from `seed_demo`: `admin` / `engineer` / `supervisor` with passwords `admin-pass`, `engineer-pass`, `supervisor-pass`.

## Validation (before inference)

Invalid files become `InspectionRecord` rows with `status=failed` and a readable `failure_reason`. Sibling files in the same batch still queue. The frozen model is not opened for those items.

- Decoded JPEG or PNG only (`PIL.Image.verify` + reload)
- Max **5 MiB**
- Width and height **64–4096** inclusive
- Batch size **1–10**

## Tests

```powershell
python manage.py test inspection
```

## Known limitations

- Batch status derives from its records and can be `queued`, `processing`,
  `completed`, `failed` or `partial` (mixed outcomes within one batch).
- The write-once guard on model output uses `select_for_update()`, which SQLite
  silently ignores. Run Celery workers against PostgreSQL or MySQL if several
  workers may touch the same batch concurrently.
- The Results page refreshes itself while items are pending (full reload every
  4 s) rather than streaming per-item progress.
- `db.sqlite3` and `media/` hold demo data; delete both and re-run `seed_demo`
  for a clean judging run.
