# Workflow quotidien avec les worktrees

Pour retirer les ateliers anciens et fermés, commence par `worktree_prune --dry-run`, puis lance `worktree_prune` (20 jours par défaut) ou `worktree_prune 30`. L'âge repose sur les fichiers locaux, l'état Git et les traces de session. Les ateliers modifiés ou actifs sont conservés ; chaque suppression efface sans sauvegarde l’enregistrement Git et le répertoire physique du worktree, et conserve la branche et le journal central. Les fichiers ignorés locaux sont effacés avec le dossier ; seuls les fichiers committés sont conservés dans la branche. Sans argument, `days=20` et `--dry-run` est désactivé : la suppression est réelle. Voir la fiche [worktree_prune / worktree_prune.sh](scripts.md#worktree_prunesh) dans la référence des scripts.

[Retour au README des outils](../README.md) · [Référence des scripts](scripts.md) · [Version TXT](workflow.txt)

Vérifié le 15 octobre 2026. Les exemples décrivent les scripts actuels ; les valeurs propres au projet viennent du `.env` des outils.

Les commandes du serveur et de la console sont définies par `WT_SERVER_COMMANDS` et `WT_CONSOLE_COMMAND`, sous forme de listes d’arguments JSON. Voir la [référence de configuration](../env/README.md) avant d’adapter le profil à un autre projet.

Les définitions Docker complètes viennent de `WT_COMPOSE_CONFIG` et `WT_CI_COMPOSE_CONFIG`. Leurs images, services, volumes et réseaux se règlent dans le même .env. Voir la [configuration Docker](../env/README.md#definition-docker-complete-dans-le-env).

## 1. Préparer le terminal

Depuis le dépôt principal, charge les fonctions Bash :

```bash
source ./doc/tools/worktree_shell.sh
```

Recharge cette ligne dans un terminal déjà ouvert après une modification des fonctions. Les scripts exécutables prennent leurs modifications en compte au lancement suivant. Le `.env` est lu comme des données par les outils : ne le charge pas avec `source`.

Le dossier des outils doit être dans le `PATH` pour utiliser les formes courtes ci-dessous. Sinon, appelle les scripts par leur chemin complet. Exécute les commandes depuis le dépôt principal ou un worktree de tâche, jamais depuis le dépôt Git imbriqué du dossier des outils.

Avant de changer de tâche, vérifie le contexte :

```bash
worktree_status.sh
```

### Démarrer la session principale de Pascal

```bash
/home/pascal/development/bo.facile/doc/tools/zz.sh
```

Ce lanceur personnel exige `master` dans le dépôt principal et tous les ateliers de tâches fermés. Vérifie leur état avec `worktree_status.sh` et ferme-les avec `worktree_close.sh` depuis le principal. Un atelier ouvert bloque le lancement avant toute actualisation Git ou action Docker.

Le script effectue ensuite `git pull --ff-only`, attend la fin de `docker_stop_all.sh`, redémarre les services fondamentaux et le runtime principal, puis ouvre quatre onglets : Codex avec le journal central de master, un shell Docker, `merge_to_master.sh` pour l’audit d’intégration et une nouvelle session Codex dédiée au rapport des tâches. Ce dernier onglet exécute automatiquement `doc/build_rapport_taches.txt`, qui prévoit la création de `doc/rapport_taches.html` puis son ouverture dans Firefox. Le prompt doit être présent avant le lancement. Le rapport est ajouté en dernier, donc en sixième position si deux onglets étaient déjà ouverts. L’arrêt Docker concerne aussi les conteneurs du projet associés aux autres tâches et à la CI. `--console` ou `--server` remplace le shell du deuxième onglet. Voir la [fiche zz.sh](scripts.md#zzsh) pour les prérequis et le détail.

### Rapport opérationnel des tâches

Le prompt personnel `doc/build_rapport_taches.txt` est la source des consignes de génération. Il reste hors du dépôt tools, comme le rapport `doc/rapport_taches.html` et le journal `sessions_codex/master.jsont`. La session consulte Git en lecture seule, écrit le rapport local et actualise le journal. Dans Incwo, la seule écriture autorisée est la note interne hors Scrum décrite ci-dessous ; aucune annonce n’est publiée automatiquement.

Le rapport couvre toutes les tâches ouvertes assignées à Pascal. Il présente les catégories dans cet ordre : **Mon intervention**, **En attente de réponse**, **En attente**, puis la section finale **Tâches en cours mais hors scrum**, y compris dans la navigation et les effectifs introductifs. Chaque tâche figure une seule fois. Chaque fiche affiche les dates de création et de dernière intervention substantielle immédiatement sous son titre, au format `JJ/MM/AAAA`. Les rappels automatiques et simples changements de date ou de classement ne constituent pas des interventions ; une intervention absente est indiquée comme telle.

Dans chaque catégorie, les tâches suivent d’abord l’ordre visuel de la colonne **En cours** du Scrum « Tâches Pascal par priorité » (sélection `9238867`, Scrum `60276638`), relu via le MCP. Le rapprochement se fait par ID exact, sans transférer le rang d’une tâche d’agent ou de suivi à sa tâche parente. Les tâches ouvertes du périmètre dont l’absence est vérifiée figurent exclusivement dans la section finale hors Scrum, avec leur état fonctionnel rappelé sur la fiche. Une appartenance non vérifiée reste signalée dans la catégorie fonctionnelle, sans conclure à une absence. Le tri secondaire est décroissant sur la dernière intervention, ou la création sans intervention, puis sur la création en cas d’égalité. La priorité Scrum ne change pas l’état fonctionnel d’une tâche.

Pour chaque tâche sans rang dont l’absence a été vérifiée sur un Scrum accessible et exhaustif, lire toutes les notes, pagination comprise. Si aucune ne contient « cette tâche n'est pas dans mon scrum » ou « cette tache n'est pas dans mon scrum », ajouter une seule note interne avec le texte exact « cette tâche n'est pas dans mon scrum ». Ignorer les différences de casse, d’apostrophe et d’espacement pour détecter les doublons. Conserver le résultat, la date et l’ID de note dans l’instantané. Si la lecture est incomplète, ne rien publier et signaler la limite ; après un résultat de création ambigu, relire avant toute nouvelle tentative. Cette note automatique ne compte pas comme intervention substantielle et ne modifie pas le tri ni l’éligibilité aux annonces.

Le chapitre **Annonces à faire** précède **Les cinq prochaines actions proposées**. Il retient uniquement les tâches dont la dernière intervention substantielle date de **moins d’un mois calendaire**, ou dont le dernier commit métier pertinent date de **moins de sept jours**. Les seuils sont stricts et calculés à la date de la revue. Le commit récent constitue une exception permettant de retenir une tâche aux échanges anciens ou absents. Une date inconnue ne satisfait pas le critère ; la création ne remplace pas l’intervention pour ce filtre.

Ce chapitre distingue deux cas :

- **Production — « On peut fermer ? »** : le dernier commit métier contient `to_prod`, la cage est présente en production et l’annonce n’a pas été faite pour cette livraison. Le contrôle du code par `check_prod_commit` et celui de l’activation par `check_feature_flag` restent distincts. Si le code ou l’activation manque ou reste incertain, l’entrée indique « À vérifier avant annonce ».
- **Développement — « C’est sur dev »** : le dernier commit métier est intégré à `master`, sans annonce correspondant à cette version. L’intégration Git sert à repérer l’annonce attendue, sans prétendre prouver le déploiement sur le serveur de développement. Une annonce de production couvrant déjà cette version rend l’annonce dev inutile.

Les échanges sont lus en tenant compte des formulations équivalentes : une annonce ancienne ne couvre pas automatiquement un correctif ultérieur. Un historique incomplet est signalé comme non vérifié. Les entrées comportent le titre cliquable de la tâche, le commit et les preuves utiles, dans l’ordre de priorité Scrum puis des dates. Les pauses et abandons restent applicables ; aucune annonce n’est publiée automatiquement. Les résultats de production concernent le serveur répondant et les contextes de dossier/utilisateur contrôlés.

Les **cinq prochaines actions** sont choisies après construction des annonces, uniquement parmi les tâches actionnables restantes ayant un rang Scrum vérifié, selon cet ordre de priorité. Ne jamais compléter avec une tâche hors Scrum ou à appartenance non vérifiée. Toutes les tâches citées dans les annonces sont exclues par ID, y compris les cas à vérifier et même pour une autre action sur la même tâche. Le rapport peut proposer moins de cinq actions. Une tâche exclue des annonces par le filtre de récence reste dans sa section détaillée et redevient éligible aux actions seulement avec un rang Scrum vérifié et selon les autres critères.

La section **Depuis le précédent rapport** donne le titre et le lien Incwo de chaque tâche concernée, suivis du changement constaté. Le HTML conserve un instantané JSON des fiches, rangs Scrum, dates et preuves pour la comparaison suivante ; les contrôles de production doivent être renouvelés lors d’une nouvelle revue. Avant livraison, vérifier notamment le périmètre exhaustif, le tri, les liens, les dates sous les titres et l’absence d’ID commun aux annonces et aux cinq actions, l’exclusion des tâches sans rang des cinq actions et la présence unique des tâches hors Scrum dans la section finale.

## 2. Actualiser la base avant une nouvelle tâche

Dans le dépôt principal, sur la branche principale configurée et avec un état adapté :

```bash
pull.sh
```

Cette commande exécute `git pull --ff-only` dans le dossier courant. Elle ne rejoint pas automatiquement le dépôt principal. Un historique divergent provoque un refus.

Cette étape est utile avant de créer une branche : une création part de la branche principale locale sans fetch automatique. L’ouverture d’une tâche existante effectue normalement sa propre actualisation, sauf option ou choix contraire.

## 3. Rejoindre une tâche

La commande habituelle est :

```bash
worktree_reach.sh 1234_ma_tache
```

Son comportement dépend de l’état existant :

- Branche absente : créer la branche et le worktree depuis la base locale.
- Branche connue sans worktree : rattacher un nouveau worktree à cette branche.
- Worktree fermé : préparer et ouvrir son atelier, avec actualisation habituelle.
- Atelier déjà vivant : afficher son onglet d’agent, sans actualisation Git ni activation Docker.

Le terminal de lancement garde son dossier. La fenêtre de tâche utilise son worktree pour l’agent, les services, le Bash et les notes selon les rôles configurés.

Pour rouvrir un atelier existant fermé, sans --no-update et lorsque le choix de conservation du travail autorise la fusion, le script exécute un fetch de origin/master, avance le master local en avance rapide si nécessaire, puis fusionne la version récupérée dans la branche de tâche si elle ne la contient pas déjà. Il ne lance pas directement git pull. Un master local divergent provoque un arrêt.

Avec --no-update, cette ouverture évite le fetch, la mise à jour du master local et la fusion dans la tâche. Un atelier déjà vivant est seulement affiché, même sans cette option. Une branche nouvellement créée part de la base locale sans cette actualisation.

Si GitHub refuse l’authentification SSH pendant le fetch (Permission denied (publickey)), cette étape échoue avant la mise à jour du master local et la fusion dans la tâche. Pour rejoindre l’atelier sans attendre le rétablissement de l’accès, relancer avec --no-update ; les nouveautés distantes ne seront pas intégrées.

Pour ouvrir sans actualisation Git :

```bash
worktree_reach.sh 1234_ma_tache --no-update
```

Pour un ticket :

```bash
worktree_reach.sh t_2345_mon_ticket
```

Les commandes explicites restent disponibles : `worktree_new.sh` exige une nouvelle branche ; `worktree_open.sh` exige une branche déjà connue. Une préparation sans fenêtre ni Docker utilise `--prepare-only`.

Dans un nouvel atelier, Codex reçoit le chemin exact `doc/<tâche>_suivi.txt`, lit ce suivi puis attend les instructions de Pascal avant d’en exécuter les étapes. Une session existante est reprise normalement. Après activation réussie du serveur, l’URL localhost du point 5 du suivi reflète le port réellement attribué et le protocole HTTP/HTTPS ; elle est réajustée si l’adresse change lors d’une activation ultérieure. Avec `--prepare-only`, cette mise à jour attend le démarrage du serveur.

## 4. Traiter les modifications lors d’une ouverture

Si une tâche existante contient du travail local, l’ouverture avec actualisation propose un choix :

- Fusionner en conservant le travail : sauvegarde temporaire par stash, actualisation et fusion, puis réapplication avec l’index. Si la branche est déjà à jour après actualisation, le travail reste en place, sans stash ni fusion ; les modifications concurrentes du même chemin ne bloquent pas ce cas. Une fusion effective signale les collisions avec les autres worktrees par un avertissement, sans les bloquer ni modifier ces autres worktrees. Cela vaut aussi pour l’actualisation de la base locale et mergetool.
- Sauvegarder en patch puis nettoyer : créer une archive vérifiée dans WT_MAIN_ROOT/doc, nettoyer les fichiers sauvegardés et ouvrir sans fusion.
- Éliminer les changements : choix destructif explicite, puis ouverture sans fusion.
- Annuler : choix par défaut lorsque la réponse est vide.

`--no-update` évite cette actualisation et ce menu. Un conflit Git non résolu ou une opération Git en cours peut interrompre le traitement. Les collisions entre worktrees distincts sont informatives pour le choix 1 ; elles restent bloquantes pour les choix de sauvegarde/nettoyage ou d’élimination des modifications.

En cas de conflit de réapplication du stash, le script peut proposer `git mergetool` dans la tâche. Si la résolution n’est pas terminée, l’ouverture reste interrompue et le travail est conservé. Ne relance pas une restauration ou une fusion à l’aveugle ; examine l’état Git affiché.

## 5. Circuler entre les terminaux

Pour afficher une tâche déjà ouverte :

```bash
worktree_reach.sh 1234_ma_tache
```

Pour revenir à l’affichage du terminal principal :

```bash
worktree_reach.sh
```

`worktree_switch.sh` propose sans argument un menu des worktrees dont l’onglet Codex est ouvert et du terminal principal disponible. Choisis un numéro pour afficher son terminal ; Entrée, 0 ou q annule. Avec un nom de tâche, il affiche directement son terminal existant. Il ne crée pas de fenêtre. Si le terminal principal n’est pas retrouvé, ouvre un terminal dans le dépôt principal. Un atelier partiellement ouvert doit être fermé avant d’être rouvert.

## 6. Faire une modification rapide sans atelier complet

Depuis un terminal ayant chargé `worktree_shell.sh` :

```bash
change_branch.sh 1234_ma_tache
change_branch.sh                  # Revenir dans le dépôt principal sur sa branche de référence
```

Si la branche locale existe sans worktree, la commande bascule directement dessus dans le dépôt courant par un simple `git checkout`, sans pull ni fusion et sans créer de branche ou de worktree. Sans argument, elle revient au dépôt principal et rétablit sa branche de référence si nécessaire, par un simple `git checkout`.

Si la tâche possède déjà un worktree, la commande refuse les changements locaux et l’activité détectée dans la cible, vérifie le dépôt principal propre sur sa branche principale, y effectue le pull en avance rapide, fait `cd` vers le worktree puis y fusionne la base.

Elle ne démarre pas de serveur. Un refus avant déplacement conserve ton dossier ; un conflit de fusion te laisse dans la tâche pour le résoudre. Après succès, tu restes dans la tâche pour travailler. `cd -` revient au dossier précédent.

Le `cd` nécessite la fonction Bash ; l’exécutable seul demande de recharger le fichier de fonctions. Les contrôles sont ponctuels : ne démarre pas simultanément un autre travail dans ce même dossier.

## 7. Sauvegarder le travail

Depuis le Bash du worktree :

```bash
patch.sh make
```

Depuis le dépôt principal, pour une tâche existante :

```bash
patch.sh 1234_ma_tache
```

Les deux formes écrivent dans **WT_MAIN_ROOT/doc**. L’archive contient le commit de base, la branche, l’index, les modifications suivies, suppressions, binaires et fichiers nouveaux non ignorés. Les fichiers personnels ignorés ne sont pas inclus automatiquement.

La création vérifie les empreintes et refuse un changement intervenu pendant la lecture. Elle ne nettoie pas le worktree. Une sauvegarde ne remplace pas un commit.

Pour lister les sauvegardes, y compris quand la tâche n’a plus de worktree :

```bash
is_any_patch.sh 1234_ma_tache
is_any_patch.sh 1234_ma_tache done
```

Sans nom de tâche, `is_any_patch.sh` et `patch.sh list` utilisent la tâche courante. `is_any_patch.sh done` conserve la forme courte pour les sauvegardes déjà traitées.

## 8. Restaurer une sauvegarde

Depuis le worktree de la tâche :

```bash
patch.sh
```

La commande affiche les sauvegardes en attente et leur date, puis demande confirmation. Une archive `.wtpatch.zip` liée à une autre branche ou à un autre commit de base reste conservée mais n’est pas proposée à la restauration automatique, notamment après une fusion lors de l’ouverture d’un atelier. Une restauration explicite incompatible reste refusée. Sans terminal interactif, elle les affiche sans restauration. Si aucun patch n’est disponible, elle le signale.

Pour choisir explicitement une archive :

```bash
patch.sh restore doc/NOM_DU_PATCH.wtpatch.zip
```

Ce chemin est résolu vers WT_MAIN_ROOT/doc, même depuis un worktree. Une archive placée ailleurs est refusée. `--yes` supprime la confirmation uniquement pour une restauration explicitement désignée.

La restauration exige notamment la même branche et le même commit de base, une archive intacte et aucune modification locale incompatible. Les fichiers non suivis restants après un `git checkout .` sont acceptés seulement s’ils sont strictement identiques à la sauvegarde. Tout fichier différent ou étranger bloque la restauration. Ne supprime pas ces fichiers pour contourner un refus.

Après vérification du résultat, l’archive est renommée avec le suffixe `.done` dans le dossier central. Aucun merge automatique n’est effectué. Si le travail est déjà présent, il n’est pas réappliqué.

## 9. Vérifier le travail et les services

Pour produire le rapport de la tâche :

```bash
desc_git.sh 1234_ma_tache
```

Le rapport est écrit dans le dossier de documentation central. Une tâche nommée peut ne plus avoir de worktree : la commande lit sa branche locale, ou sa référence distante déjà connue si la branche locale est absente, sans créer de branche ni de worktree. Il commence par les fichiers modifiés, puis présente les commits dont le sujet commence par le nom exact de la tâche et leurs diffs. Les commits déjà intégrés dans la base restent inclus.

Pour vérifier les collisions locales et lancer les tests configurés :

```bash
worktree_check_conflicts.sh
worktree_test.sh file chemin/du_test --no-db-setup
```

Le scope et le chemin de test dépendent du runner configuré. `--db-setup` est un choix explicite de réinitialisation de la base de test. Les exécutions CI sont sérialisées par un verrou commun.

Si un service de la tâche s’est arrêté :

```bash
relance_serveur.sh
relance_console.sh
```

Ces commandes acceptent aussi un nom de tâche depuis le dépôt principal. `worktree_docker.sh status` affiche les états et `worktree_docker.sh logs` suit les journaux du service courant. Pour détacher une connexion Docker sans arrêter le service : Ctrl-P puis Ctrl-Q.

## 10. Préparer un commit puis pousser

Depuis le worktree de la tâche, prépare explicitement les fichiers utiles et relis l’index avec tes commandes Git habituelles. Exécute ensuite, quand tu souhaites réellement ces actions :

```bash
commit.sh 'fix: corriger le comportement'
push.sh
```

Le script de commit ajoute automatiquement le nom de tâche au message. Ne le fournis pas toi-même. Les fichiers personnels sont refusés ; certains messages spéciaux configurés peuvent préparer des fichiers précis avant le commit.

Le push vise la branche courante de tâche et une destination configurée. Il ne pousse pas directement la branche principale. Une destination de développement peut être indiquée si elle existe dans le profil :

```bash
push.sh development
```

`merge_to_master.sh` et `merge_to_dev.sh` sont des audits d’intégration. Ils ne réalisent aucune fusion ni aucun déploiement ; leur fetch par défaut actualise les références. `--no-fetch` utilise les références déjà disponibles.

## 11. Fermer puis reprendre

Commence par afficher les ateliers ouverts :

```bash
worktree_status.sh
```

Le statut indique les onglets actifs de chaque atelier et le total ouvert. Il ignore les anciennes traces de processus terminés et les terminaux manuels non gérés.

Enregistre les buffers de l’éditeur et termine les commandes que tu veux conserver. Depuis le worktree :

```bash
worktree_close.sh
```

Depuis le dépôt principal, pour fermer une seule tâche :

```bash
worktree_close.sh 1234_ma_tache
```

Pour fermer tous les ateliers ouverts, lance sans argument depuis le dépôt principal :

```bash
worktree_close.sh
```

Les ateliers sont fermés successivement avec les mêmes protections. Le premier refus interrompt la suite ; le message précise la tâche bloquante et le nombre de fermetures réussies. Les ateliers déjà fermés le restent. Enregistre les buffers avec `:wall` si nécessaire, traite le blocage signalé, puis relance depuis le principal pour fermer les ateliers restants. S’il n’y en a aucun, la commande le signale sans action. Le terminal et les services du dépôt principal sont conservés.

La fermeture vérifie les activités, peut proposer l’arrêt coopératif de commandes, arrête les services de la tâche et crée une sauvegarde vérifiée dans WT_MAIN_ROOT/doc s’il reste du travail. Un refus ou un blocage laisse la fermeture suspendue ; aucun arrêt forcé n’est effectué.

Le worktree, la branche, les documents et le journal central restent disponibles. Le dossier de documents du worktree est un lien symbolique vers le dossier central du dépôt principal : les notes et rapports écrits depuis un atelier y sont enregistrés directement. La fonction Bash revient au dépôt principal si le shell appelant est conservé ; un terminal supplémentaire reconnu comme appartenant à l’atelier peut être fermé.

Pour reprendre :

```bash
worktree_reach.sh 1234_ma_tache
```

## 12. Retirer un worktree terminé

Cette action est distincte de la fermeture. Depuis le dépôt principal et uniquement lorsque tu as décidé de retirer le dossier :

```bash
worktree_remove.sh 1234_ma_tache
```

Le script exige un worktree inactif, sans modifications Git ni fichiers non suivis, puis supprime son enregistrement Git et son dossier **sans sauvegarde**. Les fichiers ignorés locaux sont effacés avec le dossier ; seuls les fichiers committés sont conservés dans la branche. Les permissions peuvent nécessiter une opération privilégiée ciblée. La branche et le journal central sont conservés ; aucune branche distante n’est supprimée.

Le nom de tâche est obligatoire et la suppression est réelle, sans option d’aperçu. `worktree_close.sh` ferme seulement l’atelier et conserve son dossier ; `worktree_remove.sh` supprime un dossier ciblé ; `worktree_prune` supprime les dossiers éligibles selon leur ancienneté. Les patchs existants restent dans WT_MAIN_ROOT/doc.
