"""
advice.py — Questions IA terrain + conseils personnalisés (NFC Tracker).

Deux flux découplés dans le temps, PAS un chat en direct :

  Flux A (collecte) : quand un commerce est marqué "echec" ou "repasser"
      (jamais "vendu"), le front appelle POST /business-qa/question pour
      obtenir UNE question à poser au commercial, basée sur l'historique
      déjà connu de ce commerce (pour ne jamais reposer une question déjà
      posée). La réponse — ou l'absence de réponse si le commercial ferme
      sans répondre — est ensuite enregistrée via POST /business-qa.

  Flux B (conseil) : le bouton "Conseils IA" sur une fiche commerce appelle
      GET /business-qa/advice, qui NE génère PAS de nouvelle question — il
      synthétise un conseil à partir :
        - des Q/R déjà stockées pour CE commerce précisément (s'il y en a),
        - ET des Q/R de commerces similaires : même bucket catégorie ×
          ancienneté × avis que le système déjà en place dans sales.py
          (bucket_label / reviews_bucket_label, réutilisés ici tels quels).
      Ça permet de conseiller un commercial même sur un commerce jamais
      approché, en s'appuyant sur ce qui a été appris ailleurs dans la
      même tranche.

Appelle l'API Groq (compatible OpenAI) directement en `requests`, comme le
reste de cette API — pas de SDK supplémentaire. Mode JSON simple
(response_format: json_object) plutôt que json_schema/strict, pour rester
compatible avec l'ensemble des modèles Groq sans avoir à vérifier au cas
par cas lesquels supportent le mode strict.

Variables d'environnement nécessaires (en plus de SUPABASE_URL/SUPABASE_KEY,
déjà utilisées par sales.py) :
    GROQ_API_KEY   clé API Groq (console.groq.com) — côté serveur uniquement,
                   jamais envoyée au front.

Table Supabase à créer avant de déployer (voir README) :

    create table business_qa (
      id bigint generated always as identity primary key,
      business_id text not null,
      business_name text,
      category_id text not null,
      category_label text,
      anciennete_mois integer,
      bucket text,
      reviews_count integer,
      reviews_bucket text,
      status_at_time text not null,  -- "echec" | "repasser"
      question text not null,
      response text,
      created_at timestamptz not null default now()
    );

    create index idx_business_qa_business_id on business_qa (business_id);
    create index idx_business_qa_bucket on business_qa (category_id, bucket, reviews_bucket);

À enregistrer dans main.py, comme pour sales.py :

    from advice import router as advice_router
    app.include_router(advice_router)
"""
import os
import json
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sales import SUPABASE_URL, SUPABASE_KEY, _headers, bucket_label, reviews_bucket_label

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

TABLE = "business_qa"

router = APIRouter()

# Nombre max de lignes "commerces similaires" envoyées au modèle pour la synthèse de conseil —
# évite de faire exploser le prompt sur une tranche très peuplée (ex. "restaurant, 3-7 ans,
# 10-49 avis" dans une grande ville).
MAX_SIMILAR_ROWS = 30


def _groq_headers():
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY non configurée sur ce serveur — voir le README.")
    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def _call_groq(system_prompt: str, user_prompt: str, schema_hint: str) -> dict:
    """Appelle Groq en JSON mode et renvoie le dict parsé."""
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt + "\n\n" + schema_hint},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.6,
    }
    res = requests.post(GROQ_URL, headers=_groq_headers(), json=payload, timeout=30)
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Groq ({res.status_code}) : {res.text[:300]}")
    content = res.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(502, "Réponse Groq non-JSON — modèle à revoir ou prompt à resserrer.")


def _business_context(business_name, category_label, anciennete_mois, reviews_count, status_label=None):
    lines = [f"Commerce : {business_name or 'inconnu'}", f"Catégorie : {category_label or 'inconnue'}"]
    if anciennete_mois is not None:
        lines.append(f"Ancienneté : ~{anciennete_mois // 12} an(s)")
    if reviews_count is not None:
        lines.append(f"Avis Google : {reviews_count}")
    if status_label:
        lines.append(f"Statut de la visite : {status_label}")
    return "\n".join(lines)


def _fetch_qa_rows(business_id: Optional[str] = None, exclude_business_id: Optional[str] = None,
                    category_id: Optional[str] = None, bucket: Optional[str] = None,
                    reviews_bucket: Optional[str] = None, limit: Optional[int] = None):
    params = {"select": "business_name,question,response,status_at_time,created_at", "order": "created_at.desc"}
    if business_id:
        params["business_id"] = f"eq.{business_id}"
    if exclude_business_id:
        params["business_id"] = f"neq.{exclude_business_id}"
    if category_id:
        params["category_id"] = f"eq.{category_id}"
    if bucket:
        params["bucket"] = f"eq.{bucket}"
    if reviews_bucket:
        params["reviews_bucket"] = f"eq.{reviews_bucket}"
    if limit:
        params["limit"] = str(limit)
    res = requests.get(f"{SUPABASE_URL}/rest/v1/{TABLE}", headers=_headers(), params=params, timeout=15)
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return res.json()


def _format_rows(rows):
    if not rows:
        return "(aucune donnée)"
    return "\n".join(
        f"- [{r['status_at_time']}] Q: {r['question']} / R: {r['response'] or '(pas de réponse)'}"
        for r in rows
    )


# ---------------------------------------------------------------------------
# Flux A — collecte (échec / repasser uniquement)
# ---------------------------------------------------------------------------

