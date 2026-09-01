# Audit critique — le board

Audit d'un outil qui marche. Tout ce qui suit est mesuré sur cette machine, le
26/08, à partir de `/api/instantane`, `/api/pr`, `/api/historique`, des
45 transcripts de `~/.claude/projects/`, de `~/.claude/board/` et des
`main()` d'auto-vérification des modules. Chaque affirmation chiffrée est
reproductible ; quand je n'ai pas pu vérifier, je le dis.

---

## Ce que disent les données réelles

Ces sept faits gouvernent la moitié des propositions. Ils sont contre-intuitifs
par rapport aux partis pris du produit.

| # | Fait mesuré | Conséquence |
|---|---|---|
| 1 | **0 des 45 conversations** n'a été lancée depuis un worktree. Les 45 `cwd` de session sont : `/home/<utilisateur>` (23), `.../ProjetB` (12), `.../ProjetA` (8), `.../ProjetA/PROJET_A_backend` (1), `.../ProjetA/PROJET_A_terraform` (1). `grep '"cwd":"[^"]*-\(feat\|fix\)-'` sur les 45 fichiers → **0 résultat**. | Le pari central du design — « le nom du worktree porte le n° d'US » — ne se déclenche jamais. |
| 2 | **39/45** conversations n'ont aucun n° d'US, **43/45** aucune branche, **43/45** aucun `ident`. | La colonne `ident` de l'historique est vide dans 96 % des cas. |
| 3 | Les 3 cartes vivantes affichent comme identifiant : `ProjetA`, `ProjetB.Automatisation-fix-failed-to-passed`, `<utilisateur>`. **Aucune ne porte de n° d'US.** | Le champ le plus gros de la carte (17 px, gras) ne discrimine rien. |
| 4 | `~/.claude/pane-state/` est **vide** (dernière écriture le 19/08). Donc `live_panes()` = ∅, donc `resolve_pane()` rend `None` pour **0 des 18 dépôts et worktrees** de la machine. | Le focus de pane — la réponse à la douleur n°1 — est mort à 100 %. |
| 5 | `notify.enabled` = **false** dans `~/.claude/board/config.json`. `notify.state.json` n'a jamais été créé. | Le notifieur, l'autre réponse à la douleur n°1, n'a jamais tourné. |
| 6 | Les 3 sessions vivantes ont `say: null` et `reason: "idle_prompt"`. | Le champ « ce que dit la session » est vide en permanence — et c'est un bug, pas un choix (cf. F1). |
| 7 | `AUTRE` est le **plus gros groupe de l'historique** : 23/45 conversations (51 %), toutes avec `cwd = /home/<utilisateur>` et `repo = "<utilisateur>"`. | Le seau de repli contient plus de conversations que les deux projets clients réunis. |

Autres mesures utilisées plus bas : 8 PR ouvertes dont 7 de Thomas ;
`a_traiter` = 2 ; 41 Mo de transcripts en 6,9 jours (≈ 6,4 conversations/jour) ;
scan de l'historique 143 ms à froid, 0,2 ms à chaud ; coût cumulé des 3
conversations ouvertes **117,62 $**.

---

## 1. Design / lisibilité

### D1 — Inverser la hiérarchie de la carte : le titre d'abord, l'identifiant ensuite

