# Configuration des outils — référence du .env

[Retour au README des outils](../README.md) · [Workflow](../doc/workflow.md) · [Scripts](../doc/scripts.md) · [Version TXT](README.txt)

## Lire et modifier la configuration

Le fichier privé est `doc/tools/.env`. Le modèle générique est [.env.example](../.env.example). Cette référence décrit toutes ses variables, sans reprendre les valeurs privées de ton installation. Aucun réglage applicatif n’impose un framework : serveur, console, services et sondes sont fournis par le profil.

Le format est `CLE=valeur`. Les espaces nécessitent des guillemets ; les listes et objets JSON sont généralement entourés de guillemets simples. Les chaînes multilignes utilisent des séquences JSON `\n`. Le fichier est lu comme des données : aucune commande shell, interpolation `$VARIABLE` ou substitution `$(...)` n’est exécutée. Ne lance pas `source .env`.

```bash
# Depuis le dossier des outils :
python3 _wt_config.py --check
```

Une variable exportée dans l’environnement du processus prend le pas sur le fichier. `WT_CONFIG_FILE` choisit un autre fichier ; le chemin relatif de ce sélecteur part du dossier courant du processus. Sans ce sélecteur, le fichier .env utilisé est toujours celui situé à côté de _wt_config.py. Les variables du fichier ne sont pas automatiquement exportées dans ton terminal Bash.

Les valeurs d’exemple ci-dessous viennent du modèle générique. Le modèle générique fournit une définition Compose dans le .env ; adapte images, entrypoints, services, réseaux et volumes à ton application. Les fichiers et exécutables référencés par les conteneurs doivent exister dans le projet ou dans les images.

## Serveur et console sans dépendance au framework

```bash
WT_MODES='{"http":"web_http"}'
WT_DEFAULT_MODE=http
WT_SERVER_COMMANDS='{"http":["python3","-m","http.server","3000","--bind","0.0.0.0"]}'
WT_CONSOLE_SERVICE=console
WT_CONSOLE_COMMAND='["python3","-i"]'
```

Les tableaux sont des arguments séparés, sans shell implicite. Ils remplacent le champ `command` des services Compose, mais conservent leur `entrypoint`, image et environnement définis dans `WT_COMPOSE_CONFIG`. Si l’image exige un shell, il faut le demander explicitement dans le tableau, par exemple `["sh","-c","exec mon-serveur"]`. Les exécutables doivent exister dans l’image. Le serveur doit écouter sur l’interface du conteneur adaptée au port publié ; la console exige un service interactif, notamment `stdin_open: true` et `tty: true` dans Compose.

Un lancement lit la configuration et génère le complément Compose. Un service déjà en cours ne change pas de commande à la modification du .env. Ferme l’atelier proprement avec `worktree_close.sh`, puis rouvre-le pour appliquer les changements ; une relance d’un service déjà arrêté régénère aussi sa configuration. Les superviseurs d’onglets déjà ouverts doivent être fermés puis rouverts pour recharger les paramètres importés en mémoire. Aucun service n’est redémarré par la simple édition du fichier.

Le backend actuel reste Docker Compose et GNOME Terminal. Les modes réseau http/https/debug et les options du wrapper CI conservent leur protocole ; l’indépendance au framework ne remplace pas ces composants.

## Définition Docker complète dans le .env

`WT_COMPOSE_CONFIG` contient toute la configuration des conteneurs applicatifs et de l’infrastructure partagée. `WT_CI_COMPOSE_CONFIG` contient celle des conteneurs de tests. Ce sont des objets JSON au format Compose, lus dans le même .env que les autres réglages. Ils remplacent respectivement les fichiers désignés par WT_COMPOSE_FILE et WT_CI_COMPOSE_FILE lorsqu’ils ne sont pas vides. Aucun fichier Compose du projet n’est alors nécessaire aux outils.

Exemple de structure, présenté avec des retours à la ligne pour la lecture :

