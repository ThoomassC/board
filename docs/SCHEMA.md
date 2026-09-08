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
      "meta": "Bash · EDI_backend · Opus 5",     ligne technique de la CARTE
      "attente": "attend une autorisation",      ce que la conversation attend
      "cout": "38,4 $",             "" tant que le coût est sous son seuil
      "cout_fort": true,            au-delà du second seuil -> ambre
      "agents": 3,                  sous-agents au travail, 0, ou null (inconnu)
      "say": "Je relance le pipeline EDI...",
      "ctx_pct": 41.0,
      "ctx_level": "ok",            ok|warn|crit  (seuils de config)
      "seen": false,
      "pane": 1                     index de pane, ou null
    }

### `meta` et `attente` sont deux champs parce qu'ils ont deux lecteurs

`meta` était `"attend une autorisation · Bash · EDI_backend"` : sur la carte, le
premier segment répétait la pastille posée juste en dessous et le dernier
répétait le nom de la colonne posé juste au-dessus. Une ligne entière pour ne
rien apprendre, pendant que le modèle et le coût — 38,44 $ sur une conversation,
mesuré le 02/09 — n'étaient nulle part alors qu'ils voyagent depuis toujours
dans la charge utile.

    meta      ligne technique de la CARTE. Le premier segment ne survit que
              lorsqu'il APPREND quelque chose : l'outil si l'on travaille, la
              raison si l'on a cassé. Le dépôt n'y figure que s'il diffère du
              nom de la colonne. Puis le modèle, abrégé (« Opus 5 »).
    attente   la phrase d'état, pour la FICHE : sa section « en ce moment » n'a
              aucune pastille sous les yeux quand aucun outil n'est en vol.

Une seule règle, côté serveur, deux consommateurs. Le board n'invente ni ne
recompose aucun de ces deux libellés — et surtout, il ne se sert pas de l'un
pour l'autre : la fiche a affiché « Opus 5 » sous « en ce moment » le temps
d'une itération, pour l'avoir fait.

### `cout` — un compteur permanent culpabilise au lieu d'informer

Le serveur envoie un TEXTE déjà formaté (virgule décimale) et un booléen, pas un
nombre : c'est lui qui décide si le chiffre mérite sa place, et le board n'a
aucun seuil en dur. Deux seuils, dans `thresholds` :

    cout_visible_usd   10    en dessous, `cout` vaut "" — rien ne s'affiche
    cout_fort_usd      25    au-delà, `cout_fort` -> l'ambre

L'ambre ne vole pas le canal STATUT : il vit sur un mot de 11 px dans la ligne
la plus discrète de la carte, jamais sur le bord gauche ni sur la pastille.

### `agents` — trois valeurs, et le null compte

    null   on ne sait pas : module absent, transcript introuvable ou illisible
    0      aucun agent au travail
    n > 0  n agents en vol

`null` et `0` ne s'affichent ni l'un ni l'autre : une puce permanente ne veut
plus rien dire, c'est le piège où `a_traiter` est déjà tombé. Le calcul lit le
transcript, donc il coûte — voir `serveur.agents_au_travail` : cache de 6 s ET
**budget de 25 ms par tour d'instantané**. Le TTL seul ne suffisait pas : à son
expiration toutes les conversations redevenaient périmées en même temps, et le
tour suivant les relisait toutes. Mesuré : 89 ms pour 4 conversations (9,7 Mo),
soit près d'une demi-seconde à vingt — un board qui hoquette une fois toutes les
six secondes. Avec le budget, le pire tour est retombé à 33 ms à vingt
conversations, et toutes sont revues en trois tours.

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

## `sessions_indisponibles` — l'instantané AVEUGLE

    "sessions_indisponibles": null,     on a regardé
    "sessions_indisponibles": "/…/state : Permission denied",   on n'a pas pu

Tout part de `Board.sessions()`, qui énumère `~/.claude/board/state`. Cette
énumération peut échouer — droits, montage tombé, dossier supprimé pendant la
lecture — et c'est **l'échec le plus probable de tout le serveur**.

