# Contrat de données — LIRE AVANT DE CODER

Tout module lit et écrit **uniquement** ce qui est décrit ici. Aucun module
n'invente de champ, ne renomme, ni ne devine un chemin.

Racine d'exécution : `~/.claude/board/`  (créée par install.sh, jamais versionnée)

    ~/.claude/board/
      config.json                 configuration locale (voir plus bas)
      account.json                limites 5h / 7j, global au compte
      layout.json                 ordre manuel des projets et des cartes
      seen.json                   marquages « vu » par (session_id, state)
      state/<sid>.event.json      écrit par les HOOKS
      state/<sid>.meas.json       écrit par la STATUSLINE
      notify.state.json           mémoire anti-spam du notifieur

## Pourquoi deux fichiers par session

Deux producteurs écrivent sur la même session : les hooks (événements) et la
statusline (mesures). Un seul fichier imposerait un read-modify-write et donc
des pertes. Chaque producteur possède SON fichier, en écriture atomique
(écrire `<f>.tmp` puis `os.replace`). Le serveur est le seul à fusionner.

## state/<sid>.event.json  — propriété des hooks

    {
      "session_id": "a5c7d326-...",     obligatoire
      "state": "working",               working|blocked|review|silent|error|dead
      "state_since": 1756123456,        epoch s, changé SEULEMENT si state change
      "cwd": "/home/.../PROJET_A_backend-feat-40003",
      "tool": "Bash",                   PreToolUse uniquement, sinon null
      "last_say": "Je relance le ...",   Stop uniquement (tronqué à 200 car.), sinon null
      "reason": "permission_prompt",    type de Notification, ou null
      "updated_at": 1756123456          epoch s, à chaque écriture
    }

`silent` n'est JAMAIS écrit par un hook : le serveur le dérive.

**Piège de `last_say`, vérifié en production.** `Stop` porte
`last_assistant_message` et écrit la phrase ; puis la Notification `idle_prompt`
arrive quelques centaines de millisecondes plus tard pour le MÊME événement
logique, sans ce champ. Une implémentation naïve remet alors `last_say` à `null`
et la ligne « ce que dit l'session » n'affiche jamais rien. La règle correcte :
`last_say` est CONSERVÉ tant que l'état n'est pas repassé à `working`. C'est
exactement sa durée de validité — la dernière phrase reste vraie jusqu'à ce que
l'session reprenne le travail.

## state/<sid>.meas.json  — propriété de la statusline

    {
      "session_id": "a5c7d326-...",
      "title": "ProjetA EDI arbitrage meeting",   = session_name, ou null
      "cwd": "/home/...",
      "project_dir": "/home/.../PROJET_A_backend",
      "branch": "feature/40003-sso-interne-keycloak",   <- worktree.branch SEUL,
                       jamais workspace.git_worktree (qui est un nom de dossier).
                       Le serveur re-resout de toute facon repo ET branche avec git :
                       workspace.project_dir est le dossier de LANCEMENT, pas le depot.
      "ctx_pct": 41.0,                  0-100, float
      "model": "Opus", "effort": "high",
      "permission_mode": "auto",
      "cost_usd": 0.42,
      "updated_at": 1756123456
    }

## account.json  — écrit par la statusline, le plus récent gagne

    {
      "five_hour":  {"used_pct": 62.0, "resets_at": 1756140300},
      "seven_day":  {"used_pct": 38.0, "resets_at": 1756400000},
      "updated_at": 1756123456
    }

`resets_at` = epoch s tel que fourni par Claude Code. Ne pas recalculer.

## config.json  — écrit par l'humain, lu par tous

    {
      "projects": [
        {"name":"PROJET_A",  "root":"~/repos/.../ProjetA",  "accent":"#4EC9A0"},
        {"name":"PROJET_B","root":"~/repos/.../ProjetB","accent":"#D8A657"}
      ],
      "fallback_project": "AUTRE",
      "thresholds": {
        "ctx_warn": 50, "ctx_crit": 70,
        "silent_after_s": 90, "aging_after_s": 300, "stale_after_s": 900
      },
      "notify": { "enabled": true, "quiet_hours": [22, 8] },
      "panes": { "PROJET_A_backend": 1 },
      "wt_window": "0",
      "port": 7777
    }

