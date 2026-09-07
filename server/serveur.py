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
    import decouverte
except Exception:
    decouverte = None
try:
    import attente as file_attente
except Exception:
    file_attente = None
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

# La priorité d'attention vit désormais dans `attente.POIDS_CONV`, avec celle
# des arbres et des PR : le bandeau mêle les trois sources et ne peut pas les
# trier sur deux échelles. Cette table-ci ne classait que les conversations, et
# elle n'avait pas de rang à donner à une PR en conflit.
GLYPHE = {"blocked": "✋", "error": "✖", "silent": "⋯", "review": "➜", "working": "●"}
LIBELLE = {"blocked": "BLOQUÉ", "error": "ERREUR", "silent": "SILENCE",
           "review": "À RELIRE", "working": "AU TRAVAIL"}


class EtatsIllisibles(Exception):
    """Le dossier d'états n'a pas pu être énuméré : ON NE SAIT PAS.

    Exception dédiée, et non un `return []`, parce que la nuance qu'elle porte
    est celle sur laquelle tout l'onglet Chantier est bâti : « aucune
    conversation ne tourne » et « je n'ai pas pu regarder » sont deux réponses
    différentes, et une seule des deux autorise le client à masquer un projet.
    Un dossier illisible (droits, montage tombé, suppression pendant la lecture)
    est l'échec le PLUS probable de cette lecture, et c'est précisément celui
    qu'une liste vide déguisait en réponse sûre.

    Elle est distincte d'une panne quelconque pour que les appelants puissent la
    traiter comme un état dégradé attendu — pas comme un bug à faire remonter.
    """


# Une panne qui se répète une fois par seconde (la boucle SSE) ne doit pas
# noyer la sortie : on ne la journalise qu'une fois par minute et par motif.
# Le dépôt n'a pas de journal — les modules parlent sur la sortie standard —
# donc les pannes vont sur stderr, où elles ne polluent pas le flux SSE.
_PLAINTES = {}


def plainte(cle, message, toutes_les_s=60.0):
    """Signale une panne sur stderr, au plus une fois par `toutes_les_s`."""
    maintenant = time.time()
    if maintenant - _PLAINTES.get(cle, 0.0) < toutes_les_s:
        return
    _PLAINTES[cle] = maintenant
    try:
        print("%s  board: %s" % (time.strftime("%H:%M:%S"), message),
              file=sys.stderr, flush=True)
    except Exception:
        pass                       # journaliser ne doit jamais casser l'appelant


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


def neufs_declares(brut):
    """La liste `neufs` de layout.json, lue DÉFENSIVEMENT.

    `layout.json` s'édite à la main, se perd et se restaure : sa valeur n'est
    une liste de noms que par convention. On lit le type promis par le contrat,
    ou on ne lit rien.

    Une CHAÎNE et un DICTIONNAIRE sont les deux formes qui piègent, parce que
    ni l'une ni l'autre ne lève sur un `in` : `"Alpha" in "Alphabet"` est vrai,
    et `"Alpha" in {"Alpha": 1700000000}` aussi — le jour où l'on voudra dater
    les adoptions, ce dictionnaire deviendra tentant. Les deux marqueraient un
    projet à tort, sans une ligne d'erreur.
    """
    if not isinstance(brut, list):
        return []
    return [n for n in brut if isinstance(n, str) and n]


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


def abrege_modele(v):
    """« Opus 5 (1M context) » -> « Opus 5 ». Rend "" si rien d'exploitable.

    La parenthèse porte une vraie information, mais pas au prix d'un tiers de la
    ligne technique : la fiche, elle, affiche le nom entier.
    """
    m = " ".join(str(v or "").split())
    if not m:
        return ""
    return m.split("(")[0].strip()


def sous(v, seuils):
    """Coût formaté au-delà du premier seuil, sinon "".

    UN COMPTEUR QUI TOURNE EN PERMANENCE CULPABILISE AU LIEU D'INFORMER, et
    l'audit l'avait déjà relevé pour le total par colonne (F6). D'où un seuil :
    en dessous, le chiffre ne dit rien qu'on ait envie de savoir — au-dessus,
    c'est un fait qu'on veut voir. La virgule décimale est celle du français,
    comme partout ailleurs à l'écran.
    """
    if not isinstance(v, (int, float)) or v < seuils[0]:
        return ""
    return ("%.1f $" % v).replace(".", ",")


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


