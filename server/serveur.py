#!/usr/bin/env python3
"""Serveur de le board.

Lit les fichiers d'état déposés par les capteurs, les fusionne en objets SESSION
(cf. docs/SCHEMA.md), et les pousse au navigateur en SSE. Aucune dépendance
hors bibliothèque standard.

    python3 server/serveur.py            # http://localhost:7777
"""
import json
import os
import re
import sys
import time
import threading
import mimetypes
import shlex
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RACINE = os.path.expanduser("~/.claude/board")
ETATS = os.path.join(RACINE, "state")
STATIQUE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "board")
STATIQUE = os.path.normpath(STATIQUE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wt"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "notify"))

# Les modules confiés aux agents peuvent ne pas être prêts : le serveur doit
# démarrer quand même, en dégradant proprement la fonctionnalité concernée.
try:
    import historique
except Exception:
    historique = None
try:
    import conversation
except Exception:
    conversation = None
try:
    import pullrequests
except Exception:
    pullrequests = None
try:
    import chantier
except Exception:
    chantier = None
try:
    import projets
except Exception:
    projets = None
try:
    from rouvrir import rouvrir as rouvrir_conv
except Exception:
    rouvrir_conv = None
try:
    from rouvrir import ouvrir as ouvrir_conv
except Exception:
    ouvrir_conv = None
try:
    from panes import resolve_pane
except Exception:
    resolve_pane = None
try:
    from notifier import Notifier
except Exception:
    Notifier = None
try:
    import peindre
except Exception:
    peindre = None
try:
    import confiance
except Exception:
    confiance = None

CONFIG_DEFAUT = {
    "projects": [],
    # Peinture des panes du terminal aux couleurs du board.
    "terminal": {"enabled": True, "intensite": 24, "titre": True},
    "fallback_project": "AUTRE",
    "thresholds": {"ctx_warn": 50, "ctx_crit": 70,
                   "silent_after_s": 90, "aging_after_s": 300, "stale_after_s": 900},
    "notify": {"enabled": True, "quiet_hours": [22, 8]},
    "panes": {}, "wt_window": "0", "port": 7777,
}

RE_NOM_PROJET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")

# Teintes proposées aux nouveaux projets. Aucune n'est proche des quatre
# pigments sémantiques (ambre, rouge, ciel, vert) : la couleur d'un projet ne
# doit jamais pouvoir se lire comme un statut.
PALETTE_PROJETS = ["#4EC9A0", "#D8A657", "#9B6DD6", "#4A9EFF", "#E06C75",
                   "#3FB8AF", "#C678DD", "#7EA6E0", "#B5895A", "#8FBF7F"]

PRIORITE = {"blocked": 0, "error": 1, "silent": 2, "review": 3, "working": 4}
GLYPHE = {"blocked": "✋", "error": "✖", "silent": "⋯", "review": "➜", "working": "●"}
LIBELLE = {"blocked": "BLOQUÉ", "error": "ERREUR", "silent": "SILENCE",
           "review": "À RELIRE", "working": "AU TRAVAIL"}


# ---------------------------------------------------------------- utilitaires
def lire_json(chemin, defaut=None):
    """Lecture tolérante : un fichier absent ou à moitié écrit ne casse rien."""
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defaut


def ecrire_json(chemin, obj):
    """Écriture atomique : personne ne doit jamais lire un fichier partiel."""
    try:
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, chemin)
        return True
    except Exception:
        return False


def fusion_config():
    cfg = json.loads(json.dumps(CONFIG_DEFAUT))
    perso = lire_json(os.path.join(RACINE, "config.json"), {}) or {}
    for k, v in perso.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def duree(s):
    """Format compact et stable en largeur : 12s · 4m12 · 18m · 1h04 · 2j03."""
    s = int(max(s, 0))
    if s < 60:
        return "%ds" % s
    m, sec = divmod(s, 60)
    if m < 10:
        return "%dm%02d" % (m, sec)
    if m < 60:
        return "%dm" % m
    h, m = divmod(m, 60)
    if h < 24:
        return "%dh%02d" % (h, m)
    j, h = divmod(h, 24)
    return "%dj%02d" % (j, h)


def court(txt, n):
    if not txt:
        return None
    txt = " ".join(str(txt).split())
    return txt if len(txt) <= n else txt[:n - 1] + "…"


def session_vivante(sid, cwd):
    """La conversation tourne-t-elle encore ? PREUVE DIRECTE, pas une déduction.

    On a d'abord cru pouvoir déduire la mort d'un pane de l'ancienneté de sa
    mesure. C'est faux : la statusline se déclenche sur ÉVÉNEMENT (message,
    changement de mode…), pas sur une horloge. Une session au repos ne la
    rafraîchit pas et paraissait donc morte au bout de cinq minutes.

    Le hook d'enregistrement note le pid du processus claude : son existence
    dans /proc est la seule preuve qui ne mente pas.

    Renvoie True (vivante), False (morte), ou None (on ne sait pas — et alors on
    ne conclut RIEN, on la laisse affichée).
    """
    try:
        f = os.path.join(ETATS, "%s.tty" % sid)
        d = lire_json(f, None)
        pid = (d or {}).get("pid")
        if isinstance(pid, int) and pid > 1:
            return os.path.isdir("/proc/%d" % pid)
    except Exception:
        pass
    return None


