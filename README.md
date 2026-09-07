# Le board

Tableau de bord local des conversations Claude Code, groupées par projet.
Une conversation = une session, un projet = une colonne.

Contrat de données : docs/SCHEMA.md — **lire avant de toucher au code**.

## Principe

Le moteur est générique : aucun nom de client, aucun chemin, aucun numéro d'US
dans ce dépôt. Tout ce qui est spécifique vit dans `~/.claude/board/config.json`,
qui n'est pas versionné.

## Lancer

    python3 server/serveur.py          # http://localhost:7777

## Tester

    python3 -m unittest discover -s tests     # la suite complète

**Le `-s tests` n'est pas facultatif.** Sans lui, la découverte part de la
racine, ne descend pas dans un dossier qui n'est pas un paquet — `tests/` n'a
pas d'`__init__.py` — et rend un vert VIDE :

    python3 -m unittest discover              # Ran 0 tests — vert trompeur

Chaque module du serveur porte en plus son auto-vérification, qui tourne sur la
configuration réelle du poste plutôt que sur des données fabriquées :

    python3 server/chantier.py         # inventaire des arbres + invariants
    python3 server/conversation.py     # les sept situations des sous-agents
    python3 server/attente.py          # l'échelle de priorité du bandeau