Elle rendait alors `[]`. Une liste vide est indiscernable d'un poste au repos :
tout ce qui est bâti au-dessus concluait « aucune conversation ne travaille »,
`conversations_inconnues: false` et `conversations: 0` compris, et l'onglet
Chantier masquait la totalité des projets en l'affirmant. Un écran faux et sûr
de lui, pendant que trois conversations tournaient.

Un échec se propage donc comme **inconnu**, jamais comme **vide** :

    sessions()            lève `EtatsIllisibles` — la seule frontière qui SAIT
    sessions_connues()    -> None (elle attrapait déjà) -> `conversations: null`
    instantane()          rend un instantané AVEUGLE, et NE lève pas

`instantane()` ne peut pas laisser remonter : `_flux()` avale toute exception et
referme la connexion SSE, donc un board figé. L'instantané aveugle garde donc
ses colonnes — vides, mais présentes, parce qu'un board effacé ressemble à un
board au repos — publie le motif, et refuse trois choses :

    "total": null      on ne sait pas combien il y en a ; `0` serait le même
                       mensonge, remis un cran plus haut
    pas de notification `notifier.evaluate([])` verrait toutes les conversations
                       disparues d'un coup
    pas de peinture    `peindre([])` rendrait leur fond d'origine à des panes
                       bien vivants

`dernieres_sessions` reste à `None` : c'est ce `None` qui devient le
`conversations: null` de l'onglet Chantier. La panne est journalisée sur stderr
au plus une fois par minute (`serveur.plainte`) — la boucle SSE repasse ici
chaque seconde.

## `GET /api/decouverte?autorise=1` — les projets du poste, proposés

`candidats_projets` ci-dessus ne voit que ce que l'historique lui montre : un
projet non déclaré n'y apparaît que si une conversation y a déjà tourné.
`decouverte.scan(config, racine=None)` répond à l'autre moitié du besoin — les
dépôts git présents sur le disque, qu'une conversation les ait visités ou non.

    {
      "candidats": [
        {"name": "UI-COMMUNE",        nom deviné par `serveur._nom_devine`
         "root": "/Users/…/Documents/Projets_Perso/ui-commune",
         "root_court": "~/Documents/Projets_Perso/ui-commune",
         "depot": "ui-commune",       basename du dépôt, tel qu'il est sur le disque
         "dernier_commit_at": 1756900000,   mtime de `.git/HEAD`, ou null
         "collision": null}           nom déjà déclaré dans config.json, ou null
      ],
      "scannes": 412,                 dossiers VISITÉS, pas entrées lues
      "illisibles": 0,                sautés faute de droits — un ENTIER
      "duree_ms": 2900,
      "degrade": null                 null = relevé complet ; sinon, le motif
    }