# ------------------------------------------------------------- vérité git
# Le capteur ne PEUT PAS connaître ces deux valeurs, et il l'a prouvé :
#   · workspace.project_dir est le dossier de LANCEMENT de Claude Code, pas le
#     dépôt. Une session ouverte depuis ~ donne « <utilisateur> » comme repo.
#   · worktree.branch est souvent absent, et le repli workspace.git_worktree
#     porte un NOM DE DOSSIER, pas une branche — d'où des identifiants de 44
#     caractères au lieu de « failed-to-passed », et une extraction d'US qui ne
#     se déclenchait jamais.
# Seul git sait. Un appel combiné coûte 1 ms, on le met tout de même en cache :
# la boucle SSE tourne une fois par seconde.
_GIT_CACHE = {}
GIT_TTL = 8.0
GIT_TIMEOUT = 1.5


def verite_git(cwd):
    """Renvoie (repo, branche) d'après git, ou (None, None) hors dépôt."""
    if not cwd:
        return None, None
    maintenant = time.time()
    ent = _GIT_CACHE.get(cwd)
    if ent and ent[0] > maintenant:
        return ent[1], ent[2]

    repo = branche = None
    import subprocess

    def git(*args):
        try:
            r = subprocess.run(["git", "-C", cwd] + list(args),
                               capture_output=True, text=True, timeout=GIT_TIMEOUT)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    # DEUX appels séparés, et non un seul combiné : un dépôt sans commit fait
    # échouer `rev-parse HEAD`, ce qui tuait aussi la résolution du dépôt dans
    # un appel groupé. Chaque question doit pouvoir échouer seule.
    commun = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if commun:
        if commun.endswith(os.sep + ".git") or commun.endswith("/.git"):
            commun = os.path.dirname(commun)
        nom = os.path.basename(os.path.normpath(commun))
        if nom and nom not in (".", os.sep):
            repo = nom

    # symbolic-ref plutôt que rev-parse --abbrev-ref : il échoue proprement
    # (sans message) sur une tête détachée ou un dépôt vierge, au lieu de rendre
    # la chaîne « HEAD » qu'il faudrait ensuite filtrer.
    br = git("symbolic-ref", "--short", "-q", "HEAD")
    if br and br != "HEAD":
        branche = br

    # On met AUSSI l'échec en cache : réinterroger git chaque seconde sur un
    # dossier qui n'est pas un dépôt ne servirait à rien.
    _GIT_CACHE[cwd] = (maintenant + GIT_TTL, repo, branche)
    if len(_GIT_CACHE) > 200:
        for k in [k for k, v in _GIT_CACHE.items() if v[0] < maintenant]:
            _GIT_CACHE.pop(k, None)
    return repo, branche


_STATUT_CACHE = {}
STATUT_TTL = 6.0