`projects[].root` : un `cwd` est rattaché au premier projet dont `root` est un
préfixe. Sinon → `fallback_project`. **L'ordre du tableau est l'ordre à l'écran**,
sauf réécriture par layout.json.

## L'objet SESSION — produit par le serveur, consommé par le board

C'est la première des trois structures que le board connaît — les deux autres
sont l'ARBRE (onglet Chantier) et les SOUS-AGENTS d'une conversation, décrits
plus bas. Le serveur la construit en fusionnant event + meas + config.

    {
      "sid": "a5c7d326-...",
      "project": "PROJET_A",          nom de projet résolu, ou fallback
      "accent": "#4EC9A0",
      "repo": "PROJET_A_backend", basename de project_dir
      "ident": "#40003",            n° d'US si trouvé, sinon nom de branche court
      "title": "ProjetA EDI arbitrage meeting",
      "state": "blocked",           working|blocked|review|silent|error
      "since_s": 252,               secondes dans l'état courant (calculé serveur)
      "aging": false, "stale": false,
      "meta": "attend une autorisation · Bash · EDI_backend",
      "say": "Je relance le pipeline EDI...",
      "ctx_pct": 41.0,
      "ctx_level": "ok",            ok|warn|crit  (seuils de config)
      "seen": false,
      "pane": 1                     index de pane, ou null
    }

### Extraction du n° d'US — règle STRICTE, ne pas relâcher

Un nombre nu ne suffit JAMAIS. Il doit être introduit par un préfixe qui, ici,
désigne vraiment une US. Sinon `fix/anomalies-run-9001` devient « US 9001 »,
alors que 9001 est un numéro de run TestRail — et un identifiant faux est pire
qu'un identifiant absent, puisqu'il est le premier mot de chaque notification.

    RE_US_BRANCHE = r"^(?:feature|feat|us|story)/(\d{3,6})(?:[-_/]|$)"
    RE_US_DOSSIER = r"(?:^|[-_])(?:feat|feature|us|story)[-_]?(\d{3,6})(?:[-_]|$)"
    (titre de PR uniquement, convention de commit du projet : r"^\w+\(#(\d{3,6})\)")

Ordre de résolution, premier trouvé gagne :
1. `RE_US_BRANCHE` sur la branche, puis `RE_US_DOSSIER` sur le basename du `cwd`
   → `"#40003"`
2. dernier segment de la branche, tronqué à 48 caractères — sauf si cette branche
   est muette : `main`, `master`, `develop`, `dev`, `HEAD`, `detached`, vide
3. le nom du repo, tronqué à 48 caractères

Les nombres suivants doivent être REFUSÉS, ils ont été vérifiés sur des données
réelles : `-fix-anomalies-run-9001` et `fix/anomalies-run-9001` (runs TestRail),
`preuves-40001`, `release-2024`, ainsi que les numéros de PR (19109, 31005) et de
cas de test (366591) rencontrés dans les titres.

**Toute implémentation de cette règle doit vivre au même endroit ou être testée
contre les autres.** Elle a divergé une fois entre `serveur.identifiant` et
`historique._ident`, et l'onglet Conversations affichait alors un identifiant différent de
l'onglet Historique pour la même conversation.

L'implémentation de référence est **`serveur.us_de(branche, cwd)`**, qui rend
`"#40003"` ou `""`. `serveur.identifiant` l'appelle ; l'onglet Chantier la reçoit
**injectée** (`chantier.scan(..., us_de=us_de)`) plutôt que d'en garder une copie —
le repli `chantier._us_defaut` n'existe que pour l'auto-vérification en ligne de
commande, où `serveur` n'est pas chargé. Un troisième consommateur de cette règle
ne doit pas devenir une troisième version de cette règle.

