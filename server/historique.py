#!/usr/bin/env python3
"""historique.py — l'onglet « Historique » de le board.

Balaie ~/.claude/projects/<slug>/<uuid>.jsonl et rend TOUTES les conversations
Claude Code jamais ouvertes sur la machine, groupées par projet de config.json
(contrat : docs/SCHEMA.md), avec un groupe de repli pour le reste.

Interface publique — une seule fonction :

    from historique import scan
    scan(config) -> {"groupes": [...], "total": int, "scanned_at": epoch_s}

Principes de lecture, dictés par la matière première :

* Le slug de dossier est ambigu (`_` et `.` du vrai chemin y deviennent des
  `-`). On ne reconstruit JAMAIS le chemin depuis le slug : on lit le `cwd`
  écrit dans le fichier. Le slug ne sert qu'à *départager* les cwd candidats.
* Un transcript contient plusieurs `cwd` (excursions dans un worktree, un
  sous-dossier, /mnt/c...). Le cwd de la session est celui du DÉBUT de fichier,
  et c'est aussi le seul dont le slug retombe sur le nom du dossier. Prendre le
  plus fréquent ou le dernier se tromperait — vérifié sur les 45 transcripts.
* On ne charge jamais un fichier entier en mémoire : fenêtre de tête (date de
  création) + tampon circulaire de queue (titre, dernier prompt, date de
  fin), et un simple comptage de lignes au passage.

Stdlib uniquement. Aucune exception ne sort de scan().
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from collections import deque
from datetime import datetime

# ---------------------------------------------------------------- constantes

PROJECTS_ROOT_DEFAULT = "~/.claude/projects"
BOARD_ROOT_DEFAULT = "~/.claude/board"

HEAD_LINES = 60      # la date de création et le cwd sont dans les 6 premières
TAIL_LINES = 250     # le dernier aiTitle est toujours à < 50 lignes de la fin

TITLE_MAX = 60
PROMPT_MAX = 120
IDENT_MAX = 22
NO_TITLE = "(sans titre)"

# (l'ancienne règle laxiste « un nombre de 4 à 6 chiffres » a été retirée :
#  elle promouvait n'importe quel numéro de run ou de PR en numéro d'US)

# N° d'US dans un nom de worktree ou de branche. Mêmes motifs que le serveur,
# pour que la Board et l'Historique ne racontent jamais deux histoires.
# Un nombre nu ne suffit JAMAIS : il doit être introduit par feat/feature/us/
# story. C'est ce qui évite de lire `fix/anomalies-run-9001` comme l'US 9001 —
# 9001 est un n° de run TestRail. Un identifiant faux est pire qu'absent.
RE_US_BRANCHE = re.compile(r"^(?:feature|feat|us|story)/(\d{3,6})(?:[-_/]|$)", re.I)
RE_US_DOSSIER = re.compile(
    r"(?:^|[-_])(?:feat|feature|us|story)[-_]?(\d{3,6})(?:[-_]|$)", re.I)

# Bornes de sécurité : un transcript qui recracherait du JSON dans une sortie
# d'outil ne doit pas faire gonfler ces ensembles.
_MAX_SEEN = 64
_CWD_MARK = '"cwd":"'
_BRANCH_MARK = '"gitBranch":"'

_RE_NOT_SLUG = re.compile(r"[^A-Za-z0-9]")
_RE_SPACES = re.compile(r"\s+")

# Pré-filtre bon marché : on ne parse pas le JSON pour compter les messages.
_MSG_MARKERS = ('"type":"user"', '"type":"assistant"')

# Claude Code écrit gitBranch:"HEAD" quand il n'y a pas de branche à nommer
# (dossier hors git, ou HEAD détachée). Ce n'est pas un nom de branche : un
# ident « HEAD » sur une carte ne dirait rien, donc on le traite comme absent.
_NO_BRANCH = frozenset({"main", "master", "develop", "dev", "HEAD", "head",
                        "detached", ""})   # = serveur.BRANCHES_MUETTES

# Cache mémoire du module, indexé par chemin ; invalidé par (mtime_ns, taille).
# Ne contient que ce qui est lu DANS le fichier : tout ce qui dépend de la
# config ou de l'état vivant est recalculé à chaque scan().
_CACHE: dict[str, tuple[int, int, dict]] = {}


# ------------------------------------------------------------------- outils

def _expand(path: str) -> str:
    """`~` développé, chemin normalisé, jamais d'exception."""
    try:
        return os.path.normpath(os.path.expanduser(str(path)))
    except Exception:
        return ""


