#!/usr/bin/env python3
"""Module « Pull Requests » de le board.

Interface publique unique :

    from pullrequests import scan
    scan(config: dict, force: bool = False) -> dict

Le module répond au contrat de `docs/SCHEMA.md` : bibliothèque standard
seulement, écritures atomiques, aucune exception qui remonte à l'appelant.

Le point difficile n'est pas Azure DevOps, c'est le TEMPS. Un balayage complet
coûte deux appels `az` par dépôt et par PR ; sur cette machine c'est une bonne
dizaine de secondes. Personne n'accepte d'attendre ça au clic sur un onglet.
Donc `scan()` ne parle JAMAIS à Azure DevOps : il rend le cache disque tel
quel — même périmé — et confie le rafraîchissement à un thread démon. Le board
apprend l'âge de ce qu'il affiche (`age_s`) et qu'un travail est en cours
(`rafraichit`), et se redemande la donnée plus tard. C'est tout.
"""

import calendar
import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

# ---------------------------------------------------------------- constantes

BOARD_ROOT_DEFAULT = "~/.claude/board"
CACHE_NAME = "pr.cache.json"

TTL_DEFAUT_S = 180          # au-delà, la donnée est rafraîchie en tâche de fond
BACKOFF_S = 30              # après un échec, on laisse respirer avant de retenter
TIMEOUT_LISTE_S = 120       # `az repos pr list`
TIMEOUT_FILS_S = 90         # `az devops invoke ... pullRequestThreads`
TIMEOUT_COMPTE_S = 30       # `az account show`
TIMEOUT_GIT_S = 5           # `git remote get-url origin`
POOL_MAX = 4                # jamais plus de 4 `az` en vol
PROFONDEUR_MAX = 2          # racine de projet -> [groupe] -> dépôt

SRC_MAX = 40                # longueur de branche affichable
TITRE_MAX = 160

# N° d'US. Mêmes motifs que le serveur et l'historique : la Board, l'Historique
# et les PR ne doivent jamais raconter deux histoires. Un nombre nu ne suffit
# JAMAIS — c'est ce qui évite de lire `fix/anomalies-run-9001` comme l'US 9001,
# alors que 9001 est un n° de run TestRail. Un identifiant faux est pire
# qu'absent.
RE_US_BRANCHE = re.compile(r"^(?:feature|feat|us|story)/(\d{3,6})(?:[-_/]|$)", re.I)
RE_US_DOSSIER = re.compile(
    r"(?:^|[-_])(?:feat|feature|us|story)[-_]?(\d{3,6})(?:[-_]|$)", re.I)
# Convention de commit du dépôt : `feat(#40002): ...`, `fix(#12345): ...`.
RE_US_TITRE = re.compile(r"^\w+\(#(\d{3,6})\)")

# `https://<org>@dev.azure.com/<org>/<projet>/_git/<repo>` — le projet est
# encodé (`PROJET_B%20-%20CRM`). Aussi tolérant à la forme ssh et à la forme
# historique `<org>.visualstudio.com`, qu'on décline sans bruit.
RE_REMOTE_HTTPS = re.compile(
    r"^(?:https?://)(?:[^@/]+@)?dev\.azure\.com/([^/]+)/(.+?)/_git/([^/]+?)(?:\.git)?/?$",
    re.I)
RE_REMOTE_SSH = re.compile(
    r"^(?:ssh://)?git@(?:ssh\.)?dev\.azure\.com[:/]v3/([^/]+)/([^/]+)/([^/]+?)(?:\.git)?/?$",
    re.I)

# Le vote Azure DevOps est un petit entier signé. On le nomme, une fois.
VOTES = {10: "approuve", 5: "suggestions", 0: "sans_avis",
         -5: "attente_auteur", -10: "rejete"}
VOTES_VIDE = {"approuve": 0, "suggestions": 0, "attente_auteur": 0,
              "rejete": 0, "sans_avis": 0}

# Un fil non résolu est un fil qui attend encore quelque chose de quelqu'un.
FILS_OUVERTS = ("active", "pending")

# Ordre de tri. Croissant : ce qui bloque en haut, ce qui dort en bas.
PRIORITES = {"conflit": 0, "a_corriger": 1, "a_relire": 2, "dort": 3,
             "prete": 4, "en_attente": 5, "brouillon": 6}
DORT_APRES_H_DEFAUT = 48
# Les trois états qui réclament une action de Thomas, maintenant.
# « dort » entre dans les PR à traiter : c'est tout l'intérêt de l'état. Six des
# huit PR ouvertes étaient dans ce cas — ouvertes, sans le moindre vote, depuis
# jusqu'à quatre jours — et le compteur les ignorait toutes.
A_TRAITER = ("conflit", "a_corriger", "a_relire", "dort")