## Champs d'instantané ajoutés le 27/08 — le repli et ses candidats

    {
      "fallback": "AUTRE",          nom de la colonne de repli, pour que le board
                                    sache LAQUELLE peut porter une adoption
      "candidats": [
        {"name": "BOARD",             nom deviné, passe RE_NOM_PROJET
         "root": "/home/…/repos/perso/board",
         "root_court": "~/repos/perso/board",
         "convs": 1, "idents": ["board"]}
      ]
    }

« Le capteur détecte un nouveau projet et l'ajoute au board » n'était PAS vrai :
aucun module n'écrivait `config.projects` sauf `creer_projet`, et une conversation
dans un dossier non déclaré tombait dans la colonne de repli, en gris, pour
toujours. `candidats_projets(els, cfg)` comble ce trou.

### Trois refus, et ils importent plus que la détection

    · le répertoire personnel — 23 conversations de l'historique y vivent, et
      « UTILISATEUR » n'est pas un projet ;
    · tout dossier hors dépôt git — presque toujours un shell de passage ;
    · un dossier déjà sous une racine déclarée — il aurait dû matcher avant.

On propose le **dépôt principal** (`--git-common-dir`) et non le worktree : sinon
chaque worktree d'un dépôt non déclaré deviendrait un projet distinct.

Le serveur ne DÉCIDE rien. `config.json` est le fichier de l'humain : le board
propose, le formulaire s'ouvre pré-rempli, et seule la validation écrit. Cache de
20 s, invalidé par changement de l'ensemble des `cwd` non rattachés.

## L'objet ARBRE — produit par `chantier.py`, consommé par l'onglet Chantier

Deuxième structure que le board connaît, après l'SESSION. Servie par
`GET /api/chantier` (`?force=1` pour forcer le relevé), cache mémoire de 30 s.

Un arbre de travail = un dossier portant un `.git` sous une racine de
`config.projects` — le clone comme les worktrees liés. Mesuré sur ce poste :
**29 arbres pour 2 conversations vivantes**. L'onglet Conversations ne peut donc montrer que
2/29 du travail en cours ; cet onglet montre les 29.

    {
      "total": 29,
      "compteurs": {"en_cours":2,"en_revue":8,"non_commite":3,"socle":3,"reserve":13},
      "a_traiter": 2,              voir plus bas : SEULES les contradictions
      "ordre":    ["en_cours","en_revue","non_commite","socle","reserve"],
      "libelles": {"en_cours":"EN COURS", ...},   le board n'invente AUCUN libellé
      "glyphes":  {"en_cours":"●", ...},
      "conversations_inconnues": false,           voir plus bas
      "degrade": null,
      "age_s": 0, "now": 1787825169,
      "groupes": [ {"project":"PROJET_A", "accent":"#4EC9A0", "count":10,
                    "depots":[ {"repo":"PROJET_A_backend", "count":6,
                                "arbres":[ ... ]} ]} ]
    }

Deux niveaux de groupement, et le second n'est pas décoratif : 19 des 29 arbres
partagent le dépôt `ProjetB.Automatisation`. Le regroupement se fait sur le
`--git-common-dir` et non sur le nom de dossier, qui porte le suffixe de
worktree — sans quoi chaque arbre formerait son propre groupe.

    {
      "chemin": "/home/.../PROJET_A_backend-feat-40004",
      "arbre":  "feat-40004",       suffixe seul, ou "(dépôt)" si arbre == dépôt
      "depot":  "PROJET_A_backend",
      "project": "PROJET_A", "accent": "#4EC9A0",
      "branche": "feature/40004-rotation-planifiee-jetons-nexus",  ou null si détachée
      "us": "#40004",               `serveur.us_de`, injectée — jamais recopiée
      "etat": "en_revue",           en_cours|en_revue|non_commite|socle|reserve
      "glyphe": "➜", "libelle": "EN REVUE",
      "fichiers": 0,                modifiés ET non suivis, non commités
      "ahead": 0, "behind": 0,
      "commit_at": 1787..., "commit_j": 5,
      "mergee": false,              réserve UNIQUEMENT, sinon null
      "base": "origin/develop", "base_j": 0,
      "conv": [{"sid":…,"title":…,"state":…,"ctx_pct":…,"since":…}] ou null,
      "pr":   {"id":31003,"etat":"dort","url":…,"age_j":5,
               "commentaires":0,"non_resolus":0,"dst":…} ou null,
      "attend": {"id":31001,"etat":"a_corriger"} ou null,
      "alerte": "ne peut pas merger avant #31001 (À CORRIGER)",
      "alerte_niveau": "bloque"     agir|bloque|info, ou null
    }

