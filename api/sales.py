"""
sales.py — Stockage et analyse des résultats de vente (NFC Tracker).

Comble un manque : côté site, les statuts (vendu / échec / à repasser) ne
vivent qu'en localStorage sur l'appareil — perdus au moindre changement de
téléphone ou vidage de cache, et impossibles à agréger pour calculer des
statistiques de réussite par catégorie.

Stockage : Supabase (Postgres, plan gratuit, pas de carte bancaire requise),
via son API REST (PostgREST) appelée directement avec `requests` — pas de
nouvelle dépendance lourde, cohérent avec le reste de cette API.

Important, à la différence du cache SIRENE (partitions parquet régénérées à
chaque réveil, voir main.py) : ces données de vente DOIVENT survivre aux
réveils et redéploiements de Render. Le disque du plan gratuit Render est
effacé à chaque réveil — impossible d'utiliser un fichier SQLite local ici,
d'où le stockage externe.

Variables d'environnement nécessaires (voir README pour la mise en place) :
    SUPABASE_URL   ex. https://xxxxx.supabase.co
    SUPABASE_KEY   la clé "service_role" du projet Supabase. Elle ne vit que
                   côté serveur (jamais envoyée au navigateur) : le front
                   n'appelle que /ventes et /stats sur CETTE API, jamais
                   Supabase directement.

Endpoints :
    POST /ventes
        Enregistre un résultat de vente (vendu ou échec — "à repasser"
        n'est pas un résultat final, il n'est pas envoyé ici).

    GET /stats?categoryId=...&ancienneteMois=...
        Taux de réussite lissé + prix conseillé, calculés à partir des
        ventes déjà enregistrées pour des commerces de même catégorie et de
        tranche d'ancienneté proche (élargi à la catégorie seule si
        l'échantillon sur ce croisement précis est trop faible).
"""
import os
import statistics
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TABLE = "ventes"

router = APIRouter()

# Tranches d'ancienneté (en mois). Une vente est rattachée à la tranche dans
# laquelle tombait le commerce au moment de la vente (ancienneteMois fourni
# par le front, calculé depuis biz.creationDate via monthsSince()).
BUCKETS = [
    (0, 12, "< 1 an"),
    (12, 36, "1-3 ans"),
    (36, 84, "3-7 ans"),
    (84, 180, "7-15 ans"),
    (180, None, "15 ans et +"),
]

DEFAULT_PRICE = 25.0
# En dessous de ce nombre de ventes réussies sur le croisement exact, on élargit d'abord à la
# catégorie seule, puis on retombe sur le prix par défaut si ça ne suffit toujours pas.
MIN_SAMPLE_FOR_PRICE = 3
# En dessous de ce nombre total de résultats (vendu+échec) sur le croisement exact, on élargit
# le calcul du taux de réussite à la catégorie seule (résultat trop bruité sinon).
MIN_SAMPLE_FOR_RATE = 5


def bucket_label(months: Optional[int]) -> Optional[str]:
    if months is None:
        return None
    for lo, hi, label in BUCKETS:
        if months >= lo and (hi is None or months < hi):
            return label
    return None


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


class VenteIn(BaseModel):
    businessId: str
    businessName: Optional[str] = None
    categoryId: str
    categoryLabel: Optional[str] = None
    ancienneteMois: Optional[int] = None
    status: str  # "vendu" | "echec"
    priceOffered: Optional[float] = None
    amount: Optional[float] = None

    def to_row(self):
        return {
            "business_id": self.businessId,
            "business_name": self.businessName,
            "category_id": self.categoryId,
            "category_label": self.categoryLabel,
            "anciennete_mois": self.ancienneteMois,
            "bucket": bucket_label(self.ancienneteMois),
            "status": self.status,
            "price_offered": self.priceOffered,
            "amount": self.amount,
        }


@router.post("/ventes")
def create_vente(vente: VenteIn):
    if vente.status not in ("vendu", "echec"):
        raise HTTPException(400, "status doit être 'vendu' ou 'echec'.")
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        json=vente.to_row(),
        timeout=15,
    )
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return {"ok": True}


def _fetch_rows(category_id: str, bucket: Optional[str] = None):
    params = {"category_id": f"eq.{category_id}", "select": "status,price_offered,bucket"}
    if bucket:
        params["bucket"] = f"eq.{bucket}"
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params=params,
        timeout=15,
    )
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return res.json()


@router.get("/stats")
def stats(categoryId: str, ancienneteMois: Optional[int] = None):
    bucket = bucket_label(ancienneteMois)

    rows = _fetch_rows(categoryId, bucket) if bucket else []
    scope = "categorie_et_anciennete"
    if len(rows) < MIN_SAMPLE_FOR_RATE:
        rows = _fetch_rows(categoryId)  # échantillon trop faible sur ce croisement précis -> élargir
        scope = "categorie_seule"

    success = sum(1 for r in rows if r["status"] == "vendu")
    total = len(rows)
    # Lissage bayésien (Laplace) : évite d'afficher 0% ou 100% de chance sur un tout petit
    # échantillon (ex. 1 seule vente réussie sur 1 essai).
    success_rate = (success + 1) / (total + 2)

    sold_prices = [r["price_offered"] for r in rows if r["status"] == "vendu" and r["price_offered"] is not None]
    if len(sold_prices) >= MIN_SAMPLE_FOR_PRICE:
        suggested_price = round(statistics.median(sold_prices), 2)
    else:
        suggested_price = DEFAULT_PRICE

    return {
        "successRate": round(success_rate, 3),
        "sampleSize": total,
        "scope": scope,
        "suggestedPrice": suggested_price,
        "priceSampleSize": len(sold_prices),
    }