def _slug(path: str) -> str:
    """Nom de dossier que Claude Code fabrique pour ce chemin (non injectif)."""
    return _RE_NOT_SLUG.sub("-", path)


def _clean(value, limit: int) -> str:
    """Texte d'une ligne, espaces normalisés, tronqué à `limit` caractères."""
    if not isinstance(value, str):
        return ""
    text = _RE_SPACES.sub(" ", value).strip()
    return text[:limit]


def _iso_epoch(stamp):
    """« 2026-08-19T14:46:36.826Z » -> epoch s. None si illisible."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
    except Exception:
        pass
    try:                       # dernier recours : on lit la seconde en UTC
        return calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _ident(cwd: str, branch: str) -> str:
    """Identifiant affiché. MÊME règle stricte que serveur.identifiant.

    Un nombre nu ne suffit JAMAIS : il doit être introduit par feat/feature/us/
    story. Sans ça, « fix/anomalies-run-9001 » devenait « #9001 » — un numéro de
    run TestRail promu au rang d'US, et l'onglet Historique affichait un autre
    identifiant que l'onglet Board pour la même conversation.
    """
    base = os.path.basename(cwd.rstrip("/")) if cwd else ""
    found = RE_US_DOSSIER.search(base)
    if not found and branch:
        found = (RE_US_BRANCHE.match(branch)
                 or RE_US_DOSSIER.search(branch.rstrip("/").split("/")[-1]))
    if found:
        return "#" + found.group(1)
    if branch and branch not in _NO_BRANCH:
        return branch.rstrip("/").split("/")[-1][:IDENT_MAX]
    return ""


def _raw_value(line: str, marker: str):
    """Valeur d'un champ plat sans parser la ligne. None si absent ou douteux.

    On parcourt déjà chaque ligne pour compter les messages : relever au vol les
    `cwd` et `gitBranch` de TOUT le transcript ne coûte que deux `find`. Vérifié
    sur les 45 transcripts : jeux identiques à ceux d'un parse JSON complet.
    """
    start = line.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = line.find('"', start)
    if end <= start:
        return None
    value = line[start:end]
    if "\\" in value:
        return None                # échappement JSON : on ne devine pas
    return value


def _us(cwds, branches) -> list:
    """N° d'US croisés dans les worktrees et branches du transcript, triés."""
    found = set()
    for cwd in cwds:
        match = RE_US_DOSSIER.search(os.path.basename(cwd.rstrip("/")))
        if match:
            found.add(match.group(1))
    for branch in branches:
        if branch in _NO_BRANCH:
            continue
        match = (RE_US_BRANCHE.match(branch)
                 or RE_US_DOSSIER.search(branch.rstrip("/").split("/")[-1]))
        if match:
            found.add(match.group(1))
    return sorted(found, key=lambda n: (len(n), n))


def _alive_sids(board_root: str) -> set:
    """Sessions encore vivantes = un state/<sid>.event.json existe."""
    sids = set()
    try:
        with os.scandir(os.path.join(board_root, "state")) as it:
            for entry in it:
                name = entry.name
                if name.endswith(".event.json"):
                    sids.add(name[: -len(".event.json")])
    except Exception:
        pass
    return sids


# ------------------------------------------------------- lecture d'un fichier

def _pick_cwd(objects, dirslug):
    """(cwd, branch) de la session, départagés par le slug du dossier."""
    first = None
    for obj in objects:
        cwd = obj.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        branch = obj.get("gitBranch") or ""
        if not isinstance(branch, str) or branch in _NO_BRANCH:
            branch = ""
        candidate = (cwd, branch)
        if _slug(cwd) == dirslug:
            return candidate
        if first is None:
            first = candidate
    return first


