#!/usr/bin/env python3
"""Résolution « session (ou repo) -> indice de pane Windows Terminal ».

L'indice rendu est celui attendu par wt/focus.sh : la position du pane dans
l'ORDRE DE CRÉATION de la fenêtre, 0 pour le premier créé.

Deux sources, dans cet ordre :

1. ``config["panes"]`` — dictionnaire explicite ``{"<repo>": <index>}``. La clé
   est comparée au basename du repo. C'est la source qui décide de l'indice ;
   elle est la seule à en produire un.

2. ``~/.claude/pane-state/<PROJ>.tty`` — écrits par claude-pane.sh au
   démarrage de chaque pane, effacés à sa sortie (trap EXIT). Ils ne portent
   aucun indice : ils prouvent seulement quels panes sont VIVANTS. On s'en sert
   pour invalider une entrée de config qui pointerait vers un pane mort.

Règle non négociable : un mauvais focus est pire que pas de focus. Au moindre
doute — configuration douteuse, repo non reconnu, pane mort, état illisible —
on rend None.

Bibliothèque standard uniquement.
"""

from __future__ import annotations

import json
import os

#: dossier des <PROJ>.tty, écrit par ~/.claude/claude-pane.sh
DEFAULT_STATE_DIR = "~/.claude/pane-state"

#: garde-fou : au-delà, l'entrée de config est considérée comme une coquille
#: (focus.sh borne de son côté avec BOARD_WT_PANE_COUNT).
MAX_PANE_INDEX = 63

#: séparateur des worktrees : « <repo>-feat-40003 » appartient à « <repo> ».
#: Volontairement limité à « - » : « _ » est utilisé À L'INTÉRIEUR des noms de
#: repo, l'accepter ferait matcher un préfixe de nom sur un autre repo.
_WORKTREE_SEP = "-"

#: profondeur maximale de remontée dans les parents de cwd
_MAX_DEPTH = 8


def _state_dir(state_dir: str | None = None) -> str:
    if state_dir is None:
        state_dir = os.environ.get("BOARD_PANE_STATE") or DEFAULT_STATE_DIR
    return os.path.expanduser(state_dir)


