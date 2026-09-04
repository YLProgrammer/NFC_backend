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

    GET /stats?categoryId=...&ancienneteMois=...&reviewsCount=...
        Taux de réussite lissé + prix conseillé, calculés à partir des
        ventes déjà enregistrées pour des commerces de même catégorie, de
        tranche d'ancienneté proche ET de tranche d'avis Google proche
        (reviewsCount brut, rattaché ici à une tranche via REVIEWS_BUCKETS —
        même logique qu'ancienneteMois/BUCKETS) — élargi d'abord en retirant
        les avis, puis en ne gardant que la catégorie, si l'échantillon est
        trop faible à chaque étape.

Nécessite une colonne supplémentaire sur la table Supabase `ventes` :
    ALTER TABLE ventes ADD COLUMN reviews_bucket text;
(nullable — les lignes enregistrées avant cet ajout auront simplement
reviews_bucket = null, ce qui les exclut naturellement du croisement le
plus précis mais pas des croisements élargis).
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

# Mêmes tranches que côté front (script.js, REVIEWS_STATS_BUCKETS) — les libellés n'ont pas besoin
# de matcher le front puisque c'est ICI qu'on transforme reviewsCount en tranche (même logique que
# bucket_label()/BUCKETS pour ancienneteMois), mais on les garde identiques pour rester lisible
# d'un bout à l'autre du projet.
REVIEWS_BUCKETS = [
    (0, 1, "aucun avis"),
    (1, 10, "1-9 avis"),
    (10, 50, "10-49 avis"),
    (50, 200, "50-199 avis"),
    (200, None, "200+ avis"),
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


def reviews_bucket_label(count: Optional[int]) -> Optional[str]:
    if count is None:
        return None
    for lo, hi, label in REVIEWS_BUCKETS:
        if count >= lo and (hi is None or count < hi):
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
    reviewsCount: Optional[int] = None
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
            "reviews_count": self.reviewsCount,
            "reviews_bucket": reviews_bucket_label(self.reviewsCount),
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


@router.delete("/ventes")
def delete_vente(businessId: str):
    # Appelé quand l'utilisateur fait "Annuler (remettre non traité)" sur un commerce déjà marqué
    # vendu/échoué : on retire la (les) ligne(s) correspondante(s) pour ne pas fausser les stats
    # de réussite avec un résultat que l'utilisateur a lui-même invalidé.
    res = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"business_id": f"eq.{businessId}"},
        timeout=15,
    )
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return {"ok": True}


def _fetch_rows(category_id: str, bucket: Optional[str] = None, reviews_bucket: Optional[str] = None):
    params = {"category_id": f"eq.{category_id}", "select": "status,price_offered,bucket,reviews_bucket"}
    if bucket:
        params["bucket"] = f"eq.{bucket}"
    if reviews_bucket:
        params["reviews_bucket"] = f"eq.{reviews_bucket}"
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
def stats(categoryId: str, ancienneteMois: Optional[int] = None, reviewsCount: Optional[int] = None):
    bucket = bucket_label(ancienneteMois)
    reviews_bucket = reviews_bucket_label(reviewsCount)

    # Élargissement en 3 paliers, du plus précis au plus large : catégorie + ancienneté + avis,
    # puis catégorie + ancienneté seule, puis catégorie seule. Chaque palier n'est tenté que si le
    # précédent n'a pas assez de résultats pour être fiable (MIN_SAMPLE_FOR_RATE) — comme pour
    # l'élargissement ancienneté -> catégorie qui existait déjà, juste avec un niveau de plus.
    rows = []
    scope = "categorie_seule"
    if bucket and reviews_bucket:
        rows = _fetch_rows(categoryId, bucket, reviews_bucket)
        scope = "categorie_anciennete_avis"
    if len(rows) < MIN_SAMPLE_FOR_RATE and bucket:
        rows = _fetch_rows(categoryId, bucket)
        scope = "categorie_et_anciennete"
    if len(rows) < MIN_SAMPLE_FOR_RATE:
        rows = _fetch_rows(categoryId)  # échantillon trop faible sur tout croisement précis -> catégorie seule
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