# --------------------------------------------------- sous-agents au travail
# Une conversation qui pilote trois agents et une conversation seule avaient
# exactement la même carte : l'information ne vivait que dans la fiche, à un
# clic de trop, alors qu'elle répond mieux que toute autre à « celle-là, je
# peux la laisser tranquille ? ».
#
# ELLE COÛTE CHER, ET C'EST POURQUOI ELLE EST MISE EN CACHE. La produire
# demande de lire le transcript : 20 ms à froid pour 1 098 lignes (2,3 Mo),
# mesuré le 02/09. La boucle SSE tourne une fois par seconde, et un transcript
# de conversation active change à chaque écriture, donc le cache interne de
# `conversation.py` — indexé sur le mtime — y est invalidé en permanence.
# Quatre conversations actives coûteraient 80 ms par seconde, pour un nombre
# qui ne change pas vingt fois par seconde. Même remède que `verite_git` : un
# TTL court, assumé, et le board affiche une valeur vieille de six secondes au
# pire. Aucun agent ne naît et ne meurt dans cet intervalle sans qu'on le
# revoie au tour suivant.
_AGENTS_CACHE = {}
AGENTS_TTL = 6.0
# BUDGET PAR TOUR, ET IL EST LÀ POUR UNE RAISON MESURÉE. Le TTL seul ne suffit
# pas : à son expiration, TOUTES les conversations redeviennent périmées en même
# temps et le tour suivant les relit toutes d'un coup. Mesuré le 02/09 sur ce
# poste : 89 ms et 9,7 Mo pour quatre conversations. L'audit rappelle que le
# board doit tenir à vingt — ce serait un pic de près d'une demi-seconde sur la
# boucle SSE, une fois toutes les six secondes, c'est-à-dire un board qui
# hoquette.
#
# Le budget rend le coût du tour CONSTANT au lieu de proportionnel au nombre de
# conversations : on rafraîchit tant qu'il reste du temps, et les autres gardent
# leur valeur précédente en attendant leur tour. À vingt conversations, toutes
# sont revues en une dizaine de secondes sans qu'aucun tour ne dépasse ~50 ms.
AGENTS_BUDGET_MS = 25.0
_agents_budget = [0.0]


def agents_nouveau_tour():
    """Rouvre le budget de lecture des transcripts. Appelé par instantane()."""
    _agents_budget[0] = 0.0


def agents_au_travail(sid):
    """Nombre de sous-agents en cours pour cette conversation, ou None.

    None signifie « on ne sait pas » — module absent, transcript introuvable ou
    illisible — et le board n'affiche alors rien du tout. Zéro signifie « aucun
    agent », ce qui n'est pas la même chose et ne s'affiche pas davantage.
    """
    if conversation is None:
        return None
    t = time.time()
    ent = _AGENTS_CACHE.get(sid)
    if ent is not None and t - ent[0] < AGENTS_TTL:
        return ent[1]
    # Périmé, mais le tour n'a plus de budget : on rend la valeur précédente
    # plutôt que rien. Une valeur de quelques secondes vaut mieux qu'une puce
    # qui clignote, et le tour suivant reprendra là où celui-ci s'arrête.
    if _agents_budget[0] >= AGENTS_BUDGET_MS:
        return ent[1] if ent is not None else None
    debut = time.time()
    n = None
    try:
        d = conversation.detail(sid, vivante=session_vivante(sid, None))
        if not d.get("erreur"):
            n = d.get("agents_en_cours") or 0
    except Exception:
        n = None
    _agents_budget[0] += (time.time() - debut) * 1000.0
    # Le cache est borné : une session par entrée, et le board en montre au plus
    # quelques dizaines. On purge en bloc plutôt que de tenir un ordre d'accès.
    if len(_AGENTS_CACHE) > 200:
        _AGENTS_CACHE.clear()
    _AGENTS_CACHE[sid] = (t, n)
    return n


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
# Le dépôt du board lui-même : STATIQUE vaut <dépôt>/board, son parent est donc
# la racine du dépôt. Sert uniquement à reconnaître la colonne de l'outil.
DEPOT_OUTIL = os.path.realpath(os.path.dirname(STATIQUE))