def _read_record(path: str, dirslug: str, size: int, mtime: int) -> dict:
    """Lit un transcript en deux fenêtres et rend le brut, sans config."""
    head, tail = [], deque(maxlen=TAIL_LINES)
    seen_cwds, seen_branches = set(), set()
    lines = messages = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                lines += 1
                if lines <= HEAD_LINES:
                    head.append(line)
                tail.append(line)
                for marker in _MSG_MARKERS:
                    if marker in line:
                        messages += 1
                        break
                if len(seen_cwds) < _MAX_SEEN:
                    value = _raw_value(line, _CWD_MARK)
                    if value:
                        seen_cwds.add(value)
                if len(seen_branches) < _MAX_SEEN:
                    value = _raw_value(line, _BRANCH_MARK)
                    if value:
                        seen_branches.add(value)
    except Exception:
        pass

    head_objs, tail_objs = [], []
    for raw, sink in ((head, head_objs), (tail, tail_objs)):
        for line in raw:
            try:
                obj = json.loads(line)
            except Exception:
                continue          # ligne tronquée ou illisible : on saute
            if isinstance(obj, dict):
                sink.append(obj)

    # Titre et dernier prompt : DERNIÈRE occurrence du fichier -> queue inversée.
    title = prompt = ""
    for obj in reversed(tail_objs):
        if not title:
            title = _clean(obj.get("aiTitle"), TITLE_MAX)
        if not prompt:
            prompt = _clean(obj.get("lastPrompt"), PROMPT_MAX)
        if title and prompt:
            break

    # Les timestamps ISO-8601 UTC se comparent comme des chaînes.
    first_iso = last_iso = ""
    for objs, keep_min in ((head_objs, True), (tail_objs, False)):
        for obj in objs:
            stamp = obj.get("timestamp")
            if not isinstance(stamp, str) or not stamp:
                continue
            if keep_min:
                if not first_iso or stamp < first_iso:
                    first_iso = stamp
            elif stamp > last_iso:
                last_iso = stamp

    picked = _pick_cwd(head_objs, dirslug) or _pick_cwd(tail_objs, dirslug)
    cwd, branch = picked or ("", "")

    sid = os.path.basename(path)[:-6]          # le nom de fichier est l'identité
    if not sid:
        for obj in head_objs:
            candidate = obj.get("sessionId") or obj.get("session_id")
            if isinstance(candidate, str) and candidate:
                sid = candidate
                break

    first_at = _iso_epoch(first_iso)
    last_at = _iso_epoch(last_iso)
    if last_at is None:
        last_at = mtime                        # à défaut, le mtime du fichier
    if first_at is None:
        first_at = last_at

    if not title:
        title = prompt[:TITLE_MAX] if prompt else NO_TITLE

    return {
        "sid": sid,
        "title": title,
        "cwd": cwd,
        "repo": os.path.basename(cwd.rstrip("/")) if cwd else "",
        "ident": _ident(cwd, branch),
        "us": _us(seen_cwds, seen_branches),
        "branch": branch,
        "last_prompt": prompt,
        "first_at": first_at,
        "last_at": last_at,
        "messages": messages,
        "lines": lines,
        "size_kb": int(round(size / 1024.0)),
    }


def _record(path: str, dirslug: str) -> dict | None:
    """_read_record() derrière le cache (chemin, mtime, taille)."""
    try:
        info = os.stat(path)
    except OSError:
        _CACHE.pop(path, None)
        return None
    mtime, size = info.st_mtime_ns, info.st_size
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == mtime and cached[1] == size:
        return cached[2]
    record = _read_record(path, dirslug, size, int(info.st_mtime))
    _CACHE[path] = (mtime, size, record)
    return record


def _transcripts(projects_root: str):
    """(chemin, slug du dossier) de tous les transcripts, dossiers vides inclus."""
    try:
        with os.scandir(projects_root) as dirs:
            slugs = sorted(e.name for e in dirs if e.is_dir())
    except Exception:
        return
    for slug in slugs:
        folder = os.path.join(projects_root, slug)
        try:
            with os.scandir(folder) as files:
                names = sorted(e.name for e in files
                               if e.name.endswith(".jsonl") and e.is_file())
        except Exception:
            continue
        for name in names:
            yield os.path.join(folder, name), slug


# ------------------------------------------------------------- rattachement

def _project_table(config: dict):
    """[(nom, accent, racine développée)] dans l'ordre de la config."""
    table = []
    for entry in config.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        root = _expand(entry.get("root") or "")
        name = entry.get("name")
        if not root or not isinstance(name, str) or not name:
            continue
        table.append((name, entry.get("accent"), root))
    return table


def _match(cwd: str, table) -> int:
    """Index du PREMIER projet dont la racine préfixe le cwd, sinon -1."""
    if not cwd:
        return -1
    path = _expand(cwd)
    for index, (_name, _accent, root) in enumerate(table):
        if path == root or path.startswith(root.rstrip("/") + os.sep):
            return index
    return -1


# ------------------------------------------------------------------- public

