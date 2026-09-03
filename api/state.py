"""
state.py — Stockage centralisé générique (clé -> valeur JSON) pour NFC Tracker.

Un seul utilisateur, plusieurs appareils : ce module remplace le localStorage
du navigateur (propre à chaque appareil) comme source de vérité pour :

    nfc-tracker-statuses          statuts de vente (vendu/échec/à repasser + montant)
    nfc-tracker-hidden-businesses commerces masqués définitivement
    nfc-tracker-sirene-cache      cache des dates de création déjà trouvées
    nfc-tracker-city-cache        cache des commerces déjà chargés par ville

Même stockage que sales.py (Supabase / PostgREST) et pour la même raison :
le disque du plan gratuit Render est effacé à chaque réveil, donc rien de
local sur le serveur ne survit — il faut un stockage externe.

Design volontairement générique (une seule table clé/valeur) plutôt qu'une
table par type de donnée : le frontend écrit et relit des blobs JSON déjà
structurés côté client (mêmes objets que ceux qui étaient sérialisés dans
localStorage), pas besoin de leur donner un schéma SQL dédié chacun.

Variables d'environnement : réutilise SUPABASE_URL / SUPABASE_KEY (voir
sales.py et le README pour la mise en place).

Endpoints :
    GET /state/{key}   Renvoie { "value": ... } — value vaut null si la clé
                        n'existe pas encore côté serveur (première visite).
    PUT /state/{key}   Enregistre (upsert) la valeur JSON envoyée en corps
                        de requête, quel que soit son type (objet, tableau...).
"""
import os
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TABLE = "app_state"

router = APIRouter()


def _headers():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(
            500,
            "SUPABASE_URL / SUPABASE_KEY non configurées sur ce serveur — voir le README.",
        )
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


@router.get("/state/{key}")
def get_state(key: str):
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"key": f"eq.{key}", "select": "value"},
        timeout=15,
    )
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    rows = res.json()
    return {"value": rows[0]["value"] if rows else None}


@router.put("/state/{key}")
async def put_state(key: str, request: Request):
    value: Any = await request.json()
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
        json={"key": key, "value": value},
        timeout=15,
    )
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return {"ok": True}
