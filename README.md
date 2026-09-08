# CROUS Bordeaux — surveillance des logements

Surveille les logements CROUS disponibles dans une zone de la carte
(Bordeaux Métropole, Talence…) et envoie un e-mail dès que le nombre change.

## Comment ça marche

1. **cron-job.org** envoie une requête HTTP à GitHub toutes les 2 minutes.
   Il ne fait *pas* la vérification lui-même : il demande juste à GitHub de démarrer le workflow.
2. Le **workflow GitHub Actions** (`.github/workflows/check.yml`) exécute `check.py`.
3. **`check.py`** interroge l'API du CROUS pour la zone de la carte, compare le nombre
   de logements avec celui de la fois précédente (stocké dans `state.json`) et,
   si ça a changé, le workflow envoie un e-mail via Gmail puis met à jour `state.json`.

> **Pourquoi pas le `schedule:` de GitHub ?** Les crons GitHub Actions ne sont pas
> ponctuels : aux heures chargées ils partent avec 15 à 40 minutes de retard, et
> les exécutions en retard sont abandonnées plutôt que rattrapées. `workflow_dispatch`
> (déclenchement manuel ou par API) part immédiatement — d'où cron-job.org, qui
> appuie sur le bouton à notre place toutes les 2 minutes.

---

## Configuration — à faire une seule fois

### Étape 1 — Activer GitHub Actions sur le fork

**Sur un fork, Actions est désactivé par défaut.** Tant que ce n'est pas fait, rien
ne se lancera, et l'API renverra « workflow non trouvé ».

Onglet **Actions** du repo → bouton vert **« I understand my workflows, go ahead and enable them »**.

### Étape 2 — Définir la zone de recherche (`CROUS_URL`)

1. Ouvre <https://trouverunlogement.lescrous.fr>.
2. Déplace et zoome la carte pour cadrer exactement la zone voulue
   (toute Bordeaux Métropole, ou seulement Talence).
3. Copie l'URL **complète** depuis la barre d'adresse. Elle ressemble à :

   ```
   https://trouverunlogement.lescrous.fr/tools/47/search?bounds=-0.7570_44.9280_-0.4700_44.7530
   ```

   - `bounds=ouest_nord_est_sud` = le rectangle de la carte. C'est **ce cadrage**
     qui définit la zone surveillée : plus tu zoomes, plus la zone est petite.
   - `tools/47` = l'identifiant de la campagne CROUS en cours. Le script le lit
     automatiquement dans l'URL, donc il n'y a rien à modifier dans le code quand
     le CROUS ouvre une nouvelle campagne : il suffit de recopier la nouvelle URL.

4. Repo → **Settings** → **Secrets and variables** → **Actions** → onglet **Variables**
   → **New repository variable** :

   | Name | Value |
   |------|-------|
   | `CROUS_URL` | l'URL complète copiée ci-dessus |

   ⚠️ C'est une **variable**, pas un secret (le workflow lit `vars.CROUS_URL`).
   Si tu la mets dans l'onglet Secrets, le script s'arrêtera en disant que `CROUS_URL` n'est pas défini.

### Étape 3 — Créer un mot de passe d'application Gmail

Gmail refuse le mot de passe normal du compte en SMTP. Il faut un « mot de passe
d'application », généré par Google, utilisable uniquement par ce script.