```json
{
  "services": {
    "web_http": {
      "image": "python:3.12-slim",
      "working_dir": "/app",
      "ports": ["127.0.0.1:3000:3000"],
      "volumes": ["cache:/app/cache"],
      "networks": ["frontend"]
    }
  },
  "volumes": {"cache": {}},
  "networks": {"frontend": {}}
}
```

Dans le .env, l’objet doit être sérialisé sur une seule ligne et protégé par des guillemets simples :

```bash
WT_COMPOSE_CONFIG='{"services":{"web_http":{"image":"python:3.12-slim"}},"volumes":{},"networks":{}}'
```

Cet exemple court illustre le format ; le modèle `.env.example` inclut tous les services correspondant à ses autres variables. Pour utiliser seulement un service, adapte aussi WT_APP_SERVICES, WT_MODES et les rôles du workflow.

Les champs Compose permettent de définir les particularités de l’installation :

- `services.<nom>.image` ou `build` : image, contexte de construction et Dockerfile ; les noms, versions et chemins viennent du profil.
- `command`, `entrypoint`, `working_dir`, `user`, `stdin_open`, `tty` : commande de base et exécution des services. WT_SERVER_COMMANDS et WT_CONSOLE_COMMAND priment sur command pour leurs services respectifs.
- `environment`, `env_file`, `secrets`, `configs` : paramètres des conteneurs et fichiers privés éventuels. Un fichier env_file explicitement référencé reste nécessaire ; il est distinct du .env des outils.
- `ports`, `volumes`, `networks`, `depends_on`, `healthcheck`, `profiles` et les autres champs Compose : branchements et contraintes de l’application.
- `volumes`, `networks`, `secrets` et `configs` à la racine : définitions des ressources utilisées par les services.

Les chemins relatifs du profil applicatif partent de WT_MAIN_ROOT. Ceux du profil CI partent du worktree de tâche, grâce au répertoire de projet passé explicitement à Compose. Les variables `${NOM}` d’un modèle sont interpolées par Compose à l’exécution, via son environnement habituel et WT_COMPOSE_ENV_FILE pour l’application ; le chargeur du .env des outils ne les interprète pas. Les valeurs d’environnement de l’application doivent être déclarées dans le modèle Compose ou dans son env_file : les réglages WT_* ne sont pas automatiquement transmis aux conteneurs.

Les outils écrivent des fichiers privés dans WT_STATE_DIR/configs pour transmettre ces objets à Compose. Un nom dérivé du contenu évite d’écraser le modèle déjà utilisé par un atelier. Ces fichiers sont générés ; modifie le .env, puis ferme et rouvre l’atelier pour appliquer le profil. Les anciennes définitions générées restent disponibles pour les états existants.

### Particularités des worktrees

Le complément généré par les outils adapte ensuite le modèle du profil :

- Le code du worktree est monté à WT_CONTAINER_PATH ; les fichiers temporaires utilisent le dossier partagé configuré.
- Chaque tâche utilise son projet Docker et ses volumes persistants ; les services d’infrastructure restent communs.
- Les ports publics, réseaux privés et alias sont remplacés selon WT_HTTP_SERVICE, WT_TLS_SERVICE, WT_DEBUG_SERVICE et les variables de réseaux. Le proxy reçoit WT_PROXY_TEMPLATE au chemin WT_PROXY_CONFIG_PATH.
- Les dépendances Compose directes des services applicatifs de tâche sont retirées ; l’infrastructure est démarrée et vérifiée séparément par WT_INFRA_SERVICES et WT_INFRA_PROBE.
- La CI utilise le modèle WT_CI_COMPOSE_CONFIG, le runner WT_CI_COMMAND, les montages et le cache configurés. Elle conserve les options --db-setup/--no-db-setup du workflow actuel.

Les contrôles de montage, de ports et d’isolation restent actifs quelle que soit la définition Docker fournie. Ce profil configure les conteneurs ; Docker et son plugin Compose doivent déjà être installés sur la machine.

## Sélecteur du profil