# Messages de dégradation. Courts, en français, sans jargon : ils s'affichent
# tels quels sur le board, devant quelqu'un qui n'a pas le code sous les yeux.
DEGRADES = {
    "az_absent": "Azure CLI absent : impossible de lister les PR",
    "extension": "extension azure-devops non installée "
                 "(az extension add --name azure-devops)",
    "auth": "session Azure expirée : relance az login",
    "sans_depot": "aucun dépôt Azure DevOps dans les projets configurés",
    "reseau": "Azure DevOps injoignable",
    "premier": "premier chargement en cours…",
}

# Signatures d'erreur `az`, dans l'ordre où on doit les tester : une extension
# absente et un jeton mort produisent tous deux un code retour non nul, mais
# pas le même message. On lit donc le TEXTE autant que le code retour.
SIGNES_EXTENSION = (
    "is not in the 'az' command group",
    "not in the 'az' command group",
    "az extension add",
    "'azure-devops' is not installed",
    "the command requires the extension azure-devops",
    "extension azure-devops",
)
SIGNES_AUTH = (
    "az login",
    "tf400813",
    "please run 'az login'",
    "no subscription found",
    "credentials have expired",
    "token has expired",
    "aadsts",
    "unauthorized",
    " 401",
    "(401)",
    "401 ",
    "authenticationfailed",
    "interactive authentication is needed",
    "refresh token has expired",
)
SIGNES_RESEAU = (
    "max retries exceeded",
    "temporary failure in name resolution",
    "name or service not known",
    "failed to establish a new connection",
    "connection aborted",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
    "getaddrinfo",
    "service unavailable",
    "bad gateway",
    "502",
    "503",
    "504",
    "sslerror",
    "proxyerror",
)


# ------------------------------------------------------------- état du module

# Le cache disque relu paresseusement : on ne rouvre le fichier que si son
# mtime a bougé. C'est ce qui garde `scan()` sous la milliseconde à chaud.
_MEM = {"cache": None, "mtime": -1.0, "chemin": None}
_MEM_VERROU = threading.Lock()

# Un seul rafraîchissement à la fois, jamais deux. Le drapeau est lu par
# `scan()` pour renseigner `rafraichit`.
_TRAVAIL_VERROU = threading.Lock()
_TRAVAIL = {"en_cours": False}

# Identité du compte authentifié : une fois suffit pour la vie du process.
_IDENTITE = {"valeur": None, "connue": False}
_IDENTITE_VERROU = threading.Lock()


# ------------------------------------------------------------------- outils

def _expand(chemin) -> str:
    """`~` développé, chemin normalisé, jamais d'exception."""
    try:
        return os.path.normpath(os.path.expanduser(str(chemin)))
    except Exception:
        return ""


def _tronque(valeur, limite: int) -> str:
    """Texte d'une ligne, espaces normalisés, tronqué. Jamais None."""
    if not isinstance(valeur, str):
        return ""
    return re.sub(r"\s+", " ", valeur).strip()[:limite]


def _entier(valeur, defaut=0):
    try:
        return int(valeur)
    except Exception:
        return defaut


def _chemin_cache(config: dict) -> str:
    racine = _expand((config or {}).get("board_root") or BOARD_ROOT_DEFAULT)
    return os.path.join(racine, CACHE_NAME)


def _ttl(config: dict) -> int:
    """TTL en secondes, lu dans `config["pr"]["ttl_s"]` s'il existe."""
    try:
        brut = ((config or {}).get("pr") or {}).get("ttl_s")
        ttl = int(brut)
        return ttl if ttl > 0 else TTL_DEFAUT_S
    except Exception:
        return TTL_DEFAUT_S


def _epoch(horodatage):
    """« 2026-08-24T14:48:16.984778+00:00 » -> epoch s. 0 si illisible.

    Azure DevOps renvoie toujours de l'UTC, avec ou sans `Z`, avec ou sans
    fraction de seconde. On lit les six nombres qui comptent et on ignore le
    reste : `datetime.fromisoformat` ne sait pas lire le `Z` avant 3.11.
    """
    if not isinstance(horodatage, str):
        return 0
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})",
                 horodatage)
    if not m:
        return 0
    try:
        return calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
    except Exception:
        return 0


def _env_az() -> dict:
    """Environnement des sous-processus.

    Deux corrections : `~/.local/bin` dans le PATH (le serveur peut tourner
    avec un PATH maigre, et c'est là que vit `az` sur cette machine), et une
    sortie forcée en UTF-8 pour ne pas perdre les accents des noms propres.
    """
    env = dict(os.environ)
    local = _expand("~/.local/bin")
    morceaux = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    if local and local not in morceaux:
        morceaux.append(local)
    env["PATH"] = os.pathsep.join(morceaux)
    env["PYTHONIOENCODING"] = "utf-8"
    env["AZURE_CORE_ONLY_SHOW_ERRORS"] = "true"      # pas de préambule sur stderr
    env["AZURE_CORE_COLLECT_TELEMETRY"] = "no"
    # On ne touche PAS à AZURE_DEVOPS_EXT_PAT : la poser, même vide, ferait
    # croire à l'extension qu'un jeton est fourni et casserait l'auth.
    return env


