# NFC Tracker — serveur de matching SIRENE

Remplace le flux manuel (export liste → dossier local → `python sirene_dates.py`
→ import) par un appel HTTP automatique depuis le site.

## Architecture

```
build/   → job LOURD (téléchargement 2 Go + filtrage DuckDB), exécuté une fois
           par mois dans GitHub Actions. Jamais sur Render.
api/     → job LÉGER, déployé sur Render (plan gratuit). Télécharge le fichier
           déjà filtré publié par le job ci-dessus, puis répond aux requêtes
           de matching par ville en quelques secondes.
common/  → algorithme de matching partagé (identique à l'ancien sirene_dates.py).
```

## Mise en place (une fois)

1. **Créer un dépôt GitHub** et y pousser ce dossier tel quel.

2. **Premier lancement du job de build** : onglet *Actions* → *Rafraîchir le
   cache SIRENE* → *Run workflow*. Ça prend le temps que prenait
   `sirene_dates.py --refresh` en local (téléchargement + filtrage), mais ça
   tourne sur les serveurs GitHub, pas sur ta machine. À la fin, une release
   nommée `sirene-cache` apparaît avec `stock_etablissements_slim.parquet` en
   asset. Note son URL — elle a cette forme :
   ```
   https://github.com/<owner>/<repo>/releases/download/sirene-cache/stock_etablissements_slim.parquet
   ```

3. **Déployer l'API sur Render** :
   - New → Blueprint → sélectionne ce dépôt (il détecte `render.yaml`).
   - Renseigne les deux variables d'environnement demandées :
     - `SIRENE_CACHE_URL` = l'URL notée à l'étape 2.
     - `ALLOWED_ORIGINS` = l'URL du site NFC Tracker (ou `*` pour commencer).
   - Une fois déployé, Render donne une URL du type
     `https://nfc-tracker-sirene-api.onrender.com`.

4. **Vérifier** : `GET https://<ton-url-render>/health` doit répondre
   `{"ok": true, ...}`.

## Utilisation depuis le site

Le endpoint `POST /match` attend exactement le format du bouton
« Exporter la liste » de l'app, et répond exactement au format attendu par
« Importer des dates » :

```js
const res = await fetch("https://<ton-url-render>/match", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ city: appCityName.textContent, entries }),
});
const payload = await res.json(); // même format que DATES_EXPORT_TYPE
```

## Le mois suivant

Le workflow GitHub Actions se relance tout seul le 1er de chaque mois et
écrase l'asset de la release — rien à faire côté Render, l'API retélécharge
automatiquement la nouvelle version dès qu'elle a plus de 35 jours de cache.

## Pourquoi ce découpage

Le plan gratuit Render (512 Mo RAM, 0.1 CPU, disque effacé à chaque réveil)
ne peut pas absorber le téléchargement + filtrage DuckDB du fichier SIRENE
France entière (2 Go) sans risquer de saturer la mémoire ou d'être trop lent.
En sortant cette étape vers GitHub Actions (gratuit, ressources généreuses) et
en ne laissant à Render que le téléchargement du fichier déjà allégé + des
requêtes DuckDB filtrées par département (comme en local), tout reste dans les
limites du plan gratuit.