### WT_CONFIG_FILE

Chemin du fichier .env alternatif, défini dans l’environnement du processus, avant le démarrage des outils. Ne se définit pas à l’intérieur du .env pour sélectionner ce même fichier.

```bash
WT_CONFIG_FILE=/chemin/profil.env python3 _wt_config.py --check
```

## Variables du fichier .env

### WT_MAIN_ROOT

Chemin du dépôt principal. Un chemin relatif part du dossier contenant les outils, même avec WT_CONFIG_FILE. Les patchs sont toujours centralisés dans son sous-dossier doc.

Exemple générique (syntaxe `.env`) :

```bash
WT_MAIN_ROOT=../..
```

### WT_PROJECT_NAME

Nom du projet, employé dans les valeurs de repli et les titres.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROJECT_NAME=project
```

### WT_WORKTREE_PREFIX

Préfixe des dossiers de tâches, créés à côté du dépôt principal. Sans séparateur de chemin.

Exemple générique (syntaxe `.env`) :

```bash
WT_WORKTREE_PREFIX=project-
```

### WT_BASE_BRANCH

Branche de référence pour créer les tâches et actualiser leur base. Les commandes historiques contenant master utilisent cette valeur.

Exemple générique (syntaxe `.env`) :

```bash
WT_BASE_BRANCH=main
```

### WT_REMOTE

Remote Git de référence pour les actualisations et les audits.

Exemple générique (syntaxe `.env`) :

```bash
WT_REMOTE=origin
```

### WT_FEATURE_PREFIX

Préfixe des branches de tâches, avec le / final.

Exemple générique (syntaxe `.env`) :

```bash
WT_FEATURE_PREFIX=feature/
```

### WT_TICKET_PREFIX

Préfixe des branches de tickets, avec le / final.

Exemple générique (syntaxe `.env`) :

```bash
WT_TICKET_PREFIX=tickets/
```

### WT_STATE_DIR

Dossier relatif au dépôt principal contenant verrous, états des ateliers et configurations Compose générées.

Exemple générique (syntaxe `.env`) :

```bash
WT_STATE_DIR=doc/docker-worktrees
```

### WT_DOCUMENTS_DIR

Nom du dossier de documents central dans WT_MAIN_ROOT. Dans chaque worktree, ce chemin est un lien symbolique vers le dossier central ; les notes et rapports ne sont plus copiés. Codex reçoit un accès en écriture explicite à ce dossier. Les anciens dossiers sont sauvegardés avant remplacement ; la version la plus récente par date de modification est conservée en cas de divergence, avec sauvegarde des deux versions. Des dates identiques ou des types incompatibles nécessitent une décision. Ne change pas le stockage des patchs : WT_MAIN_ROOT/doc.

Exemple générique (syntaxe `.env`) :

```bash
WT_DOCUMENTS_DIR=doc
```

### WT_JOURNAL_DIR

Dossier relatif au dépôt principal pour les journaux personnels .jsont et liens de reprise .jsonl.

Exemple générique (syntaxe `.env`) :

```bash
WT_JOURNAL_DIR=sessions_codex
```

### WT_PERSONAL_NAMES

Liste JSON de noms situés à la racine du dépôt, considérés comme personnels et exclus du travail à committer.

Exemple générique (syntaxe `.env`) :

```bash
WT_PERSONAL_NAMES='[".idea", "doc", "sessions_codex", ".codex", "AGENTS-local.md", "AGENTS.override.md"]'
```

### WT_PERSONAL_PREFIXES

Liste JSON de préfixes de noms personnels à la racine, par exemple .env. Les fichiers correspondants sont protégés des commits via les outils.

Exemple générique (syntaxe `.env`) :

```bash
WT_PERSONAL_PREFIXES='["vimsession_", ".env"]'
```

### WT_PROFILE_FILES

Liste JSON de fichiers d’instructions du dépôt principal liés dans chaque worktree. Chemins relatifs sûrs.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROFILE_FILES='["AGENTS-local.md"]'
```

