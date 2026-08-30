"""
sirene_common.py — Constantes et algorithme de matching partagés entre :
  - build/build_cache.py  (le job lourd, exécuté une fois par mois dans GitHub Actions)
  - api/main.py           (l'API légère, déployée sur Render, appelée à chaque ville)

Reprend EXACTEMENT le même algorithme que le script original sirene_dates.py
(recherche par nom dans le même code postal, puis repli géographique à 150 m /
50 m si pas de nom qui correspond), pour que les résultats soient identiques
qu'on lance l'ancien script en local ou qu'on passe par le nouveau serveur.
"""

import unicodedata
from collections import defaultdict

# URL stable data.gouv.fr du fichier stock des établissements SIRENE, format parquet (~2,1 Go).
SIRENE_STOCK_URL = "https://www.data.gouv.fr/api/1/datasets/r/a29c1297-1f92-4e2a-8f6b-8c902ce96c5f"

DATES_EXPORT_TYPE = "nfc-tracker-creation-dates"
DATES_EXPORT_VERSION = 1

GENERIC_NAME_WORDS = {
    "mama", "papa", "chez", "house", "food", "foods", "burger", "burgers", "pizza", "pizzeria",
    "sushi", "bar", "cafe", "restaurant", "resto", "snack", "grill", "kebab", "shop",
    "market", "epicerie", "boulangerie", "patisserie", "coiffure",
    "beaute", "institut", "salon", "nails", "nail", "place", "saveurs", "cuisine",
    "delice", "delices", "gourmet", "express", "new", "the", "and", "des",
    "les", "du", "cours", "rue", "avenue", "boulevard",
}

# Préfixes NAF exclus pour le repli géographique "sans nom" (holdings, banques-immeuble,
# administrations...) — sert à écarter le propriétaire du local plutôt que le commerce.
NAF_EXCLUDE_PREFIXES = ("64", "65", "66", "68", "84", "94", "97", "98", "99")

STEP1_MAX_DIST_M = 3000   # recherche par nom (même code postal) : marge large, le nom filtre déjà
STEP2_NAMED_MAX_DIST_M = 150   # repli géo, avec nom qui correspond
STEP2_UNNAMED_MAX_DIST_M = 50  # repli géo, sans nom (dernier recours)
GRID_CELL_M = 500  # taille des cases de la grille spatiale utilisée pour le repli géo


def normalize_words(text):
    if not text:
        return []
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")  # enlève les accents
    text = "".join(c if c.isalnum() else " " for c in text)
    return [w for w in text.split() if len(w) >= 3 and w not in GENERIC_NAME_WORDS]


def name_matches(biz_words, row):
    if not biz_words:
        return False
    candidate_text = " ".join(filter(None, [
        row["denominationUsuelleEtablissement"],
        row["enseigne1Etablissement"],
        row["enseigne2Etablissement"],
        row["enseigne3Etablissement"],
    ]))
    candidate_words = set(normalize_words(candidate_text))
    return any(w in candidate_words for w in biz_words)


def is_plausible_activity(row):
    naf = (row["activitePrincipaleEtablissement"] or "").replace(".", "")
    if not naf:
        return True
    return not any(naf.startswith(p) for p in NAF_EXCLUDE_PREFIXES)


def dist_m(x1, y1, x2, y2):
    return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5


def match_entries(entries, rows, transformer):
    """Fait le matching d'une liste de commerces (`entries`, dicts avec au moins
    id/name/lat/lon/postcode) contre un sous-ensemble déjà chargé de lignes SIRENE
    (`rows`, liste de dicts issus du cache filtré). `transformer` = un
    pyproj.Transformer WGS84 -> Lambert-93 déjà construit (réutilisable entre appels).

    Retourne (matched, unmatched_count) — matched au format attendu par le bouton
    "Importer des dates" du site (id, name, lat, lon, creationDate)."""
    by_postcode = defaultdict(list)
    grid = defaultdict(list)
    for row in rows:
        by_postcode[row["codePostalEtablissement"]].append(row)
        if row["x_lambert"] is not None and row["y_lambert"] is not None:
            gx = int(row["x_lambert"] // GRID_CELL_M)
            gy = int(row["y_lambert"] // GRID_CELL_M)
            grid[(gx, gy)].append(row)

    matched = []
    unmatched_count = 0

    for biz in entries:
        biz_words = normalize_words(biz.get("name") or "")
        postcode = (biz.get("postcode") or "").strip()
        lat, lon = biz.get("lat"), biz.get("lon")

        biz_x = biz_y = None
        if lat and lon:
            biz_x, biz_y = transformer.transform(lon, lat)

        result_date = None

        # ---- Étape 1 : recherche par nom, même code postal ----
        if postcode and biz_words:
            best_dist = float("inf")
            best_date = None
            for row in by_postcode.get(postcode, []):
                if not name_matches(biz_words, row):
                    continue
                d = (dist_m(biz_x, biz_y, row["x_lambert"], row["y_lambert"])
                     if biz_x is not None and row["x_lambert"] is not None else 0)
                if d > STEP1_MAX_DIST_M:
                    continue
                if d < best_dist:
                    best_dist = d
                    best_date = row["dateCreationEtablissement"]
            result_date = best_date

        # ---- Étape 2 : repli géographique ----
        if result_date is None and biz_x is not None:
            gx, gy = int(biz_x // GRID_CELL_M), int(biz_y // GRID_CELL_M)
            candidates = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidates.extend(grid.get((gx + dx, gy + dy), []))

            best_named_dist, best_named_date = float("inf"), None
            best_unnamed_dist, best_unnamed_date = float("inf"), None
            for row in candidates:
                d = dist_m(biz_x, biz_y, row["x_lambert"], row["y_lambert"])
                if name_matches(biz_words, row):
                    if d <= STEP2_NAMED_MAX_DIST_M and d < best_named_dist:
                        best_named_dist, best_named_date = d, row["dateCreationEtablissement"]
                elif is_plausible_activity(row):
                    if d <= STEP2_UNNAMED_MAX_DIST_M and d < best_unnamed_dist:
                        best_unnamed_dist, best_unnamed_date = d, row["dateCreationEtablissement"]

            result_date = best_named_date if best_named_date is not None else best_unnamed_date

        if result_date is not None:
            matched.append({
                "id": biz["id"],
                "name": biz.get("name"),
                "lat": lat,
                "lon": lon,
                "creationDate": str(result_date),
            })
        else:
            unmatched_count += 1

    return matched, unmatched_count