def scan(config: dict) -> dict:
    """Historique complet, groupé par projet. Ne lève jamais."""
    started = time.time()
    config = config if isinstance(config, dict) else {}

    table = _project_table(config)
    fallback = config.get("fallback_project") or "AUTRE"
    if not isinstance(fallback, str) or not fallback:
        fallback = "AUTRE"

    projects_root = _expand(config.get("projects_root") or PROJECTS_ROOT_DEFAULT)
    board_root = _expand(config.get("board_root") or BOARD_ROOT_DEFAULT)
    alive = _alive_sids(board_root)

    # Un seau par projet configuré (ordre de la config) + le repli, en dernier.
    order = []
    buckets = {}
    for name, accent, _root in table:
        if name not in buckets:
            buckets[name] = {"project": name, "accent": accent, "convs": []}
            order.append(name)
    if fallback not in buckets:
        buckets[fallback] = {"project": fallback, "accent": None, "convs": []}
    order = [n for n in order if n != fallback] + [fallback]

    seen = {}
    for path, dirslug in _transcripts(projects_root):
        try:
            record = _record(path, dirslug)
        except Exception:
            record = None                      # un transcript ne casse pas le scan
        if not record or not record["sid"]:
            continue
        previous = seen.get(record["sid"])
        if previous is not None and previous["last_at"] >= record["last_at"]:
            continue                           # même session vue deux fois
        seen[record["sid"]] = record

    for record in seen.values():
        index = _match(record["cwd"], table)
        bucket = buckets[table[index][0] if index >= 0 else fallback]
        bucket["convs"].append({
            "sid": record["sid"],
            "title": record["title"],
            "cwd": record["cwd"],
            "repo": record["repo"],
            "ident": record["ident"],
            "us": list(record["us"]),
            "branch": record["branch"],
            "last_prompt": record["last_prompt"],
            "first_at": record["first_at"],
            "last_at": record["last_at"],
            "messages": record["messages"],
            "size_kb": record["size_kb"],
            "alive": record["sid"] in alive,
        })

    groupes = []
    for name in order:
        bucket = buckets[name]
        if not bucket["convs"]:
            continue                           # pas de colonne morte à l'écran
        bucket["convs"].sort(key=lambda c: (-c["last_at"], c["sid"]))
        bucket["count"] = len(bucket["convs"])
        groupes.append({"project": bucket["project"], "accent": bucket["accent"],
                        "count": bucket["count"], "convs": bucket["convs"]})

    return {
        "groupes": groupes,
        "total": sum(g["count"] for g in groupes),
        "scanned_at": int(started),
    }


# --------------------------------------------------------------- vérification

def _demo_config() -> dict:
    """Config de contrôle : ce que scan() reçoit en vrai vient de config.json."""
    path = _expand(os.path.join(BOARD_ROOT_DEFAULT, "config.json"))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict) and loaded.get("projects"):
            return loaded
    except Exception:
        pass
    return {
        "projects": [
            {"name": "PROJET_A", "root": "~/repos/clients/ProjetA", "accent": "#4EC9A0"},
            {"name": "PROJET_B", "root": "~/repos/clients/ProjetB", "accent": "#D8A657"},
        ],
        "fallback_project": "AUTRE",
    }


def main() -> None:
    config = _demo_config()

    cold_start = time.time()
    result = scan(config)
    cold = (time.time() - cold_start) * 1000
    warm_start = time.time()
    scan(config)
    warm = (time.time() - warm_start) * 1000

    print(f"HISTORIQUE — {result['total']} conversations, "
          f"{len(result['groupes'])} groupes")
    print(f"scan à froid {cold:.0f} ms · à chaud (cache) {warm:.1f} ms\n")

    for groupe in result["groupes"]:
        accent = groupe["accent"] or "—"
        print(f"  {groupe['project']}  ({groupe['count']})  accent {accent}")
        for conv in groupe["convs"][:3]:
            when = time.strftime("%d/%m %H:%M", time.localtime(conv["last_at"]))
            flag = " ·VIVANT" if conv["alive"] else ""
            us = ",".join(conv["us"]) or "—"
            print(f"     {when}{flag}  {conv['title'][:46]:46} "
                  f"{conv['repo'][:20]:20} {conv['ident'][:8]:8} "
                  f"US {us[:23]:23} {conv['messages']:4d} msg "
                  f"{conv['size_kb']:5d} ko")
            if conv["last_prompt"]:
                print(f"                 » {conv['last_prompt'][:96]}")
        if groupe["count"] > 3:
            print(f"     … {groupe['count'] - 3} de plus")
        print()


if __name__ == "__main__":
    main()