### WT_PROFILE_OVERRIDE

Chaîne JSON multiligne initialisant AGENTS.override.md. Un fichier existant différent est conservé et signalé.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROFILE_OVERRIDE='"# Instructions locales\n\nLis AGENTS.md et AGENTS-local.md ; conserve les journaux centralisés.\n"'
```

### WT_AGENT_INSTRUCTIONS

Texte transmis à l’agent lors du lancement, complété par le chemin du journal central et le contexte de tâche.

Exemple générique (syntaxe `.env`) :

```bash
WT_AGENT_INSTRUCTIONS='Lis AGENTS.md et AGENTS-local.md.'
```

### WT_LEGACY_ROOT_ALIASES

Liste JSON d’anciens chemins du dépôt principal à remplacer dans les copies initiales des sessions Vim.

Exemple générique (syntaxe `.env`) :

```bash
WT_LEGACY_ROOT_ALIASES='[]'
```

### WT_COPY_FILES

Liste JSON de fichiers privés copiés du dépôt principal vers une nouvelle tâche si la destination est absente, avec permissions 600.

Exemple générique (syntaxe `.env`) :

```bash
WT_COPY_FILES='[".env"]'
```

### WT_SHARED_TEMP_DIR

Dossier temporaire relatif partagé via un lien depuis les tâches. La préparation peut nettoyer l’ancien contenu temporaire. Refus des chemins dangereux et des chevauchements avec les documents ou journaux.

Exemple générique (syntaxe `.env`) :

```bash
WT_SHARED_TEMP_DIR=tmp
```

### WT_NOTES_TEMPLATE

Fichier modèle relatif au dépôt principal utilisé pour créer les notes de suivi d’une nouvelle tâche.

Exemple générique (syntaxe `.env`) :

```bash
WT_NOTES_TEMPLATE=doc/model_suivi.txt
```

### WT_DOCKER_PROJECT

Nom du projet Docker Compose principal. Sert aussi à reconnaître ses conteneurs.

Exemple générique (syntaxe `.env`) :

```bash
WT_DOCKER_PROJECT=project
```

### WT_DOCKER_PREFIX

Préfixe des projets Docker Compose propres aux worktrees, suivi de leur identifiant stable.

Exemple générique (syntaxe `.env`) :

```bash
WT_DOCKER_PREFIX=project-wt-
```

### WT_VOLUME_PREFIX

Préfixe des noms de volumes persistants propres aux worktrees.

Exemple générique (syntaxe `.env`) :

```bash
WT_VOLUME_PREFIX=project_wt_
```

### WT_RUNTIME_LABEL

Clé du label Docker identifiant les runtimes de tâches ; sa valeur est worktree. Éviter de changer cette clé pendant que des runtimes existent.

Exemple générique (syntaxe `.env`) :

```bash
WT_RUNTIME_LABEL=worktree.runtime
```

### WT_WORKTREE_LABEL

Clé du label Docker contenant le chemin absolu du worktree. Conserver cette clé pour reconnaître les conteneurs existants.

Exemple générique (syntaxe `.env`) :

```bash
WT_WORKTREE_LABEL=worktree.path
```

### WT_COMPOSE_FILE

Fichier Compose applicatif de repli, relatif au dépôt principal, utilisé uniquement si WT_COMPOSE_CONFIG est vide ou absent. Si le profil fournit la configuration complète, ce fichier n’est pas lu.

Exemple générique (syntaxe `.env`) :

```bash
WT_COMPOSE_FILE=compose.yml
```

### WT_COMPOSE_ENV_FILE

Fichier d’environnement utilisé par Compose, relatif au dépôt principal. Distinct du .env des outils. Si absent, /dev/null est utilisé.

Exemple générique (syntaxe `.env`) :

```bash
WT_COMPOSE_ENV_FILE=.env
```

### WT_APP_SERVICES

Liste JSON des services applicatifs à monter sur le bon dépôt et à isoler par worktree. Inclure les services serveur et console configurés.

Exemple générique (syntaxe `.env`) :

```bash
WT_APP_SERVICES='["runner", "web", "web_http", "console", "shell", "debug"]'
```

### WT_INFRA_SERVICES

Liste JSON des services communs démarrés dans le projet principal. Ils ne sont pas recréés pour chaque tâche. Une liste vide désactive cette étape.

Exemple générique (syntaxe `.env`) :

```bash
WT_INFRA_SERVICES='[]'
```

### WT_MODES

Objet JSON associant chaque mode à un nom de service serveur Compose. Les modes http, https et debug ont des règles de ports et de proxy propres au backend actuel.

Exemple générique (syntaxe `.env`) :

```bash
WT_MODES='{"http": "web_http", "https": "web", "debug": "debug"}'
```

### WT_DEFAULT_MODE

Mode choisi lorsqu’aucun mode explicite ni état précédent ne s’applique. Doit figurer dans WT_MODES.

Exemple générique (syntaxe `.env`) :

```bash
WT_DEFAULT_MODE=http
```

### WT_CONTAINER_PATH

Chemin de montage du code dans les conteneurs applicatifs, également employé par le runner CI.

Exemple générique (syntaxe `.env`) :

```bash
WT_CONTAINER_PATH=/app
```

### WT_CONSOLE_SERVICE

Nom du service Compose utilisé par le rôle interne console. Doit être distinct des services serveur.

Exemple générique (syntaxe `.env`) :

```bash
WT_CONSOLE_SERVICE=console
```

### WT_SHELL_SERVICE

Service Compose dont l’image fournit le shell de nettoyage des fichiers temporaires générés par Docker.

Exemple générique (syntaxe `.env`) :

```bash
WT_SHELL_SERVICE=shell
```

### WT_PROXY_SERVICE

Nom du service Compose qui publie le proxy pour les modes autres que http.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROXY_SERVICE=caddy
```