def etat_git(cwd):
    """{fichiers, ahead, behind} du worktree, ou None hors dépôt.

    « Lequel de mes worktrees a du travail non commité ou non poussé ? » est la
    question qu'un dev à huit worktrees se pose vingt fois par jour. La statusline
    y répond pour le pane courant ; le board y répond pour TOUS à la fois.

    Une passe coûte 9 ms. On ne l'appelle que pour les sessions affichées, et on
    met en cache 6 s : la boucle SSE tourne chaque seconde.
    """
    if not cwd:
        return None
    maintenant = time.time()
    ent = _STATUT_CACHE.get(cwd)
    if ent and ent[0] > maintenant:
        return ent[1]

    res = None
    try:
        import subprocess
        r = subprocess.run(["git", "-C", cwd, "status", "--porcelain=v1", "--branch"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode == 0:
            lignes = [l for l in r.stdout.splitlines() if l]
            if lignes and lignes[0].startswith("##"):
                ahead = behind = 0
                tete = lignes[0][3:]
                if "[" in tete:
                    _, _, suivi = tete.partition("[")
                    for jeton, cle in (("ahead ", "a"), ("behind ", "b")):
                        if jeton in suivi:
                            try:
                                n = int(suivi.split(jeton)[1].split(",")[0]
                                        .split("]")[0].strip())
                            except (ValueError, IndexError):
                                n = 0
                            if cle == "a":
                                ahead = n
                            else:
                                behind = n
                res = {"fichiers": len(lignes) - 1, "ahead": ahead, "behind": behind}
    except Exception:
        pass

    _STATUT_CACHE[cwd] = (maintenant + STATUT_TTL, res)
    if len(_STATUT_CACHE) > 200:
        for k in [k for k, v in _STATUT_CACHE.items() if v[0] < maintenant]:
            _STATUT_CACHE.pop(k, None)
    return res


# ------------------------------------------------------------ rattachement
def projet_de(cwd, cfg):
    """Premier projet dont la racine préfixe le cwd, sinon le repli."""
    if cwd:
        cwd_n = os.path.normpath(cwd)
        for p in cfg.get("projects", []):
            racine = os.path.normpath(os.path.expanduser(p.get("root", "")))
            if racine and (cwd_n == racine or cwd_n.startswith(racine + os.sep)):
                return p.get("name") or "?", p.get("accent")
    return cfg.get("fallback_project", "AUTRE"), None


# Un n° d'US ne se devine pas d'un nombre quelconque : « fix/anomalies-run-9001 »
# porte un n° de run TestRail, pas une US. On n'accepte un nombre que s'il est
# introduit par un préfixe qui, chez nous, désigne vraiment une US.
RE_US_BRANCHE = re.compile(r"^(?:feature|feat|us|story)/(\d{3,6})(?:[-_/]|$)", re.I)
RE_US_DOSSIER = re.compile(r"(?:^|[-_])(?:feat|feature|us|story)[-_]?(\d{3,6})(?:[-_]|$)", re.I)
BRANCHES_MUETTES = {"main", "master", "develop", "dev", "head", "detached", ""}


def us_de(branche, cwd=""):
    """Le n° d'US porté par une branche ou un nom de dossier, ou "".

    UNE SEULE implémentation de la règle du SCHEMA, et c'est celle-ci :
    `identifiant()` l'appelle, et le serveur l'injecte dans `chantier.scan()`.
    Elle avait déjà divergé une fois entre deux modules, et l'onglet Board
    affichait alors un identifiant différent de l'onglet Historique.
    """
    base = os.path.basename(os.path.normpath(cwd or ""))
    m = RE_US_BRANCHE.match(str(branche or "").strip()) or RE_US_DOSSIER.search(base)
    return "#" + m.group(1) if m else ""


def identifiant(cwd, branche, repo=""):
    """Identifiant primaire de la carte : n° d'US, sinon branche courte, sinon repo.

    C'est ce que l'utilisateur tape dans ses commits et reconnaît dans une
    notification tronquée : il ne doit jamais être approximatif.
    """
    base = os.path.basename(os.path.normpath(cwd or ""))
    br = str(branche or "").strip()

    us = us_de(br, cwd)
    if us:
        return us

    seg = br.split("/")[-1]
    if seg.lower() not in BRANCHES_MUETTES:
        return court(seg, 48) or "?"

    # Branche sans information (main, HEAD…) : le repo dit mieux où l'on est.
    return court(repo or base, 48) or "?"


# ------------------------------------------------- projets non encore déclarés
# « Le capteur détecte un nouveau projet et l'ajoute au board » : ce n'était PAS
# vrai. Une conversation dans un dossier non déclaré tombait dans la colonne de
# repli, en gris, pour toujours — et le seul moyen d'en faire un vrai projet était
# un bouton dans la barre du haut, loin de l'endroit où le manque se voit.
# Ici, le serveur remonte le dossier, en devine un nom, et le board le propose en
# tête de la colonne de repli. Il ne DÉCIDE rien : écrire dans config.json reste
# un geste humain, via le formulaire pré-rempli.
_CANDIDATS = {"cle": None, "at": 0.0, "val": []}
CANDIDATS_TTL = 20.0
RE_NON_NOM = re.compile(r"[^A-Za-z0-9_-]+")


def _nom_devine(chemin):
    """Un nom de projet plausible depuis un nom de dossier, ou None.

    Doit passer RE_NOM_PROJET, puisque c'est ce que `creer_projet` exigera : on ne
    propose pas un nom que le formulaire refusera.
    """
    base = os.path.basename(os.path.normpath(chemin or ""))
    # Les accents d'abord, sinon RE_NON_NOM les mange et colle les mots.
    try:
        import unicodedata
        base = "".join(c for c in unicodedata.normalize("NFD", base)
                       if not unicodedata.combining(c))
    except Exception:
        pass
    nom = RE_NON_NOM.sub("-", base).strip("-_")[:32]
    return nom.upper() if RE_NOM_PROJET.match(nom) else None


def candidats_projets(els, cfg):
    """[{name, root, convs, idents}] — les dépôts vus, non déclarés, proposables.

    RÈGLE DE PRUDENCE, et elle compte plus que la détection elle-même : on ne
    propose QUE des dépôts git, et on remonte au dépôt principal plutôt qu'au
    worktree. Trois refus explicites :
      · le répertoire personnel — 23 conversations de l'historique y vivent, et
        « UTILISATEUR » n'est pas un projet ;
      · tout dossier hors dépôt, qui est presque toujours un shell de passage ;
      · un dossier déjà sous une racine déclarée, qui aurait dû matcher avant.
    """
    repli = cfg.get("fallback_project", "AUTRE")
    maison = os.path.realpath(os.path.expanduser("~"))
    cwds = sorted({e.get("cwd") for e in (els or [])
                   if e.get("project") == repli and e.get("cwd")})
    cle = tuple(cwds)
    maintenant = time.time()
    if _CANDIDATS["cle"] == cle and (maintenant - _CANDIDATS["at"]) < CANDIDATS_TTL:
        return _CANDIDATS["val"]

    racines = []
    for p in cfg.get("projects", []):
        r = os.path.realpath(os.path.expanduser(p.get("root") or ""))
        if r:
            racines.append(r)

    vus = {}
    for cwd in cwds:
        commun = None
        if chantier is not None:
            try:
                _, commun = chantier._depot_canonique(cwd)
            except Exception:
                commun = None
        if not commun:
            continue                      # hors dépôt : on ne propose rien
        racine = os.path.realpath(commun)
        if racine == maison or racine == os.sep:
            continue
        if any(racine == r or racine.startswith(r + os.sep) for r in racines):
            continue                      # déjà couvert : aurait dû matcher
        nom = _nom_devine(racine)
        if not nom:
            continue
        court_ = racine
        if court_ == maison or court_.startswith(maison + os.sep):
            court_ = "~" + court_[len(maison):]
        ent = vus.setdefault(racine, {"name": nom, "root": racine,
                                      "root_court": court_,
                                      "convs": 0, "idents": []})
        ent["convs"] += 1

    for e in (els or []):
        for ent in vus.values():
            c = os.path.realpath(e.get("cwd") or "") if e.get("cwd") else ""
            if c and (c == ent["root"] or c.startswith(ent["root"] + os.sep)):
                if e.get("ident") and e["ident"] not in ent["idents"]:
                    ent["idents"].append(e["ident"])

    val = sorted(vus.values(), key=lambda x: (-x["convs"], x["name"]))
    _CANDIDATS.update({"cle": cle, "at": maintenant, "val": val})
    return val


# ------------------------------------------------------------------- l'état
class Board:
    """Source de vérité du serveur : fusionne, dérive, ordonne."""

    def __init__(self):
        self.cfg = fusion_config()
        self.cfg_at = 0
        self.vus = lire_json(os.path.join(RACINE, "seen.json"), {}) or {}
        self.layout = lire_json(os.path.join(RACINE, "layout.json"), {}) or {}
        # sid -> horodatage de mise à la poubelle. Une session archivée quitte
        # le board vivant mais reste dans l'historique : rien n'est détruit.
        self.archive = lire_json(os.path.join(RACINE, "archive.json"), {}) or {}
        # Dernier lot d'sessions calculé, pour que /api/chantier sache quels arbres
        # sont occupés SANS rappeler instantane() : celui-ci notifie et repeint
        # les panes, deux effets de bord qui n'ont rien à faire sur le chemin
        # d'une lecture. `None` signifie « on ne sait pas », jamais « aucun ».
        self.dernieres_sessions = None
        self.dernieres_sessions_at = 0
        self.notifieur = None
        if Notifier is not None:
            try:
                self.notifieur = Notifier(self.cfg, os.path.join(RACINE, "notify.state.json"))
            except Exception:
                self.notifieur = None

    def recharge_config(self):
        """Relit config.json au plus une fois par seconde s'il a changé."""
        try:
            chemin = os.path.join(RACINE, "config.json")
            mt = os.path.getmtime(chemin)
            if mt != self.cfg_at:
                self.cfg = fusion_config()
                self.cfg_at = mt
        except Exception:
            pass

    def _index_pr(self):
        """{(dépôt, branche) -> PR} depuis le cache du module PR.

        La jointure passe par la BRANCHE et non par le n° d'US : sur les données
        réelles, l'US est absente de la plupart des branches (les correctifs n'en
        portent pas) alors que la branche source d'une PR est toujours exacte.
        `scan()` rend son cache instantanément, donc aucun appel réseau ici.
        """
        if pullrequests is None:
            return {}
        try:
            d = pullrequests.scan(self.cfg)
        except Exception:
            return {}
        idx = {}
        for g in d.get("groupes", []):
            for pr in g.get("prs", []):
                cle = (pr.get("repo") or "", pr.get("src") or "")
                if all(cle):
                    idx[cle] = {"id": pr.get("id"), "etat": pr.get("etat"),
                                "commentaires": pr.get("commentaires"),
                                "non_resolus": pr.get("non_resolus"),
                                "url": pr.get("url"), "age_j": pr.get("age_j")}
        return idx

    def sessions(self, maintenant):
        seuils = self.cfg.get("thresholds", {})
        silence_apres = seuils.get("silent_after_s", 90)
        # Deux fraîcheurs, et il ne faut surtout pas les confondre :
        #   · l'ÉVÉNEMENT prouve que l'agent a agi           -> détecte le mutisme
        #   · la MESURE (statusline) prouve que le pane VIT   -> détecte sa fermeture
        # Confondre les deux, c'était afficher « à relire » sur une conversation
        # dont le terminal était fermé depuis trois heures.
        pane_mort = seuils.get("pane_mort_apres_s", 300)
        oubli = seuils.get("oubli_apres_s", 3600)
        vieillit = seuils.get("aging_after_s", 300)
        oublie = seuils.get("stale_after_s", 900)
        warn = seuils.get("ctx_warn", 50)
        crit = seuils.get("ctx_crit", 70)

        # Le .tty compte comme preuve d'existence : c'est le SEUL fichier écrit
        # à l'instant du démarrage. Sans lui, une conversation neuve n'apparaît
        # qu'au premier rendu de la statusline — voire au premier prompt si
        # celle-ci tarde. Avec lui, elle prend sa place dans sa colonne tout de
        # suite, avant même qu'on lui ait parlé.
        sids = set()
        try:
            for f in os.listdir(ETATS):
                for suffixe in (".event.json", ".meas.json", ".tty"):
                    if f.endswith(suffixe):
                        sids.add(f[:-len(suffixe)])
                        break
        except Exception:
            return []

        index_pr = self._index_pr()
        out = []
        for sid in sids:
            ev = lire_json(os.path.join(ETATS, sid + ".event.json"), {}) or {}
            me = lire_json(os.path.join(ETATS, sid + ".meas.json"), {}) or {}
            if sid in self.archive:
                continue                      # à la poubelle : hors du board vivant
            etat = ev.get("state") or "working"
            if etat == "dead":
                # Le pane doit retrouver son fond d'origine, sinon il garde la
                # teinte du board pour toujours.
                if peindre is not None:
                    try:
                        peindre.effacer(sid)
                    except Exception:
                        pass
                continue

            tt = lire_json(os.path.join(ETATS, sid + ".tty"), {}) or {}
            cwd = ev.get("cwd") or me.get("cwd") or tt.get("cwd") or ""
            # Sans aucun événement reçu, on ne SAIT PAS depuis quand : on
            # l'affiche, on ne l'invente pas. Le chrono restera « — » jusqu'au
            # premier hook de cette session.
            neuve = not ev and not me and bool(tt)
            inconnu = not ev.get("state_since") and not ev.get("updated_at")
            if neuve and tt.get("at"):
                # On sait depuis quand elle existe : le hook l'a horodatée.
                depuis_naissance = tt["at"]
            else:
                depuis_naissance = None
            depuis = (ev.get("state_since") or ev.get("updated_at")
                      or depuis_naissance or maintenant)
            if depuis_naissance:
                inconnu = False
            maj_ev = ev.get("updated_at") or 0
            maj_me = me.get("updated_at") or 0
            maj = max(maj_ev, maj_me)
            pane_ferme = False

            # Preuve directe de mort : le processus n'existe plus.
            vivante = session_vivante(sid, cwd)
            if vivante is False:
                if peindre is not None:
                    try:
                        peindre.effacer(sid, cwd)
                    except Exception:
                        pass
                continue

            # Sans pid connu (session ouverte avant le hook), on retombe sur
            # l'ancienneté de la mesure — mais avec un seuil LARGE, parce que ce
            # signal est faible : une session au repos ne rafraîchit rien.
            if vivante is None and maj_me and (maintenant - maj_me) > oubli:
                if peindre is not None:
                    try:
                        peindre.effacer(sid, cwd)
                    except Exception:
                        pass
                continue
            # SILENCE par mutisme de l'agent : dérivé ici, jamais reçu d'un hook.
            # On regarde la fraîcheur de l'ÉVÉNEMENT, pas celle de la mesure.
            elif etat == "working" and maj_ev and (maintenant - maj_ev) > silence_apres:
                etat = "silent"
                depuis = maj_ev
                inconnu = False
                # On sait exactement depuis quand elle s'est taue : c'est la
                # dernière mesure reçue. Le chrono redevient donc légitime,
                # même sans aucun événement de hook pour cette session.
                inconnu = False

            dans_etat = max(0, maintenant - depuis)
            projet, accent = projet_de(cwd, self.cfg)
            # git d'abord — il est le seul à savoir. Le capteur ne sert que de
            # repli quand on est hors dépôt.
            repo_git, branche_git = verite_git(cwd)
            pdir = me.get("project_dir") or ""
            repo = repo_git or (
                os.path.basename(os.path.normpath(pdir)) if pdir else
                os.path.basename(os.path.normpath(cwd)) if cwd else "")
            branche = branche_git or me.get("branch")
            ident = identifiant(cwd, branche, repo)

            ctx = me.get("ctx_pct")
            if isinstance(ctx, (int, float)):
                niveau = "crit" if ctx >= crit else "warn" if ctx >= warn else "ok"
            else:
                ctx, niveau = None, "ok"

            # Ligne de métadonnée : dit l'action attendue, pas l'origine technique.
            if etat == "blocked":
                meta = "attend une autorisation"
            elif etat == "error":
                meta = court(ev.get("reason"), 40) or "arrêt en erreur"
            elif etat == "silent":
                meta = "muet depuis " + duree(maintenant - maj_ev)
            elif etat == "review":
                meta = "main rendue" if ev.get("reason") == "idle_prompt" else "a rendu la main"
            elif neuve:
                meta = "vient de démarrer"
            else:
                meta = ev.get("tool") or "au travail"
            if repo:
                meta += " · " + repo

            cle_vu = "%s:%s:%s" % (sid, etat, int(depuis))
            out.append({
                "sid": sid,
                "project": projet, "accent": accent, "repo": repo, "ident": ident,
                "title": court(me.get("title"), 70) or ident,
                "state": etat, "glyphe": GLYPHE.get(etat, "●"),
                "libelle": LIBELLE.get(etat, etat),
                "since_s": None if inconnu else dans_etat,
                "since": "—" if inconnu else duree(dans_etat),
                "aging": (not inconnu) and etat in ("blocked", "review") and dans_etat > vieillit,
                "stale": (not inconnu) and etat in ("blocked", "review") and dans_etat > oublie,
                "meta": meta,
                "say": court(ev.get("last_say"), 110),
                "ctx_pct": ctx, "ctx_level": niveau,
                "seen": bool(self.vus.get(cle_vu)),
                "cle_vu": cle_vu,
                "pane": self._pane(cwd),
                "model": me.get("model"), "cost_usd": me.get("cost_usd"),
                "age_s": max(0, maintenant - maj) if maj else None,
                # Republiés bruts : le notifieur en a besoin pour rédiger ses toasts
                # sans avoir à réinterpréter la ligne de métadonnée.
                "tool": ev.get("tool"), "reason": ev.get("reason"),
                "branch": branche,
                "cwd": cwd,
                "git": etat_git(cwd),
                "pr": index_pr.get((repo, branche or "")),
            })
        return out

    def _pane(self, cwd):
        if resolve_pane is None:
            return None
        try:
            return resolve_pane(cwd, self.cfg)
        except Exception:
            return None

    def instantane(self):
        self.recharge_config()
        maintenant = int(time.time())
        els = self.sessions(maintenant)
        self.dernieres_sessions = els
        self.dernieres_sessions_at = maintenant

        # Ordre des colonnes : config d'abord, layout.json s'il en impose un autre.
        ordre = [p.get("name") for p in self.cfg.get("projects", [])]
        repli = self.cfg.get("fallback_project", "AUTRE")
        impose = self.layout.get("projects") or []
        if impose:
            ordre = [n for n in impose if n in ordre or n == repli] + \
                    [n for n in ordre if n not in impose]
        presents = {e["project"] for e in els}
        colonnes = [n for n in ordre if n in presents]
        for n in sorted(presents):
            if n not in colonnes and n != repli:
                colonnes.append(n)
        if repli in presents:
            colonnes.append(repli)   # le repli est TOUJOURS en dernier

        accents = {p.get("name"): p.get("accent") for p in self.cfg.get("projects", [])}
        cartes = self.layout.get("cards") or {}
        groupes = []
        for nom in colonnes:
            lot = [e for e in els if e["project"] == nom]
            impose_c = cartes.get(nom) or []
            rang = {sid: i for i, sid in enumerate(impose_c)}
            # Tri déterministe : ordre manuel d'abord, sinon repo puis identifiant.
            lot.sort(key=lambda e: (rang.get(e["sid"], 10_000),
                                    e["repo"] or "", e["ident"] or ""))
            groupes.append({"project": nom, "accent": accents.get(nom),
                            "count": len(lot), "sessions": lot})

        # Bandeau d'attention : la SEULE zone triée par urgence.
        attente = [e for e in els if e["state"] in ("blocked", "error", "silent", "review")
                   and not e["seen"]]
        attente.sort(key=lambda e: (PRIORITE.get(e["state"], 9), -(e["since_s"] or 0)))

        total = len(els)
        mode = "L" if total <= 4 else "M" if total <= 8 else "S" if total <= 16 else "XS"
        compte = lire_json(os.path.join(RACINE, "account.json"), {}) or {}

        if self.notifieur is not None:
            try:
                self.notifieur.evaluate(els, compte, False)
            except Exception:
                pass

        # Le board dit au terminal à quel projet appartient chaque pane.
        # peindre() est idempotent : il n'écrit que sur changement.
        if peindre is not None:
            try:
                peindre.peindre(els, self.cfg)
            except Exception:
                pass

        return {
            "now": maintenant, "total": total, "mode": mode,
            "account": compte, "groupes": groupes,
            "attention": [{k: e[k] for k in
                           ("sid", "ident", "state", "glyphe", "since", "project", "libelle")}
                          for e in attente],
            "capteurs_age_s": min([e["age_s"] for e in els if e["age_s"] is not None],
                                  default=None),
            # Le board doit pouvoir avouer que les alertes sont muettes : une
            # promesse tacite non tenue est aussi trompeuse qu'un faux chiffre.
            "notify_actif": bool((self.cfg.get("notify") or {}).get("enabled")
                                 and self.notifieur is not None),
            # Le board doit savoir LAQUELLE de ses colonnes est le repli : c'est
            # la seule où proposer une adoption a un sens.
            "fallback": repli,
            "candidats": candidats_projets(els, self.cfg),
        }

    def sessions_connues(self, tolerance_s=15):
        """Les sessions, pour un consommateur en LECTURE (l'onglet Chantier).

        Le lot mémorisé par la boucle SSE d'abord : elle en produit un par
        seconde tant qu'un onglet est ouvert, c'est donc gratuit dans le cas
        courant.

        S'il est absent ou périmé, on RECALCULE au lieu de rendre None. C'est
        possible parce que `sessions()` est la fonction pure : les deux effets de
        bord — notifier et repeindre les panes — vivent dans `instantane()`, pas
        ici. Le premier appel de la page, qui arrive avant que le flux ait
        produit quoi que ce soit, est donc servi juste au lieu d'être servi
        dégradé.

        Ne rend None que si le recalcul lui-même échoue. Dans ce cas seulement,
        l'écran dit qu'il ne sait pas — plutôt que d'étiqueter « réserve » un
        arbre où une conversation travaille.
        """
        if (self.dernieres_sessions is not None
                and (int(time.time()) - self.dernieres_sessions_at) <= tolerance_s):
            return self.dernieres_sessions
        try:
            self.recharge_config()
            els = self.sessions(int(time.time()))
        except Exception:
            return None
        # On ne met PAS à jour dernieres_sessions_at : ce lot n'a pas été publié à
        # un client, et la boucle SSE reste la seule à horodater sa vérité.
        return els

    def marquer_vu(self, cle):
        self.vus[cle] = int(time.time())
        # purge : on ne garde pas indéfiniment des clés d'états révolus
        if len(self.vus) > 400:
            for k in sorted(self.vus, key=self.vus.get)[:200]:
                self.vus.pop(k, None)
        ecrire_json(os.path.join(RACINE, "seen.json"), self.vus)

    # ---- poubelle -----------------------------------------------------
    def archiver(self, sid):
        """Sort une session du board vivant. Rien n'est supprimé : son
        transcript reste, et elle reste visible dans l'historique."""
        if not sid:
            return False
        self.archive[str(sid)] = int(time.time())
        return ecrire_json(os.path.join(RACINE, "archive.json"), self.archive)

    def restaurer(self, sid):
        """Remet une session archivée dans les projets en cours."""
        if not sid or str(sid) not in self.archive:
            return False
        self.archive.pop(str(sid), None)
        return ecrire_json(os.path.join(RACINE, "archive.json"), self.archive)

    # ---- création de projet -------------------------------------------
    def creer_projet(self, nom, racine, accent=None):
        """Ajoute un projet à la configuration et pose son lanceur WSL.

        Renvoie (ok, message, detail). Volontairement strict sur le nom : il
        devient un nom de fichier exécutable, il n'y a aucune raison d'y
        tolérer autre chose que des lettres, des chiffres et des tirets.
        """
        nom = (nom or "").strip()
        if not RE_NOM_PROJET.match(nom):
            return False, "nom invalide : lettres, chiffres, tiret et souligné, 2 à 32 caractères", None
        chemin = os.path.abspath(os.path.expanduser((racine or "").strip()))
        if not os.path.isdir(chemin):
            return False, "ce dossier n'existe pas : " + chemin, None

        cfg_path = os.path.join(RACINE, "config.json")
        cfg = lire_json(cfg_path, None)
        if cfg is None:
            return False, "configuration illisible", None
        projets = cfg.setdefault("projects", [])
        if any((p.get("name") or "").lower() == nom.lower() for p in projets):
            return False, "un projet porte déjà ce nom", None
        if any(os.path.abspath(os.path.expanduser(p.get("root") or "")) == chemin
               for p in projets):
            return False, "un projet pointe déjà sur ce dossier", None

        projets.append({"name": nom, "root": chemin,
                        "accent": accent or self._accent_libre(projets)})
        if not ecrire_json(cfg_path, cfg):
            return False, "écriture de la configuration impossible", None
        self.cfg = fusion_config()
        self.cfg_at = 0

        ok, lanceur = self._poser_lanceur(nom, chemin)
        return True, ("projet créé" if ok else
                      "projet créé, mais le lanceur n'a pas pu être écrit"), lanceur

    def _accent_libre(self, projets):
        """Une teinte non prise, et jamais l'un des quatre pigments
        sémantiques : la couleur projet ne doit pas pouvoir être confondue
        avec un statut ou un niveau de contexte."""
        pris = {(p.get("accent") or "").lower() for p in projets}
        for c in PALETTE_PROJETS:
            if c.lower() not in pris:
                return c
        return PALETTE_PROJETS[len(projets) % len(PALETTE_PROJETS)]

    def _poser_lanceur(self, nom, chemin):
        """~/.local/bin/claude-<nom> : ouvre une conversation dans le projet.
        Suit le patron de claude-solo.sh (le dossier est positionné avant
        l'appel à claude).

        Le lanceur pré-accepte la confiance de l'espace de travail (cf.
        server/confiance.py) : un projet déclaré ici est un projet que
        l'utilisateur a lui-même désigné, l'écran de confiance ne lui apprend
        rien. Le rappel à chaque lancement est volontaire : une autre session
        Claude Code peut avoir réécrit ~/.claude.json entre-temps.
        """
        if confiance is not None:
            confiance.marquer(chemin)
        try:
            bindir = os.path.expanduser("~/.local/bin")
            os.makedirs(bindir, exist_ok=True)
            f = os.path.join(bindir, "claude-" + nom.lower())
            outil = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "confiance.py")
            corps = (
                "#!/usr/bin/env bash\n"
                "# Ouvre une conversation Claude Code dans le projet %s.\n"
                "# Généré par le board — régénéré à chaque création.\n"
                "cd %s || exit 1\n"
                "# Confiance de l'espace de travail : accordée d'avance, le\n"
                "# projet est déclaré dans le board. Sans effet si ça échoue.\n"
                "python3 %s %s >/dev/null 2>&1 || true\n"
                "exec claude \"$@\"\n" % (nom, shlex.quote(chemin),
                                            shlex.quote(outil),
                                            shlex.quote(chemin))
            )
            tmp = f + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(corps)
            os.chmod(tmp, 0o755)
            os.replace(tmp, f)
            return True, f
        except Exception:
            return False, None

    def poser_layout(self, data):
        for k in ("projects", "cards"):
            if k in data:
                self.layout[k] = data[k]
        ecrire_json(os.path.join(RACINE, "layout.json"), self.layout)


BOARD = Board()


# -------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Board/1"

    def log_message(self, *a):
        pass   # pas de bruit dans le terminal

    # -- helpers
    def _envoyer(self, code, corps, ctype="application/json; charset=utf-8"):
        if isinstance(corps, (dict, list)):
            corps = json.dumps(corps, ensure_ascii=False).encode("utf-8")
        elif isinstance(corps, str):
            corps = corps.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(corps)
        except Exception:
            pass

    def _statique(self, nom):
        chemin = os.path.normpath(os.path.join(STATIQUE, nom))
        if not chemin.startswith(STATIQUE) or not os.path.isfile(chemin):
            return self._envoyer(404, {"erreur": "introuvable"})
        ctype = mimetypes.guess_type(chemin)[0] or "application/octet-stream"
        if chemin.endswith(".woff2"):
            ctype = "font/woff2"      # non deviné par mimetypes sur cette version
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(chemin, "rb") as f:
            self._envoyer(200, f.read(), ctype)

    def _corps(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # -- routes
    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/", "/index.html"):
            return self._statique("board.html")
        if route == "/flux":
            return self._flux()
        if route == "/api/instantane":
            return self._envoyer(200, BOARD.instantane())
        if route == "/api/historique":
            if historique is None:
                return self._envoyer(200, {"groupes": [], "total": 0,
                                           "erreur": "module historique absent"})
            try:
                h = historique.scan(BOARD.cfg)
                # Décoration : l'historique ne connaît pas la poubelle, c'est
                # le serveur qui sait ce qui peut être remis en cours.
                for g in h.get("groupes", []):
                    for c in g.get("convs", []):
                        c["archived"] = c.get("sid") in BOARD.archive
                        c["restaurable"] = bool(c["archived"])
                return self._envoyer(200, h)
            except Exception as e:
                return self._envoyer(200, {"groupes": [], "total": 0, "erreur": str(e)})
        if route == "/api/conv":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            sid = (q.get("sid") or [""])[0]
            if not sid or "/" in sid or ".." in sid:
                return self._envoyer(400, {"erreur": "identifiant de session invalide"})
            if conversation is None:
                return self._envoyer(200, {"erreur": "module conversation absent"})
            try:
                d = conversation.detail(sid)
            except Exception as e:
                return self._envoyer(200, {"erreur": "lecture impossible : %s" % e})
            # on y joint la session courant : le panneau évite un second appel
            for g in BOARD.instantane().get("groupes", []):
                for e in g.get("sessions", []):
                    if e["sid"] == sid:
                        d["session"] = e
            return self._envoyer(200, d)
        if route == "/api/chantier":
            if chantier is None:
                return self._envoyer(200, {"groupes": [], "total": 0, "compteurs": {},
                                           "degrade": "module Chantier absent"})
            try:
                force = "force=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
                return self._envoyer(200, chantier.scan(
                    BOARD.cfg, sessions=BOARD.sessions_connues(),
                    us_de=us_de, force=force))
            except Exception as e:
                return self._envoyer(200, {"groupes": [], "total": 0, "compteurs": {},
                                           "degrade": "erreur du module Chantier : %s" % e})
        if route == "/api/pr":
            if pullrequests is None:
                return self._envoyer(200, {"groupes": [], "total": 0, "a_traiter": 0,
                                           "degrade": "module Pull Requests absent"})
            try:
                force = "force=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
                return self._envoyer(200, pullrequests.scan(BOARD.cfg, force=force))
            except Exception as e:
                return self._envoyer(200, {"groupes": [], "total": 0, "a_traiter": 0,
                                           "degrade": "erreur du module PR : %s" % e})
        if route == "/api/projets":
            if projets is None:
                return self._envoyer(200, {"projets": [], "a_traiter": 0,
                                           "degrade": "module Projets absent"})
            try:
                force = "force=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
                snap = BOARD.instantane()
                ch = chantier.scan(BOARD.cfg, sessions=BOARD.sessions_connues(),
                                   us_de=us_de, force=force) if chantier else {}
                pr = pullrequests.scan(BOARD.cfg, force=force) if pullrequests else {}
                # Les deux scans sortent de leur propre cache : cette route ne
                # coûte donc rien de plus que les onglets qu'elle résume.
                return self._envoyer(200, projets.synthese(BOARD.cfg, snap, ch, pr))
            except Exception as e:
                return self._envoyer(200, {"projets": [], "a_traiter": 0,
                                           "degrade": "erreur du module Projets : %s" % e})
        # les polices vivent dans board/fonts/ : un seul sous-dossier autorisé,
        # et _statique() vérifie de toute façon qu'on ne sort pas de board/
        if route.startswith("/fonts/") and route.count("/") == 2:
            return self._statique(route[1:])
        if route.startswith("/") and "/" not in route[1:]:
            return self._statique(route[1:])
        return self._envoyer(404, {"erreur": "introuvable"})

    def do_POST(self):
        route = self.path.split("?")[0]
        d = self._corps()
        if route == "/api/vu":
            cle = d.get("cle_vu")
            if cle:
                BOARD.marquer_vu(cle)
            return self._envoyer(200, {"ok": True})
        if route == "/api/layout":
            BOARD.poser_layout(d)
            return self._envoyer(200, {"ok": True})
        if route == "/api/archiver":
            return self._envoyer(200, {"ok": BOARD.archiver(d.get("sid"))})
        if route == "/api/restaurer":
            return self._envoyer(200, {"ok": BOARD.restaurer(d.get("sid"))})
        if route == "/api/projet":
            ok, msg, lanceur = BOARD.creer_projet(d.get("name"), d.get("root"),
                                                  d.get("accent"))
            return self._envoyer(200 if ok else 400,
                                 {"ok": ok, "message": msg, "lanceur": lanceur})
        if route == "/api/rouvrir":
            if rouvrir_conv is None:
                return self._envoyer(200, {"ok": False, "message": "module absent"})
            ok, msg = rouvrir_conv(d.get("sid"), d.get("cwd"), BOARD.cfg)
            return self._envoyer(200, {"ok": ok, "message": msg})
        if route == "/api/ouvrir":
            if ouvrir_conv is None:
                return self._envoyer(200, {"ok": False, "message": "module absent"})
            ok, msg = ouvrir_conv(d.get("cwd"), BOARD.cfg)
            return self._envoyer(200, {"ok": ok, "message": msg})
        if route == "/api/focus":
            return self._envoyer(200, self._focus(d.get("pane")))
        return self._envoyer(404, {"erreur": "introuvable"})

    def _focus(self, pane):
        """Délègue à wt/focus.sh. Un mauvais focus est pire que pas de focus."""
        if pane is None:
            return {"ok": False, "raison": "pane inconnu"}
        script = os.path.normpath(os.path.join(STATIQUE, "..", "wt", "focus.sh"))
        if not os.path.isfile(script):
            return {"ok": False, "raison": "focus.sh absent"}
        import subprocess
        try:
            r = subprocess.run(["bash", script, str(int(pane)),
                                "--window", str(BOARD.cfg.get("wt_window", "0"))],
                               capture_output=True, timeout=3, text=True)
            return {"ok": r.returncode == 0, "raison": (r.stderr or "").strip()[:200]}
        except Exception as e:
            return {"ok": False, "raison": str(e)[:200]}

    def _flux(self):
        """SSE : un instantané complet par seconde. Simple et suffisant ici."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                charge = json.dumps(BOARD.instantane(), ensure_ascii=False)
                self.wfile.write(b"data: " + charge.encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(1.0)
        except Exception:
            return   # le navigateur a fermé l'onglet : sortie silencieuse


def main():
    os.makedirs(ETATS, exist_ok=True)
    port = int(BOARD.cfg.get("port", 7777))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    manque = [n for n, m in (("historique", historique), ("panes", resolve_pane),
                             ("notifier", Notifier),
                             ("pullrequests", pullrequests),
                             ("chantier", chantier),
                             ("peindre", peindre),
                             ("conversation", conversation),
                             ("rouvrir", rouvrir_conv)) if m is None]
    print("board  ->  http://localhost:%d" % port)
    print("état             ->  %s" % RACINE)
    if manque:
        print("modules absents  ->  %s (fonctionnalité dégradée, serveur opérationnel)"
              % ", ".join(manque))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\narrêt.")


if __name__ == "__main__":
    main()