**Le problème.** Thomas regarde une colonne et lit d'abord, en 17 px gras,
`ProjetA`. Puis, en dessous, en 13,5 px gris `--t2`, `ProjetA application generic
design`. Le premier ne lui apprend rien — il sait qu'il regarde la colonne
PROJET_A, c'est écrit en en-tête. Le second est la seule chose qui répond à
« laquelle est-ce ? ». La typographie dit exactement le contraire de
l'information. Ce n'est pas un défaut d'exécution : c'est le pari n°1 du design
(`ident` = n° d'US) qui ne s'est jamais réalisé (faits 1, 2, 3).

**Ce que ça change.** `title` passe en 17 px `--t1`, `ident` descend en petit
tag à côté du chrono. Sur les trois cartes actuelles on lit
« Création de jeux de données », « ProjetA application generic design »,
« Outil de gestion conversations multi-projets » au lieu de
« ProjetB.Automatisation-fix-failed-to-passed », « ProjetA », « <utilisateur> ».

**Coût.** 1 h. Une dizaine de lignes de `board.css` et deux dans `majCarte()` —
l'échelle `.long/.tres-long/.enorme` se transpose telle quelle.

**Risque, honnêtement.** Le titre est généré par Claude : il peut être mauvais,
et il est `(sans titre)` pour 2/45 conversations. Il n'est pas non plus ce que
Thomas tape dans ses commits. D'où : on rétrograde `ident`, on ne le supprime
pas. Et si un jour il lance ses conversations depuis les worktrees, `ident`
redevient bon — il faudra alors le repromouvoir, ce qui est une bascule d'une
ligne de CSS, pas une refonte.

### D2 — Ne jamais cacher le titre : `mode-XS` sacrifie la mauvaise chose

**Le problème.** `mode` est choisi sur le **nombre** de conversations (>16 →
`XS`), pas sur la place disponible. En `XS`, `board.css` cache `.titre`, `.meta`
et `.dit`. À 20 conversations sur 2 projets, il reste sur chaque carte : glyphe,
`ident`, chrono, jauge. Compte tenu du fait 3, cela donne **dix cartes qui
affichent toutes `ProjetA`**. Le jour où le board est le plus utile est le jour où
il devient illisible. Et il reste de la place : 10 cartes XS ≈ 48 px = ~500 px
dans une colonne qui en offre ~920 sur un écran 1080.

**Ce que ça change.** Ordre de sacrifice inversé : partent d'abord `.ctx-n`, puis
`.dit`, puis `.meta`. `.titre` ne part jamais. Mieux : la densité se décide sur la
hauteur réelle de `.cartes` (`@container` — la feuille le fait déjà pour la
largeur des colonnes) plutôt que sur un compte global.

**Coût.** 1 h pour réordonner les sacrifices. Une demi-journée pour passer la
densité en requête de conteneur.

**Risque.** Aucun réel. Le seul argument pour cacher le titre serait le manque de
place verticale, et la mesure dit qu'il n'y en a pas. Ce palier a été calibré sur
un cas qui ne se produit pas.

### D3 — `repo` est faux sur les 3 cartes vivantes

**Le problème.** `serveur.py` calcule `repo` depuis `project_dir` (l'espace de
travail que Claude Code déclare), avec repli sur `cwd`. Résultat aujourd'hui :

- session dans `~/repos/perso/board` → `repo = "<utilisateur>"` ;
- session dans `~/repos/clients/ProjetB/ProjetB.Automatisation-fix-failed-to-passed`
  → `repo = "ProjetB"`, alors que le dépôt est `ProjetB.Automatisation` et le
  worktree `-fix-failed-to-passed`.

La ligne de méta affiche donc « main rendue · ProjetB » pour une session qui
travaille dans un worktree de `ProjetB.Automatisation`. Le board présente un
répertoire personnel comme un dépôt, et efface le seul élément qui distingue les
7 worktrees frères de `ProjetB.Automatisation`.

**Ce que ça change.** `repo` dérivé de `basename(cwd)` (en remontant au premier
parent qui porte un `.git`), et le suffixe de worktree conservé et affiché. La
ligne devient « main rendue · ProjetB.Automatisation ▸ fix-failed-to-passed ».

**Coût.** 1 h.

**Risque.** Une session lancée dans un sous-dossier nommerait le sous-dossier —
d'où la remontée jusqu'au `.git`, que `pullrequests.py` sait déjà faire
(`_est_depot`, qui gère le cas du `.git` fichier d'un worktree lié).

### D4 — Le voyant de fraîcheur ne surveille que la moitié qui compte le moins

**Le problème.** `#flux` mesure l'âge du dernier message SSE, c'est-à-dire le
lien **navigateur ↔ serveur**. Le lien **capteurs ↔ serveur** n'est pas affiché,
alors que le serveur le calcule et l'envoie : `capteurs_age_s` est dans chaque
instantané et **`board.js` ne le lit jamais** (0 occurrence). Si les hooks
cassent — c'est le risque documenté par `sensors/README.md`, qui épingle le
bundle 2.1.245 — le board garde ses cartes, garde son point vert et n'a rien à
dire. C'est exactement ce que le commentaire d'en-tête de `board.js` interdit :
« un board grisé est honnête, un board figé ne l'est pas ». Aujourd'hui
`capteurs_age_s` vaut 1 s parce qu'**une** session travaille, alors que les deux
autres n'ont pas été mesurées depuis 16 h — et le `min()` du serveur masque
précisément ça.

**Ce que ça change.** Le point de flux devient double, ou porte le pire des deux
âges. Et sur chaque carte, quand son propre `age_s` dépasse largement sa durée
d'état, le chrono le dit au lieu de faire semblant.

**Coût.** 2 h.

**Risque.** Deux voyants, c'est une chose de plus à lire sur un écran dont tout
le mérite est de se lire d'un coup d'œil. Atténuation : le second n'apparaît que
quand il est anormal.

### D5 — Le bandeau d'attention plafonne à 3, et le survol le vide

Deux défauts sur la zone qui répond à la douleur n°1.

**Le problème.** (a) Le bandeau est plafonné à 3 entrées, le reste devient
« (+5 autres) » — un texte non cliquable qui ne dit ni quoi ni où. À 2–4
conversations c'est du confort ; à 20, c'est une censure sur exactement ce qu'on
est venu voir. (b) `mouseenter` + 600 ms marque la carte « vue », ce qui la
**retire du bandeau**. Traverser une colonne dense à la souris pour atteindre une
carte grille les voisines au passage et vide silencieusement la file d'attente.

**Ce que ça change.** Le bandeau affiche autant d'entrées que la largeur en
laisse tenir, et « +N » devient un bouton qui déroule le reste. Le marquage par
survol reste pour `review` et `silent`, et disparaît pour `blocked` et `error` :
une demande d'autorisation n'est pas traitée parce qu'on a passé la souris
dessus.

**Coût.** 2 h.

**Risque.** La règle du survol est ce qui empêche le bandeau de crier au loup en
permanence — la retirer partout ferait revenir le bruit. D'où la distinction par
état, qui est la vraie ligne : `blocked`/`error` demandent un acte,
`review`/`silent` demandent un regard.

### D6 — Arrêter de promettre le focus de pane

**Le problème.** « clic sur une carte = focus du pane » est écrit dans la
légende, dans le bandeau d'attention et dans l'attribut `title` de chaque carte.
Or `resolve_pane` rend `None` pour les 18 dépôts et worktrees de la machine
(fait 4). `board.js` fait `if (e.pane != null)` — donc n'appelle même pas
`/api/focus`. Et quand l'appel a lieu, le serveur renvoie
`{ok:false, raison:"..."}` que `poste()` jette (`.catch(() => {})`, réponse
jamais lue). Les deux couches se taisent, et l'écran affirme le contraire.

**Ce que ça change.** Quand `pane == null` : la carte ne prétend plus être
cliquable pour ça, la légende le dit, et le clic met le `cwd` complet dans le
presse-papier. Thomas colle `cd <cwd>` dans le pane de son choix : ce n'est pas
le focus, mais c'est vrai et ça sert.

**Coût.** 1 h.

**Risque.** Aucun. C'est le seul item de la liste qui ne fait que rendre le board
honnête sur son propre état — ce que sa doctrine réclame déjà.

### Le reste, franchement mineur

- **Le thème clair est un gadget.** Une machine, un Windows Terminal, un thème
  sombre. Il coûte une seconde palette à maintenir en cohérence et a déjà exigé
  un correctif (`--acc-txt-mix: 62%`) parce que les accents projet sont pensés
  pour le sombre. Il est écrit, il marche, on le garde — mais aucune heure de
  plus n'y va, et toute nouvelle couleur se valide dans les deux ou nulle part.
- **`--sky` est surchargé.** Il est à la fois le pigment de `review`, l'anneau de
  `focus-visible`, le liseré de cible de glisser-déposer, le bouton `reprendre`
  et l'accent du dialogue d'aide. Sur un board où 2 cartes sur 3 sont en
  `review`, la couleur de statut est aussi la couleur du chrome. Le budget de
  quatre pigments est respecté à la lettre, pas dans l'esprit. Coût du
  redressement : 2 h, valeur : faible. À faire seulement si un jour une cible de
  drop devient confondable avec un statut.
- **56 lignes de CSS mort.** `board.css` style un `dialog#aide` et un `#btn-aide`
  que `board.html` ne contient pas. Soit on livre le dialogue d'aide — il est
  entièrement dessiné, et le board a un vocabulaire à expliquer — soit on
  supprime le CSS. 1 h dans les deux cas ; ne pas laisser en l'état.

---

## 2. Fonctionnalités

### F1 — Réparer `last_say` : le champ le plus utile est détruit à chaque tour

**Le problème.** Les 3 sessions vivantes ont `say: null` et
`reason: "idle_prompt"`. Ce n'est pas un hasard, c'est mécanique. En fin de tour,
deux hooks écrivent coup sur coup :

1. `Stop` → `board-event.py review`, qui lit `last_assistant_message` et écrit
   `last_say: "Je relance le ..."` ;
2. `Notification` de type `idle_prompt` → `board-event.py --from-notification`,
   qui rend aussi `review`. Le payload d'une Notification ne contient pas
   `last_assistant_message`, donc `last_say = None`, et le fichier est réécrit.

`state_since` survit parce que le capteur relit l'ancien fichier avant d'écrire
(l'invariant documenté). `last_say`, `tool` et `reason` non : ils sont écrasés.
Résultat : la phrase qui répond le mieux à « qu'est-ce que fait cette
conversation ? » est effacée quelques centaines de millisecondes après avoir été
écrite, à tous les tours, depuis toujours. La ligne `.dit` du board est du CSS
qui n'a jamais rien affiché.

**Ce que ça change.** La carte gagne une phrase réelle sous son titre. Combiné à
D1, la carte répond enfin à la douleur n°2 sans qu'on ait à aller voir le pane.

**Coût.** 1 h — trois lignes dans `board-event.py`, dans le bloc qui relit déjà
l'ancien fichier pour `state_since`.

**Risque.** Montrer une phrase périmée pour un tour qui a avancé. Bornage
naturel : on conserve `last_say` **seulement** si `state` ne change pas, et on
l'efface à tout vrai changement d'état — exactement la condition que le code
calcule déjà. Même traitement à décider explicitement pour `tool` : le garder au
travers d'un `PreToolUse` a du sens, le garder au travers d'un `Stop` non.

### F2 — Reprendre une conversation depuis l'Historique

**Le problème.** 45 conversations listées, aucune action dessus — sauf le `↩`
qui ne s'affiche que pour les archivées, et il y en a **0**. L'onglet est une
archive en lecture seule alors que chaque ligne porte déjà `sid` et `cwd`, soit
exactement les deux arguments de `cd <cwd> && claude --resume <sid>`.

**Ce que ça change.** Deux options, à ne pas confondre :
(a) un bouton « copier la commande » qui met `cd <cwd> && claude --resume <sid>`
dans le presse-papier — Thomas colle dans le pane qu'il veut ;
(b) lancer réellement, ce qui suppose ouvrir un pane
(`wt.exe -w <window> split-pane`) avec la même interop Windows que `focus.sh`.

**Coût.** 1 h pour (a). Une demi-journée pour (b), plus le débogage d'interop.

**Risque.** (a) : aucun. (b) : c'est le bouton le plus dangereux du board — une
page web qui ouvre des terminaux — et un `--resume` dans le mauvais `cwd`
reprend une conversation avec le mauvais contexte. Faire (a), et n'aller vers
(b) que si (a) devient agaçant à l'usage. Ce sera probablement non.

### F3 — Relier une PR à une carte : par le dépôt, pas par l'US

**Le problème.** L'onglet PR et l'onglet Board ne se parlent pas. Thomas voit
8 PR d'un côté et 3 conversations de l'autre, et fait la jointure de tête.

**Le contre-argument que les données imposent.** Une jointure par n° d'US ne
donnerait **rien**. Les 8 PR portent les US {40001, 40002, 40003, 40004, 40005,
40006} ; les 3 cartes portent {`ProjetA`, `ProjetB.Automatisation-fix-failed-to-passed`,
`<utilisateur>`}. Intersection : **vide**, et elle restera vide tant que les
conversations partiront de la racine projet (fait 1). En revanche, la jointure
par **dépôt** marche déjà : le `basename(cwd)` de la session ProjetB est
`ProjetB.Automatisation-fix-failed-to-passed`, et 2 PR ouvertes sont sur le
dépôt `ProjetB.Automatisation` — un préfixe de worktree, exactement la règle que
`panes.py` implémente déjà (`_match_key`).

**Ce que ça change.** Une pastille discrète « 2 PR » sur la carte, qui bascule
vers l'onglet PR filtré sur ce dépôt. Quand un n° d'US est présent des deux
côtés, la pastille se resserre sur la PR exacte.

**Coût.** Une demi-journée (le rapprochement + la pastille + le filtre d'onglet).

**Risque, et il est réel.** `ProjetB.Automatisation` a 7 worktrees : les 7
cartes correspondantes afficheraient les 2 mêmes PR. La pastille dit donc « ce
dépôt a des PR ouvertes », pas « ta PR ». C'est une information plus faible que
promis. **Verdict :** à faire après D1/D3, et à n'exploiter à fond que si Thomas
change d'habitude de lancement. À défaut, la version dépôt vaut quand même mieux
que rien, à condition que l'intitulé ne mente pas.

### F4 — Ce que « à traiter » ne compte pas : les PR de Thomas qui dorment

**Le problème.** `a_traiter` = 2 sur 8. Les 6 autres sont `en_attente`, et ce
sont **6 PR de Thomas dont aucun relecteur n'a voté** :

| PR | US | âge | relecteurs sans avis |
|---|---|---|---|
| 31003 | 40004 | 4 j | 1 |
| 31004 | 40003 | 1 j | 1 |
| 31006 | 40005 | 1 j | 1 |
| 31007 | 40006 | 1 j | 1 |
| 31008 | — | 0 j | 2 |
| 31009 | — | 0 j | 2 |

Pour un lead en ESN, « ma PR, un relecteur, aucun vote, 4 jours » est la ligne la
plus actionnable de l'écran : elle veut dire « va relancer quelqu'un ». L'état
`en_attente` la range à la priorité 4, en bas de liste, sans compteur.

Deuxième angle mort, dans les mêmes données : **la PR 31003 cible
`feature/40002-auth-jwt-nexus`**, qui est la branche source de la PR 31001 —
laquelle est `a_corriger` (1 rejet, 12 commentaires). 31003 ne peut pas être
mergée avant 31001, et rien ne le dit. C'est une chaîne de PR, et le board la
présente comme deux lignes indépendantes.

**Ce que ça change.** (a) Un état `dort` : la mienne, aucun vote, âge ≥ 2 j —
compté à part. (b) Détection de chaîne : `dst` d'une PR = `src` d'une autre PR
ouverte → la dépendance est affichée, et la PR aval n'est plus présentée comme
actionnable.

**Coût.** 2 h chacune. Ce sont deux fonctions pures sur des données déjà
récupérées : zéro appel `az` de plus.

**Risque.** Gonfler `a_traiter` le dilue jusqu'à le rendre inutile. D'où deux
compteurs distincts — « 2 sur toi · 6 sur les autres » — plutôt qu'un seul
nombre qui grossit.

### F5 — Détecter deux conversations sur le même worktree

**Le problème.** Non observable aujourd'hui (3 sessions, 3 `cwd` distincts), mais
la machine porte 5 worktrees `PROJET_A_backend-feat-*` et 7
`ProjetB.Automatisation-*`, pour 2 à 8 conversations simultanées. Deux
conversations qui écrivent dans le même arbre de travail, c'est la seule façon
silencieuse de perdre du travail sur ce poste : aucune des deux ne le sait, et le
board les affiche comme deux cartes normales.

**Ce que ça change.** `sessions()` groupe par `cwd` et marque les doublons. Une
mention « 2 ici » sur les deux cartes concernées ; et si les deux sont `blocked`
en même temps, le bandeau d'attention le dit.

**Coût.** 1 h. Le serveur a déjà tous les `cwd`.

**Risque.** Le cas est parfois légitime : une conversation qui lit, une qui écrit.
Donc on signale, on n'empêche pas — et surtout on ne colore pas : ce serait un
cinquième pigment pour un cas rare.

### F6 — Le temps et le coût par projet — avec l'avertissement qui va avec

**Le problème.** `cost_usd` est collecté par le capteur, transporté dans chaque
instantané, et **jamais lu par `board.js`**. Il vaut en ce moment 12,66 $ +
32,70 $ + 72,26 $ = **117,62 $** pour trois conversations ouvertes. `first_at` et
`last_at` sont dans l'historique et ne sont pas lus non plus : leurs sommes
donnent PROJET_A 69,3 h, PROJET_B 64,6 h, AUTRE 31,4 h sur 6,9 jours. Pour
quelqu'un qui facture deux clients, c'est le seul chiffre que ce board est le
seul à pouvoir produire, et il est déjà dans la charge utile.

**Ce que ça change.** Un total par en-tête de colonne, et une ligne
hebdomadaire par projet dans l'onglet Historique.

**Coût.** 2 h.

**Risque, et c'est le point le plus important de cette proposition.**
`last_at - first_at` est une **amplitude**, pas du temps passé : une conversation
ouverte à 9 h et relancée à 18 h compte 9 h. Les 69 h de PROJET_A sur 7 jours en
sont la preuve — c'est physiquement impossible en temps travaillé sur un seul
projet. Ce nombre finira collé dans un CRA. Il doit donc être libellé
« amplitude » et non « temps », ou recalculé en sommant les intervalles entre
messages consécutifs avec un plafond de trou (5 min, par exemple). Livrer le
chiffre brut sous le mot « temps » serait la seule proposition de ce document
capable de causer un vrai dégât.

### F7 — Rendre le toast cliquable

**Le problème.** `board-toast.ps1` n'émet qu'une action :
`<action content="OK" arguments="dismiss" activationType="system"/>`. Pas de
`launch`, pas d'activation par protocole. Le toast dit donc à Thomas que
`#40003` attend son OK, et le laisse chercher le pane lui-même — c'est-à-dire la
moitié de la douleur n°1.

**Ce que ça change.** Un `launch="http://localhost:7777/"` avec
`activationType="protocol"` sur la racine du toast : cliquer ouvre le board.

**Coût.** 1 h.

**Risque.** Un clic sur un toast vole le focus vers un navigateur, ce qui n'est
pas ce qu'on veut au milieu d'une frappe. Et ça mène au board, pas au pane —
donc ça ne clôt la boucle que si R4 aboutit. À faire quand même : arriver sur le
board est strictement mieux qu'arriver nulle part.

### Gadget assumé

- **Navigation clavier et filtre `/` sur le board.** Les cartes sont focusables
  et répondent à Entrée, mais il n'existe aucun moyen d'atteindre « la prochaine
  chose qui m'attend » sans la souris. À 3–8 conversations, la souris gagne. À
  20, peut-être pas — mais D5 (bandeau déplafonné) achète l'essentiel du
  bénéfice pour moins cher. Gadget jusqu'au jour des 20 conversations.
- **Agir depuis le board** (approuver une PR, répondre à une permission) :
  voir la section « ce qu'il ne faut pas faire ». Ce n'est pas un gadget, c'est
  une erreur.

---

## 3. Robustesse et exploitation

### R1 — La mort sans `SessionEnd` est le cas normal, pas le cas limite

**Le problème.** `SessionEnd` est le seul producteur de l'état `dead`, et le seul
hook synchrone. Il ne s'exécute pas quand le pane est tué, quand Windows Terminal
se ferme, quand WSL s'arrête, ni quand le processus est tué par l'OOM killer —
c'est-à-dire dans la majorité des fins de session réelles d'un poste de dev.

Le symptôme est à l'écran en ce moment : `state/` contient 3 sessions, dont
`ad5d1fcd` avec un `updated_at` vieux de 16 h, affichée « À RELIRE · 16h38 ·
oubliée ? ». Il se trouve qu'il y a bien 3 processus `claude` vivants — mais le
board **ne peut pas le savoir**, et il donnerait exactement le même hachurage
« oubliée ? » à un fantôme. Sa réponse à « cette carte est-elle réelle ? » est
une supposition.

**Ce que ça change.** Une preuve de vie au lieu d'une supposition. Trois pistes,
par ordre de certitude :
1. Le capteur écrit un PID dans `event.json`, le serveur vérifie `/proc/<pid>`.
2. Le répertoire de travail temporaire de la session contient le `sid`
   (`/tmp/claude-1000/<slug>/<sid>/`) : sa présence est un signal indépendant des
   hooks, à vérifier avant de s'y fier.
3. À défaut de preuve : archivage automatique au-delà d'un âge configurable
   (24 h), ce qui vaut mieux qu'un hachurage ambigu conservé indéfiniment.

**Coût.** 2 h pour la piste 1, une fois tranché quel PID est le bon.

**Risque.** Le PID vu par un hook est celui du hook, pas celui de Claude Code ; il
faut remonter au parent et vérifier son `cmdline`. Si la vérification n'est pas
**certaine**, ne pas afficher de verdict : un « morte » faux est bien pire que le
« oubliée ? » actuel, qui a au moins le mérite d'être une question.

### R2 — `docs/SCHEMA.md`, le contrat, est faux sur sa règle la plus importante

**Le problème.** Le fichier qui porte « LIRE AVANT DE CODER » écrit :

> Chercher `(\d{4,6})` dans le basename de `cwd`. Trouvé → `"#40003"`.

`serveur.py` et `pullrequests.py` ont depuis abandonné cette règle et exigent un
préfixe `feat|feature|us|story` — avec, dans les deux fichiers, un commentaire
qui explique pourquoi : `fix/anomalies-run-9001` porte un n° de run TestRail, pas
une US. Et ce worktree **existe sur cette machine**
(`ProjetB.Automatisation-fix-anomalies-run-9001`). Or `historique.py::_ident`
applique toujours l'ancienne règle documentée. Vérifié en exécutant les deux :

```
ProjetB.Automatisation-fix-anomalies-run-9001
    historique._ident   -> "#9001"     (faux)
    serveur.identifiant -> "…-run-9001" (juste)
```

Deux modules, deux réponses, sur un nom de dossier de la machine — exactement ce
que la docstring de `historique.py` promet d'éviter (« pour que la Board et
l'Historique ne racontent jamais deux histoires »).

Dérives secondaires du même fichier : `archive.json`, `pr.cache.json` et
`sensor.log` existent et n'y sont pas décrits ; l'objet SESSION a gagné 8 champs
non documentés (`glyphe`, `libelle`, `cle_vu`, `model`, `cost_usd`, `age_s`,
`tool`, `reason`).

**Ce que ça change.** Rien à l'écran. Tout pour le prochain agent — ou le prochain
Thomas — qui lira le contrat en croyant qu'il est vrai.

**Coût.** 2 h : corriger la règle d'US, aligner `historique._ident`, ajouter les
trois fichiers et les huit champs.

**Risque.** Aucun. C'est l'item le moins cher avec la plus longue durée de vie de
tout ce document.

### R3 — Les notifications sont désactivées et le board ne le dit pas

**Le problème.** `notify.enabled` = `false`, `notify.state.json` n'a jamais
existé. Le module qui répond à la douleur n°1 **quand le board n'est pas à
l'écran** — 1 034 lignes, une matrice anti-spam, une fatigue sonore — n'a jamais
tourné. Que ce soit volontaire (trop bruyant) ou oublié, l'écran ne dit ni l'un
ni l'autre : Thomas peut passer une journée à croire qu'il sera prévenu.

Accessoirement, `serveur.py` appelle toujours
`self.notifieur.evaluate(els, compte, **False**)` : le board ne dit jamais au
serveur qu'il est au premier plan. Toute la mécanique `_track_focus` /
`FOCUS_GRACE_S` / `_muted_by_focus` est du code inatteignable — et son effet
voulu (ne pas notifier ce que l'utilisateur regarde déjà) n'existe pas.

**Ce que ça change.** Un indicateur d'état des notifications dans le chrome
(cloche barrée quand c'est coupé), commutable depuis le board — le serveur relit
déjà `config.json` sur changement de mtime. Et le board envoie son état de focus,
ce qui réveille 40 lignes de notifieur déjà écrites et testées.

**Coût.** 2 h.

**Risque.** Un commutateur dans l'interface écrit dans `config.json`, qui est le
fichier de l'humain. `creer_projet` le réécrit déjà intégralement depuis le
navigateur (lecture, mutation, réécriture) : l'ordre des clés et l'indentation
d'une édition manuelle sont perdus. Écrire la clé, pas le fichier.

### R4 — Le focus de pane est mort, et sa conception ne survit pas aux worktrees

**Le problème.** Deux pannes indépendantes, qui se cumulent.

1. `~/.claude/pane-state/` est vide : le producteur (`claude-pane.sh`) ne tourne
   plus, les 3 processus vivants ont été lancés en `claude` nu. Donc
   `live_panes()` = ∅, donc `resolve_pane` = `None` **toujours**, pour les 18
   dépôts et worktrees de la machine.
2. Même avec les tty vivants, `config.panes` associe un indice à un **dépôt**.
   Or `PROJET_A_backend` a 5 worktrees `-feat-*` plus le dépôt lui-même : six
   chemins qui préfixent-matchent la même clé, donc **six conversations
   possibles pour un seul indice de pane**. Cinq clics sur six focaliseraient la
   mauvaise conversation — ce que la règle du module interdit explicitement
   (« un mauvais focus est pire que pas de focus »). La conception est correcte
   pour un dépôt par pane, et Thomas travaille en worktrees.

**Ce que ça change.** L'association doit porter sur la **session**, pas sur le
chemin : si le capteur écrit le tty sur lequel il tourne, « quel pane » devient un
fait par session au lieu d'une déduction depuis un chemin ambigu.

**Coût.** Une demi-journée — mais **commencer par une expérience de dix
minutes** : un hook `async` voit-il un tty (`/proc/self/fd/0`) ? Si la réponse
est non, cette proposition n'existe pas et la bonne réponse est D6 (dire « pane
inconnu » et donner le `cwd`).

**Risque.** Windows Terminal 1.24 n'offre aucun identifiant de pane
(`focus.sh` le documente longuement) : même avec le tty, l'indice dans l'ordre de
création reste déclaré par l'humain. On réduit l'ambiguïté, on ne la supprime
pas. Ne pas investir plus d'une demi-journée là-dedans avant d'avoir fait D6, qui
rend l'outil honnête pour 1 h.

### R5 — Aucun commit, aucun test

**Le problème.** `git log` → *« your current branch 'master' does not have any
commits yet »*. 6 200 lignes, cinq modules qui déclarent chacun « ne lève
jamais », zéro filet. Les auto-vérifications (`main()` de `historique.py`,
`pullrequests.py`, `panes.py`, `focus.sh --dry-run`) sont réelles et utiles —
elles m'ont servi à établir la moitié de ce document — mais elles ne testent
rien automatiquement. Et la chose qui cassera est identifiée : `sensors/README.md`
épingle le bundle 2.1.245 et la liste des `notification_type`. Une montée de
version qui renomme un champ éteint le board en silence, puisque tous les
capteurs rendent 0 en toute circonstance.

**Ce que ça change.** (a) Commiter. (b) Un fichier de tests `unittest` qui
rejoue un payload enregistré par type de hook dans `board-event.py` et vérifie
l'état écrit — c'est ce qui aurait attrapé F1. (c) Un test qui affirme que
`serveur.identifiant` et `historique._ident` s'accordent sur les 18 noms de
dossiers réels de la machine — c'est ce qui aurait attrapé R2.

**Coût.** Une demi-journée pour (b) et (c) ensemble. Cinq minutes pour (a).

**Risque.** Aucun, sauf la tentation de viser une couverture. Trois tests qui
gardent les trois invariants nommés dans les docstrings valent mieux que trente
qui gardent des détails.

### R6 — État mutable partagé entre threads, et notifieur sur le chemin des requêtes

**Le problème.** `ThreadingHTTPServer` + un unique `BOARD` global.
`marquer_vu`, `archiver` et `poser_layout` mutent des dictionnaires que
`instantane()` parcourt, une fois par seconde et **par client SSE**. Deux onglets
ouverts = deux threads qui appellent `instantane()`, donc `notifieur.evaluate()`,
à 2 Hz : le seau à jetons anti-spam voit deux fois plus de ticks que la seconde
qu'il croit mesurer. Rien n'a cassé parce que les objets sont petits et que le
GIL rend atomiques la plupart des mutations d'un dict — c'est de la chance, pas
une garantie.

**Ce que ça change.** Un `threading.Lock` autour des mutations de `Board`, et le
notifieur déplacé dans un unique thread de fond au lieu du chemin des requêtes.
Au passage, `resolve_pane` appelle `live_panes()` (un `listdir` + un `exists` par
tty) une fois **par session et par instantané** : à 20 sessions et 2 onglets, 40
`listdir`/s pour un résultat identique. À hisser hors de la boucle.

**Coût.** 2 h.

**Risque.** Un verrou tenu pendant le balayage de `state/` sérialiserait les
clients SSE. Le tenir sur les mutations seulement. Probabilité de panne
aujourd'hui : faible. C'est le genre de défaut qui ne mord que le jour où ça
compte.

### R7 — L'historique n'a pas de filtre, et son cache meurt avec le process

**Le problème.** 143 ms à froid pour 45 conversations et 41 Mo, 0,2 ms à chaud.
Le cache est en mémoire de process : il repart de zéro à chaque redémarrage du
serveur, et le scan est synchrone sur le thread de la requête. Au rythme observé
— 45 conversations en 6,9 jours, soit ~6,4/jour — l'onglet passe à ~200 lignes et
~180 Mo dans un mois, donc ~600 ms à froid. Et surtout : **200 lignes sans
recherche, sans filtre de date, sans pagination**, dans un onglet dont le seul
usage est de retrouver une conversation précise.

**Ce que ça change.** Un champ de filtre texte côté client sur titre / dernier
prompt / dépôt. C'est 1 h et c'est de loin le meilleur rapport de cette section.
Ensuite seulement, des facettes (`us`, `alive`).

**Coût.** 1 h pour le filtre. 2 h pour persister le cache — **à ne pas faire
maintenant** : 143 ms ne gêne personne, et un cache sur disque est un fichier de
plus à invalider.

**Risque.** Aucun pour le filtre.

### Les petites dettes qu'il faut juste solder

- **`seen.json` contient encore six clés `demo-*`** d'une session de
  démonstration. La purge ne se déclenche qu'au-delà de 400 entrées, et jette
  alors les 200 plus anciennes — sans vérifier qu'elles ne sont pas des états
  courants. À 20 conversations changeant d'état plusieurs fois par heure, 400 est
  atteint en quelques jours. Purger sur l'âge et sur l'absence du `sid` dans
  `state/`, pas sur le nombre. 1 h.
- **`README.md` renvoie à `docs/SPEC.md`, qui n'existe pas.** Soit le document
  existe ailleurs (l'artifact mentionné) et le README doit le dire, soit la ligne
  part. 10 minutes.
- **11 sous-agents tournent sous la session courante et le board l'ignore.**
  `~/.claude/projects/<slug>/<sid>/subagents/` en contient 11 pour une seule
  carte. Ils sont correctement exclus de l'historique (45 conversations, pas 57),
  mais une conversation à 11 sous-agents est indiscernable d'une conversation
  seule — alors que `worker_permission_prompt` fait passer le parent en `blocked`
  sans dire que c'est un sous-agent qui bloque. Compter les fichiers du dossier
  suffirait à afficher « 11 » sur la carte. 1 h, valeur moyenne : à faire quand
  D1 aura libéré de la place sur la carte.

---

## 4. Ce qu'il ne faut PAS faire

### N1 — Répondre à une permission ou envoyer un prompt depuis le board

Ça séduit parce que ça ferme la boucle : je vois `BLOQUÉ`, je clique « oui ».
C'est ce qui casserait le produit.

Toute l'architecture repose sur « les capteurs sont les seuls producteurs de
données » et le serveur est un fusionneur en lecture seule. Le contrat
d'honnêteté du board est écrit en tête de `board.js` : « aucun compteur n'est
incrémenté localement, si le flux meurt l'affichage GÈLE ». Un chemin d'écriture
du navigateur vers une conversation vivante inverse ce rapport : le board devient
un pilote, et un pilote qui peut avoir une seconde de retard est un pilote qui
autorise le mauvais appel d'outil. C'est aussi une page sur `localhost:7777`,
sans authentification, à qui l'on donne le droit d'accorder des permissions au
nom de Thomas sur deux dépôts clients.

Et le vrai besoin coûte moins cher : l'amener au pane (R4) ou, à défaut, lui
donner le `cwd` (D6). On refuse.

### N2 — Approuver une PR depuis l'onglet Pull Requests

Même forme, conséquences pires. 7 des 8 PR ouvertes sont les siennes et il est
le gate Lead Tech. Un bouton « approuver » sur une liste dont l'état est calculé
depuis un cache de 0 à 180 s (`age_s` = 45 s à l'instant de la mesure, TTL 180 s),
c'est voter sur une PR dont les 12 commentaires ont peut-être bougé depuis
l'affichage.

La valeur de cet onglet est le **triage** : il dit quelle PR ouvrir. Le voyage
doit finir dans Azure DevOps, là où se trouve le diff — et le `↗` le fait déjà.
Ce qui manque n'est pas un bouton d'approbation, c'est F4 : savoir laquelle de
ses propres PR pourrit.

### N3 — Trier automatiquement les cartes par urgence

La plus séduisante des trois, parce que c'est ce que fait n'importe quel
dashboard et parce que la donnée est déjà là : `PRIORITE` est calculé et déjà
utilisé pour le bandeau.

Elle détruirait la seule propriété qui rend ce board lisible à deux mètres : la
carte est à une place fixe, donc Thomas reconnaît « la troisième de la colonne
ProjetA » sans la lire. Le design actuel a eu raison — le tri est
`(ordre manuel, repo, ident)`, donc **stable**, et l'urgence vit dans un seul
endroit, le bandeau d'attention, « la SEULE zone triée par urgence ». Un board
qui se réorganise, c'est la carte ambre qui pulse et qu'on visait qui se déplace
sous le curseur.

Si le bandeau ne suffit pas, on corrige le bandeau (D5), pas la géométrie du
board. Même verdict pour « replier les colonnes calmes » et pour tout
masquage automatique : une place qui peut disparaître n'est pas une place.

---

## 5. Les trois choses à faire en premier

### 1. F1 — réparer `last_say` (1 h)

Trois lignes dans le seul fichier qui est la source de vérité des événements, et
le champ qui répond à la douleur n°2 réapparaît sur toutes les cartes, à tous les
tours, dès aujourd'hui. Aucun autre item n'a ce rapport. Vérifiable en dix
secondes : terminer un tour, lire `event.json`.

### 2. D1 + D2 — inverser la hiérarchie de la carte, et arrêter de cacher le titre (2 h)

La douleur n°2 est « je ne sais plus quelle conversation fait quoi », et le board
y répond aujourd'hui par `ProjetA` en 17 px gras. Une fois F1 passé, la carte a
deux champs porteurs — le titre et la phrase — et tous les deux sont soit
rétrogradés, soit cachés à partir de 17 conversations. C'est le changement le
moins cher au plus fort effet sur la raison d'être de l'outil.

### 3. D6 + R3 — arrêter de promettre ce qui ne marche pas (2 h)

La légende promet le focus de pane à trois endroits et il se résout pour 0 des 18
dépôts ; les notifications sont coupées et rien ne le dit. Ce sont les deux
réponses à la douleur n°1, et les deux sont éteintes en silence. Aucun de ces
deux correctifs n'exige de résoudre le problème difficile (R4) : ils exigent que
le board avoue son état — précisément le principe qu'il revendique déjà. À faire
**avant** R4, parce que R4 commence par une expérience de dix minutes (un hook
voit-il son tty ?) dont la réponse décide si le focus de pane est constructible
du tout.

**Et la quatrième, à faire la semaine où tu retouches le code :** R2, corriger
`SCHEMA.md`. C'est le fichier qui dit « lire avant de coder » et il est faux sur
sa règle la plus importante. Il induira en erreur le prochain qui le lira, humain
ou agent.