### WT_MAIN_PORT

Port public du serveur principal sur localhost.

Exemple générique (syntaxe `.env`) :

```bash
WT_MAIN_PORT=3000
```

### WT_PORT_START

Premier port public attribuable aux tâches.

Exemple générique (syntaxe `.env`) :

```bash
WT_PORT_START=3002
```

### WT_PORT_END

Dernier port public attribuable aux tâches, inclus. Un worktree conserve son ancien port s’il est disponible. Sinon, les ports suivants sont essayés, puis ceux du début de la plage. Les ports réservés ou occupés sont ignorés, ainsi que les ports dont le port de débogage associé est indisponible en mode debug. Un refus n’intervient que si aucun port utilisable ne reste dans la plage.

Exemple générique (syntaxe `.env`) :

```bash
WT_PORT_END=3009
```

### WT_DEBUG_PORT

Premier port public de débogage des tâches. Le port effectif ajoute le décalage du port HTTP par rapport à WT_PORT_START.

Exemple générique (syntaxe `.env`) :

```bash
WT_DEBUG_PORT=1234
```

### WT_INTERNAL_PORT

Port applicatif dans les conteneurs, cible des publications HTTP et du proxy.

Exemple générique (syntaxe `.env`) :

```bash
WT_INTERNAL_PORT=3000
```

### WT_INTERNAL_DEBUG_PORT

Port de débogage interne publié par le service debug.

Exemple générique (syntaxe `.env`) :

```bash
WT_INTERNAL_DEBUG_PORT=1234
```

### WT_VOLUMES

Liste JSON des clés de volumes Compose auxquelles donner un nom persistant propre au worktree.

Exemple générique (syntaxe `.env`) :

```bash
WT_VOLUMES='["cache", "dependencies"]'
```

### WT_PROXY_VOLUME

