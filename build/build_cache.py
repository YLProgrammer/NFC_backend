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

    print("[traitement] Filtrage + découpage par département (peut prendre 1-2 min)...")
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT
                siret,
                siren,
                codePostalEtablissement,
                -- Ne découper proprement (01, 02, ..., 97, 98...) que pour les codes postaux
                -- à 5 chiffres valides. Sans ce filtre, les codes postaux manquants/mal formés
                -- (adresses à l'étranger, données incomplètes...) fabriquent chacun leur propre
                -- "faux département" -> des centaines de petits dossiers parasites au lieu d'une
                -- centaine de dossiers propres, ce qui alourdit inutilement l'extraction de
                -- l'archive côté serveur. Tout ce qui n'est pas un code valide part dans une
                -- seule partition "na" (jamais utile au matching de toute façon : sans code
                -- postal exploitable, aucune requête ne cible ce département).
                CASE
                    WHEN regexp_matches(codePostalEtablissement, '^[0-9]{5}$')
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
        ) TO '{OUT_DIR.as_posix()}' (FORMAT PARQUET, PARTITION_BY (department), OVERWRITE_OR_IGNORE true)
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

