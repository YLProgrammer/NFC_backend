#!/usr/bin/env python3
"""
build_cache.py — L'étape LOURDE : télécharge le stock SIRENE France entière
(~2 Go) et le filtre aux colonnes/lignes utiles.

Conçu pour tourner UNE FOIS PAR MOIS dans GitHub Actions (voir
.github/workflows/refresh-sirene-cache.yml), PAS sur le serveur Render — c'est
tout l'intérêt du découpage : garder le téléchargement + le filtrage DuckDB
(gourmand en RAM et en CPU) hors d'un service à 512 Mo de RAM / 0.1 CPU.

Sortie : stock_etablissements_slim.parquet, publié ensuite comme asset d'une
release GitHub que l'API va simplement télécharger (voir api/main.py).

Usage (dans le workflow, ou en local pour tester) :
    pip install -r build/requirements.txt
    python build/build_cache.py
"""
import sys
from pathlib import Path

import duckdb
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))
from sirene_common import SIRENE_STOCK_URL  # noqa: E402

RAW_FILE = Path("stock_etablissements_raw.parquet")
OUT_FILE = Path("stock_etablissements_slim.parquet")


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

    print("[traitement] Filtrage des colonnes et des lignes utiles...")
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT
                siret,
                siren,
                codePostalEtablissement,
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
        ) TO '{OUT_FILE.as_posix()}' (FORMAT PARQUET)
    """)
    RAW_FILE.unlink(missing_ok=True)

    size_mb = OUT_FILE.stat().st_size / 1e6
    print(f"[ok] {OUT_FILE} écrit ({size_mb:.0f} Mo).")


if __name__ == "__main__":
    main()