Clé du volume Compose persistant du proxy, également isolé par tâche.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROXY_VOLUME=caddy_config
```

### WT_APP_NETWORKS

Objet JSON des réseaux affectés aux services applicatifs des tâches. Les options correspondent aux options Compose de chaque réseau.

Exemple générique (syntaxe `.env`) :

```bash
WT_APP_NETWORKS='{"frontend": {}, "backend": {}, "shared_frontend": {}}'
```

### WT_PRIVATE_NETWORK

Clé du réseau privé dans lequel les services sélectionnés reçoivent l’alias applicatif.

Exemple générique (syntaxe `.env`) :

```bash
WT_PRIVATE_NETWORK=frontend
```

### WT_SHARED_NETWORK

Clé du réseau partagé où l’alias applicatif est interdit pour éviter les collisions entre tâches.

Exemple générique (syntaxe `.env`) :

```bash
WT_SHARED_NETWORK=shared_frontend
```

### WT_APP_ALIAS

Alias réseau du serveur, utilisé notamment par le proxy dans le réseau privé.

Exemple générique (syntaxe `.env`) :

```bash
WT_APP_ALIAS=app
```

### WT_ALIAS_SERVICES

Liste JSON des services recevant WT_APP_ALIAS dans WT_PRIVATE_NETWORK.

Exemple générique (syntaxe `.env`) :

```bash
WT_ALIAS_SERVICES='["web", "debug"]'
```

### WT_HTTP_SERVICE

Service serveur dont les ports sont remplacés par le port HTTP attribué au worktree.

Exemple générique (syntaxe `.env`) :

```bash
WT_HTTP_SERVICE=web_http
```

### WT_DEBUG_SERVICE

Service serveur dont les ports sont remplacés par le port de débogage attribué.

Exemple générique (syntaxe `.env`) :

```bash
WT_DEBUG_SERVICE=debug
```

### WT_TLS_SERVICE

Service serveur HTTPS sans publication directe ; le proxy publie le port de la tâche.

Exemple générique (syntaxe `.env`) :

```bash
WT_TLS_SERVICE=web
```

### WT_EXTERNAL_NETWORKS

Objet JSON associant les clés des réseaux Compose aux noms Docker des réseaux partagés préexistants.

Exemple générique (syntaxe `.env`) :

```bash
WT_EXTERNAL_NETWORKS='{"backend": "project_backend", "shared_frontend": "project_frontend"}'
```

### WT_PROXY_CONFIG_PATH

Chemin de la configuration du proxy à monter dans son conteneur.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROXY_CONFIG_PATH=/etc/caddy/Caddyfile
```

### WT_PROXY_TEMPLATE

Chaîne JSON multiligne du fichier de configuration du proxy. Le marqueur @PORT@ est remplacé par le port public attribué. La syntaxe dépend du proxy choisi.

Exemple générique (syntaxe `.env`) :

```bash
WT_PROXY_TEMPLATE='"{\n auto_https disable_redirects\n}\nhttps://localhost:3000 {\n tls internal\n reverse_proxy app:3000 {\n  header_up X-Forwarded-Proto https\n  header_up X-Forwarded-Port @PORT@\n }\n}\n"'
```

### WT_SERVER_PROBE

Liste JSON argv exécutée par docker exec dans le conteneur serveur pour vérifier sa disponibilité. Une liste vide vérifie seulement que les services requis tournent. Délai total de disponibilité : 60 secondes.

Exemple générique (syntaxe `.env`) :

```bash
WT_SERVER_PROBE='["python3", "-c", "import socket; socket.create_connection(('"'"'127.0.0.1'"'"', 3000), 2).close()"]'
```

### WT_INFRA_PROBE

Liste JSON argv ajoutée à la commande Compose principale pour attendre l’infrastructure, par exemple ["exec","-T","database","check-ready"]. Liste vide : aucune sonde. Délai total : 300 secondes.

Exemple générique (syntaxe `.env`) :

```bash
WT_INFRA_PROBE='[]'
```

### WT_SHARED_CONFIG_FILES

Liste JSON de fichiers relatifs comparés au dépôt principal avant activation. Une différence dans la tâche provoque un refus.

Exemple générique (syntaxe `.env`) :

