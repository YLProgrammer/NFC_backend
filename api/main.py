"""
main.py — API de matching des dates de création SIRENE pour NFC Tracker.

Déployée sur Render (plan gratuit, 512 Mo RAM / 0.1 CPU). Au démarrage / au
réveil du service (après mise en veille), télécharge simplement le cache
SIRENE DÉJÀ FILTRÉ ET DÉCOUPÉ PAR DÉPARTEMENT publié par le job GitHub Actions
(build/build_cache.py) — aucun téléchargement du fichier France entière brut,
aucun filtrage DuckDB lourd ici : exprès, pour rester dans les limites du plan
gratuit.

Le découpage par département (fait une fois par mois côté build) est ce qui
permet à CETTE requête de rester rapide même sur 0.1 CPU : on ne lit jamais
que les quelques Mo du/des département(s) concerné(s), jamais les ~500 Mo
France entière — sans ça, la requête est trop lente pour le plan gratuit
Render et se fait couper par l'infrastructure avant d'avoir fini.

Endpoint principal :

    POST /match
    Entrée  : { "city": "...", "entries": [{id, name, lat, lon, postcode, address}, ...] }
              (= exactement le format exporté par le bouton "Exporter la liste" du site)
    Sortie  : { type, version, city, exportedAt, entries: [{id, name, lat, lon, creationDate}, ...] }
              (= exactement le format attendu par le bouton "Importer des dates" du site,
                 donc réutilisable tel quel côté frontend)

Lancement en local pour tester :
    pip install -r api/requirements.txt
    uvicorn api.main:app --reload
"""
import os
import shutil
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import duckdb
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from sirene_common import match_entries, DATES_EXPORT_TYPE, DATES_EXPORT_VERSION  # noqa: E402

from sales import router as sales_router  # noqa: E402
from state import router as state_router  # noqa: E402

# URL de l'asset publié par le job GitHub Actions. À définir via la variable
# d'environnement SIRENE_CACHE_URL sur Render une fois le dépôt en place, ex. :
#   https://github.com/<owner>/<repo>/releases/download/sirene-cache/stock_etablissements_slim.tar.gz
CACHE_ASSET_URL = os.environ.get("SIRENE_CACHE_URL", "")

# Sur Render le disque est éphémère (effacé à chaque réveil/redéploiement), donc /tmp convient :
# on retélécharge de toute façon à chaque redémarrage du service.
CACHE_DIR = Path(os.environ.get("SIRENE_CACHE_DIR", "/tmp/sirene_cache"))
CACHE_ARCHIVE_TMP = Path("/tmp/sirene_cache_download.tar.gz")
CACHE_MARKER = CACHE_DIR / ".fetched_at"  # horodatage du dernier téléchargement réussi
CACHE_MAX_AGE_DAYS = 35
PARTITION_ROOT = CACHE_DIR / "stock_etablissements_slim"  # nom du dossier à l'intérieur de l'archive

# Origine(s) autorisée(s) à appeler cette API depuis le navigateur (l'URL réelle du site une
# fois déployé). "*" en attendant, à resserrer ensuite via la variable ALLOWED_ORIGINS sur Render.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(title="NFC Tracker — Sirene Matching API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)
app.include_router(sales_router)
app.include_router(state_router)

_transformer = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
_con = duckdb.connect()
# Sur le plan gratuit Render (512 Mo RAM au total, pour tout le process Python + FastAPI + DuckDB),
# DuckDB peut mal évaluer la mémoire réellement disponible dans un conteneur restreint et tenter
# d'en utiliser plus que ce qui existe physiquement -> le process se fait tuer (OOM) en plein
# milieu d'une requête. On borne donc explicitement, avec une bonne marge sous les 512 Mo. Avec le
# découpage par département, ça ne devrait de toute façon plus jamais approcher cette limite.
_con.execute("PRAGMA memory_limit='300MB'")
_con.execute("PRAGMA threads=2")


def ensure_cache():
    if not CACHE_ASSET_URL:
        raise HTTPException(
            500,
            "SIRENE_CACHE_URL n'est pas configurée sur ce serveur — voir le README pour l'étape de déploiement.",
        )

    if CACHE_MARKER.exists():
        age_days = (time.time() - CACHE_MARKER.stat().st_mtime) / 86400
        if age_days <= CACHE_MAX_AGE_DAYS:
            return

    print(f"[cache] Téléchargement depuis {CACHE_ASSET_URL}")
    with requests.get(CACHE_ASSET_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(CACHE_ARCHIVE_TMP, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024 * 4):
                f.write(chunk)

    print("[cache] Extraction de l'archive...")
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(CACHE_ARCHIVE_TMP, "r:gz") as tar:
        tar.extractall(CACHE_DIR, filter="data")
    CACHE_ARCHIVE_TMP.unlink(missing_ok=True)
    CACHE_MARKER.touch()

    total_size = sum(f.stat().st_size for f in CACHE_DIR.rglob("*.parquet"))
    print(f"[cache] OK ({total_size / 1e6:.0f} Mo, {len(list(PARTITION_ROOT.glob('department=*')))} départements).")


class BizEntry(BaseModel):
    id: str
    name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    postcode: Optional[str] = None
    address: Optional[str] = None

    # En amont (Geoapify), certains champs texte arrivent parfois avec un type inattendu
    # (ex: un nom entièrement numérique renvoyé comme nombre plutôt que texte). Plutôt que de
    # rejeter TOUT le lot à cause d'une seule entrée mal typée, on convertit en texte ici.
    @field_validator("name", "postcode", "address", mode="before")
    @classmethod
    def _coerce_to_str(cls, v):
        if v is None:
            return v
        return str(v)


class MatchRequest(BaseModel):
    city: str
    entries: List[BizEntry]


@app.get("/health")
def health():
    return {"ok": True, "cache_present": CACHE_MARKER.exists()}


@app.post("/match")
def match(req: MatchRequest):
    if not req.entries:
        raise HTTPException(400, "Aucun commerce fourni.")

    ensure_cache()

    postcodes = sorted({(e.postcode or "").strip() for e in req.entries if e.postcode})
    departments = sorted({pc[:2] for pc in postcodes if len(pc) >= 2})
    if not departments:
        raise HTTPException(400, "Aucun code postal exploitable dans la liste fournie.")

    # On ne lit QUE les fichiers parquet des départements concernés — jamais les ~500 Mo France
    # entière — c'est ce qui garde cette requête rapide même sur un CPU aussi bridé que celui du
    # plan gratuit Render.
    paths = []
    for d in departments:
        part_dir = PARTITION_ROOT / f"department={d}"
        if part_dir.exists():
            paths.extend(str(p) for p in part_dir.glob("*.parquet"))

    if not paths:
        raise HTTPException(
            404,
            f"Aucune donnée SIRENE en cache pour le(s) département(s) {', '.join(departments)}.",
        )

    paths_sql = ", ".join(f"'{p}'" for p in paths)
    rows = _con.execute(f"SELECT * FROM read_parquet([{paths_sql}])").fetchall()
    columns = [d[0] for d in _con.description]
    rows = [dict(zip(columns, r)) for r in rows]

    entries_dicts = [e.model_dump() for e in req.entries]
    matched, unmatched_count = match_entries(entries_dicts, rows, _transformer)

    return {
        "type": DATES_EXPORT_TYPE,
        "version": DATES_EXPORT_VERSION,
        "city": req.city,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "entries": matched,
        "unmatchedCount": unmatched_count,
    }
