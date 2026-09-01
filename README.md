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