```bash
WT_SHARED_CONFIG_FILES='["compose.yml"]'
```

### WT_RUNTIME_DIRS

Liste JSON de dossiers à créer dans le dépôt ciblé avant activation des services, par exemple log.

Exemple générique (syntaxe `.env`) :

```bash
WT_RUNTIME_DIRS='["log"]'
```

### WT_ROLE_TITLES

Objet JSON des rôles et titres des onglets de tâche. Rôles reconnus : codex, console, server, vim (Bash) et vim_doc (notes Vim). L’ordre des clés donne l’ordre des onglets.

Exemple générique (syntaxe `.env`) :

```bash
WT_ROLE_TITLES='{"codex": "Codex", "console": "Console applicative", "server": "Service applicatif", "vim": "Bash", "vim_doc": "Vim documentation"}'
```

### WT_TERMINAL_PROFILE_NAMESPACE

Préfixe employé pour calculer l’identifiant stable du profil GNOME Terminal à partir du dépôt principal.

Exemple générique (syntaxe `.env`) :

```bash
WT_TERMINAL_PROFILE_NAMESPACE=worktree-atelier:
```

### WT_TERMINAL_PROFILE_TITLE

Préfixe du nom visible du profil GNOME Terminal, complété par un identifiant.

Exemple générique (syntaxe `.env`) :

```bash
WT_TERMINAL_PROFILE_TITLE='Worktrees '
```

### WT_SPECIAL_COMMITS

Objet JSON associant un message spécial à une liste de fichiers relatifs à ajouter automatiquement avant commit. Objet vide conseillé pour garder une préparation explicite de l’index.

Exemple générique (syntaxe `.env`) :

```bash
WT_SPECIAL_COMMITS='{}'
```

### WT_PUSH_TARGETS

Objet JSON des destinations acceptées par push.sh. Chaque entrée contient remote et, facultativement, before (liste argv exécutée avant le push, délai 30 secondes). Exemple : {"origin":{"remote":"origin"}}.

Exemple générique (syntaxe `.env`) :

```bash
WT_PUSH_TARGETS='{"origin": {"remote": "origin"}}'
```

### WT_FORCE_PUSH_REMOTES

Liste JSON des remotes pour lesquels un --force-with-lease explicitement demandé est autorisé. Une liste vide interdit cette option partout.

Exemple générique (syntaxe `.env`) :

```bash
WT_FORCE_PUSH_REMOTES='[]'
```

### WT_AUDIT_TARGETS

Liste JSON des références Git examinées par merge_to_dev.sh. Les audits ne réalisent pas de fusion.

Exemple générique (syntaxe `.env`) :

```bash
WT_AUDIT_TARGETS='[]'
```

### WT_CI_PROJECT

Nom du projet Compose de tests, commun aux tâches et protégé par un verrou CI.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_PROJECT=project-ci
```

### WT_CI_COMPOSE_FILE

Fichier Compose CI de repli, relatif au worktree de tâche, utilisé uniquement si WT_CI_COMPOSE_CONFIG est vide ou absent.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_COMPOSE_FILE=compose.ci.yml
```

### WT_CI_SCHEMA_GLOB

Motif glob relatif à la tâche pour les fichiers de schéma dont le hash doit être compatible avec la dernière préparation CI connue.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_SCHEMA_GLOB='schema/*.sql'
```

### WT_CI_COMMAND

Liste JSON argv du runner de tests exécuté dans le service CI. Les arguments utilisateur sont ajoutés ensuite. Le wrapper ajoute actuellement --no-db-setup par défaut et reconnaît --db-setup ; prévoir un runner compatible.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_COMMAND='["./run-tests"]'
```

### WT_CI_SERVICE

Nom du service Compose exécutant les tests.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_SERVICE=test
```

### WT_CI_SHARED_DIRS

Liste JSON de dossiers relatifs montés en lecture seule depuis le dépôt principal lorsqu’ils sont absents de la tâche.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_SHARED_DIRS='[]'
```

