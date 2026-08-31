"""
main.py — API de matching des dates de création SIRENE pour NFC Tracker.

Déployée sur Render (plan gratuit, 512 Mo RAM / 0.1 CPU). Au démarrage / au
réveil du service (après mise en veille), télécharge simplement le cache
SIRENE DÉJÀ FILTRÉ publié par le job GitHub Actions (build/build_cache.py) —
aucun téléchargement du fichier France entière brut, aucun filtrage DuckDB
lourd ici : exprès, pour rester dans les limites du plan gratuit.

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
import sys
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

# URL de l'asset publié par le job GitHub Actions. À définir via la variable
# d'environnement SIRENE_CACHE_URL sur Render une fois le dépôt en place, ex. :
#   https://github.com/<owner>/<repo>/releases/download/sirene-cache/stock_etablissements_slim.parquet
CACHE_ASSET_URL = os.environ.get("SIRENE_CACHE_URL", "")

# Sur Render le disque est éphémère (effacé à chaque réveil/redéploiement), donc /tmp convient :
# on retélécharge de toute façon à chaque redémarrage du service.
CACHE_PATH = Path(os.environ.get("SIRENE_CACHE_PATH", "/tmp/stock_etablissements_slim.parquet"))
CACHE_MAX_AGE_DAYS = 35

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

_transformer = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
_con = duckdb.connect()
# Sur le plan gratuit Render (512 Mo RAM au total, pour tout le process Python + FastAPI + DuckDB),
# DuckDB peut mal évaluer la mémoire réellement disponible dans un conteneur restreint et tenter
# d'en utiliser plus que ce qui existe physiquement -> le process se fait tuer (OOM) en plein
# milieu d'une requête, ce qui coupe la connexion sans réponse (vu côté navigateur comme une
# erreur CORS générique, alors que le vrai problème est une mémoire insuffisante). On borne donc
# explicitement, avec une bonne marge sous les 512 Mo pour laisser de la place à Python/FastAPI.
_con.execute("PRAGMA memory_limit='300MB'")
_con.execute("PRAGMA threads=2")


def ensure_cache():
    if not CACHE_ASSET_URL:
        raise HTTPException(
            500,
            "SIRENE_CACHE_URL n'est pas configurée sur ce serveur — voir le README pour l'étape de déploiement.",
        )

    if CACHE_PATH.exists():
        age_days = (time.time() - CACHE_PATH.stat().st_mtime) / 86400
        if age_days <= CACHE_MAX_AGE_DAYS:
            return

    print(f"[cache] Téléchargement depuis {CACHE_ASSET_URL}")
    tmp = CACHE_PATH.with_suffix(".tmp")
    with requests.get(CACHE_ASSET_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024 * 4):
                f.write(chunk)
    tmp.replace(CACHE_PATH)
    print(f"[cache] OK ({CACHE_PATH.stat().st_size / 1e6:.0f} Mo).")


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
    return {"ok": True, "cache_present": CACHE_PATH.exists()}


@app.post("/match")
def match(req: MatchRequest):
    if not req.entries:
        raise HTTPException(400, "Aucun commerce fourni.")

    ensure_cache()

    postcodes = sorted({(e.postcode or "").strip() for e in req.entries if e.postcode})
    departments = sorted({pc[:2] for pc in postcodes if len(pc) >= 2})
    if not departments:
        raise HTTPException(400, "Aucun code postal exploitable dans la liste fournie.")

    dep_list_sql = ", ".join(f"'{d}'" for d in departments)
    rows = _con.execute(f"""
        SELECT * FROM read_parquet('{CACHE_PATH.as_posix()}')
        WHERE substr(codePostalEtablissement, 1, 2) IN ({dep_list_sql})
    """).fetchall()
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
