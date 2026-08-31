#!/usr/bin/env python3
"""
build_cache.py — L'étape LOURDE : télécharge le stock SIRENE France entière
(~2 Go) et le filtre aux colonnes/lignes utiles, PRÉ-DÉCOUPÉ PAR DÉPARTEMENT.

Conçu pour tourner UNE FOIS PAR MOIS dans GitHub Actions (voir
.github/workflows/refresh-sirene-cache.yml), PAS sur le serveur Render.

Le découpage par département (PARTITION_BY) est essentiel : sans lui, l'API doit
scanner les 500+ Mo du fichier à CHAQUE requête pour en extraire un seul
département, ce qui est bien trop lent sur les 0.1 CPU du plan gratuit Render
(la requête finit par dépasser le délai imposé par l'infrastructure Render avant
d'avoir fini). Avec le découpage, l'API lit directement les quelques Mo du bon
département, sans jamais toucher au reste.

Sortie : stock_etablissements_slim.tar.gz (une archive contenant un dossier par
département), publiée comme asset d'une release GitHub — voir api/main.py qui la
télécharge et l'extrait.

Usage (dans le workflow, ou en local pour tester) :
    pip install -r build/requirements.txt
    python build/build_cache.py
"""
import shutil
import sys
import tarfile
from pathlib import Path

import duckdb
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from sirene_common import SIRENE_STOCK_URL  # noqa: E402

RAW_FILE = Path("stock_etablissements_raw.parquet")
OUT_DIR = Path("stock_etablissements_slim")
OUT_ARCHIVE = Path("stock_etablissements_slim.tar.gz")


def download_file(url, dest):
    print(f"[téléchargement] {url}")
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024 * 8):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done / total * 100
                    print(f"\r  {done / 1e6:.0f} / {total / 1e6:.0f} Mo ({pct:.0f}%)", end="", flush=True)
    print("\n[téléchargement] Terminé.")


def main():
    download_file(SIRENE_STOCK_URL, RAW_FILE)

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    con = duckdb.connect()

    print("[diagnostic] Aperçu des codes postaux bruts (20 valeurs les plus fréquentes)...")
    sample = con.execute(f"""
        SELECT codePostalEtablissement, count(*) AS n
        FROM read_parquet('{RAW_FILE.as_posix()}')
        WHERE etatAdministratifEtablissement != 'F' AND dateCreationEtablissement IS NOT NULL
        GROUP BY codePostalEtablissement
        ORDER BY n DESC
        LIMIT 20
    """).fetchall()
    for cp, n in sample:
        print(f"  {cp!r} ({n})")

    total_null = con.execute(f"""
        SELECT count(*) FROM read_parquet('{RAW_FILE.as_posix()}')
        WHERE etatAdministratifEtablissement != 'F' AND dateCreationEtablissement IS NOT NULL
          AND codePostalEtablissement IS NULL
    """).fetchone()[0]
    total = con.execute(f"""
        SELECT count(*) FROM read_parquet('{RAW_FILE.as_posix()}')
        WHERE etatAdministratifEtablissement != 'F' AND dateCreationEtablissement IS NOT NULL
    """).fetchone()[0]
    print(f"[diagnostic] {total_null} / {total} lignes ont un codePostalEtablissement NULL.")

    print("[traitement] Filtrage + calcul du département (peut prendre 1-2 min)...")
    # On matérialise d'abord dans une vraie table (CREATE TABLE), plutôt que de calculer le
    # département à la volée dans le COPY ... PARTITION_BY : lors d'un essai précédent, la même
    # expression utilisée directement comme colonne de partitionnement n'a donné que 11
    # départements au lieu d'une centaine, malgré des données sources visiblement saines —
    # la matérialisation élimine ce genre de comportement inattendu en forçant le calcul complet
    # AVANT le partitionnement, qui n'a plus ensuite qu'à lire une colonne déjà toute faite.
    con.execute(f"""
        CREATE OR REPLACE TABLE base AS
        SELECT
            siret,
            siren,
            codePostalEtablissement,
            -- Ne découper proprement (01, 02, ..., 97, 98...) que pour les codes postaux
            -- valides. Deux cas très fréquents à exclure (vus dans le diagnostic) : le code
            -- spécial INSEE "[ND]" (donnée non-diffusible, 2,4 millions de lignes) et les
            -- codes postaux manquants (NULL). Tout ce qui n'est pas un vrai code part dans une
            -- seule partition "na" (jamais utile au matching de toute façon).
            -- Comparaison caractère par caractère plutôt qu'une regex : un essai précédent avec
            -- regexp_matches('^[0-9]{2}$', ...) a fait basculer la quasi-totalité des lignes en
            -- 'na' (y compris des codes visiblement valides comme "75008"), signe d'un problème
            -- avec la regex elle-même plutôt qu'avec les données. BETWEEN '0' AND '9' sur
            -- chaque caractère ne laisse aucune place à ce genre d'ambiguïté.
            CASE
                WHEN substr(codePostalEtablissement, 1, 1) BETWEEN '0' AND '9'
                 AND substr(codePostalEtablissement, 2, 1) BETWEEN '0' AND '9'
                    THEN substr(codePostalEtablissement, 1, 2)
                ELSE 'na'
            END AS department,
            etatAdministratifEtablissement,
            dateCreationEtablissement,
            denominationUsuelleEtablissement,
            enseigne1Etablissement,
            enseigne2Etablissement,
            enseigne3Etablissement,
            activitePrincipaleEtablissement,
            TRY_CAST(coordonneeLambertAbscisseEtablissement AS DOUBLE) AS x_lambert,
            TRY_CAST(coordonneeLambertOrdonneeEtablissement AS DOUBLE) AS y_lambert
        FROM read_parquet('{RAW_FILE.as_posix()}')
        WHERE etatAdministratifEtablissement != 'F'
          AND dateCreationEtablissement IS NOT NULL
    """)

    dep_count, na_count, total_count = con.execute("""
        SELECT
            count(DISTINCT department) FILTER (WHERE department != 'na'),
            count(*) FILTER (WHERE department = 'na'),
            count(*)
        FROM base
    """).fetchone()
    print(f"[diagnostic] {dep_count} départements distincts détectés (sur {total_count} lignes, {na_count} en 'na').")

    print("[traitement] Découpage par département...")
    con.execute(f"""
        COPY base TO '{OUT_DIR.as_posix()}' (FORMAT PARQUET, PARTITION_BY (department), OVERWRITE_OR_IGNORE true)
    """)
    RAW_FILE.unlink(missing_ok=True)

    print("[traitement] Compression en une seule archive...")
    with tarfile.open(OUT_ARCHIVE, "w:gz") as tar:
        tar.add(OUT_DIR, arcname=OUT_DIR.name)
    shutil.rmtree(OUT_DIR)

    size_mb = OUT_ARCHIVE.stat().st_size / 1e6
    print(f"[ok] {OUT_ARCHIVE} écrit ({size_mb:.0f} Mo).")


if __name__ == "__main__":
    main()