### WT_CI_CACHE_KEY

Clé du volume Compose CI dont le nom est remplacé pour isoler le cache de la tâche.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_CACHE_KEY=cache
```

### WT_CI_CACHE_PREFIX

Préfixe du nom du volume de cache CI, suivi de l’identifiant du worktree et du suffixe _cache.

Exemple générique (syntaxe `.env`) :

```bash
WT_CI_CACHE_PREFIX=project_ci_
```

### WT_MAIN_ROLE_TITLES

Objet JSON des rôles et titres des onglets ajoutés au terminal principal. Rôles de services : console et server.

Exemple générique (syntaxe `.env`) :

```bash
WT_MAIN_ROLE_TITLES='{"console": "Console applicative", "server": "Service principal"}'
```

### WT_BRANCH_MARKER

Chemin relatif du fichier où put_branch_in_idea.sh écrit le nom de la branche courante.

Exemple générique (syntaxe `.env`) :

```bash
WT_BRANCH_MARKER=.idea
```

### WT_LEGACY_ENV_PREFIXES

Liste JSON des anciens préfixes de variables de session acceptés pour les superviseurs encore ouverts. Les nouveaux ateliers utilisent WT_.

Exemple générique (syntaxe `.env`) :

```bash
WT_LEGACY_ENV_PREFIXES='[]'
```

### WT_SERVER_COMMANDS

Objet JSON associant les modes de WT_MODES aux listes argv de lancement du serveur dans le conteneur. Remplace command du service Compose pour le principal et les tâches. Exemple générique : {"http":["python3","-m","http.server","3000","--bind","0.0.0.0"]}. Mode absent ou objet vide : conserver command du Compose. Deux modes utilisant le même service doivent avoir la même commande.

Exemple générique (syntaxe `.env`) :

```bash
WT_SERVER_COMMANDS='{"http": ["python3", "-m", "http.server", "3000", "--bind", "0.0.0.0"], "https": ["python3", "-m", "http.server", "3000", "--bind", "0.0.0.0"], "debug": ["python3", "-m", "http.server", "3000", "--bind", "0.0.0.0"]}'
```

### WT_CONSOLE_COMMAND

Liste JSON argv de lancement de la console dans son conteneur. Remplace command du service WT_CONSOLE_SERVICE. Exemple générique : ["python3","-i"]. Liste vide : conserver command du Compose.

Exemple générique (syntaxe `.env`) :

```bash
WT_CONSOLE_COMMAND='["python3", "-i"]'
```


### WT_COMPOSE_CONFIG

Définition Compose complète de l’application et des services d’infrastructure, sous forme d’objet JSON. Contient services, images ou build, volumes, réseaux, environnement, entrypoints et toutes les particularités des conteneurs. Prioritaire sur WT_COMPOSE_FILE. Chemins relatifs à WT_MAIN_ROOT. Objet vide `{}` : employer le fichier de repli configuré. Un objet non vide doit contenir une section services non vide. Voir la section sur la définition Docker complète ci-dessus et le modèle [.env.example](../.env.example).

### WT_CI_COMPOSE_CONFIG

Définition Compose complète des tests, sous forme d’objet JSON. Prioritaire sur WT_CI_COMPOSE_FILE. Chemins relatifs au worktree de tâche ; images, build, base de test et volumes sont propres à ce profil CI. Objet vide `{}` : employer le fichier de repli configuré. Un objet non vide doit contenir une section services non vide. Voir la section sur la définition Docker complète ci-dessus et le modèle [.env.example](../.env.example).

## Variables de session internes

Les variables `WT_*` visibles dans un atelier ne sont pas toutes des réglages .env. `WT_WORKTREE`, `WT_ROLE`, les chemins `WT_VIM_*` et `WT_SHELL_CLOSE` sont des informations temporaires transmises par les superviseurs. Ne les définis pas dans le profil. La liste ci-dessus couvre les variables de configuration du modèle, plus le sélecteur WT_CONFIG_FILE.
