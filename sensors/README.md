# Les deux capteurs

Ce sont les seuls producteurs de données de « le board ». Tout le reste
(serveur, board, notifieur) ne fait que lire ce qu'ils écrivent.

| capteur | déclencheur | écrit |
|---|---|---|
| `board-sensor.sh` | commande de statusline | `state/<sid>.meas.json`, `account.json` |
| `board-event.py` | hooks Claude Code | `state/<sid>.event.json` |

Racine d'écriture : `~/.claude/board/`. Les deux capteurs créent l'arborescence
au besoin ; aucun ordre d'installation à respecter. La variable d'environnement
`BOARD_HOME` redirige la racine — elle n'existe que pour les tests, ne pas la
définir en production.

---

## 1. `board-sensor.sh` — l'enveloppe de la statusline

`context_window.used_percentage` et `rate_limits` **n'existent que** dans le stdin
de la commande de statusline. Aucun hook ne les voit. Plutôt que de toucher aux
285 lignes de `~/.claude/statusline-command.sh`, on les enveloppe :

    statusLine.command ─► board-sensor.sh ─┬─► meas.json + account.json  (arrière-plan)
                                           └─► exec la statusline d'origine

Le stdin est lu **une seule fois** (il n'est lisible qu'une fois), puis rejoué
vers la statusline d'origine par here-string. La sortie de celle-ci passe telle
quelle : `board-sensor.sh` n'écrit jamais un octet sur stdout ni sur stderr.

L'extraction tourne dans un sous-shell détaché dont `stdout` et `stderr` sont
fermés sur `/dev/null` — obligatoire, sinon le descripteur de sortie hérité
resterait ouvert et Claude Code attendrait la fin du fils avant d'afficher la
ligne. Conséquence : **zéro démarrage de Python sur le chemin d'affichage.**