def _trouve_az(env: dict):
    """Chemin de l'exécutable `az`, ou None. Pure lecture du PATH.

    Volontairement sans `shutil` et sans sous-processus : cette vérification
    est faite dans `scan()`, sur le chemin critique, et doit coûter des
    microsecondes.
    """
    for dossier in (env.get("PATH") or "").split(os.pathsep):
        if not dossier:
            continue
        for nom in ("az", "az.cmd", "az.exe"):
            cible = os.path.join(dossier, nom)
            try:
                if os.path.isfile(cible) and os.access(cible, os.X_OK):
                    return cible
            except Exception:
                continue
    return None


def _classer(err: str, expire: bool) -> str:
    """Une erreur `az` -> une clé de `DEGRADES`.

    L'ordre est celui des causes les plus spécifiques d'abord : une extension
    absente et un jeton mort ressemblent tous deux à « ça n'a pas marché ».
    """
    if expire:
        return "reseau"
    texte = (err or "").lower()
    for signe in SIGNES_EXTENSION:
        if signe in texte:
            return "extension"
    for signe in SIGNES_AUTH:
        if signe in texte:
            return "auth"
    for signe in SIGNES_RESEAU:
        if signe in texte:
            return "reseau"
    return "reseau"          # inconnu : on parie sur le transitoire, pas sur la panne