def projet_outil(cfg):
    """Le nom du projet déclaré qui pointe sur le dépôt du board, ou None.

    Le board est un OUTIL, pas une mission : sa colonne n'a pas à prendre la
    place d'un projet client dans l'ordre de lecture. On regarde d'abord les
    projets pour lesquels on est payé, on regarde son outil ensuite.

    La reconnaissance se fait sur le CHEMIN, jamais sur le nom. Coder
    « BOARD en dernier » aurait mis un nom de projet en dur dans le moteur —
    exactement ce que ce dépôt s'interdit (cf. README) — et n'aurait pas marché
    pour quelqu'un qui appelle sa colonne autrement. La règle vaut donc pour
    n'importe quel nom, sur n'importe quel poste : est « l'outil » le projet dont
    la racine est le dossier qui contient ce fichier.

    Renvoie None quand le board n'est pas déclaré comme projet — le cas normal
    pour quelqu'un qui ne développe pas le board.
    """
    for p in cfg.get("projects", []):
        racine = (p.get("root") or "").strip()
        if not racine:
            continue
        try:
            if os.path.realpath(os.path.expanduser(racine)) == DEPOT_OUTIL:
                return p.get("name")
        except OSError:
            continue
    return None


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
        # `layout.json` a désormais TROIS écrivains — le glisser-déposer du
        # client (`poser_layout`), l'adoption d'un projet (`_inscrire_neuf`) et
        # la purge des projets servis (`_oublier_neufs_servis`) — et le serveur
        # est un `ThreadingHTTPServer` : la boucle SSE et une requête HTTP
        # tournent dans deux fils. Chacun fait un lire-modifier-écrire sur ce
        # dict, et sans verrou `json.dump` peut sérialiser un dictionnaire en
        # train de changer de taille. Réentrant parce que les deux écrivains de
        # `neufs` prennent le verrou pour lire la liste, puis appellent
        # `_ecrire_neufs`, qui le reprend.
        self._verrou_layout = threading.RLock()
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
        # Seuils du coût : en dessous du premier, le chiffre reste caché ; à
        # partir du second, il prend l'ambre. Réglables comme les autres, avec
        # des valeurs par défaut qui n'obligent personne à toucher sa config.
        seuil_sou = (seuils.get("cout_visible_usd", 10),
                     seuils.get("cout_fort_usd", 25))

        # Le .tty compte comme preuve d'existence : c'est le SEUL fichier écrit
        # à l'instant du démarrage. Sans lui, une conversation neuve n'apparaît
        # qu'au premier rendu de la statusline — voire au premier prompt si
        # celle-ci tarde. Avec lui, elle prend sa place dans sa colonne tout de
        # suite, avant même qu'on lui ait parlé.
        #
        # UN ÉCHEC ICI REMONTE, IL NE REND PAS UNE LISTE VIDE.
        # C'était un `return []`, et c'était le trou par lequel tous les
        # garde-fous du dessous étaient contournés : le dossier d'états devient
        # illisible (droits, montage tombé, suppression) pendant que trois
        # conversations tournent, et TOUT ce qui est bâti là-dessus conclut
        # « aucune conversation » au lieu de « je ne sais pas ». Vérifié :
        # `conversations_inconnues: false`, `conversations: 0` sur chaque
        # groupe, et l'onglet Chantier masquait alors la totalité des projets en
        # affirmant qu'aucun travail n'était en cours. Un écran faux et sûr de
        # lui est pire qu'un écran qui avoue.
        #
        # La distinction ne peut pas être rétablie plus bas : à partir d'ici la
        # liste vide est indiscernable d'un poste au repos.
        sids = set()
        try:
            for f in os.listdir(ETATS):
                for suffixe in (".event.json", ".meas.json", ".tty"):
                    if f.endswith(suffixe):
                        sids.add(f[:-len(suffixe)])
                        break
        except OSError as e:
            raise EtatsIllisibles("%s : %s" % (ETATS, e))

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

            # ── LA LIGNE TECHNIQUE DIT CE QU'ON IGNORE ────────────────────────
            # Elle disait « a rendu la main · ProjetA » sous une pastille
            # « À RELIRE » et dans une colonne « PROJET_A » : une ligne entière
            # pour répéter ses deux voisins. Pendant ce temps le modèle et le
            # coût — 38,44 $ sur une conversation, mesuré le 02/09 — n'étaient
            # nulle part, alors qu'ils sont dans la charge utile depuis le
            # premier jour.
            #
            # Le premier segment ne survit donc que là où il APPREND quelque
            # chose : l'outil en cours quand ça travaille, la raison quand ça
            # casse. Pour `blocked`, `review` et `silent`, la pastille dit déjà
            # le mot et le chrono dit déjà la durée.
            # CE QUE LA CONVERSATION ATTEND, dans son propre champ. C'était le
            # premier segment de `meta`, et il en sort pour ne plus répéter la
            # pastille sur la carte — mais la FICHE en a besoin, elle : sa
            # section « en ce moment » n'a pas de pastille sous les yeux quand
            # aucun outil n'est en vol. Une seule règle, ici, deux lecteurs qui
            # n'en font pas le même usage.
            if etat == "blocked":
                attente = "attend une autorisation"
            elif etat == "error":
                attente = court(ev.get("reason"), 40) or "arrêt en erreur"
            elif etat == "silent":
                attente = "muet depuis " + duree(maintenant - maj_ev)
            elif etat == "review":
                attente = ("main rendue" if ev.get("reason") == "idle_prompt"
                           else "a rendu la main")
            elif neuve:
                attente = "vient de démarrer"
            else:
                attente = ev.get("tool") or "au travail"

            bouts = []
            if etat == "error":
                bouts.append(court(ev.get("reason"), 40) or "arrêt en erreur")
            elif neuve:
                bouts.append("vient de démarrer")
            elif etat == "working":
                bouts.append(ev.get("tool") or "au travail")
            # Le dépôt ne se répète que s'il n'est PAS le nom de la colonne.
            # « ProjetA » sous la colonne PROJET_A n'apprend rien ;
            # « ProjetB.Automatisation » sous la colonne PROJETB, si.
            if repo and repo.upper() != (projet or "").upper():
                bouts.append(repo)
            modele = abrege_modele(me.get("model"))
            if modele:
                bouts.append(modele)
            meta = " · ".join(bouts)
            # Le coût voyage à part, pas dans `meta` : il est le seul segment de
            # cette ligne qui peut prendre un pigment, et une chaîne unique ne
            # se teinte pas par morceaux. Le serveur décide de son texte ET de
            # son seuil ; le board ne fait que le poser, comme partout ailleurs.
            cout = sous(me.get("cost_usd"), seuil_sou)
            cout_fort = bool(cout and isinstance(me.get("cost_usd"), (int, float))
                             and me["cost_usd"] >= seuil_sou[1])

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
                "meta": meta, "attente": attente,
                "cout": cout, "cout_fort": cout_fort,
                # Nombre de sous-agents au travail, ou None si on ne sait pas.
                # Voir agents_au_travail() pour le cache et son prix.
                "agents": agents_au_travail(sid),
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

    def _colonnes(self, presents):
        """(colonnes, repli) — l'ordre des colonnes du board.

        Extrait d'`instantane()` parce que l'instantané AVEUGLE (dossier d'états
        illisible) doit montrer exactement les mêmes colonnes, vides : c'est ce
        qui distingue « je ne sais pas » d'un board effacé. Deux copies de cette
        règle auraient divergé au premier réglage de layout.json.
        """
        # Ordre des colonnes : config d'abord, layout.json s'il en impose un autre.
        ordre = [p.get("name") for p in self.cfg.get("projects", [])]
        repli = self.cfg.get("fallback_project", "AUTRE")
        # La colonne de l'outil passe DERRIÈRE les projets de mission, quel que
        # soit son rang dans la configuration. Elle reste néanmoins devant le
        # repli, qui est absolument dernier : « AUTRE » n'est pas un projet, c'est
        # ce qui n'a pas trouvé de projet.
        # Cette règle s'applique AVANT layout.json, donc un ordre posé à la main
        # par glisser-déposer continue de gagner — un geste de l'utilisateur
        # passe toujours devant une règle du moteur.
        outil = projet_outil(self.cfg)
        if outil and outil in ordre:
            ordre = [n for n in ordre if n != outil] + [outil]
        impose = self.layout.get("projects") or []
        if impose:
            ordre = [n for n in impose if n in ordre or n == repli] + \
                    [n for n in ordre if n not in impose]
        # TOUS LES PROJETS DÉCLARÉS ONT LEUR COLONNE, occupée ou non.
        # Avant, une colonne n'existait qu'à partir de sa première conversation :
        # un projet qu'on vient de créer était donc invisible sur l'écran censé
        # dire où on en est. La colonne vide n'est pas du vide, elle dit « ce
        # projet est prêt » et porte le nom de son lanceur.
        # Le REPLI reste conditionnel : une colonne « AUTRE » vide ne dirait
        # rien, puisqu'on ne déclare pas ce seau, on y tombe.
        colonnes = [n for n in ordre if n != repli]
        for n in sorted(presents):
            if n not in colonnes and n != repli:
                colonnes.append(n)
        if repli in presents:
            colonnes.append(repli)   # le repli est TOUJOURS en dernier
        return colonnes, repli

    def instantane(self):
        self.recharge_config()
        maintenant = int(time.time())
        agents_nouveau_tour()          # cf. AGENTS_BUDGET_MS
        try:
            els = self.sessions(maintenant)
        except EtatsIllisibles as e:
            # ON NE SAIT PAS, ET ON LE DIT — sans casser l'écran pour autant.
            # `_flux()` avale toute exception et referme la connexion SSE : la
            # laisser remonter d'ici, c'est un board qui se fige, ce qui est le
            # remède pire que le mal. On rend donc un instantané AVEUGLE, qui
            # garde ses colonnes et avoue son trou, et surtout on laisse
            # `dernieres_sessions` à None : c'est ce None que `sessions_connues`
            # puis `chantier.scan` transforment en `conversations: null` plutôt
            # qu'en zéro.
            plainte("etats", "dossier d'états illisible (%s) — instantané "
                             "aveugle servi, aucune conversation n'est niée" % e)
            self.dernieres_sessions = None
            self.dernieres_sessions_at = 0
            return self._instantane_aveugle(maintenant, str(e))
        self.dernieres_sessions = els
        self.dernieres_sessions_at = maintenant

        # AVANT de publier les groupes : ce lot est une observation vivante, il
        # peut retirer un projet de `neufs`, et le drapeau publié doit être
        # celui d'APRÈS le retrait. Publier puis purger ferait afficher une fois
        # de plus un projet qu'on vient de déclarer servi.
        self._oublier_neufs_servis(els)
        neufs = set(self.neufs())

        presents = {e["project"] for e in els}
        colonnes, repli = self._colonnes(presents)

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
            # `jamais_servi` TOUJOURS PRÉSENT, et booléen : « pas neuf » est une
            # valeur du contrat, jamais l'absence d'une clé. Le serveur publie
            # le FAIT ; la décision de masquer appartient au client.
            groupes.append({"project": nom, "accent": accents.get(nom),
                            "count": len(lot), "sessions": lot,
                            "jamais_servi": nom in neufs})

        # ── LE BANDEAU D'ATTENTION : LA SEULE ZONE TRIÉE PAR URGENCE ─────────
        #
        # Il était une file de CONVERSATIONS, et c'était sa limite : une PR en
        # conflit ou un arbre non commité n'y entrait pas, donc n'attendait
        # nulle part. L'onglet « Projets » portait cet axe et il a été fusionné
        # ici le 02/09 — il affichait une ligne pour deux projets, déjà dite
        # deux fois ailleurs. Voir server/attente.py pour l'histoire complète.
        #
        # DEUX SOURCES, UNE SEULE ÉCHELLE. Les poids viennent tous de
        # `attente.py` — y compris pour les conversations, via POIDS_CONV.
        # Trier deux listes avec deux échelles et les concaténer aurait donné un
        # ordre qui n'a de sens dans aucune des deux.
        conv = [e for e in els if e["state"] in ("blocked", "error", "silent", "review")
                and not e["seen"]]
        poids_conv = (file_attente.POIDS_CONV if file_attente is not None
                      else {"blocked": 0, "error": 1, "silent": 2, "review": 3})

        # Ce que les conversations ne savent pas dire. Les deux relevés sont lus
        # DANS LEUR CACHE, jamais déclenchés : cette fonction tourne une fois
        # par seconde, et ni un balayage git ni un réveil de `az` n'ont leur
        # place sur ce chemin. Cache absent ou froid -> on ne prétend rien.
        hors_conv = []
        degrade_attente = None
        if file_attente is not None:
            try:
                ch = chantier.dernier() if chantier is not None else None
                pr = pullrequests.dernier(self.cfg) if pullrequests is not None else None
                if ch or pr:
                    hors_conv = file_attente.attente(self.cfg, ch, pr)
                    degrade_attente = file_attente.degrade(ch, pr)
            except Exception:
                hors_conv = []

        # UN SEUL TRI, sur les deux sources réunies. Deux listes triées chacune
        # de son côté puis concaténées auraient donné un ordre qui n'a de sens
        # dans aucune des deux : une PR en conflit (2) doit passer devant une
        # conversation muette (6), pas derrière toutes les conversations.
        # À poids égal — jamais entre deux sources, mais possible entre deux
        # conversations de même état — la plus ancienne passe devant.
        bandeau = [(poids_conv.get(e["state"], 99), -(e["since_s"] or 0),
                    dict({k: e[k] for k in
                          ("sid", "ident", "title", "repo", "state", "glyphe",
                           "since", "project", "libelle")}, genre="conv"))
                   for e in conv]
        bandeau += [(i["poids"], 0, dict(i, genre="item")) for i in hors_conv]
        bandeau.sort(key=lambda t: (t[0], t[1]))

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
            # `title` et `repo` REJOIGNENT le bandeau : sans eux, le board ne
            # pouvait y appliquer la règle « le titre porte, l'identifiant
            # suit » et affichait l'identifiant seul. Mesuré le 02/09 : les deux
            # conversations à relire portaient le même — « ProjetA a rendu la
            # main » deux fois, pour deux travaux différents. Le serveur ne
            # tranche pas quel libellé gagne : c'est `nomLisible()` côté board,
            # une seule implémentation pour la carte et pour le bandeau.
            #
            # `genre` dit au board comment lire l'entrée et ce qu'un clic doit
            # faire. Deux valeurs, et aucune n'est devinable depuis les autres
            # champs : une conversation ouvre sa fiche, un item ouvre son URL ou
            # l'onglet qui le détaille.
            "attention": [e for _, _, e in bandeau],
            "attente_degrade": degrade_attente,
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
            # Toujours présent, pour que « je n'ai pas pu regarder » soit une
            # valeur du contrat et non l'absence d'une clé. `null` = on a
            # regardé ; une chaîne = le motif de l'aveuglement.
            "sessions_indisponibles": None,
        }

    def _instantane_aveugle(self, maintenant, raison):
        """L'instantané quand le dossier d'états n'a pas pu être lu.

        Trois refus, et ce sont eux qui font la valeur de cette fonction :

          · on ne rend pas `total: 0` — on ne sait pas combien il y en a, et un
            zéro serait exactement le mensonge qu'on vient de retirer d'un cran
            plus bas ;
          · on ne NOTIFIE pas et on ne REPEINT pas. `notifier.evaluate([])`
            verrait toutes les conversations disparues d'un coup, et
            `peindre([])` rendrait leur fond d'origine à des panes bien vivants.
            Deux effets de bord irréversibles déclenchés par une ignorance ;
          · on ne touche pas au bandeau d'attention : ses items hors
            conversation viennent des caches Chantier et PR, qui n'ont rien à
            voir avec ce dossier, mais les mêler à un instantané aveugle
            laisserait croire que la liste est complète.

        Les colonnes, elles, restent : un board vide et muet ressemble à un
        board au repos. Un board avec ses colonnes et un motif d'aveuglement
        ressemble à ce qu'il est.
        """
        colonnes, repli = self._colonnes(set())
        accents = {p.get("name"): p.get("accent") for p in self.cfg.get("projects", [])}
        # `neufs` est LU mais pas purgé : un dossier d'états illisible ne prouve
        # pas qu'aucune conversation ne tourne. Le drapeau reste publié, comme
        # les colonnes — c'est ce qui empêche le filtre du client de faire
        # disparaître, pendant l'aveuglement, le projet qu'on vient d'adopter.
        neufs = set(self.neufs())
        return {
            "now": maintenant, "total": None, "mode": "L",
            "account": lire_json(os.path.join(RACINE, "account.json"), {}) or {},
            "groupes": [{"project": n, "accent": accents.get(n),
                         "count": 0, "sessions": [],
                         "jamais_servi": n in neufs} for n in colonnes],
            "attention": [], "attente_degrade": None,
            "capteurs_age_s": None,
            "notify_actif": bool((self.cfg.get("notify") or {}).get("enabled")
                                 and self.notifieur is not None),
            "fallback": repli,
            "candidats": [],
            "sessions_indisponibles": raison,
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

        Ce `except` n'était pas un filet théorique : l'échec le plus probable de
        `sessions()` — le dossier d'états illisible — ne l'atteignait JAMAIS,
        parce qu'il était converti en liste vide un cran plus bas. Le None
        promis ici n'était donc pas rendu, et l'onglet Chantier recevait « aucune
        conversation » là où il aurait dû recevoir « on ne sait pas ». C'est
        `EtatsIllisibles` qui rend cette promesse tenable.
        """
        if (self.dernieres_sessions is not None
                and (int(time.time()) - self.dernieres_sessions_at) <= tolerance_s):
            return self.dernieres_sessions
        try:
            self.recharge_config()
            els = self.sessions(int(time.time()))
        except Exception:
            return None
        # CE CHEMIN AUSSI RETIRE LES PROJETS SERVIS, et il le faut : c'est le
        # seul qui observe des conversations vivantes quand aucun onglet du
        # board n'est ouvert pour alimenter la boucle SSE — l'onglet Chantier
        # seul, ou la sonde de pastille au chargement de la page. La purge est
        # posée sur les DEUX chemins d'observation, pas sur un seul.
        # Le chemin du dessus n'en a pas besoin : `instantane()` a déjà purgé ce
        # lot-là avant de le mémoriser, il y a moins de `tolerance_s`.
        self._oublier_neufs_servis(els)
        # On ne met PAS à jour dernieres_sessions_at : ce lot n'a pas été publié à
        # un client, et la boucle SSE reste la seule à horodater sa vérité.
        return els

    # ---- les projets NEUFS ---------------------------------------------
    #
    # LE DÉFAUT CORRIGÉ. Le filtre « avec conversation » de l'onglet Chantier
    # masque les projets sans conversation en cours. On découvrait un dépôt du
    # poste, on l'adoptait — et il disparaissait aussitôt, puisqu'il n'a
    # évidemment aucune conversation. Or c'est sa colonne vide qui porte le nom
    # de son lanceur `claude-<projet>` : le filtre retirait la porte d'entrée du
    # projet qu'on venait d'ajouter.
    #
    # LA RÈGLE. Un projet adopté reste visible jusqu'à ce qu'une conversation
    # Claude y ait tourné au moins une fois. Ensuite il rejoint le lot commun et
    # redevient masquable, DÉFINITIVEMENT : « a déjà servi » est irréversible.
    # Sans cette irréversibilité, un projet ressortirait du filtre chaque fois
    # qu'on ferme sa dernière conversation — le contraire du besoin.
    #
    # POURQUOI C'EST PERSISTÉ alors que le dépôt refuse de mémoriser les
    # préférences d'affichage : ce n'en est pas une. `avecConv` (l'interrupteur)
    # reste volontairement non persisté ; `neufs` est un FAIT DE CYCLE DE VIE,
    # au même titre que `seen.json` ou `archive.json`, et il doit survivre au
    # redémarrage du serveur — sinon le projet redevient masquable à la première
    # relance et le défaut revient tel quel.
    #
    # DEUX NOMS FRANCHEMENT DIFFÉRENTS pour deux choses différentes : `neufs`
    # est la liste persistée, `jamais_servi` le booléen publié par groupe. Un
    # `g.neufs` côté client rendrait `undefined`, donc faux, donc le bug qu'on
    # corrige — en silence. Le dépôt s'est déjà fait prendre une fois à ce jeu
    # (`convs` contre `conversations`).
    def neufs(self):
        """Les projets adoptés qu'aucune conversation n'a encore vus."""
        return neufs_declares(self.layout.get("neufs"))

    def _ecrire_neufs(self, liste):
        with self._verrou_layout:
            self.layout["neufs"] = liste
            return ecrire_json(os.path.join(RACINE, "layout.json"), self.layout)

    def _projets_configures(self):
        """Les noms de projets déclarés — ou None si `config.json` est illisible.

        Le None n'est pas décoratif : c'est lui qui empêche la purge des
        fantômes de vider la liste. `fusion_config()` ne peut pas le dire — elle
        retombe sur `CONFIG_DEFAUT`, dont `projects` est vide, et un
        `config.json` momentanément illisible (droits, montage tombé, écriture
        en cours surprise hors du `os.replace`) ferait alors passer TOUS les
        projets neufs pour des fantômes. On interroge donc le fichier lui-même
        avant de conclure quoi que ce soit sur ce que la configuration contient.

        Les NOMS, eux, viennent de `self.cfg` et non du fichier brut : c'est la
        configuration fusionnée qui fait foi partout ailleurs dans ce serveur,
        et deux lectures divergentes finiraient par se contredire.
        """
        if lire_json(os.path.join(RACINE, "config.json"), None) is None:
            return None
        noms = {p.get("name") for p in (self.cfg.get("projects") or [])
                if isinstance(p, dict) and p.get("name")}
        return noms

    def _inscrire_neuf(self, nom):
        """Inscrit un projet fraîchement adopté. SEUL `creer_projet` appelle ici.

        Une seule fois : `neufs` n'est pas une liste de faveurs qui grandit
        indéfiniment. Un nom déjà présent — layout édité à la main, config
        remise à zéro, adoption rejouée — y reste UNE fois.
        """
        with self._verrou_layout:
            courants = self.neufs()
            if nom in courants:
                return True
            return self._ecrire_neufs(courants + [nom])

    def _oublier_neufs_servis(self, els):
        """Retire de `neufs` tout projet où une conversation vient d'être VUE.

        Auto-guérissant : aucun geste de l'utilisateur, aucune commande de
        nettoyage, rien à refaire au prochain démarrage. Appelée sur les DEUX
        chemins qui produisent une observation vivante — `instantane()` (la
        boucle SSE) et `sessions_connues()` (l'onglet Chantier) —, et sur eux
        seuls : une ignorance ne doit jamais déclencher un retrait irréversible.

        `els` est TOUJOURS un lot réellement observé. L'instantané AVEUGLE
        (dossier d'états illisible) ne passe pas ici : il ne prouve pas qu'aucune
        conversation ne tourne, il prouve qu'on n'a pas pu regarder. Déclencher
        sur une ignorance un retrait définitif, ce serait perdre pour de bon la
        visibilité d'un projet qu'on vient d'adopter.

        DEUX TROUS ASSUMÉS, et ils vont tous deux dans le sens sûr — le projet
        reste neuf, donc reste VISIBLE, ce qui est le besoin :

          · BOARD FERMÉ. On n'interroge que les conversations vivantes, jamais
            l'historique des transcripts. Adopter un projet, y travailler board
            fermé, et rouvrir le board plus d'une heure après la fin de la
            conversation (`thresholds.oubli_apres_s`, qui la fait sortir de
            `sessions()`) laisse le projet « neuf ». Aller lire les transcripts
            coûterait un couplage plus cher que le trou qu'il bouche.
          · CONVERSATIONS `dead`. Elles sont écartées en amont par `sessions()`
            — qui rend leur fond d'origine aux panes et passe au suivant — et
            n'atteignent donc pas cette fonction : une conversation qui a tourné
            puis s'est proprement terminée avant le premier passage ici ne
            retire rien.

        LES FANTÔMES sont purgés dans la foulée : un nom que la configuration ne
        porte plus ne désigne aucune colonne, ne peut plus rien rendre visible,
        et resterait sinon dans `layout.json` pour toujours. Mais UNIQUEMENT
        quand la configuration a été lue avec succès (cf. `_projets_configures`).
        """
        # Hors verrou : le cas courant est une liste vide, et il ne doit rien
        # coûter à une boucle qui repasse ici une fois par seconde.
        if not self.neufs():
            return                       # rien à retirer : pas une écriture
        # `state` est déjà filtré par `sessions()`, qui écarte les `dead` et les
        # panes morts. On ne le refait pas : ce lot EST la liste des vivantes.
        servis = {e.get("project") for e in els if isinstance(e, dict)}
        with self._verrou_layout:
            courants = self.neufs()      # relue SOUS le verrou : elle a pu bouger
            # LA CONFIGURATION SE LIT SOUS LE MÊME VERROU QUE LA LISTE, sinon une
            # adoption qui se glisse entre les deux lectures inscrit un projet
            # que la configuration d'avant ne connaît pas : il serait purgé comme
            # fantôme, et l'adoption annulée en silence.
            connus = self._projets_configures()
            restants = [n for n in courants if n not in servis]
            if connus is not None:
                restants = [n for n in restants if n in connus]
            if restants != courants:
                self._ecrire_neufs(restants)

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

        # LE PROJET EST NEUF, et il le reste jusqu'à sa première conversation.
        # Inscrit APRÈS l'écriture de la configuration : une adoption refusée
        # (nom pris, dossier déjà rattaché, configuration illisible) sort plus
        # haut et n'inscrit personne. Inscrit AVANT le lanceur, dont l'échec ne
        # remet pas le projet en cause — il existe, sa colonne doit rester
        # visible, et c'est même à ce moment-là qu'on en a le plus besoin.
        # Un échec d'écriture de `layout.json` ne fait pas échouer l'adoption :
        # le projet est créé, il sera simplement masquable tout de suite.
        self._inscrire_neuf(nom)

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
        """Écrit dans `layout.json` les seules clés que le client a le droit de poser.

        La liste blanche n'est pas une précaution de style : le corps de la
        requête vient du navigateur, `layout.json` est relu à chaque démarrage,
        et une clé inconnue y resterait pour toujours.

        `NEUFS N'EN FAIT PAS PARTIE`, et c'est délibéré. Le cycle de vie d'un
        projet neuf est piloté de bout en bout par le serveur : `creer_projet`
        inscrit, l'observation d'une conversation retire (cf.
        `_oublier_neufs_servis`). Aucun appelant côté client n'écrit cette clé.
        L'accepter n'ouvrirait qu'une chose : le droit, pour un `curl`, de
        réinscrire comme neuf un projet qui a déjà servi, et de le faire
        ressortir du filtre à volonté. Une capacité dont personne n'a besoin et
        qui ne sert qu'à défaire un invariant se supprime, elle ne se borde pas.
        Un `neufs` posté est donc ignoré comme n'importe quelle clé inconnue.
        """
        with self._verrou_layout:
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
            # La session courante est cherchée AVANT la lecture du transcript,
            # et non après : sa présence dans l'instantané est une preuve de
            # vivacité, dont `conversation.detail` a besoin pour statuer sur les
            # agents — un agent lancé et jamais notifié est « au travail » dans
            # une conversation qui tourne, et « jamais revenu » dans une
            # conversation éteinte.
            courante = None
            for g in BOARD.instantane().get("groupes", []):
                for e in g.get("sessions", []):
                    if e["sid"] == sid:
                        courante = e
            # DEUX PREUVES, ET LE « ON NE SAIT PAS » EST CONSERVÉ. /proc tranche
            # seul quand il répond ; il ne répond pas pour une conversation
            # ancienne, dont le fichier .tty a disparu — et l'absence de cette
            # conversation de l'instantané dit alors que le board ne la suit
            # plus. Si les deux preuves manquent, `vivante` reste None et rien
            # n'est conclu : c'est la doctrine de `session_vivante`, respectée
            # jusqu'ici.
            viv = session_vivante(sid, None)
            if viv is None and courante is None:
                viv = False
            try:
                d = conversation.detail(sid, vivante=viv)
            except Exception as e:
                return self._envoyer(200, {"erreur": "lecture impossible : %s" % e})
            if courante is not None:
                d["session"] = courante      # le panneau évite un second appel
            return self._envoyer(200, d)
        if route == "/api/chantier":
            if chantier is None:
                return self._envoyer(200, {"groupes": [], "total": 0, "compteurs": {},
                                           "degrade": "module Chantier absent"})
            try:
                force = "force=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
                # L'ORDRE DES DEUX LIGNES EST UN INVARIANT, et c'est pour cela
                # qu'elles ne sont pas des arguments en ligne :
                # `sessions_connues()` est une observation vivante, elle peut
                # retirer un projet de `neufs`. Lire la liste avant l'appel
                # publierait un `jamais_servi` d'avant le retrait.
                sessions = BOARD.sessions_connues()
                neufs = BOARD.neufs()
                return self._envoyer(200, chantier.scan(
                    BOARD.cfg, sessions=sessions, us_de=us_de, force=force,
                    neufs=neufs))
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
        if route == "/api/decouverte":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            # L'AUTORISATION SE VÉRIFIE ICI, ET NULLE PART AILLEURS. La
            # découverte est la seule lecture du serveur qui sorte des projets
            # déclarés — elle parcourt tout `~` —, et l'utilisateur a demandé à
            # en garder la main. Une garde côté client ne serait qu'une
            # convention : un onglet resté ouvert sur une ancienne version du
            # JS, un `curl`, un lien partagé la contourneraient. Le refus est
            # donc rendu AVANT le moindre appel au module : pas une lecture de
            # dossier n'a lieu sans `autorise=1`.
            if (q.get("autorise") or [""])[0] != "1":
                return self._envoyer(403, {
                    "erreur": "découverte non autorisée : appelez "
                              "/api/decouverte?autorise=1"})
            if decouverte is None:
                return self._envoyer(200, {"candidats": [], "scannes": 0,
                                           "illisibles": 0, "duree_ms": 0,
                                           "degrade": "module Découverte absent"})
            try:
                return self._envoyer(200, decouverte.scan(BOARD.cfg))
            except Exception as e:
                return self._envoyer(200, {
                    "candidats": [], "scannes": 0, "illisibles": 0, "duree_ms": 0,
                    "degrade": "erreur du module Découverte : %s" % e})
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
                             ("attente", file_attente),
                             ("chantier", chantier),
                             ("decouverte", decouverte),
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