Surcoût mesuré (40 appels, payload réaliste, `~/.claude/statusline-command.sh`
comme statusline d'origine) :

| | médiane | p95 |
|---|---|---|
| appel direct | 21,1 ms | 23,1 ms |
| via l'enveloppe | 26,8 ms | 29,9 ms |
| **surcoût** | **+5,7 ms** | **+6,9 ms** |

À titre de comparaison, faire l'extraction de façon synchrone coûterait ~13 ms de
démarrage Python supplémentaires par appel, avant même le travail utile : c'est
ce qui a tranché en faveur du détachement.

Champs extraits (schéma vérifié sur le bundle 2.1.245) :

    session_id                          -> session_id
    session_name                        -> title
    cwd (repli workspace.current_dir)   -> cwd
    workspace.project_dir               -> project_dir
    worktree.branch, sinon
      workspace.git_worktree            -> branch
    context_window.used_percentage      -> ctx_pct
    model.display_name                  -> model
    effort.level                        -> effort
    cost.total_cost_usd                 -> cost_usd
    (absent du payload)                 -> permission_mode : null
    rate_limits.five_hour  {used_percentage, resets_at} -> account.five_hour  {used_pct, resets_at}
    rate_limits.seven_day  {used_percentage, resets_at} -> account.seven_day {used_pct, resets_at}

`account.json` n'est **pas** écrit quand `rate_limits` est absent du payload
(abonnement non concerné, ou avant la première réponse API) : écrire des `null`
écraserait des valeurs valables posées par une autre session. Quand une seule des
deux fenêtres est présente, l'autre est écrite à `null`, conformément au schéma.

---

## 2. `board-event.py` — l'écrivain d'événements

    usage : board-event.py <state>              state ∈ working|blocked|review|error|dead
            board-event.py --from-notification  état déduit du type de notification

`silent` n'est jamais écrit : le serveur le dérive.

Les deux invariants qui font marcher tous les chronos du board :

* **`state_since` ne bouge que si `state` change réellement.** L'ancien fichier
  est relu avant chaque écriture. Sans ça, « attend depuis 4m12 » n'existerait pas.
* `updated_at` bouge à chaque écriture — c'est l'indicateur de fraîcheur.

`tool` est renseigné sur `PreToolUse` et remis à `null` sur toute autre
transition. `last_say` vient de `last_assistant_message` (donc `Stop`), aplati de
ses retours à la ligne et tronqué à 200 caractères. `reason` porte le type de
Notification, sinon `null`.

Sortie muette, **code retour toujours 0**. Les erreurs partent dans
`~/.claude/board/sensor.log`, plafonné à 200 Ko avec une génération conservée
(`sensor.log.1`).

Latence mesurée (50 appels, payload `PreToolUse`) : **médiane 13,6 ms, p95
16,0 ms** — l'essentiel étant le démarrage de l'interpréteur. Avec `"async": true`
dans les hooks, ce coût sort du chemin critique du prompt.

### Types de Notification réellement observés

Le champ qui porte le type est **`notification_type`** : c'est celui que Claude
Code construit dans le payload du hook (`hook_event_name: "Notification"`,
`message`, `title`, `notification_type`) et c'est aussi celui qu'il compare aux
`matcher` de `settings.json`. Les clés `type` / `subtype` n'existent pas dans ce
payload ; elles sont conservées comme repli défensif, avec en dernier recours un
balayage de `message` / `title` à la recherche d'un identifiant connu. Aucun de
ces replis ne s'est déclenché en pratique.

L'énumération complète est figée dans le bundle 2.1.245 :

    permission_prompt          -> blocked
    agent_needs_input          -> blocked
    elicitation_dialog         -> blocked
    idle_prompt                -> review
    agent_completed            -> review
    auth_success               -> ignoré
    quota_auto_resume_fired    -> ignoré
    quota_auto_resume_stale    -> ignoré
    quota_auto_resume_disabled -> ignoré
    worker_permission_prompt   -> blocked   ← extension, voir ci-dessous
    elicitation_url_dialog     -> blocked   ← extension, voir ci-dessous
    push_notification          -> ignoré    ← extension
    computer_use_enter         -> ignoré    ← extension
    computer_use_exit          -> ignoré    ← extension

Tout type non listé est ignoré sans écriture, et journalisé une ligne dans
`sensor.log` — c'est le mécanisme de découverte des types futurs.

**Extension assumée par rapport à la spec.** La spec ne connaissait que 5 types
utiles. `worker_permission_prompt` et `elicitation_url_dialog` sont des variantes
exactes de `permission_prompt` et `elicitation_dialog` ; les traiter en « inconnu
donc ignoré » ferait disparaître du board une session réellement bloquée. Elles
sont donc mappées, dans un bloc commenté de `STATE_BY_NOTIF` retirable en
supprimant deux lignes. Les trois autres types découverts ne correspondent à aucun
état du board et sont ignorés explicitement, ce qui évite de polluer le journal.

---

## 3. Branchement dans `~/.claude/settings.json`

### À sauvegarder AVANT

    cp ~/.claude/settings.json ~/.claude/settings.json.bak-board

### Retour arrière, en une ligne

    cp ~/.claude/settings.json.bak-board ~/.claude/settings.json

### On AJOUTE, on ne retire rien

`pane-state.sh` et `pane-title.sh` doivent continuer de fonctionner : le titre
d'onglet Windows Terminal des 4 panes ProjetA en dépend. Les hooks ci-dessous
s'ajoutent **à côté** des entrées existantes de `Stop`, `UserPromptSubmit` et
`Notification` — un même événement peut porter plusieurs commandes, elles sont
toutes exécutées. Ne pas remplacer les tableaux existants : y pousser un élément.

### `statusLine` — remplacer le bloc existant par celui-ci

```json
"statusLine": {
  "type": "command",
  "command": "bash ~/repos/perso/board/sensors/board-sensor.sh ~/.claude/statusline-command.sh"
}
```

La statusline d'origine n'est **pas** modifiée : elle devient l'argument `$1` de
l'enveloppe. Elle est exécutable et porte un shebang `#!/usr/bin/env python3`,
donc l'enveloppe l'exécute directement ; si le bit `+x` disparaissait, elle
retomberait sur `python3 <chemin>`.

Si les `~` posaient problème, la variante en chemins absolus est équivalente :

```json
"command": "bash $HOME/repos/perso/board/sensors/board-sensor.sh $HOME/.claude/statusline-command.sh"
```

### `hooks` — éléments à pousser dans les tableaux

```json
"hooks": {
  "UserPromptSubmit": [
    {
      "hooks": [
        {
          "type": "command",
          "async": true,
          "command": "python3 ~/repos/perso/board/sensors/board-event.py working"
        }
      ]
    }
  ],
  "PreToolUse": [
    {
      "matcher": "*",
      "hooks": [
        {
          "type": "command",
          "async": true,
          "command": "python3 ~/repos/perso/board/sensors/board-event.py working"
        }
      ]
    }
  ],
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "async": true,
          "command": "python3 ~/repos/perso/board/sensors/board-event.py review"
        }
      ]
    }
  ],
  "StopFailure": [
    {
      "hooks": [
        {
          "type": "command",
          "async": true,
          "command": "python3 ~/repos/perso/board/sensors/board-event.py error"
        }
      ]
    }
  ],
  "Notification": [
    {
      "matcher": "permission_prompt|agent_needs_input|elicitation_dialog|worker_permission_prompt|elicitation_url_dialog|idle_prompt|agent_completed",
      "hooks": [
        {
          "type": "command",
          "async": true,
          "command": "python3 ~/repos/perso/board/sensors/board-event.py --from-notification"
        }
      ]
    }
  ],
  "SessionEnd": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "python3 ~/repos/perso/board/sensors/board-event.py dead"
        }
      ]
    }
  ]
}
```

Notes de branchement :

* **`matcher` de `Notification`** : Claude Code compare le motif à
  `notification_type` (vérifié dans le bundle : `case "Notification": return
  e.notification_type`). Le motif accepte l'alternative `|`. Le capteur filtre de
  toute façon lui-même, le matcher n'est qu'une économie d'appels.
  *Variante découverte* : retirer la ligne `matcher` (ou la mettre à `"*"`) pour
  que le capteur voie tous les types et journalise les inconnus dans
  `sensor.log`. Utile une fois, après une montée de version de Claude Code.
* **`async: true` partout sauf `SessionEnd`** : le capteur ne décide rien, il ne
  doit jamais retarder un prompt ni un appel d'outil. `SessionEnd` reste
  synchrone, sinon le processus peut mourir avant l'écriture du `dead`.
* **`PreToolUse` avec `matcher: "*"`** : sert à renseigner `tool`. Comme l'état
  reste `working`, `state_since` ne bouge pas — c'est voulu.
* **`StopFailure`** est facultatif : c'est le seul producteur de l'état `error`.
  Sans lui, une session en échec restera affichée « à relire ».
* Le tableau `Notification` existant contient déjà `pane-state.sh waiting` sans
  `matcher`. Le nouvel élément est un **objet séparé** dans le même tableau,
  avec son propre `matcher` : les deux cohabitent sans interférence.

### Vérification après branchement

    # la statusline s'affiche-t-elle toujours normalement ?
    # (relancer une session, la ligne doit être identique à avant)

    ls -l ~/.claude/board/state/          # un .meas.json et un .event.json par session
    cat ~/.claude/board/account.json      # limites 5h / 7j
    cat ~/.claude/board/sensor.log        # doit rester vide

Un `sensor.log` non vide est le seul symptôme visible d'un problème : les capteurs
ne remontent jamais rien à l'utilisateur.
