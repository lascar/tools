Workflow worktree — index des outils

Version HTML (README.html) · Version TXT (README.txt) · Configuration du .env (env/README.txt)

Documentation vérifiée le 15 octobre 2026 sur les fichiers .sh actuellement présents dans ce dossier : 28 commandes et 2 fichiers de support Bash. Les versions Markdown servent de source ; chaque page existe aussi en HTML et TXT. Les liens du Markdown permettent de naviguer dans un lecteur Markdown ou sur une forge Git. Pour lire la documentation dans un navigateur local, ouvre la version HTML (README.html).

Lire dans l’ordre

- Prérequis et installation (doc/installation.md) — préparer le poste, configurer les outils et vérifier l’installation.
- Workflow quotidien (doc/workflow.md) — préparer le terminal, rejoindre une tâche, travailler, sauvegarder, restaurer et terminer.
- Référence des scripts (doc/scripts.md) — arguments, effets et exemples des seuls .sh actuels.
- Configuration du .env (env/README.txt) — rôle et syntaxe de chaque variable, commandes du serveur et de la console.
- Modèle de configuration (.env.example) — exemple générique, à adapter seulement pour une nouvelle installation.

Rejoindre et gérer les ateliers

- worktree_reach.sh (doc/scripts.md#worktree_reachsh) — créer, ouvrir ou afficher une tâche ; sans argument, afficher le terminal principal.
- worktree_new.sh (doc/scripts.md#worktree_newsh) — créer explicitement une nouvelle branche et son worktree.
- worktree_open.sh (doc/scripts.md#worktree_opensh) — ouvrir une branche existante dans son worktree.
- worktree_open_current.sh (doc/scripts.md#worktree_open_currentsh) — ouvrir l’atelier du worktree courant.
- worktree_switch.sh (doc/scripts.md#worktree_switchsh) — afficher un terminal existant.
- worktree_status.sh (doc/scripts.md#worktree_statussh) — afficher l’état Git des worktrees, leurs ateliers ouverts ou fermés et le total ouvert.
- worktree_close.sh (doc/scripts.md#worktree_closesh) — fermer un atelier ; sans argument depuis le principal, fermer tous les ateliers ouverts en conservant les worktrees.
- worktree_remove.sh (doc/scripts.md#worktree_removesh) — retirer explicitement un worktree fermé, après archive vérifiée.
- change_branch.sh (doc/scripts.md#change_branchsh) — rejoindre un worktree inactif dans le terminal appelant, après actualisation et fusion.

Sauvegarder et examiner le travail

- patch.sh (doc/scripts.md#patchsh) — créer, lister ou restaurer une sauvegarde.
- is_any_patch.sh (doc/scripts.md#is_any_patchsh) — lister les sauvegardes d’une tâche, même sans worktree.
- desc_git.sh (doc/scripts.md#desc_gitsh) — produire le rapport des commits de la tâche, avec les fichiers modifiés en tête.
- worktree_check_conflicts.sh (doc/scripts.md#worktree_check_conflictssh) — repérer les fichiers modifiés ailleurs et simuler une fusion.

Exécuter et relancer les services

- worktree_lance_master.sh (doc/scripts.md#worktree_lance_mastersh) — ouvrir les onglets des services du dépôt principal.
- worktree_docker.sh (doc/scripts.md#worktree_dockersh) — gérer les services Docker d’un worktree.
- relance_serveur.sh (doc/scripts.md#relance_serveursh) — remettre en service et attacher le serveur ciblé.
- relance_console.sh (doc/scripts.md#relance_consolesh) — remettre en service et attacher la console ciblée.
- relance_docker.sh (doc/scripts.md#relance_dockersh) — alias actuel de la relance du serveur.
- docker_stop_all.sh (doc/scripts.md#docker_stop_allsh) — arrêter les conteneurs selon le contexte courant.
- worktree_test.sh (doc/scripts.md#worktree_testsh) — lancer le runner de tests configuré depuis la tâche.

Git et sessions

- pull.sh (doc/scripts.md#pullsh) — faire un pull en avance rapide dans le dossier courant.
- commit.sh (doc/scripts.md#commitsh) — committer l’index préparé dans le worktree de tâche.
- push.sh (doc/scripts.md#pushsh) — pousser la branche de tâche vers une destination configurée.
- merge_to_master.sh (doc/scripts.md#merge_to_mastersh) — auditer l’intégration dans la branche principale, sans fusion.
- merge_to_dev.sh (doc/scripts.md#merge_to_devsh) — auditer l’intégration dans une branche de développement, sans fusion.
- relance_codex.sh (doc/scripts.md#relance_codexsh) — reprendre l’agent dans le worktree courant.
- vim_session.sh (doc/scripts.md#vim_sessionsh) — ouvrir une session d’éditeur existante ou les notes de la tâche.
- put_branch_in_idea.sh (doc/scripts.md#put_branch_in_ideash) — écrire le marqueur de branche configuré.

Fichiers de support Bash

- worktree_shell.sh (doc/scripts.md#worktree_shellsh) — à charger avec source pour les fonctions du terminal.
- worktree_bashrc.sh (doc/scripts.md#worktree_bashrcsh) — fichier chargé automatiquement par les onglets gérés.

Conventions de ce guide

Les exemples utilisent 1234_ma_tache et t_2345_mon_ticket. Les préfixes de branches, chemins, services, ports et titres d’onglets viennent du .env des outils. WT_MAIN_ROOT désigne le dépôt principal ; les scripts Python le lisent dans .env, sans l’exporter automatiquement dans ton terminal Bash.

Les patchs et leurs fichiers .done se trouvent toujours dans WT_MAIN_ROOT/doc, commun à toutes les tâches. Le code et les opérations Git d’une tâche restent dans son worktree. Les journaux d’agent utilisent le dossier central configuré.

Les commandes d’ouverture conservent le dossier du terminal de lancement. change_branch.sh fait un véritable cd grâce à la fonction Bash. La fermeture peut fermer le shell appelant s’il s’agit d’un onglet supplémentaire de l’atelier ; lorsqu’il est conservé, la fonction revient au dépôt principal.

La référence couvre uniquement les .sh présents à la date de vérification. Les aides des 28 commandes ont été exécutées sans action ; les comportements ont été recoupés avec les modules appelés. Cette vérification documentaire n’a démarré aucun service, ouvert aucun atelier ni exécuté de commit, push ou fusion.

Installer et vérifier

Le fichier .env placé à côté des scripts contient les réglages propres à l’installation, y compris les définitions complètes des conteneurs applicatifs et CI. Avec WT_COMPOSE_CONFIG et WT_CI_COMPOSE_CONFIG renseignés, les outils génèrent leurs fichiers Compose sans dépendre des fichiers Compose du projet. Il est ignoré par Git et reste privé. .env.example fournit un profil générique à adapter ; conserve ton .env existant lors d’une mise à jour.

# Pour une nouvelle installation uniquement :
cp .env.example .env
python3 _wt_config.py --check

Le format est CLE=valeur, avec des guillemets pour une valeur contenant des espaces. Les listes, objets et chaînes multilignes utilisent du JSON entouré de guillemets simples. Les valeurs ne sont jamais exécutées comme du shell : pas de source .env, de substitution de commande ni d’expansion de variables. Les chemins relatifs de WT_MAIN_ROOT partent du dossier des outils ; les chemins projet partent de cette racine. WT_CONFIG_FILE sélectionne un autre fichier ; une variable WT_* exportée prend le pas sur ce fichier.

Les paramètres sont regroupés par usage :

- WT_MAIN_ROOT, WT_PROJECT_NAME, WT_WORKTREE_PREFIX, WT_STATE_DIR : dépôt et stockage local.
- WT_BASE_BRANCH, WT_REMOTE, WT_FEATURE_PREFIX, WT_TICKET_PREFIX : conventions Git. Les noms des commandes historiques contenant master restent des alias de la branche configurée.
- WT_PROFILE_FILES, WT_PROFILE_OVERRIDE, WT_AGENT_INSTRUCTIONS, WT_COPY_FILES, WT_LEGACY_ROOT_ALIASES : instructions et fichiers personnels des ateliers.
- WT_JOURNAL_DIR, WT_PERSONAL_NAMES, WT_PERSONAL_PREFIXES, WT_SHARED_TEMP_DIR, WT_NOTES_TEMPLATE : journaux centralisés, exclusions des commits, fichiers temporaires et notes.
- WT_ROLE_TITLES, WT_MAIN_ROLE_TITLES, WT_TERMINAL_PROFILE_NAMESPACE, WT_TERMINAL_PROFILE_TITLE : onglets et profil GNOME.
- WT_COMPOSE_CONFIG, WT_CI_COMPOSE_CONFIG : définitions Docker complètes (images, services, volumes, réseaux et paramètres applicatifs ou CI) ; voir la configuration Docker (env/README.txt).
- WT_COMPOSE_FILE, WT_COMPOSE_ENV_FILE, WT_DOCKER_PROJECT, WT_DOCKER_PREFIX, WT_APP_SERVICES, WT_INFRA_SERVICES, WT_MODES : Compose et services. Le .env des outils est distinct du fichier d’environnement transmis à Compose.
- WT_CONTAINER_PATH, WT_MAIN_PORT, WT_PORT_START, WT_PORT_END, WT_DEBUG_PORT, WT_INTERNAL_PORT, WT_INTERNAL_DEBUG_PORT : montages et ports.
- WT_CONSOLE_SERVICE, WT_SHELL_SERVICE, WT_PROXY_SERVICE, WT_HTTP_SERVICE, WT_TLS_SERVICE, WT_DEBUG_SERVICE : services affectés aux rôles internes du workflow.
- WT_SERVER_COMMANDS, WT_CONSOLE_COMMAND : commandes applicatives de lancement dans les conteneurs ; voir la référence de chaque variable (env/README.txt).
- WT_SERVER_PROBE, WT_INFRA_PROBE : commandes de disponibilité, sous forme de tableaux d’arguments ; aucune dépendance à un langage ou à une base de données dans le moteur.
- WT_APP_NETWORKS, WT_EXTERNAL_NETWORKS, WT_PRIVATE_NETWORK, WT_SHARED_NETWORK, WT_APP_ALIAS, WT_ALIAS_SERVICES : réseaux privés et infrastructure commune.
- WT_PROXY_TEMPLATE, WT_PROXY_CONFIG_PATH, WT_PROXY_VOLUME, WT_VOLUMES, WT_VOLUME_PREFIX, WT_RUNTIME_DIRS, WT_SHARED_CONFIG_FILES : proxy et fichiers du runtime. Le marqueur @PORT@ du modèle est remplacé par le port attribué.
- WT_RUNTIME_LABEL, WT_WORKTREE_LABEL : reconnaissance des conteneurs ; conserver les valeurs lors d’une migration de runtimes existants.
- WT_CI_* : Compose, commande de test, cache et dépendances de l’exécution CI.
- WT_SPECIAL_COMMITS, WT_PUSH_TARGETS, WT_FORCE_PUSH_REMOTES, WT_AUDIT_TARGETS : conventions Git optionnelles, fichiers associés à des messages spéciaux et éventuelle commande préalable à une destination de push.

Les contrôles refusent une configuration invalide. Les commandes externes restent des listes d’arguments, sans eval. Le protocole interne conserve les rôles console et server, mais leurs libellés, services et sondes sont configurables.

Utilisation

Depuis un terminal Bash, charge le fichier à son emplacement réel :

source /chemin/vers/tools/worktree_shell.sh
worktree_reach.sh 1234_ma_tache
worktree_reach.sh                 # Afficher le terminal principal existant
worktree_close.sh 1234_ma_tache

Voir les ateliers ouverts et les fermer

worktree_status.sh                # État Git, onglets actifs et nombre d’ateliers ouverts
worktree_close.sh 1234_ma_tache   # Fermer uniquement cette tâche

Sans argument, worktree_close.sh ferme la tâche courante lorsqu’il est lancé dans son worktree. Depuis WT_MAIN_ROOT, il ferme tous les ateliers de tâches ouverts, un par un. Les ateliers déjà fermés sont ignorés ; le terminal et les services du principal restent disponibles.

La fermeture collective s’arrête au premier refus (buffers Vim non enregistrés, commande active non arrêtée ou sauvegarde impossible). Le message indique la tâche bloquante et le nombre de fermetures réussies. Corrige le blocage, puis relance la même commande depuis le principal : les ateliers encore ouverts sont de nouveau détectés. Aucune branche ni aucun worktree n’est supprimé.

Le statut et la fermeture collective concernent les ateliers gérés par les outils. Un simple terminal ouvert manuellement dans un dossier de tâche ne suffit pas à déclarer un atelier ouvert.

worktree_reach.sh crée ou rattache le worktree si nécessaire, ouvre un atelier fermé, ou affiche un atelier vivant. --no-update évite l’actualisation Git lors d’une ouverture. Sans tâche, la commande affiche le terminal principal existant sans actualisation ni démarrage du runtime.

change_branch.sh 1234_ma_tache utilise un worktree déjà rattaché : contrôles d’inactivité et de propreté, actualisation en avance rapide de la branche principale, véritable cd dans le terminal appelant, puis fusion dans la branche de tâche. Un refus préalable conserve le dossier initial ; un conflit laisse le terminal dans le worktree cible. La fonction Bash est obligatoire pour ce déplacement.

desc_git.sh <tâche> produit le rapport Git dans le dossier de documentation du worktree existant demandé, même depuis le dépôt principal. Sans argument, desc_git.sh décrit le worktree courant. Le rapport commence par les fichiers modifiés, puis détaille les commits dont le sujet commence par le nom exact de la tâche, y compris ceux déjà intégrés dans la branche principale. La commande ne change pas de branche et ne crée pas de worktree.

is_any_patch.sh [tâche] [done] liste les sauvegardes de la tâche demandée, ou du worktree courant si aucun nom n’est donné. done sélectionne les sauvegardes traitées ; la forme is_any_patch.sh done reste disponible. La recherche utilise uniquement WT_MAIN_ROOT/doc, commun à toutes les tâches. Une tâche nommée ne nécessite ni branche ni worktree ; aucun n’est créé. La commande n’applique aucune sauvegarde.

Tous les patchs, archives et fichiers .done sont stockés exclusivement dans WT_MAIN_ROOT/doc, indépendamment du worktree courant et de WT_DOCUMENTS_DIR. La création, les listes, les restaurations et les sauvegardes automatiques d’ouverture/fermeture utilisent ce même emplacement et le même verrou. Une restauration avec doc/<patch> depuis un worktree vise ce dossier central ; un chemin absolu extérieur est refusé.

patch.sh sans argument propose les sauvegardes en attente du worktree courant et demande confirmation avant restauration. Après git checkout ., les fichiers non suivis restants sont acceptés uniquement s’ils sont strictement identiques à la sauvegarde ; toute modification différente ou étrangère bloque la restauration. Sans terminal interactif, aucune restauration automatique.

Les commandes Git, sauvegardes, sessions d’éditeur et d’agent, relances de services et tests sont disponibles via leurs scripts --help. La fermeture conserve le worktree, ses fichiers et son journal central. Elle demande confirmation avant d’arrêter une commande active ; aucun arrêt forcé.

Après une mise à jour, recharge worktree_shell.sh dans les terminaux ouverts. Les noms de variables internes utilisent le préfixe WT_ ; les nouveaux ateliers doivent utiliser la même version des scripts que leurs superviseurs.

Vérification

python3 -m unittest discover -s tests -v
python3 -m compileall -q .

Les tests emploient des dépôts temporaires et simulent Docker et les actions graphiques. Un test ouvre une socket locale pour vérifier la détection d’un port occupé. Le profil générique est testé séparément avec un autre nom de projet, une autre branche principale et d’autres services.

Les outils métier et de base de données, les documents et anciennes copies sont conservés hors de cette boîte à outils, dans le dossier voisin tools-projet/. Le générateur des guides personnels se trouve également dans ce dossier.


Actualiser la documentation locale

L’index canonique est ce README. La référence couvre uniquement les .sh actuels. Après une modification de commande ou de variable, actualise le workflow, sa fiche et la référence du .env, puis génère les formats HTML et TXT depuis la racine du projet :

python3 doc/tools-projet/render_worktree_docs.py --root doc/tools --pages README
python3 doc/tools-projet/render_worktree_docs.py --root doc/tools/env --pages README
python3 doc/tools-projet/render_worktree_docs.py --root doc/tools/doc --pages index installation workflow scripts
