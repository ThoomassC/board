#!/usr/bin/env bash
# Prépare ~/.claude/board/ et rappelle le branchement des capteurs.
# N'écrase JAMAIS une configuration existante et ne touche pas à settings.json :
# le branchement des hooks et de la statusline se fait à la main, après revue.
set -euo pipefail

RACINE="$HOME/.claude/board"
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$RACINE/state"
echo "dossier d'état      : $RACINE"

if [ -f "$RACINE/config.json" ]; then
  echo "configuration       : déjà présente, laissée intacte"
else
  cp "$ICI/config.example.json" "$RACINE/config.json"
  echo "configuration       : $RACINE/config.json créé depuis l'exemple"
  echo "                      -> à éditer : déclare tes projets et leurs racines"
fi

chmod +x "$ICI/sensors/"*.sh "$ICI/wt/"*.sh 2>/dev/null || true

echo
echo "Lancer le board :"
echo "    python3 $ICI/server/serveur.py"
echo
echo "Brancher les capteurs : voir $ICI/sensors/README.md"
echo "(sauvegarde ~/.claude/settings.json avant toute modification)"
