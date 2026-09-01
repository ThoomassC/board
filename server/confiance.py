#!/usr/bin/env python3
"""Confiance de l'espace de travail : pré-accepte, pour un dossier de projet,
l'écran « Do you trust the files in this folder? » de Claude Code.

Claude Code garde cet accord dans ~/.claude.json, une clé par projet :

    {"projects": {"/chemin/du/projet": {"hasTrustDialogAccepted": true, ...}}}

Il n'existe aucune option en ligne de commande pour l'accorder d'avance
(`--dangerously-skip-permissions` ne couvre pas cet écran) : poser la clé est
le seul moyen d'ouvrir une conversation sans passer par le dialogue.

Deux précautions, parce que ~/.claude.json contient tout l'état de Claude Code
(dont les jetons) et qu'il n'est pas à nous :

  - on n'écrit qu'une seule clé, on ne touche à rien d'autre, et on s'abstient
    dès que le fichier n'est pas relu proprement ;
  - le remplacement est atomique et le fichier temporaire est créé en 0600,
    comme l'original.

Un Claude Code déjà lancé réécrit ~/.claude.json depuis sa mémoire et peut donc
effacer la clé : les lanceurs posés par le board rappellent ce module juste
avant chaque `exec claude`, de sorte que la confiance est réaffirmée au moment
où elle sert.

    python3 server/confiance.py /chemin/du/projet
"""
import json
import os
import sys

CLAUDE_JSON = os.path.expanduser("~/.claude.json")


def marquer(chemin, fichier=CLAUDE_JSON):
    """Pose hasTrustDialogAccepted pour `chemin`.

    Renvoie True si la clé est en place (posée maintenant ou déjà présente),
    False si on n'a rien pu faire — auquel cas Claude Code affichera son
    dialogue, ce qui est le comportement d'origine et non une panne.
    """
    chemin = os.path.abspath(os.path.expanduser((chemin or "").strip()))
    if not os.path.isdir(chemin):
        return False
    try:
        with open(fichier, encoding="utf-8") as f:
            etat = json.load(f)
    except FileNotFoundError:
        etat = {}
    except Exception:
        return False          # illisible : on n'écrase pas ce qu'on ne relit pas
    if not isinstance(etat, dict):
        return False
    projets = etat.setdefault("projects", {})
    if not isinstance(projets, dict):
        return False
    entree = projets.get(chemin)
    if not isinstance(entree, dict):
        entree = {}
        projets[chemin] = entree
    if entree.get("hasTrustDialogAccepted") is True:
        return True           # déjà accordé : aucune écriture, aucun risque pris
    entree["hasTrustDialogAccepted"] = True
    try:
        tmp = fichier + ".board.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(etat, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fichier)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: confiance.py <dossier>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0 if marquer(sys.argv[1]) else 1)