### L'état est EXCLUSIF, le détail ne l'est pas

Un arbre porte **un seul** état, le premier qui s'applique dans l'ordre de
`ETATS`. C'est ce qui rend la liste lisible : aucune ligne ambiguë.

    en_cours     une conversation vivante a son `cwd` ici
    en_revue     une PR ouverte porte cette branche
    non_commite  arbre sale, ni PR ni conversation
    socle        main|master|develop|dev — CALCULÉ et envoyé, mais le board ne
                 liste PAS ces lignes : on n'y travaille pas, il n'y a rien à y
                 libérer. Le serveur les garde dans la charge utile (elles
                 servent à `principal` et aux compteurs) et l'onglet affiche leur
                 nombre à part, pour ne pas cacher en silence.
    reserve      branche de travail, arbre propre, rien qui la suive

L'ordre dit ce qui me revient : ce qui vit, puis ce qui est chez quelqu'un
d'autre, puis ce qui peut se perdre, puis le fond de tableau, puis le stock.
Conséquence voulue : un `develop` avec des fichiers modifiés est `non_commite`
et non `socle`.

Mais `conv` et `pr` sont republiés tels quels et **peuvent coexister** — vérifié
sur `fix/tests-run-9001-blocked`, qui porte à la fois une conversation et la PR
19825. L'affichage doit montrer les deux : l'état choisit un mot, il ne choisit
pas ce qu'on a le droit de savoir.

`reserve` n'est PAS un reproche. Sur ce poste les 13 arbres au repos sont du
stock voulu (un worktree par test à réparer) — d'où le mot « réserve » et non
« orphelin », et d'où le repli par défaut dans l'interface. Un mot accusateur
sur 13 lignes normales détruirait la crédibilité de l'écran.

### `a_traiter` ne compte QUE les contradictions

`a_traiter` = nombre d'arbres dont `alerte_niveau` vaut `agir` ou `bloque`.

    agir    une contradiction dans l'état : PR ouverte mais `ahead > 0` (le
            relecteur ne voit pas le dernier état), ou PR ouverte et arbre sale
    bloque  la ligne paraît actionnable et ne l'est pas : `dst` de cette PR est
            la branche source d'une autre PR ouverte
    info    du rangement possible (branche de réserve déjà dans la base).
            JAMAIS compté.

`non_commite` est délibérément **hors** du compte, bien qu'il porte l'ambre dans
l'onglet. C'est un état normal chez quelqu'un qui travaille en worktrees : le
compter allumerait la pastille en permanence, et une pastille permanente ne veut
plus rien dire. C'est exactement le piège où `a_traiter` de l'onglet PR est
tombé en laissant `dort` le gonfler à 7 sur 9.

### `conversations_inconnues` — pourquoi ce drapeau existe

`chantier.scan()` reçoit les sessions du **dernier instantané mémorisé**
(`Board.sessions_connues`, tolérance 15 s) et non un instantané frais : `instantane()`
notifie et repeint les panes du terminal, deux effets de bord qui n'ont rien à
faire sur le chemin d'une lecture.

Si ce lot est absent ou périmé, `sessions` vaut `None`, ce qui signifie **« on ne
sait pas »** et jamais « aucune ». Aucun arbre ne peut alors être marqué
`en_cours`, et le drapeau le dit à l'écran. Étiqueter en silence `reserve` un
arbre où une conversation travaille serait faux, pas dégradé.

### Puis-je retirer cet arbre de travail ? — `liberable`, `terminee`, `retenu`

    "liberables": 4,      arbres retirables sans rien perdre
    "terminees": 0,       …dont la branche est en plus déjà dans la base

    {
      "liberable": true,
      "terminee": false,
      "retenu": null,                       ou LE motif qui bloque, un seul
      "principal": false,                   clone principal du dépôt ?
      "depot_chemin": "/home/…/ProjetB.Automatisation"
    }