Les six clés d'un candidat sont TOUJOURS là, les cinq du relevé aussi. `null` y
est une affirmation (« aucune collision », « date inconnue », « j'ai tout lu »),
pas une clé oubliée — même règle que `sessions_indisponibles`.

### Les trois choses qui ne se devinent pas

**`autorise=1` est obligatoire.** Sans lui, la route répond `403` et
**aucun dossier n'est lu** : le module n'est même pas appelé. Le balayage est la
seule lecture du serveur qui sorte des projets déclarés, l'utilisateur a demandé
à en garder la main, et cette main se vérifie côté serveur — une garde côté
client ne serait qu'une convention, qu'un `curl` ou un onglet resté ouvert sur
une ancienne version du JS contournerait.

**La découverte n'écrit RIEN.** Ni config.json, ni cache, ni fichier de
marquage, et elle ne mute pas la `config` qu'on lui passe. Elle PROPOSE ;
l'adoption est un second geste, humain, et `POST /api/projet` reste le seul
écrivain de `config.projects`.

**La liste est ordonnée par récence**, et cet ordre est un contrat :
`dernier_commit_at` décroissant, les dates inconnues en dernier, puis `name`
croissant à égalité. Ce qu'on a touché récemment se propose en premier — c'est
ce qui rend la liste utile plutôt qu'alphabétique.

### Ce qu'on ne propose pas, et pourquoi

    · ce qui est déjà couvert par une racine déclarée — la racine elle-même ET
      tout ce qui vit dessous, une racine pouvant valoir
      `~/Documents/Projets_Perso` en entier ;
    · le répertoire personnel lui-même — il porte souvent un `.git` de
      dotfiles, et « UTILISATEUR » n'est pas un projet ;
    · tout dossier caché, à quelque profondeur que ce soit, `node_modules`,
      `~/Library` et `~/.Trash` — sans ces exclusions le balayage remonte
      `~/.codex/.tmp/…`, `~/.islands-dark-temp` et
      `~/.local/share/ruby-advisory-db`, les trois faux positifs mesurés ;
    · ce qui vit DANS un dépôt trouvé — un dépôt arrête la descente, sinon
      `antomappat` et `antomappat/antomappat-front` seraient proposés tous les
      deux et l'utilisateur devrait deviner lequel adopter ;
    · un dossier dont `_nom_devine` ne tire aucun nom recevable — proposer un
      candidat que le formulaire d'adoption refusera est une impasse.

Les liens symboliques ne sont **jamais** suivis : un lien vers `~` ou vers un
parent ferait boucler la descente, et un lien vers un dossier déjà balayé le
proposerait deux fois sous deux chemins.

### Les bornes, et ce qu'elles obligent à dire

Deux bornes, `PROFONDEUR_MAX = 16` et `BUDGET_S = 10`. La profondeur protège
d'une arborescence pathologique, le budget d'un disque lent ou d'un montage
réseau — cas où aucune profondeur ne borne le temps.

**La profondeur n'est pas un réglage de vitesse**, c'est le budget qui tient le
temps. Mesures sur ce poste, `~` entier :

    profondeur   candidats   dossiers visités   durée    `degrade`
         4          14              298          15 ms   interrompu
         8          14            3 047         110 ms   interrompu
        16          14            9 457         275 ms   null
        40          14            9 457         228 ms   null

La liste est complète dès 4 niveaux et ne bouge plus ; mais le poste porte des
arbres hors dépôt qui descendent jusqu'à ~15 niveaux, si bien qu'une borne à 8
rendrait « balayage interrompu » **à chaque appel** sans jamais rien ajouter à la
liste. Un avertissement qu'on voit toujours n'est plus lu, et le jour où la
descente serait vraiment tronquée personne ne le remarquerait. D'où 16 : le
premier palier où ce poste se balaie en entier. `BUDGET_S = 10` vaut trente fois
la durée nominale et trois fois la pire mesure connue à cache froid (2,9 s) — il
n'arbitre pas le cas nominal, il fait finir le cas anormal.

Une borne atteinte se DIT dans `degrade` (« balayage interrompu … ») : rendre
une liste tronquée avec `degrade` à `null` la ferait passer pour exhaustive.

`illisibles` est un entier À CÔTÉ de `degrade` : le client ne doit jamais avoir
à parser une phrase pour obtenir un compte.

### Un seul balayage à la fois — refusé, pas mis en file

C'est l'opération la plus lente du serveur et elle tourne dans un thread de
requête. Un appel concurrent est refusé sur-le-champ :

    {"candidats": [], "scannes": 0, "illisibles": 0, "duree_ms": 0,
     "degrade": "balayage déjà en cours : réessayez dans quelques secondes"}

Le scénario n'est pas le polling — `autorise=1` implique un geste humain — mais
l'impatience : trois secondes sans retour visuel, et l'utilisateur reclique. Dix
parcours de `~` en parallèle s'écroulent ensemble, l'I/O disque ne se partageant
pas. Et à la différence de `chantier.scan`, la découverte ne COALESCE pas
(attendre le balayage en cours pour resservir son relevé) : le contrat de retour
est fermé, il n'a pas de champ pour dire l'âge d'un relevé, donc un relevé
resservi serait indiscernable d'un relevé frais.

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
                    "conversations": 2,     conversations du projet, ou null
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

Ce drapeau décrit **le relevé**, pas l'appel : voir le tableau des portées plus
bas, qui est ce qui empêche de le confondre avec `conversations`.

### `conversations` — combien de conversations vivantes par projet

Chaque groupe porte `conversations` : le nombre de conversations vivantes
rattachées à ce projet. Il existe pour l'onglet, qui replie les projets sans
conversation en cours ; il n'existe pas pour être joli.

Le champ s'appelait `convs`, et ce nom était un **piège de collision** : les
groupes de `/api/historique` portent eux aussi un `convs`, mais c'est une LISTE
de conversations. Même nom de collection (`groupes`), même nom de propriété,
deux types — et `0 || []` coerce en silence, donc la confusion ne produit aucune
erreur, juste un écran faux. Le nom complet lève l'ambiguïté ; il fait en outre
la paire avec `conversations_inconnues`, sur la même charge utile.

**`null` veut dire INCONNU, pas zéro.** `0` est une information sûre : le projet
n'a aucune conversation ouverte. Le client ne masque un projet que sur
`conversations === 0`, et **jamais** sur `null` : masquer sur l'inconnu ferait
disparaître de l'écran des projets où du travail tourne.

Cet invariant ne tient que parce qu'il est tenu **jusqu'en amont** :
`serveur.sessions()` LÈVE (`EtatsIllisibles`) quand le dossier d'états ne peut
pas être énuméré, au lieu de rendre une liste vide. Un `return []` à cet
endroit-là contournait tout ce qui est écrit ici — dossier illisible pendant que
trois conversations tournent, et la réponse était `conversations: 0` partout,
`conversations_inconnues: false`, écran vide et sûr de lui. Voir
« `sessions_indisponibles` » dans la section instantané.

#### `conversations` et `conversations_inconnues` n'ont pas la même portée

C'est **la** confusion à ne pas refaire : les deux champs ne répondent pas à la
même question, et ne coïncident que sur le chemin frais.

| champ | portée | question |
|---|---|---|
| `conversations_inconnues` | **le relevé** | les états d'arbres ont-ils été calculés sans instantané ? |
| `conversations` | **l'appel** | combien de conversations pour cet appelant-ci ? |

Conséquence concrète, sur un cache-hit avec `sessions=None` (la sonde de
pastille du board) : `conversations` vaut `null` — cet appelant n'a rien fourni
à compter — mais `conversations_inconnues` reste `false`, parce que le relevé
servi a moins de 30 s et a bien été calculé avec un instantané réel (le cache
refuse les relevés dégradés). Ses arbres `en_cours` sont justes ; les nier
afficherait « les arbres portant une conversation vivante ne sont pas
distingués » alors qu'ils l'étaient parfaitement.

**La règle de rattachement est celle de la RACINE DE PROJET, pas celle de
`conv`.** `conv` rattache une conversation à un arbre par égalité exacte des
chemins ; `conversations` compte par préfixe de `projects[].root` — la même
règle que `project` sur l'arbre et que le serveur partout ailleurs
(`chantier._projet_de`). Les deux ne coïncident pas, et c'est voulu : une
conversation ouverte dans `.../board/server` n'occupe aucun arbre, n'apparaît
dans le `conv` d'aucun d'eux, et compte pourtant dans le `conversations` du
projet. Sommer les `conv` des arbres donnerait un nombre plus petit, sans que
rien ne le signale.

**`conversations` n'entre JAMAIS dans le cache.** C'est une valeur de réponse,
pas une valeur mémorisée : le relevé mémorisé est partagé par tous les
appelants, et le comptage, lui, appartient à un seul. `scan()` le dérive au
retour, sur ses deux chemins — le froid comme celui du cache. Ce que le cache ne
contient pas ne peut pas être servi périmé, et `chantier.dernier()` (le bandeau,
une fois par seconde) n'a donc rien à en retirer.

Ces sept propriétés sont figées dans **`tests/test_chantier.py`**
(`python3 -m unittest discover -s tests`), y compris le cas du cache-hit sans
instantané, que l'affichage ne montre jamais.

#### `?force=1` — ce qu'il garantit, et son dédoublonnage

`force` court-circuite le TTL de 30 s. Il ne promet pas « refaire le travail »,
il promet **« ne pas te servir un relevé d'avant l'événement »** : le board ne
l'emploie que lorsque l'ENSEMBLE des conversations vivantes a changé, seule
chose qui puisse déplacer un projet d'un côté ou de l'autre du filtre.

Un balayage forcé coûte, mesuré sur ce poste (18 arbres), **149-164 ms de mur,
81 processus git, 700-816 ms de CPU**. Sans dédoublonnage, trois onglets ouverts
payaient ce prix trois fois pour le même événement : **349 ms, 243 processus,
2 699 ms de CPU**. Trois garde-fous, et il en fallait trois :

    signature             le relevé mémorisé n'est resservi à un `force` que
                          s'il a vu le MÊME lot de sessions — l'ensemble trié
                          des couples `(normpath(cwd), sid)`. C'est la condition
                          exacte : à signature égale, un rebalayage rendrait les
                          mêmes états d'arbres, les mêmes occupants, les mêmes
                          motifs de rétention.
    FENETRE_FORCE = 2 s   et il doit dater de moins de 2 s. Les états d'arbres ne
                          dépendent pas que des conversations — un `git status`,
                          une PR, un commit poussé les changent aussi ; sur la
                          seule signature, un lot de sessions stable dix minutes
                          rendrait tout `force` inopérant. 2 s couvre la seconde
                          pleine sur laquelle les onglets se répartissent (chacun
                          a son propre flux SSE, donc sa propre phase) plus la
                          durée d'un balayage.
    coalescence           un `force` qui arrive pendant qu'un balayage tourne
                          ATTEND son résultat au lieu d'en lancer un second. La
                          fenêtre seule ne couvre que les appels postérieurs à
                          la fin du premier balayage ; le cas mesuré est fait
                          d'appels qui se recouvrent.

**Ce que la signature retient, et ce qu'elle ignore.** `_balayer` ne fait qu'une
chose des sessions : il les indexe par `normpath(cwd)` et accroche à chaque arbre
celles dont le chemin coïncide. Le `cwd` décide donc de l'arbre occupé, le `sid`
distingue deux conversations dans le même arbre — un nombre qui se lit à l'écran
(« +1 », « 2 conversations y travaillent »). `state`, `title`, `glyphe`,
`ctx_pct` et `since` voyagent jusqu'au relevé mais ne déplacent aucun arbre, et
changent chaque seconde : les inclure ferait rebalayer sur un pourcentage de
contexte qui monte. C'est la même coupe que `majVeilleChantier` côté board pour
décider quand forcer, et les deux doivent rester d'accord.

**`sessions=null` sur un `force`** ne rebalaie pas. Un appelant qui ne sait pas
quelles conversations tournent ne peut pas rapprocher le relevé de son événement,
seulement l'en éloigner : le rebalayage produirait un relevé où AUCUN arbre n'est
occupé — moins vrai que celui du cache — et ne serait même pas mémorisé. On sert
le cache, avec `conversations: null` et `conversations_inconnues: false`, qui
disent exactement ce qui est su et par qui. Même doctrine que l'instantané
aveugle du serveur : une ignorance ne déclenche pas d'effet visible.

Vérifié : 3 appels forcés simultanés → **1 balayage, 161 ms**, et chacun repart
avec SON `conversations`. 8 `force` de même signature → **1 balayage, 81
processus git, 164 ms** — le prix d'un seul ; les mêmes 8 à signatures distinctes
→ **8 balayages, 648 processus, 1 213 ms**.

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

## Le bandeau d'attention — `attention` dans l'instantané

Trié par urgence, et c'est la SEULE zone qui l'est (cf. le refus N3 de
`docs/AMELIORATIONS.md`). **Deux genres d'entrées**, et `genre` dit lequel :

    {"genre":"conv", "sid":…, "title":"107 écarts caractérisés",
     "ident":"ProjetA", "repo":"ProjetA", "state":"review", "glyphe":"➜",
     "since":"44s", "project":"PROJET_A", "libelle":"À RELIRE"}

    {"genre":"item", "texte":"1 PR en conflit — 5 j", "detail":"#31004 (US 40003)",
     "glyphe":"⚠", "niveau":"bloque", "action":"résoudre le conflit",
     "onglet":"pr", "url":"https://…", "project":"PROJET_A", "poids":2, "n":1}

### Pourquoi deux genres — l'onglet Projets a été fusionné ici

Le bandeau était une file de CONVERSATIONS : une PR en conflit ou un arbre non
commité n'y entrait pas, donc n'attendait nulle part. L'onglet « Projets »
portait cet axe et il a disparu le 02/09 — mesuré, il affichait **une ligne pour
deux projets** (« 3 conversations à relire »), entièrement dérivée des
conversations, donc déjà dite deux fois ailleurs : les jetons du bandeau et la
pastille de chaque carte. Sa propre pastille comptait les mêmes trois
conversations que le bandeau.

`server/attente.py` (ex-`projets.py`) fournit désormais au bandeau **ce qui ne
vient pas des conversations, et rien d'autre** — les reprendre les compterait
deux fois.

**Seul le seau « à toi » monte.** « Qui tient la balle ? » avait trois réponses ;
`chez_les_autres` et `a_ranger` ne remontent pas — c'est ce qui empêche le
bandeau de se noyer, et ces deux lectures vivent toujours dans les onglets Pull
Requests et Chantier.

### Une seule échelle de priorité, dans `attente.POIDS_CONV` et `P_*`

`serveur.PRIORITE` n'existe plus : elle ne classait que les conversations et
n'avait aucun rang à donner à une PR en conflit. Les dix poids vivent dans
`attente.py`, sans ex aequo possible, ordonnés par **coût de l'inaction** :

    0 bloquée · 1 erreur · 2 PR en conflit · 3 PR prête · 4 non commité
    5 PR à corriger · 6 conversation muette · 7 mon vote · 8 à relire
    9 non poussé

Le tri se fait sur les deux sources RÉUNIES. Deux listes triées séparément puis
concaténées donneraient un ordre qui n'a de sens dans aucune des deux : une PR
en conflit doit passer devant une conversation muette, pas derrière toutes les
conversations.

### Les relevés sont LUS dans leur cache, jamais déclenchés

`attention` se reconstruit à chaque instantané, une fois par seconde. Le chemin
utilise donc `chantier.dernier()` et `pullrequests.dernier(cfg)`, deux lectures
strictement passives — `scan()` y est interdit : le premier lance un balayage
git de tous les arbres dès que son cache expire, le second peut réveiller un
thread `az`. Cache absent ou froid → aucun item, et le bandeau ne prétend rien
(`attente_degrade` porte le motif quand un relevé se sait incomplet).

Conséquence assumée : au démarrage du serveur, le bandeau ne montre que les
conversations jusqu'à ce qu'un affichage du board ait réchauffé le cache du
chantier. Mieux vaut ça qu'un balayage git sur la boucle SSE.

### `onglet` est une donnée, pas une déduction

Un jeton d'item mène quelque part : sa PR dans le navigateur (`url` gagne quand
elle existe), sinon l'onglet qui la détaille (`onglet` vaut `pr` ou `chantier`).
Le board ne lit pas le préfixe de `cle` pour le deviner.

**`title` et `repo` y sont, et ce n'est pas décoratif.** Le bandeau n'affichait
que `ident`, qui vaut le nom du dépôt dans le cas courant : mesuré le 02/09, les
deux conversations à relire donnaient deux jetons identiques — « ➜ ProjetA a
rendu la main » deux fois, pour deux travaux différents. La zone qui répond à la
douleur n°1 ne disait pas *laquelle*.

Le serveur ne tranche pas quel libellé gagne : il envoie les trois champs, et
**`nomLisible()` dans `board.js` applique la règle « le titre porte,
l'identifiant suit »** — la même fonction pour la carte et pour le bandeau. Cette
règle avait déjà divergé une fois, la carte passant au titre (D1) pendant que le
bandeau restait sur l'ancienne hiérarchie. Un troisième consommateur de cette
règle ne doit pas devenir une troisième version de cette règle.

Le libellé d'état RESTE dans le jeton, atténué. Le glyphe seul ne suffit pas :
une couleur de statut ne voyage jamais sans son glyphe **et** son libellé, et
« ✋ » nu demanderait au lecteur de connaître quatre glyphes par cœur.

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