1. Le compte Gmail qui **enverra** les e-mails doit avoir la **validation en deux
   étapes activée** (<https://myaccount.google.com/signinoptions/two-step-verification>).
   Sans ça, l'option des mots de passe d'application n'apparaît pas.
2. Va sur <https://myaccount.google.com/apppasswords>, donne un nom (ex. `crous`),
   et Google affiche un mot de passe de **16 caractères**.
3. Copie-le. Il ne sera plus jamais réaffiché (mais tu peux en régénérer un).

### Étape 4 — Enregistrer les secrets

Même page qu'à l'étape 2, mais onglet **Secrets** → **New repository secret**, trois fois :

| Name | Valeur |
|------|--------|
| `MAIL_USERNAME` | l'adresse Gmail qui **envoie** (ex. `moncompte@gmail.com`) |
| `MAIL_PASSWORD` | le mot de passe d'application à 16 caractères de l'étape 3 |
| `MAIL_TO` | l'adresse qui **reçoit** l'alerte (peut être la même) |

### Étape 5 — Tester à la main

Onglet **Actions** → workflow **check-crous** → **Run workflow** → branche `main`.

Ce qu'il faut vérifier dans les logs de l'exécution :

- L'étape *Check CROUS availability* affiche `zone=... tools=[...]` puis une ligne
  `previous=... current=... changed=...`.
- Si `changed=true`, l'étape *Send email on change* s'exécute → vérifie ta boîte mail (et les spams).

**Le tout premier run n'envoie pas forcément d'e-mail** : le script compare avec
`state.json`, qui contient déjà `tool47=0`. S'il trouve 0 logement, rien ne change,
donc pas d'e-mail — c'est normal. L'e-mail partira au premier changement réel.

Tant que cette étape manuelle ne marche pas, inutile de brancher cron-job.org.

### Étape 6 — Créer le token GitHub pour cron-job.org

<https://github.com/settings/personal-access-tokens/new> (*Fine-grained token*) :

- **Repository access** → *Only select repositories* → `yahyabmo/crousBordeaux`
  (c'est ce qui limite le token à ce seul repo).
- **Permissions** → *Repository permissions* → **Actions : Read and write**.
  (« Metadata : Read-only » s'ajoute tout seul, c'est normal.)
- **Expiration** : note la date dans un coin — à l'expiration, cron-job.org
  recevra des 401 et la surveillance s'arrêtera **en silence**.

Copie le token (`github_pat_…`), il ne sera affiché qu'une fois.

### Étape 7 — Configurer cron-job.org

Crée un cronjob avec exactement ces paramètres :

- **URL** :
  ```
  https://api.github.com/repos/yahyabmo/crousBordeaux/actions/workflows/check.yml/dispatches
  ```
- **Schedule** : toutes les 2 minutes (*Every 2 minutes*, ou personnalisé `*/2`).
- Dans les options avancées (*Advanced* / *Headers*) :
  - **Request method** : `POST`
  - **Headers** :
    ```
    Accept: application/vnd.github+json
    Authorization: Bearer github_pat_ton_token_ici
    X-GitHub-Api-Version: 2022-11-28
    Content-Type: application/json
    ```
  - **Request body** :
    ```json
    {"ref":"main"}
    ```

GitHub répond **`204 No Content`** quand ça marche : pas de corps de réponse, c'est
normal, ce n'est pas une erreur. Tu peux le vérifier tout de suite en cliquant
*TEST RUN* sur cron-job.org, puis en regardant qu'une nouvelle exécution apparaît
dans l'onglet Actions du repo.

⚠️ Mets le token dans l'en-tête `Authorization`, **jamais** dans l'URL : les URLs
sont journalisées un peu partout.

---

## Les exécutions ne se marchent pas sur les pieds

Le bloc `concurrency` du workflow empêche deux vérifications de tourner en même
temps. Si un run prend plus de 2 minutes, le déclenchement suivant attend au lieu
de se superposer. Un run dure normalement moins d'une minute.

Le repo est **public**, donc les minutes GitHub Actions sont illimitées : le rythme
de 2 minutes ne consomme aucun quota.

## Quand un e-mail part-il ?

Dès que le **nombre total** de logements de la zone change, dans **les deux sens** :
`0 → 2` (un logement s'est libéré) mais aussi `2 → 0` (quelqu'un a été plus rapide).
Les deux sont des informations utiles, mais si tu ne veux être prévenu que des
hausses, c'est la ligne `changed = ...` de `check.py` qu'il faut modifier.

## En cas de problème

| Symptôme | Cause probable |
|---|---|
| cron-job.org renvoie **404** | Actions pas activé (étape 1), ou le token n'a pas accès à ce repo |
| cron-job.org renvoie **403 / 401** | Token expiré, ou permission *Actions : Read and write* manquante |
| cron-job.org renvoie **422** | La branche du champ `"ref"` n'existe pas — ça doit être `main` |
| `CROUS_URL n'est pas defini` | La valeur a été mise dans *Secrets* au lieu de *Variables* |
| `ne contient pas de parametre 'bounds'` | URL copiée avant d'avoir bougé la carte — rebouge la carte et recopie |
| L'e-mail ne part pas, SMTP refusé | Mot de passe normal utilisé au lieu du mot de passe d'application, ou validation en 2 étapes désactivée |
| L'étape *Commit state* échoue en 403 | Settings → Actions → General → *Workflow permissions* → « Read and write permissions » |
| `skipping run, site unavailable` | Le site du CROUS répond mal sur ce coup-là. Volontaire : le run est ignoré, le suivant réessaiera |
| Plus aucune alerte depuis des semaines | Nouvelle campagne CROUS : recadre la carte et remets à jour `CROUS_URL` |