**LA NUANCE QUI GOUVERNE TOUT :** `git worktree remove` retire le RÉPERTOIRE de
travail, pas la branche. Celle-ci survit dans le dépôt avec tous ses commits. Le
risque de perte se limite donc à ce qui n'existe QUE dans ce répertoire — les
fichiers non commités et les commits non poussés. D'où deux niveaux, et non un.

    liberable   rien à perdre : arbre propre, rien à pousser, aucune conversation
                dedans, aucune PR ouverte (une PR ouverte veut dire qu'on aura
                encore besoin de l'arbre), et ce n'est pas le clone principal.
    terminee    en plus, la branche est déjà dans la base : elle peut partir avec
                l'arbre.

`retenu` nomme LE motif qui bloque, un seul, le plus grave d'abord — c'est la
réponse à « pourquoi pas celui-là ? ». Ordre : clone principal, conversation en
cours, fichiers non commités, commits à pousser, PR ouverte, branche socle.

La commande proposée à l'écran (jamais exécutée) se lance depuis le clone
principal et non depuis l'arbre retiré, sinon git refuse :

    git -C <depot_chemin> worktree remove <chemin>
    [&& git -C <depot_chemin> branch -d <branche>]     si terminee

`git branch -d` en minuscule refuse de lui-même une branche non fusionnée : le
second niveau porte donc son propre filet, indépendant de ce que ce module croit.

### `mergee` est une réponse à prendre avec son âge

Calculé par `merge-base --is-ancestor HEAD origin/develop`, donc **contre une
référence locale**. Sans `fetch` récent, elle est périmée. `base_j` porte son
âge et la phrase d'alerte le dit. Ce module ne fait **pas** de `fetch` pour
arranger ça : il ne touche à aucun dépôt.

### Ce que l'onglet ne fait pas

Aucune écriture git : pas de `fetch`, pas de `commit`, pas de `push`, pas de
`worktree remove` — la suppression d'un arbre est proposée sous forme de
commande à copier, jamais exécutée. Le seul geste qui agit est
`POST /api/ouvrir {cwd}`, qui lance une conversation NEUVE dans l'arbre (via
`wt.ouvrir`, sans `--resume`). Il exige que le dossier soit sous une racine de
`config.projects` : le chemin vient du navigateur, et la seule chose que cette
route doit pouvoir faire est ce que l'onglet montre déjà.


## Les SOUS-AGENTS d'une conversation — `conversation.py`, servis par `/api/conv`

Troisième structure connue du board, après l'SESSION et l'ARBRE. Elle répond à
« qui travaille pour cette conversation en ce moment », dans la fiche qui
s'ouvre au clic sur une carte.

    "agents": [
      {"type": "difai-core:difai-dev",   `subagent_type` de l'appel, ou null —
                                         jamais deviné : un appel peut l'omettre
       "desc": "A1 attentes et éléments périmés",
       "modele": "opus",
       "statut": "en_cours",             en_cours|perdu|echoue|fini
       "depuis_s": 252,                  SI en cours ou perdu, sinon null
       "duree_s": null}                  SI fini ou échoué, sinon null
    ],
    "agents_en_cours": 2,
    "agents_total": 5                    `agents` est tronqué à 12, ce compte non

### « Appel non apparié = en vol » NE VAUT PAS pour un agent

C'est la règle des OUTILS, et elle est juste pour eux. Un `Agent` part en
arrière-plan : son `tool_result` revient en une seconde et demie et n'est qu'un
accusé de lancement (« Async agent launched successfully », puis un `agentId`).
Tout appel d'agent est donc apparié aussitôt — la fiche a affiché « 0 en cours
sur N » pendant un mois, et un lot de huit agents au travail se présentait comme
huit agents finis.