def _az(args, timeout: int, env: dict):
    """Un appel `az`. Rend (données|None, clé de dégradation|None).

    Ne lève jamais. La sortie est du JSON ; tout le reste est une erreur.
    """
    try:
        fini = subprocess.run(
            args, capture_output=True, timeout=timeout, env=env,
            cwd=_expand("~"),           # neutre : pas d'auto-détection depuis un dépôt
            encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "az_absent"
    except subprocess.TimeoutExpired:
        return None, "reseau"
    except Exception:
        return None, "reseau"

    if fini.returncode != 0:
        return None, _classer(fini.stderr or fini.stdout, False)
    sortie = (fini.stdout or "").strip()
    if not sortie:
        return None, None                 # succès muet : rien à lire, pas une panne
    try:
        return json.loads(sortie), None
    except Exception:
        return None, _classer(fini.stderr or sortie, False)


# ----------------------------------------------------- découverte des dépôts

def _remote(dossier: str):
    """`git remote get-url origin` -> (org, projet, repo) Azure DevOps, ou None.

    Aucun nom de client n'est écrit ici : tout vient de l'URL. Le nom de projet
    est encodé dans l'URL (`PROJET_B%20-%20CRM`) et doit être décodé, sinon
    l'appel `az` porte sur un projet qui n'existe pas.
    """
    try:
        fini = subprocess.run(
            ["git", "-C", dossier, "remote", "get-url", "origin"],
            capture_output=True, timeout=TIMEOUT_GIT_S,
            encoding="utf-8", errors="replace")
    except Exception:
        return None
    if fini.returncode != 0:
        return None
    url = (fini.stdout or "").strip()
    m = RE_REMOTE_HTTPS.match(url) or RE_REMOTE_SSH.match(url)
    if not m:
        return None                       # GitHub, GitLab, local : pas notre affaire
    org, projet, repo = m.group(1), m.group(2), m.group(3)
    try:
        projet = urllib.parse.unquote(projet)
        org = urllib.parse.unquote(org)
    except Exception:
        pass
    # Un remote peut viser `<projet>/<équipe>/_git/<repo>` : seul le premier
    # segment est le projet.
    projet = projet.split("/")[0].strip()
    if not (org and projet and repo):
        return None
    return org, projet, repo


def _est_depot(chemin: str) -> bool:
    """Un `.git` — dossier pour un clone, fichier pour un worktree lié."""
    try:
        return os.path.exists(os.path.join(chemin, ".git"))
    except Exception:
        return False


def _depots(config: dict):
    """Dépôts Azure DevOps sous les racines configurées, dédupliqués.

    Un dépôt est identifié par (org, projet, repo) : les huit worktrees de
    `ProjetB.Automatisation` partagent un seul remote, donc un seul appel.
    Le premier projet de config qui l'a vu lui donne son nom et son accent —
    c'est l'ordre de la config qui décide, comme partout ailleurs.
    """
    vus = {}
    ordre = []
    for entree in (config or {}).get("projects") or []:
        if not isinstance(entree, dict):
            continue
        nom = entree.get("name")
        racine = _expand(entree.get("root") or "")
        if not nom or not isinstance(nom, str) or not racine:
            continue
        if nom not in [n for n, _ in ordre]:
            ordre.append((nom, entree.get("accent")))
        for dossier in _parcours(racine):
            trouve = _remote(dossier)
            if not trouve:
                continue
            if trouve in vus:
                continue
            vus[trouve] = {"org": trouve[0], "ado_project": trouve[1],
                           "repo": trouve[2], "project": nom,
                           "accent": entree.get("accent")}
    return list(vus.values()), ordre


def _parcours(racine: str):
    """Dossiers candidats sous `racine`, en largeur, profondeur bornée.

    On ne descend pas dans un dépôt trouvé : ses sous-dossiers ne sont pas des
    dépôts frères. La borne de profondeur évite de marcher dans un `node_modules`
    si la racine configurée est trop haute.
    """
    if not racine or not os.path.isdir(racine):
        return
    if _est_depot(racine):
        yield racine
        return
    file = [(racine, 0)]
    while file:
        dossier, niveau = file.pop(0)
        try:
            entrees = sorted(os.scandir(dossier), key=lambda e: e.name)
        except Exception:
            continue
        for entree in entrees:
            try:
                if not entree.is_dir(follow_symlinks=False):
                    continue
                if entree.name.startswith("."):
                    continue
            except Exception:
                continue
            if _est_depot(entree.path):
                yield entree.path
            elif niveau + 1 < PROFONDEUR_MAX:
                file.append((entree.path, niveau + 1))


# --------------------------------------------------------------- identité

def _identite(env: dict, memo):
    """`user.name` du compte authentifié. Demandée une fois, puis mémorisée.

    Rend (identité|None, clé de dégradation|None). `memo` est l'identité
    éventuellement retenue par un scan précédent (venue du cache disque) :
    elle évite un appel `az` de plus au démarrage du serveur.
    """
    with _IDENTITE_VERROU:
        if _IDENTITE["connue"] and _IDENTITE["valeur"]:
            return _IDENTITE["valeur"], None
        if memo and isinstance(memo, str):
            _IDENTITE["valeur"], _IDENTITE["connue"] = memo, True
            return memo, None

    data, souci = _az(["az", "account", "show", "-o", "json"],
                      TIMEOUT_COMPTE_S, env)
    if souci in ("az_absent", "auth"):
        return None, souci
    nom = None
    if isinstance(data, dict):
        nom = ((data.get("user") or {}) if isinstance(data.get("user"), dict)
               else {}).get("name")
    if isinstance(nom, str) and nom:
        with _IDENTITE_VERROU:
            _IDENTITE["valeur"], _IDENTITE["connue"] = nom, True
        return nom, None
    # Pas d'identité : on saura encore lister les PR, on ne saura juste pas
    # dire lesquelles sont les miennes. C'est une perte, pas une panne.
    return None, None


def _c_est_moi(personne, identite) -> bool:
    """`uniqueName` d'abord, `displayName` en repli. Insensible à la casse."""
    if not identite or not isinstance(personne, dict):
        return False
    cible = identite.strip().lower()
    for champ in ("uniqueName", "displayName"):
        valeur = personne.get(champ)
        if isinstance(valeur, str) and valeur.strip().lower() == cible:
            return True
    return False


# -------------------------------------------------------- lecture des PR

def _liste_pr(depot: dict, env: dict):
    """PR actives d'un dépôt. Rend (liste, clé de dégradation|None)."""
    args = ["az", "repos", "pr", "list",
            "--org", "https://dev.azure.com/%s" % depot["org"],
            "--project", depot["ado_project"],
            "--repository", depot["repo"],
            "--status", "active",
            "--detect", "false",
            "-o", "json"]
    data, souci = _az(args, TIMEOUT_LISTE_S, env)
    if souci:
        return [], souci
    if isinstance(data, dict):                 # certaines versions enveloppent
        data = data.get("value")
    return (data if isinstance(data, list) else []), None


def _fils(depot: dict, pr_id: int, env: dict):
    """Fils de discussion d'une PR -> (commentaires, non_resolus, dégradation).

    `commentaires` ne compte que les commentaires HUMAINS : Azure DevOps
    fabrique des fils `system` pour chaque poussée, chaque vote, chaque mise à
    jour de politique. Les compter donnerait un nombre qui ne veut rien dire.
    """
    args = ["az", "devops", "invoke",
            "--org", "https://dev.azure.com/%s" % depot["org"],
            "--area", "git", "--resource", "pullRequestThreads",
            "--route-parameters",
            "project=%s" % depot["ado_project"],
            "repositoryId=%s" % depot["repo"],
            "pullRequestId=%d" % pr_id,
            "--api-version", "7.1",
            "--detect", "false",
            "-o", "json"]
    data, souci = _az(args, TIMEOUT_FILS_S, env)
    if souci:
        return None, None, souci
    fils = data.get("value") if isinstance(data, dict) else data
    if not isinstance(fils, list):
        return 0, 0, None

    commentaires = 0
    non_resolus = 0
    for fil in fils:
        if not isinstance(fil, dict) or fil.get("isDeleted"):
            continue
        humains = 0
        for c in fil.get("comments") or []:
            if not isinstance(c, dict) or c.get("isDeleted"):
                continue
            if (c.get("commentType") or "").lower() == "system":
                continue
            humains += 1
        commentaires += humains
        statut = (fil.get("status") or "").lower()
        if statut in FILS_OUVERTS:
            non_resolus += 1
    return commentaires, non_resolus, None


def _us(titre: str, branche: str):
    """N° d'US : le titre d'abord (convention de commit), la branche ensuite."""
    m = RE_US_TITRE.match(titre or "")
    if m:
        return m.group(1)
    br = (branche or "").strip()
    m = RE_US_BRANCHE.match(br) or RE_US_DOSSIER.search(
        br.rstrip("/").split("/")[-1])
    return m.group(1) if m else None


def _pr(brut: dict, depot: dict, identite, maintenant: int) -> dict:
    """Un objet PR du contrat, sans les champs qui viennent des fils."""
    pr_id = _entier(brut.get("pullRequestId"))
    titre = _tronque(brut.get("title"), TITRE_MAX)
    src_plein = (brut.get("sourceRefName") or "")
    src = src_plein[len("refs/heads/"):] if src_plein.startswith("refs/heads/") \
        else src_plein
    dst_plein = (brut.get("targetRefName") or "")
    dst = dst_plein[len("refs/heads/"):] if dst_plein.startswith("refs/heads/") \
        else dst_plein

    auteur = brut.get("createdBy") if isinstance(brut.get("createdBy"), dict) else {}
    cree_le = _epoch(brut.get("creationDate"))

    votes = dict(VOTES_VIDE)
    mon_vote = None
    for relecteur in brut.get("reviewers") or []:
        if not isinstance(relecteur, dict):
            continue
        vote = _entier(relecteur.get("vote"), 0)
        votes[VOTES.get(vote, "sans_avis")] += 1
        if _c_est_moi(relecteur, identite):
            mon_vote = vote

    projet_url = urllib.parse.quote(depot["ado_project"], safe="")
    return {
        "id": pr_id,
        "titre": titre,
        "us": _us(titre, src),
        "repo": depot["repo"],
        "ado_project": depot["ado_project"],
        "url": "https://dev.azure.com/%s/%s/_git/%s/pullrequest/%d" % (
            depot["org"], projet_url,
            urllib.parse.quote(depot["repo"], safe=""), pr_id),
        "auteur": _tronque(auteur.get("displayName")
                           or auteur.get("uniqueName"), 60),
        "mienne": _c_est_moi(auteur, identite),
        "draft": bool(brut.get("isDraft")),
        # PAS de troncature ici : « src » sert de CLÉ DE JOINTURE avec la branche
        # du worktree cote board. Couper a 40 caracteres faisait echouer la
        # jointure sur fix/tests-run-9001-c100001-c100002-c100003. On tronque a
        # l'AFFICHAGE, jamais dans la donnee.
        "src": src,
        "src_court": src[:SRC_MAX],
        "dst": dst[:SRC_MAX],
        "cree_le": cree_le,
        "age_j": max(0, (maintenant - cree_le) // 86400) if cree_le else 0,
        "merge": brut.get("mergeStatus") or None,
        "votes": votes,
        "mon_vote": mon_vote,
        "commentaires": 0,
        "non_resolus": 0,
        "etat": "en_attente",
        "priorite": PRIORITES["en_attente"],
        # interne, retiré avant publication
        "_project": depot["project"],
        "_accent": depot["accent"],
    }


def _etat(pr: dict, dort_apres_h: float = DORT_APRES_H_DEFAUT) -> str:
    """L'état d'une PR. Premier cas qui s'applique gagne.

    C'est le seul endroit du module qui porte un jugement, et donc le seul qui
    mérite d'être lu deux fois. Tout le reste n'est que de la plomberie.

    « dort » : une PR à soi que PERSONNE n'a encore regardée, passé un délai.
    Elle n'est ni bloquée ni cassée — elle est simplement en train de pourrir, et
    c'est le cas le plus fréquent en pratique. Sans cet état, elle se rangeait
    dans « en attente » et n'apparaissait dans aucun compteur.
    """
    votes = pr.get("votes") or {}
    contre = votes.get("rejete", 0) + votes.get("attente_auteur", 0)
    pour = votes.get("approuve", 0) + votes.get("suggestions", 0)
    mienne = bool(pr.get("mienne"))
    ouverts = _entier(pr.get("non_resolus"), 0)

    if pr.get("draft"):
        return "brouillon"
    if (pr.get("merge") or "").lower() == "conflicts":
        return "conflit"
    if mienne and (contre > 0 or ouverts > 0):
        return "a_corriger"
    if not mienne and pr.get("mon_vote") == 0:
        return "a_relire"
    if mienne and pour > 0 and contre == 0 and ouverts == 0:
        return "prete"
    # Aucun vote du tout, la mienne, et ouverte depuis trop longtemps.
    if mienne and pour == 0 and contre == 0:
        age_h = _entier(pr.get("age_j"), 0) * 24
        if age_h >= dort_apres_h:
            return "dort"
    return "en_attente"


# ------------------------------------------------------- le rafraîchissement

def _rafraichir(config: dict, precedent: dict) -> dict:
    """Le travail coûteux, en tâche de fond. Rend le nouvel objet de cache.

    Ne lève jamais : chaque panne se traduit par un `degrade` et, quand la
    panne est transitoire, par la conservation du cache précédent. Un board qui
    montre une donnée de trois minutes vaut mieux qu'un board vide.
    """
    debut = time.time()
    env = _env_az()
    maintenant = int(debut)
    precedent = precedent if isinstance(precedent, dict) else {}

    def garde(cle_degrade):
        """Échec dur : on garde tout du précédent, on ne change que le motif."""
        sortie = dict(precedent)
        sortie["degrade"] = DEGRADES[cle_degrade]
        sortie["tente_a"] = maintenant
        sortie.setdefault("groupes", [])
        sortie.setdefault("total", 0)
        sortie.setdefault("a_traiter", 0)
        sortie.setdefault("scanned_at", 0)
        return sortie

    if not _trouve_az(env):
        return garde("az_absent")

    depots, ordre = _depots(config)
    if not depots:
        # Ce n'est pas une panne, c'est un fait établi : on l'enregistre comme
        # un scan réussi, sinon on retenterait la découverte en boucle.
        return {"groupes": [], "total": 0, "a_traiter": 0,
                "scanned_at": maintenant, "tente_a": maintenant,
                "degrade": DEGRADES["sans_depot"],
                "identite": precedent.get("identite")}

    identite, souci = _identite(env, precedent.get("identite"))
    if souci in ("az_absent", "auth"):
        return garde(souci)

    soucis = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=POOL_MAX) as pool:
        # Phase 1 — une liste par dépôt.
        futurs = {pool.submit(_liste_pr, d, env): d for d in depots}
        brutes = []
        for futur in concurrent.futures.as_completed(futurs):
            depot = futurs[futur]
            try:
                liste, souci = futur.result()
            except Exception:
                liste, souci = [], "reseau"
            if souci:
                soucis.append(souci)
                continue
            for brut in liste:
                if isinstance(brut, dict) and brut.get("pullRequestId"):
                    brutes.append((depot, brut))

        if soucis and not brutes:
            # Tout a échoué : on ne remplace pas une donnée par du vide.
            return garde(_priorise(soucis))

        prs = [_pr(brut, depot, identite, maintenant) for depot, brut in brutes]

        # Phase 2 — les fils, uniquement pour les PR listées. C'est l'appel
        # cher, et le seul qui puisse dire ce qui attend vraiment Thomas.
        futurs = {pool.submit(_fils, {"org": d["org"],
                                      "ado_project": d["ado_project"],
                                      "repo": d["repo"]}, pr["id"], env): pr
                  for (d, _b), pr in zip(brutes, prs)}
        for futur in concurrent.futures.as_completed(futurs):
            pr = futurs[futur]
            try:
                commentaires, non_resolus, souci = futur.result()
            except Exception:
                commentaires, non_resolus, souci = None, None, "reseau"
            if souci:
                soucis.append(souci)
                continue                       # on garde la PR, sans ses fils
            pr["commentaires"] = _entier(commentaires, 0)
            pr["non_resolus"] = _entier(non_resolus, 0)

    dort_h = _entier(((config or {}).get('pr') or {}).get('dort_apres_h'),

                     DORT_APRES_H_DEFAUT) or DORT_APRES_H_DEFAUT

    for pr in prs:
        pr["etat"] = _etat(pr, dort_h)
        pr["priorite"] = PRIORITES.get(pr["etat"], PRIORITES["en_attente"])

    groupes = _grouper(prs, ordre)
    resultat = {
        "groupes": groupes,
        "total": sum(g["count"] for g in groupes),
        "a_traiter": sum(1 for pr in prs if pr["etat"] in A_TRAITER),
        "scanned_at": maintenant,
        "tente_a": maintenant,
        "degrade": DEGRADES[_priorise(soucis)] if soucis else None,
        "identite": identite or precedent.get("identite"),
        "duree_ms": int((time.time() - debut) * 1000),
    }
    return resultat


def _priorise(soucis) -> str:
    """La cause la plus explicative parmi plusieurs : la plus spécifique gagne."""
    for cle in ("az_absent", "extension", "auth", "reseau"):
        if cle in soucis:
            return cle
    return "reseau"


def _grouper(prs, ordre):
    """PR -> groupes de projet, dans l'ordre de la config, triés."""
    seaux = {}
    noms = []
    for nom, accent in ordre:
        if nom not in seaux:
            seaux[nom] = {"project": nom, "accent": accent, "prs": []}
            noms.append(nom)
    for pr in prs:
        nom = pr.pop("_project", None)
        accent = pr.pop("_accent", None)
        if nom not in seaux:
            seaux[nom] = {"project": nom, "accent": accent, "prs": []}
            noms.append(nom)
        seaux[nom]["prs"].append(pr)

    groupes = []
    for nom in noms:
        seau = seaux[nom]
        if not seau["prs"]:
            continue                          # pas de colonne morte à l'écran
        # Ce qui bloque d'abord ; à égalité d'état, la plus vieille d'abord.
        seau["prs"].sort(key=lambda p: (p["priorite"], -p["age_j"], p["id"]))
        seau["count"] = len(seau["prs"])
        groupes.append(seau)
    return groupes


# ------------------------------------------------------------------- cache

def _lire_cache(chemin: str):
    """Cache disque, relu seulement si son mtime a bougé. Jamais d'exception."""
    try:
        mtime = os.path.getmtime(chemin)
    except Exception:
        with _MEM_VERROU:
            if _MEM["chemin"] == chemin and _MEM["mtime"] == -1.0:
                return _MEM["cache"]
        return None
    with _MEM_VERROU:
        if _MEM["chemin"] == chemin and _MEM["mtime"] == mtime:
            return _MEM["cache"]
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = None
    except Exception:
        data = None
    with _MEM_VERROU:
        _MEM["chemin"], _MEM["mtime"], _MEM["cache"] = chemin, mtime, data
    return data


def _ecrire_cache(chemin: str, data: dict) -> None:
    """`.tmp` + `os.replace` : un lecteur ne voit jamais un fichier à moitié écrit."""
    try:
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        temporaire = chemin + ".tmp"
        with open(temporaire, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(temporaire, chemin)
    except Exception:
        return
    with _MEM_VERROU:
        try:
            _MEM["chemin"], _MEM["mtime"] = chemin, os.path.getmtime(chemin)
            _MEM["cache"] = data
        except Exception:
            _MEM["chemin"], _MEM["mtime"], _MEM["cache"] = None, -1.0, None


def _lancer(config: dict, chemin: str, precedent) -> bool:
    """Lance le rafraîchissement en tâche de fond. False s'il tourne déjà."""
    with _TRAVAIL_VERROU:
        if _TRAVAIL["en_cours"]:
            return False
        _TRAVAIL["en_cours"] = True

    def travailler():
        try:
            _ecrire_cache(chemin, _rafraichir(config, precedent))
        except Exception:
            pass                              # un thread démon ne crie jamais
        finally:
            with _TRAVAIL_VERROU:
                _TRAVAIL["en_cours"] = False

    fil = threading.Thread(target=travailler, name="pr-scan", daemon=True)
    fil.start()
    return True


# ------------------------------------------------------------------- public

def dernier(config: dict):
    """Le cache tel qu'il est sur le disque, ou None. NE LANCE RIEN.

    `scan()` ne parle pas non plus au réseau, mais il peut réveiller un thread
    `az` quand le cache est périmé. Le bandeau d'attention se reconstruit à
    chaque instantané : il lui faut une lecture strictement passive, sans quoi
    un Azure DevOps injoignable ferait tourner un `az` derrière chaque battement
    du board — le piège que `BACKOFF_S` existe déjà pour éviter ailleurs.

    Le cache disque porte déjà les PR JUGÉES (`etat` par PR) : il n'y a rien à
    recalculer, seulement à lire.
    """
    try:
        cache = _lire_cache(_chemin_cache(config if isinstance(config, dict) else {}))
    except Exception:
        return None
    if not isinstance(cache, dict):
        return None
    groupes = cache.get("groupes")
    scanne_a = _entier(cache.get("scanned_at"), 0)
    return {
        "groupes": groupes if isinstance(groupes, list) else [],
        "age_s": max(0, int(time.time()) - scanne_a) if scanne_a else 0,
        "degrade": cache.get("degrade") if isinstance(cache.get("degrade"), str) else None,
    }


def scan(config: dict, force: bool = False) -> dict:
    """État des Pull Requests. Rend en quelques millisecondes, ne lève jamais.

    Ne parle pas à Azure DevOps : rend le cache — même périmé, avec son âge
    juste — et déclenche au besoin un rafraîchissement en tâche de fond.
    """
    maintenant = int(time.time())
    config = config if isinstance(config, dict) else {}
    chemin = _chemin_cache(config)
    ttl = _ttl(config)
    cache = _lire_cache(chemin) or {}

    scanne_a = _entier(cache.get("scanned_at"), 0)
    tente_a = _entier(cache.get("tente_a"), 0)
    age = max(0, maintenant - scanne_a) if scanne_a else None

    # Sans `az`, inutile de réveiller un thread pour se le faire dire.
    az_absent = _trouve_az(_env_az()) is None

    perime = force or not scanne_a or age is None or age > ttl
    # Après un échec, on laisse respirer : sinon un Azure DevOps injoignable
    # ferait tourner un `az` en boucle derrière chaque affichage du board.
    repos = (not force) and tente_a and (maintenant - tente_a) < BACKOFF_S
    lance = False
    if perime and not repos and not az_absent:
        lance = _lancer(config, chemin, cache)

    with _TRAVAIL_VERROU:
        en_cours = _TRAVAIL["en_cours"]

    groupes = cache.get("groupes") if isinstance(cache.get("groupes"), list) else []
    degrade = cache.get("degrade")
    if az_absent:
        degrade = DEGRADES["az_absent"]
    elif not scanne_a and not degrade:
        degrade = DEGRADES["premier"]

    return {
        "groupes": groupes,
        "total": _entier(cache.get("total"), 0),
        "a_traiter": _entier(cache.get("a_traiter"), 0),
        "scanned_at": scanne_a,
        "age_s": age if age is not None else 0,
        "rafraichit": bool(lance or en_cours),
        "degrade": degrade if isinstance(degrade, str) and degrade else None,
    }


# --------------------------------------------------------------- vérification

ETATS_AFFICHES = {"conflit": "CONFLIT", "a_corriger": "À CORRIGER",
                  "a_relire": "À RELIRE", "prete": "PRÊTE",
                  "dort": "DORT", "en_attente": "EN ATTENTE",
                  "brouillon": "BROUILLON"}


def _config_reelle() -> dict:
    """La vraie config de la machine ; un repli minimal si elle manque."""
    chemin = _expand(os.path.join(BOARD_ROOT_DEFAULT, "config.json"))
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            charge = json.load(f)
        if isinstance(charge, dict) and charge.get("projects"):
            return charge
    except Exception:
        pass
    return {"projects": [], "fallback_project": "AUTRE"}


def _resume_votes(votes: dict) -> str:
    """`+1 ~0 !1 x0 ?2` — approuvé, suggestions, attente auteur, rejeté, sans avis."""
    v = votes or {}
    return "+%d ~%d !%d x%d ?%d" % (
        v.get("approuve", 0), v.get("suggestions", 0),
        v.get("attente_auteur", 0), v.get("rejete", 0), v.get("sans_avis", 0))


def main() -> None:
    config = _config_reelle()

    cache_present = os.path.exists(_chemin_cache(config))
    froid_debut = time.time()
    premier = scan(config, force=True)
    froid = (time.time() - froid_debut) * 1000

    rafraichi_debut = time.time()
    while True:
        with _TRAVAIL_VERROU:
            if not _TRAVAIL["en_cours"]:
                break
        time.sleep(0.05)
    rafraichi = (time.time() - rafraichi_debut) * 1000

    chaud_debut = time.time()
    resultat = scan(config)
    chaud = (time.time() - chaud_debut) * 1000

    print("PULL REQUESTS — %d PR, %d groupe(s), %d à traiter"
          % (resultat["total"], len(resultat["groupes"]), resultat["a_traiter"]))
    print("premier appel (%s) %.1f ms · rafraîchissement complet %.0f ms "
          "· à chaud %.1f ms"
          % ("cache present" if cache_present else "cache vide",
             froid, rafraichi, chaud))
    print("âge des données %d s%s%s\n"
          % (resultat["age_s"],
             "  ·  RAFRAÎCHIT" if resultat["rafraichit"] else "",
             "  ·  " + resultat["degrade"] if resultat["degrade"] else ""))
    if premier["degrade"]:
        print("premier appel : %s (rafraichit=%s)\n"
              % (premier["degrade"], premier["rafraichit"]))

    entete = ("  %-9s %-6s %-11s %-13s %-4s %-4s %-4s %s"
              % ("GROUPE", "ID", "ÉTAT", "VOTES", "CMT", "!RES", "ÂGE", "TITRE"))
    print(entete)
    print("  " + "-" * (len(entete) - 2))
    for groupe in resultat["groupes"]:
        for pr in groupe["prs"]:
            print("  %-9s %-6d %-11s %-13s %-4d %-4d %-4s %s"
                  % (groupe["project"][:9], pr["id"],
                     ETATS_AFFICHES.get(pr["etat"], pr["etat"]),
                     _resume_votes(pr["votes"]), pr["commentaires"],
                     pr["non_resolus"], "%dj" % pr["age_j"],
                     pr["titre"][:58]))
            print("            %-6s %s  US %-7s %s -> %s%s"
                  % ("", pr["repo"][:22], pr["us"] or "—",
                     pr["src"][:30], pr["dst"][:16],
                     "  (moi)" if pr["mienne"] else "  " + pr["auteur"][:22]))
        print()


if __name__ == "__main__":
    main()