class QuestionIn(BaseModel):
    businessId: str
    businessName: Optional[str] = None
    categoryId: str
    categoryLabel: Optional[str] = None
    ancienneteMois: Optional[int] = None
    reviewsCount: Optional[int] = None
    status: str  # "echec" | "repasser"


@router.post("/business-qa/question")
def generate_question(payload: QuestionIn):
    if payload.status not in ("echec", "repasser"):
        raise HTTPException(400, "status doit être 'echec' ou 'repasser'.")

    history = _fetch_qa_rows(business_id=payload.businessId)
    history.sort(key=lambda r: r["created_at"])  # chronologique pour le prompt (fetch renvoie desc)

    system_prompt = (
        "Tu es un conseiller commercial terrain, spécialisé dans la vente de cartes NFC de "
        "collecte d'avis Google aux commerces de proximité. Tu aides un commercial à recueillir "
        "de l'information utile juste après une visite ratée ou reportée, en posant UNE question "
        "ciblée qui complète ce qu'on sait déjà — jamais une question déjà posée précédemment."
    )
    schema_hint = (
        'Réponds UNIQUEMENT avec un objet JSON de la forme : '
        '{"question": "string", "suggestions_reponse": ["string", ...]} '
        '(0 à 4 suggestions courtes et concrètes ; tableau vide si une réponse libre est '
        'nécessaire pour bien répondre).'
    )
    user_prompt = (
        f"{_business_context(payload.businessName, payload.categoryLabel, payload.ancienneteMois, payload.reviewsCount, payload.status)}\n\n"
        f"Historique des visites précédentes sur ce commerce :\n{_format_rows(history)}"
    )
    return _call_groq(system_prompt, user_prompt, schema_hint)


class QuestionResponseIn(BaseModel):
    businessId: str
    businessName: Optional[str] = None
    categoryId: str
    categoryLabel: Optional[str] = None
    ancienneteMois: Optional[int] = None
    reviewsCount: Optional[int] = None
    status: str  # "echec" | "repasser"
    question: str
    response: Optional[str] = None  # null si le commercial a fermé sans répondre


@router.post("/business-qa")
def store_question_response(payload: QuestionResponseIn):
    if payload.status not in ("echec", "repasser"):
        raise HTTPException(400, "status doit être 'echec' ou 'repasser'.")
    row = {
        "business_id": payload.businessId,
        "business_name": payload.businessName,
        "category_id": payload.categoryId,
        "category_label": payload.categoryLabel,
        "anciennete_mois": payload.ancienneteMois,
        "bucket": bucket_label(payload.ancienneteMois),
        "reviews_count": payload.reviewsCount,
        "reviews_bucket": reviews_bucket_label(payload.reviewsCount),
        "status_at_time": payload.status,
        "question": payload.question,
        "response": payload.response,
    }
    res = requests.post(f"{SUPABASE_URL}/rest/v1/{TABLE}", headers=_headers(), json=row, timeout=15)
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Flux B — conseil, lecture seule (pas de nouvelle question générée ici)
# ---------------------------------------------------------------------------

@router.delete("/business-qa/all")
def delete_all_business_qa():
    # Remise à zéro complète — supprime TOUTES les questions/réponses IA collectées. Irréversible.
    # PostgREST refuse un DELETE sans filtre, d'où le filtre "toujours vrai" ci-dessous
    # (business_id est une colonne obligatoire, donc jamais null), même pattern que
    # /ventes/all dans sales.py.
    res = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"business_id": "not.is.null"},
        timeout=15,
    )
    if res.status_code >= 300:
        raise HTTPException(502, f"Erreur Supabase ({res.status_code}) : {res.text[:300]}")
    return {"ok": True}


@router.get("/business-qa/advice")
def advice(businessId: str, categoryId: str, businessName: Optional[str] = None,
           categoryLabel: Optional[str] = None, ancienneteMois: Optional[int] = None,
           reviewsCount: Optional[int] = None):
    own_rows = _fetch_qa_rows(business_id=businessId)

    bucket = bucket_label(ancienneteMois)
    reviews_bucket = reviews_bucket_label(reviewsCount)
    similar_rows = _fetch_qa_rows(
        exclude_business_id=businessId,
        category_id=categoryId,
        bucket=bucket,
        reviews_bucket=reviews_bucket,
        limit=MAX_SIMILAR_ROWS,
    )

    system_prompt = (
        "Tu es un conseiller commercial terrain, spécialisé dans la vente de cartes NFC de "
        "collecte d'avis Google aux commerces de proximité. Un commercial te demande un conseil "
        "avant d'aborder ou de relancer un commerce précis. Base-toi UNIQUEMENT sur les "
        "informations fournies ci-dessous — n'invente aucun détail sur ce commerce. Donne la "
        "priorité aux informations propres à ce commerce si elles existent ; sinon, appuie-toi "
        "sur les patterns observés chez des commerces similaires. Si aucune des deux sources n'a "
        "de données, dis-le clairement et donne un conseil générique basé sur la catégorie, "
        "l'ancienneté et le nombre d'avis. Conseil court : 2 à 4 phrases, concret et actionnable."
    )
    schema_hint = 'Réponds UNIQUEMENT avec un objet JSON de la forme : {"conseil": "string"}.'
    user_prompt = (
        f"{_business_context(businessName, categoryLabel, ancienneteMois, reviewsCount)}\n\n"
        f"Informations recueillies sur CE commerce précisément :\n{_format_rows(own_rows)}\n\n"
        f"Informations recueillies sur des commerces similaires (même catégorie, ancienneté et "
        f"nombre d'avis comparables) :\n{_format_rows(similar_rows)}"
    )
    return _call_groq(system_prompt, user_prompt, schema_hint)