La fin réelle arrive dans un message `user` portant un bloc
`<task-notification>` : `<task-id>`, `<status>` (`completed`|`failed`),
`<summary>`. **`task-id` est égal à l'`agentId` de l'accusé** — vérifié sur
trois transcripts et dix agents. C'est la seule clé d'appariement admise, pour
deux raisons mesurées :

  · `<tool-use-id>` CHANGE d'une notification à l'autre pour le même agent : un
    agent relancé par `SendMessage` renotifie en citant l'identifiant de ce
    `SendMessage`. Sur un transcript, 3 des 8 identifiants notifiés étaient
    introuvables parmi les lancements.
  · les commandes Bash en arrière-plan notifient dans le MÊME format, avec des
    `task-id` qui ne sont pas des agents. Sur un transcript, 17 notifications
    pour 1 seul agent. Apparier par `agentId` les écarte sans avoir à deviner à
    quoi ressemble un identifiant d'agent.

Un agent est donc **en cours tant qu'aucune notification n'est venue après son
dernier réveil** — son lancement, ou le dernier `SendMessage` qui lui était
adressé. C'est chronologique, pas booléen : un agent fini puis relancé
retravaille. Un agent sans accusé de lancement est synchrone, et là son
`tool_result` EST son résultat.

### `perdu` — un agent ne survit pas à sa conversation

« Lancé, jamais notifié » se lit « au travail » dans une conversation qui tourne
et « jamais revenu » dans une conversation éteinte. `conversation.detail` reçoit
donc `vivante` (True|False|**None**), et la route `/api/conv` la construit avec
DEUX preuves : `serveur.session_vivante` (le pid dans /proc) tranche seule quand
elle répond ; elle ne répond pas pour une conversation ancienne, dont le fichier
`.tty` a disparu, et l'absence de cette conversation de l'instantané dit alors
que le board ne la suit plus. Les deux preuves manquantes laissent `None`, et
**rien n'est conclu** : l'agent reste au travail plutôt que d'être déclaré perdu
sur une supposition. C'est la doctrine de `session_vivante`, tenue jusqu'au bout.

`vivante` entre dans la clé du cache de `detail`. Sans cela, une conversation
qui s'éteint sans que son transcript bouge aurait continué à montrer ses agents
au travail.

Ces sept situations sont figées dans `python3 server/conversation.py`, qui
fabrique son transcript au lieu d'en lire un vrai : la plupart des transcripts
n'ont aucun sous-agent, donc une auto-vérification qui en lit un au hasard peut
rester muette pendant des semaines — c'est exactement ce qui est arrivé.

## Vocabulaire des états — français à l'écran, anglais dans le code

    code       affiché        glyphe   pigment
    blocked    BLOQUÉ         ✋       #F5A524
    error      ERREUR         ✖       #FF5A52
    silent     SILENCE        ⋯       #7D8590
    review     À RELIRE       ➜       #5AC8FA
    working    AU TRAVAIL     ●       aucun (#333B47)

Priorité d'attention : blocked > error > silent > review > working.

## Pigments — les seuls autorisés

    --amber #F5A524   --red #FF5A52   --sky #5AC8FA   --green #35C46A   --grey #7D8590
    --chrome #0A0C11  --bg #10131A    --panel #171B23 --card #1D222B    --line #2A313B
    --t1 #E8EDF4      --t2 #A6B0BE    --t3 #6E7987

Règle des canaux, non négociable :
  couleur PROJET   -> filet 2px du panneau + titre de colonne. JAMAIS sur une carte.
  couleur STATUT   -> bord gauche 5px + glyphe + bandeau d'attention.
  couleur CONTEXTE -> UNIQUEMENT l'intérieur de la piste de jauge. Mat.

## Contraintes transverses

- Python 3, **bibliothèque standard uniquement**. Aucun pip, aucun npm.
- Aucune police ni ressource distante dans le board.
- Toute écriture de fichier est atomique (`.tmp` + `os.replace`).
- Tout module doit fonctionner si un fichier est absent ou corrompu : try/except,
  valeur par défaut, jamais de trace remontée à l'utilisateur.
- Les chemins avec `~` sont développés avec `os.path.expanduser`.