def live_panes(state_dir: str | None = None) -> set[str]:
    """Noms de projet dont le pane est vivant, d'après les <PROJ>.tty.

    Un tty est retenu s'il est non vide et si le device qu'il désigne existe
    encore : claude-pane.sh nettoie normalement à la sortie, mais un pane tué
    brutalement laisse un fichier orphelin dont le /dev/pts a disparu.
    """
    alive: set[str] = set()
    directory = _state_dir(state_dir)
    try:
        names = os.listdir(directory)
    except OSError:
        return alive
    for name in names:
        if not name.endswith(".tty"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                dev = handle.read().strip()
        except OSError:
            continue
        if not dev:
            continue
        try:
            if not os.path.exists(dev):
                continue
        except (OSError, ValueError):
            continue
        alive.add(name[: -len(".tty")])
    return alive


def _candidates(cwd: str) -> list[str]:
    """Basenames de cwd puis de ses parents, du plus précis au plus général."""
    if not isinstance(cwd, str) or not cwd.strip():
        return []
    path = os.path.normpath(os.path.expanduser(cwd.strip()))
    # On ne remonte pas au-delà du home : « <utilisateur> », « home », « mnt » ne
    # sont pas des repos, et une clé de config malheureuse ne doit pas matcher.
    stop = os.path.normpath(os.path.expanduser("~"))
    out: list[str] = []
    seen: set[str] = set()
    for _ in range(_MAX_DEPTH):
        if path == stop:
            break
        base = os.path.basename(path)
        if base and base not in seen:
            seen.add(base)
            out.append(base)
        parent = os.path.dirname(path)
        if not parent or parent == path:
            break
        path = parent
    return out


def _clean_panes(config: object) -> dict[str, int]:
    """Entrées de config["panes"] exploitables. Tout le reste est ignoré."""
    if not isinstance(config, dict):
        return {}
    raw = config.get("panes")
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        # bool est un int en Python : on ne veut pas de True -> pane 1.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if not 0 <= value <= MAX_PANE_INDEX:
            continue
        clean[key.strip()] = value
    return clean


def _match_key(candidates: list[str], panes: dict[str, int]) -> tuple[str, str] | None:
    """(clé de config, candidat qui l'a fait matcher), du plus précis au moins.

    Pour un candidat donné : match exact d'abord, sinon préfixe de worktree
    (« <clé>-… »), la clé la plus longue gagnant — elle est la plus spécifique.
    Deux clés ne peuvent pas être préfixes de même longueur du même candidat,
    mais on garde le garde-fou d'ambiguïté : au doute, on renonce.
    """
    for cand in candidates:
        exact = [k for k in panes if k == cand]
        if len(exact) == 1:
            return exact[0], cand
        if exact:
            return None
        prefixes = [k for k in panes if cand.startswith(k + _WORKTREE_SEP)]
        if not prefixes:
            continue
        longest = max(len(k) for k in prefixes)
        best = [k for k in prefixes if len(k) == longest]
        if len(best) != 1:
            return None
        return best[0], cand
    return None


def resolve_pane(cwd: str, config: dict) -> int | None:
    """Indice de pane pour une session tournant dans `cwd`, ou None.

    None dès que le focus ne peut pas être garanti : repo inconnu de
    ``config["panes"]``, aucun pane vivant, ou pane de la config mort.
    """
    try:
        panes = _clean_panes(config)
        if not panes:
            return None

        candidates = _candidates(cwd)
        if not candidates:
            return None

        matched = _match_key(candidates, panes)
        if matched is None:
            return None
        key, cand = matched

        # Source 2 : la preuve de vie. Aucun tty -> Windows Terminal n'est pas
        # (ou plus) suivi ; on ne focalise pas au hasard.
        alive = live_panes()
        if not alive:
            return None
        if key not in alive and cand not in alive:
            return None

        return panes[key]
    except Exception:  # noqa: BLE001 — aucun appelant ne doit planter là-dessus
        return None


# --- inspection à l'œil ------------------------------------------------------


def _load_config(path: str | None = None) -> tuple[str, dict]:
    """Config locale réelle, sinon l'exemple du dépôt, sinon vide."""
    tries = []
    if path:
        tries.append(os.path.expanduser(path))
    tries.append(os.path.expanduser("~/.claude/board/config.json"))
    tries.append(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "config.example.json")
    )
    for candidate in tries:
        try:
            with open(candidate, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return candidate, data
        except (OSError, ValueError):
            continue
    return "(aucune)", {}


def _detected_repos(config: dict, alive: set[str]) -> dict[str, str]:
    """{nom de repo -> chemin (ou '-' si seulement connu par son nom)}.

    Sources : sous-dossiers des racines de projets de la config, clés de
    config["panes"], et panes vivants.
    """
    found: dict[str, str] = {}
    projects = config.get("projects")
    if isinstance(projects, list):
        for project in projects:
            if not isinstance(project, dict):
                continue
            root = project.get("root")
            if not isinstance(root, str):
                continue
            root = os.path.expanduser(root)
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                continue
            for entry in entries:
                if entry.startswith("."):
                    continue
                full = os.path.join(root, entry)
                if os.path.isdir(full):
                    found.setdefault(entry, full)
    for name in sorted(set(_clean_panes(config)) | alive):
        found.setdefault(name, "-")
    return found


def _main() -> None:
    import sys

    config_path, config = _load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    panes = _clean_panes(config)
    raw_panes = config.get("panes") if isinstance(config.get("panes"), dict) else {}
    state_dir = _state_dir()
    alive = live_panes()

    print(f"config        : {config_path}")
    print(f"panes (config): {raw_panes or '{}'}"
          + ("" if len(panes) == len(raw_panes) else f"  -> retenues : {panes}"))
    print(f"état des tty  : {state_dir}"
          + ("" if os.path.isdir(state_dir) else "  (dossier absent)"))
    print(f"panes vivants : {', '.join(sorted(alive)) if alive else '(aucun)'}")
    print()

    repos = _detected_repos(config, alive)
    if not repos:
        print("aucun repo détecté : ni racine de projet lisible, ni pane vivant,")
        print("ni entrée dans config[\"panes\"].")
    else:
        width = max(len(name) for name in repos)
        print(f"{'repo'.ljust(width)}  config  vivant  résolu  chemin")
        print(f"{'-' * width}  ------  ------  ------  ------")
        for name in sorted(repos):
            path = repos[name]
            probe = path if path != "-" else name
            index = resolve_pane(probe, config)
            print("{}  {:^6}  {:^6}  {:^6}  {}".format(
                name.ljust(width),
                panes.get(name, "-") if name in panes else "-",
                "oui" if name in alive else "non",
                "-" if index is None else index,
                path,
            ))

    print()
    here = os.getcwd()
    print(f"cwd courant   : {here}")
    print(f"  candidats   : {', '.join(_candidates(here)) or '(aucun)'}")
    print(f"  résolu      : {resolve_pane(here, config)}")


if __name__ == "__main__":
    _main()
