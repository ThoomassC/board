#!/usr/bin/env python3
"""Onglet Chantier — l'inventaire des arbres de travail.

Les trois autres onglets regardent le travail par la conversation (Board,
Historique) ou par la pull request (Pull Requests). Ni l'une ni l'autre ne dure :
une conversation est un moment, une PR est une fin de parcours. Ce qui dure,
c'est la BRANCHE et son arbre de travail — et c'est la seule clé qui noue les
quatre : `conversation.cwd` est un chemin d'arbre, `PR.src` est un nom de
branche, le n° d'US est un préfixe de branche.

Mesuré sur le poste au moment de l'écriture : 29 arbres de travail, dont 1
portant une conversation vivante. Le board voyait donc 1/29 du travail en cours.

Ce module ne fait que CROISER quatre sources déjà produites ailleurs :

    pullrequests._parcours()   énumère dépôts et worktrees sous les racines
    git (4 questions/arbre)    branche, propreté, avance/retard, âge du commit
    pullrequests.scan()        les PR ouvertes, servies depuis son cache
    les sessions de l'instantané les arbres actuellement occupés

Il n'écrit rien, n'appelle aucun réseau, et ne touche à aucun dépôt : pas de
`fetch`, pas de `commit`, pas de `worktree remove`. Il montre et il amène.

    python3 server/chantier.py      # auto-vérification sur la config réelle
"""
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# `_parcours` sait déjà descendre sous une racine sans entrer dans un dépôt
# trouvé, et reconnaître le `.git` FICHIER d'un worktree lié. Aucune raison
# d'en écrire une seconde version.
try:
    from pullrequests import _parcours, scan as _scan_pr
except Exception:                                    # module absent : on dégrade
    _parcours = None
    _scan_pr = None

TTL = 30.0            # les données sont locales et rapides : un cache court suffit
TIMEOUT = 3.0         # un `git status` sur un montage lent ne doit pas tout figer
PARALLELE = 8         # 4 questions git par arbre, 29 arbres : on les mène de front

BRANCHES_SOCLE = {"main", "master", "develop", "dev"}
BASES_CANDIDATES = ("origin/develop", "origin/main", "origin/master")

# Français à l'écran, comme partout dans ce dépôt. L'ordre EST la priorité :
# un arbre n'a qu'un seul état, le premier qui s'applique.
ETATS = ("en_cours", "en_revue", "non_commite", "socle", "reserve")
GLYPHE = {"en_cours": "●", "en_revue": "➜", "non_commite": "✋",
          "reserve": "⋯", "socle": "·"}
LIBELLE = {"en_cours": "EN COURS", "en_revue": "EN REVUE",
           "non_commite": "NON COMMITÉ", "reserve": "RÉSERVE", "socle": "SOCLE"}

# Mêmes libellés que l'onglet Pull Requests : un état PR se nomme pareil d'un
# écran à l'autre, sinon l'utilisateur croit lire deux choses différentes.
LIBELLE_PR = {"conflit": "CONFLIT", "a_corriger": "À CORRIGER", "a_relire": "À RELIRE",
              "dort": "DORT", "prete": "PRÊTE", "en_attente": "EN ATTENTE",
              "brouillon": "BROUILLON"}

_CACHE = {"at": 0.0, "data": None}
_VERROU = threading.Lock()


# ------------------------------------------------------------------ git brut
def _git(dossier, *args):
    """Une question à git. Rend la sortie, ou None si la question échoue.

    Aucune exception ne sort d'ici : un dossier qui a perdu son dépôt, un
    montage qui ne répond pas ou un git absent doivent rendre une ligne
    incomplète, jamais casser l'onglet.
    """
    try:
        r = subprocess.run(["git", "-C", dossier] + list(args),
                           capture_output=True, text=True, timeout=TIMEOUT)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _entete_statut(sortie):
    """(branche, fichiers, ahead, behind) depuis `status --porcelain=v1 --branch`.

    UN SEUL appel donne les quatre : la ligne `##` porte la branche et l'écart
    au distant, les suivantes portent les fichiers. Les non suivis sont comptés
    — un fichier non suivi est du travail qu'on peut perdre au même titre qu'un
    fichier modifié.

    Formes rencontrées :
        ## fix/foo...origin/fix/foo [ahead 1, behind 6]
        ## fix/foo                              (aucun distant)
        ## HEAD (no branch)                     (tête détachée)
    """
    lignes = [l for l in (sortie or "").splitlines() if l]
    if not lignes or not lignes[0].startswith("##"):
        return None, 0, 0, 0
    tete = lignes[0][3:]
    fichiers = len(lignes) - 1

    ahead = behind = 0
    if "[" in tete:
        suivi = tete.split("[", 1)[1]
        for jeton, cle in (("ahead ", "a"), ("behind ", "b")):
            if jeton in suivi:
                try:
                    n = int(suivi.split(jeton)[1].split(",")[0].split("]")[0].strip())
                except (ValueError, IndexError):
                    n = 0
                if cle == "a":
                    ahead = n
                else:
                    behind = n

    nom = tete.split("[")[0].strip()
    nom = nom.split("...")[0].strip()
    if not nom or nom.startswith("HEAD ") or nom == "HEAD":
        nom = None                       # tête détachée : pas de branche à nommer
    return nom, fichiers, ahead, behind


def _depot_canonique(dossier):
    """Le dépôt auquel cet arbre appartient, par son `--git-common-dir`.

    C'est ce qui regroupe les 17 worktrees de `ProjetB.Automatisation` sous une
    seule ligne d'en-tête. Le nom de DOSSIER ne suffirait pas : il porte le
    suffixe de worktree, donc chaque arbre formerait son propre groupe.
    """
    commun = _git(dossier, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not commun:
        return None, None
    if commun.endswith(os.sep + ".git") or commun.endswith("/.git"):
        commun = os.path.dirname(commun)
    commun = os.path.normpath(commun)
    nom = os.path.basename(commun)
    if not nom or nom in (".", os.sep):
        return None, None
    return nom, commun


def _releve(dossier):
    """Les quatre faits git d'un arbre, en trois appels menés d'affilée."""
    branche, fichiers, ahead, behind = _entete_statut(
        _git(dossier, "status", "--porcelain=v1", "--branch"))
    depot, commun = _depot_canonique(dossier)
    horodatage = _git(dossier, "log", "-1", "--format=%ct")
    try:
        commit_at = int(horodatage) if horodatage else None
    except ValueError:
        commit_at = None
    return {"chemin": dossier, "branche": branche, "fichiers": fichiers,
            "ahead": ahead, "behind": behind, "depot": depot,
            "commun": commun, "commit_at": commit_at}


def _base_du_depot(dossier):
    """La première base de comparaison qui existe vraiment, et son âge.

    On rend AUSSI la date de la référence distante : `mergée` se calcule contre
    `origin/develop`, qui peut être périmé de plusieurs jours si personne n'a
    fait `fetch`. Afficher la réponse sans son âge serait affirmer plus que ce
    qu'on sait — et ce module ne fait pas de `fetch` pour arranger ça.
    """
    for ref in BASES_CANDIDATES:
        sortie = _git(dossier, "rev-parse", "--verify", "-q", ref + "^{commit}")
        if not sortie:
            continue
        horodatage = _git(dossier, "log", "-1", "--format=%ct", ref)
        try:
            return ref, int(horodatage) if horodatage else None
        except ValueError:
            return ref, None
    return None, None


def _est_mergee(dossier, base):
    """La branche courante est-elle entièrement contenue dans `base` ?

    `merge-base --is-ancestor` ne rend rien sur la sortie standard : c'est son
    code de retour qui répond. `_git` rendrait donc `""` en cas de succès et
    `None` en cas d'échec — les deux sont faux ici, d'où l'appel direct.
    """
    if not base:
        return None
    try:
        r = subprocess.run(["git", "-C", dossier, "merge-base",
                            "--is-ancestor", "HEAD", base],
                           capture_output=True, timeout=TIMEOUT)
        return r.returncode == 0
    except Exception:
        return None


# --------------------------------------------------------------- croisement
def _us_defaut(branche, cwd=""):
    """Repli si le serveur n'injecte pas sa propre règle.

    La règle vit dans `serveur.us_de` — un nombre nu ne suffit JAMAIS (cf.
    docs/SCHEMA.md). Ce repli n'existe que pour l'auto-vérification en ligne de
    commande, où `serveur` n'est pas chargé.
    """
    m = re.match(r"^(?:feature|feat|us|story)/(\d{3,6})(?:[-_/]|$)",
                 str(branche or "").strip(), re.I)
    return "#" + m.group(1) if m else ""


def _liberation(a):
    """(liberable, terminee, retenu) — peut-on retirer cet arbre de travail ?

    LA NUANCE QUI GOUVERNE TOUT : `git worktree remove` retire le RÉPERTOIRE de
    travail, pas la branche. La branche survit dans le dépôt, avec tous ses
    commits. Le risque de perte se limite donc à ce qui n'existe QUE dans ce
    répertoire : les fichiers non commités, et les commits non poussés.

    D'où deux niveaux, et pas un :

      liberable  l'arbre peut être retiré sans rien perdre. Rien de non commité,
                 rien à pousser, aucune conversation dedans, aucune PR ouverte
                 (si une PR est ouverte on aura encore besoin de l'arbre), et ce
                 n'est pas le clone principal — qu'on ne retire jamais.
      terminee   en plus, la branche est déjà dans la base. Elle peut donc partir
                 avec l'arbre : plus rien n'y dort.

    `retenu` nomme LE motif qui bloque, un seul, le plus grave d'abord. C'est la
    réponse à « pourquoi pas celui-là ? », qui est la question qu'on se pose
    vraiment en regardant une ligne qui n'est pas libérable.
    """
    if a.get("principal"):
        return False, False, "clone principal du dépôt — jamais retiré"
    if a.get("conv"):
        n = len(a["conv"])
        return False, False, ("%d conversation%s y travaille%s"
                              % (n, "s" if n > 1 else "", "nt" if n > 1 else ""))
    if a.get("fichiers"):
        n = a["fichiers"]
        return False, False, ("%d fichier%s non commité%s — le seul travail qui "
                              "ne survivrait pas" % (n, "s" if n > 1 else "",
                                                     "s" if n > 1 else ""))
    if a.get("ahead"):
        n = a["ahead"]
        return False, False, ("%d commit%s à pousser — ils n'existent que dans "
                              "cet arbre" % (n, "s" if n > 1 else ""))
    if a.get("pr"):
        return False, False, ("PR #%s encore ouverte — l'arbre sert toujours"
                              % a["pr"]["id"])
    if (a.get("branche") or "").lower() in BRANCHES_SOCLE:
        return False, False, "branche socle"
    # Rien ne peut plus être perdu : l'arbre est libérable.
    # La fusion décide seulement si la BRANCHE peut partir avec lui.
    return True, bool(a.get("mergee")), None


def _etat(arbre):
    """L'état unique de l'arbre. L'ordre est la doctrine :

    ce qui vit d'abord, puis ce qui est chez quelqu'un d'autre, puis ce qui peut
    se perdre, puis le socle, puis la réserve. Un `develop` avec des fichiers
    modifiés est donc `non_commite` et non `socle` : du travail non commité sur
    la branche d'intégration mérite d'être vu.
    """
    if arbre.get("conv"):
        return "en_cours"
    if arbre.get("pr"):
        return "en_revue"
    if arbre.get("fichiers"):
        return "non_commite"
    if (arbre.get("branche") or "").lower() in BRANCHES_SOCLE:
        return "socle"
    return "reserve"


def _projet_de(chemin, config):
    """Même règle de rattachement que le serveur : premier préfixe qui gagne."""
    chemin_n = os.path.normpath(chemin or "")
    for p in (config or {}).get("projects") or []:
        if not isinstance(p, dict):
            continue
        racine = os.path.normpath(os.path.expanduser(p.get("root") or ""))
        if racine and (chemin_n == racine or chemin_n.startswith(racine + os.sep)):
            return p.get("name") or "?", p.get("accent")
    return (config or {}).get("fallback_project", "AUTRE"), None


def _index_pr(config):
    """{(dépôt, branche) -> PR} et {branche_source -> PR}, depuis le cache PR.

    La première clé est celle qu'utilise déjà `serveur.Board._index_pr` : on ne
    change pas de jointure d'un onglet à l'autre. La seconde sert UNIQUEMENT à
    détecter les chaînes (`dst` d'une PR = `src` d'une autre), où le dépôt est
    forcément le même.
    """
    if _scan_pr is None:
        return {}, {}, "module Pull Requests absent"
    try:
        d = _scan_pr(config)
    except Exception as e:
        return {}, {}, "cache PR illisible : %s" % e
    par_cle, par_src = {}, {}
    for g in d.get("groupes", []):
        for pr in g.get("prs", []):
            court = {"id": pr.get("id"), "etat": pr.get("etat"),
                     "url": pr.get("url"), "age_j": pr.get("age_j"),
                     "commentaires": pr.get("commentaires"),
                     "non_resolus": pr.get("non_resolus"),
                     "dst": pr.get("dst"), "draft": pr.get("draft")}
            src = pr.get("src") or ""
            if pr.get("repo") and src:
                par_cle[(pr["repo"], src)] = court
            if src:
                par_src[src] = court
    return par_cle, par_src, None


def _alerte(arbre):
    """(phrase, niveau) — ce que les chiffres impliquent, ou (None, None).

    On n'en émet une que si elle change une décision. « ↑1 » ne veut rien dire
    en soi ; « ↑1 avec une PR ouverte » veut dire que le relecteur regarde un
    diff qui n'est pas le tien.

    Trois niveaux, et la distinction porte tout le comptage de l'onglet :

      agir   une CONTRADICTION dans l'état de l'arbre. Anormal par nature, donc
             rare, donc digne d'une pastille sur l'onglet.
      bloque la ligne paraît actionnable et ne l'est pas — le pire faux positif
             qu'un tableau de bord puisse produire.
      info   du rangement possible. Jamais compté : un état normal qui allume un
             compteur finit par éteindre le compteur, pas l'état.
    """
    if arbre["etat"] == "en_revue" and arbre["ahead"]:
        n = arbre["ahead"]
        return ("%d commit%s local%s absent%s de la PR — le relecteur ne voit "
                "pas ton dernier état" % (n, "s" if n > 1 else "",
                                          "aux" if n > 1 else "",
                                          "s" if n > 1 else ""), "agir")
    if arbre["etat"] == "en_revue" and arbre["fichiers"]:
        return ("travail non commité alors que la PR est ouverte", "agir")
    if arbre.get("attend"):
        return ("ne peut pas merger avant #%s (%s)" % (
            arbre["attend"]["id"],
            LIBELLE_PR.get(arbre["attend"]["etat"], arbre["attend"]["etat"])), "bloque")
    return None, None


# ------------------------------------------------------------------- balayage
def _balayer(config, sessions, us_de):
    racines = []
    for p in (config or {}).get("projects") or []:
        if isinstance(p, dict) and p.get("root"):
            racines.append(os.path.expanduser(p["root"]))
    if _parcours is None:
        return None, "module Pull Requests absent : impossible d'énumérer les dépôts"
    dossiers = []
    for racine in racines:
        for d in _parcours(racine):
            if d not in dossiers:
                dossiers.append(d)
    if not dossiers:
        return [], None

    with ThreadPoolExecutor(max_workers=PARALLELE) as pool:
        arbres = list(pool.map(_releve, dossiers))

    # Une base de comparaison par DÉPÔT, pas par arbre : les worktrees d'un même
    # dépôt partagent leurs références distantes.
    bases = {}
    a_sonder = {}
    for a in arbres:
        if a["commun"] and a["commun"] not in a_sonder:
            a_sonder[a["commun"]] = a["chemin"]
    if a_sonder:
        with ThreadPoolExecutor(max_workers=PARALLELE) as pool:
            for commun, res in zip(a_sonder,
                                   pool.map(_base_du_depot, a_sonder.values())):
                bases[commun] = res

    par_cle, par_src, degrade = _index_pr(config)
    par_cwd = {}
    for e in (sessions or []):
        if e.get("cwd"):
            par_cwd.setdefault(os.path.normpath(e["cwd"]), []).append(e)

    for a in arbres:
        chemin_n = os.path.normpath(a["chemin"])
        occupants = par_cwd.get(chemin_n) or []
        a["conv"] = [{"sid": e.get("sid"), "title": e.get("title"),
                      "state": e.get("state"), "glyphe": e.get("glyphe"),
                      "ctx_pct": e.get("ctx_pct"), "since": e.get("since")}
                     for e in occupants] or None
        a["pr"] = par_cle.get((a["depot"] or "", a["branche"] or "")) or None
        a["attend"] = None
        if a["pr"] and a["pr"].get("dst") and a["pr"]["dst"] in par_src:
            amont = par_src[a["pr"]["dst"]]
            if amont.get("id") != a["pr"].get("id"):
                a["attend"] = {"id": amont["id"], "etat": amont["etat"]}
        a["etat"] = _etat(a)
        a["us"] = us_de(a["branche"], a["chemin"])

    # `mergée` était calculé pour la seule réserve. Il l'est maintenant pour tout
    # arbre LIÉ (pas le clone principal, qu'on ne retire jamais) : c'est le
    # signal qui décide si la branche peut partir AVEC son arbre de travail.
    #
    # PROPRIÉTÉ DE SÛRETÉ, et c'est elle qui rend ce champ utilisable pour
    # décider d'une suppression : `merge-base --is-ancestor HEAD origin/develop`
    # se calcule contre une référence LOCALE, éventuellement périmée. Une
    # référence vieille peut donc rendre `False` sur une branche fusionnée depuis
    # — un faux NÉGATIF. Elle ne peut jamais rendre `True` sur une branche qui ne
    # l'est pas : si HEAD est un ancêtre d'un commit qui était sur origin/develop,
    # il l'est pour toujours. L'erreur ne va que dans le sens prudent.
    for a in arbres:
        a["principal"] = bool(a["commun"] and
                              os.path.normpath(a["chemin"]) == os.path.normpath(a["commun"]))
    lies = [a for a in arbres if not a["principal"]]
    if lies:
        with ThreadPoolExecutor(max_workers=PARALLELE) as pool:
            verdicts = list(pool.map(
                lambda a: _est_mergee(a["chemin"], (bases.get(a["commun"]) or (None, None))[0]),
                lies))
        for a, v in zip(lies, verdicts):
            base, base_at = bases.get(a["commun"]) or (None, None)
            a["mergee"] = v
            a["base"] = base
            a["base_at"] = base_at
    for a in arbres:
        a.setdefault("mergee", None)
        a.setdefault("base", None)
        a.setdefault("base_at", None)
        a.setdefault("principal", False)

    for a in arbres:
        a["liberable"], a["terminee"], a["retenu"] = _liberation(a)

    return arbres, degrade


def _habiller(arbres, config, maintenant):
    """Met en forme, groupe par projet puis par dépôt, ordonne."""
    for a in arbres:
        projet, accent = _projet_de(a["chemin"], config)
        a["project"], a["accent"] = projet, accent
        a["glyphe"] = GLYPHE.get(a["etat"], "·")
        a["libelle"] = LIBELLE.get(a["etat"], a["etat"])
        a["commit_j"] = (int((maintenant - a["commit_at"]) // 86400)
                         if a["commit_at"] else None)
        a["base_j"] = (int((maintenant - a["base_at"]) // 86400)
                       if a["base_at"] else None)
        # Groupé par dépôt, le préfixe est déjà écrit dans l'en-tête : on
        # n'affiche que ce qui distingue l'arbre de son dépôt.
        nom = os.path.basename(os.path.normpath(a["chemin"]))
        depot = a["depot"] or ""
        if nom == depot:
            a["arbre"] = "(dépôt)"
        elif depot and nom.startswith(depot):
            a["arbre"] = nom[len(depot):].lstrip("-_.") or "(dépôt)"
        else:
            a["arbre"] = nom
        a["alerte"], a["alerte_niveau"] = _alerte(a)
        # Le clone principal, parce que `git worktree remove` se lance depuis lui
        # et non depuis l'arbre qu'on retire — sinon git refuse.
        a["depot_chemin"] = a.get("commun")
        a.pop("commun", None)
        a.pop("base_at", None)

    rang = {e: i for i, e in enumerate(ETATS)}

    def cle(a):
        # Dans un dépôt : par état, puis le plus sale d'abord, puis le plus
        # ancien commit — c'est-à-dire ce qui pourrit le plus, en haut.
        return (rang.get(a["etat"], 9), -a["fichiers"],
                -(maintenant - (a["commit_at"] or maintenant)),
                a["branche"] or "")

    ordre_projets = [p.get("name") for p in (config or {}).get("projects") or []
                     if isinstance(p, dict)]
    repli = (config or {}).get("fallback_project", "AUTRE")
    presents = {a["project"] for a in arbres}
    colonnes = [n for n in ordre_projets if n in presents]
    for n in sorted(presents):
        if n not in colonnes and n != repli:
            colonnes.append(n)
    if repli in presents:
        colonnes.append(repli)

    groupes = []
    for nom in colonnes:
        lot = [a for a in arbres if a["project"] == nom]
        depots, vus = [], {}
        for a in sorted(lot, key=cle):
            d = vus.get(a["depot"] or "?")
            if d is None:
                d = {"repo": a["depot"] or "?", "arbres": [], "count": 0}
                vus[a["depot"] or "?"] = d
                depots.append(d)
            d["arbres"].append(a)
            d["count"] += 1
        # Les dépôts les plus chargés en premier : c'est là que se joue la revue.
        depots.sort(key=lambda d: (-d["count"], d["repo"]))
        groupes.append({"project": nom, "accent": (lot[0]["accent"] if lot else None),
                        "count": len(lot), "depots": depots})
    return groupes


def scan(config, sessions=None, us_de=None, force=False):
    """L'inventaire complet, servi depuis un cache de 30 s.

    `sessions` : la liste d'sessions du dernier instantané. `None` signifie « on ne
    sait pas » et NON « aucune » : dans ce cas aucun arbre ne peut être marqué
    `en_cours`, ce que le drapeau `conversations_inconnues` dit à l'écran.
    Étiqueter en silence un arbre occupé comme `réserve` serait faux, pas dégradé.

    `us_de` : la règle d'extraction du n° d'US, injectée par le serveur pour
    qu'il n'en existe qu'une seule implémentation (cf. docs/SCHEMA.md).
    """
    maintenant = int(time.time())
    with _VERROU:
        frais = _CACHE["data"] is not None and (time.time() - _CACHE["at"]) < TTL
        if frais and not force:
            d = dict(_CACHE["data"])
            d["age_s"] = int(time.time() - _CACHE["at"])
            d["now"] = maintenant
            return d

    try:
        arbres, degrade = _balayer(config, sessions, us_de or _us_defaut)
    except Exception as e:                       # aucune trace ne remonte à l'écran
        return {"groupes": [], "total": 0, "compteurs": {}, "now": maintenant,
                "age_s": 0, "degrade": "balayage impossible : %s" % e,
                "conversations_inconnues": sessions is None}
    if arbres is None:
        return {"groupes": [], "total": 0, "compteurs": {}, "now": maintenant,
                "age_s": 0, "degrade": degrade,
                "conversations_inconnues": sessions is None}

    groupes = _habiller(arbres, config, maintenant)
    compteurs = {e: sum(1 for a in arbres if a["etat"] == e) for e in ETATS}
    liberables = sum(1 for a in arbres if a.get("liberable"))
    terminees = sum(1 for a in arbres if a.get("terminee"))
    data = {
        "groupes": groupes,
        "total": len(arbres),
        "compteurs": compteurs,
        # « Quand puis-je supprimer un worktree ? » — la question à laquelle
        # aucun écran ne répondait. Deux nombres, parce que ce sont deux gestes
        # différents : retirer l'arbre, et retirer l'arbre ET sa branche.
        "liberables": liberables,
        "terminees": terminees,
        # Seules les CONTRADICTIONS sont comptées. `non_commite` est un état
        # normal chez quelqu'un qui travaille en worktrees : le compter ici
        # allumerait une pastille en permanence, et une pastille permanente ne
        # veut plus rien dire. C'est le piège dans lequel `a_traiter` de
        # l'onglet PR est tombé en laissant `dort` le gonfler à 7 sur 9.
        "a_traiter": sum(1 for a in arbres
                         if a.get("alerte_niveau") in ("agir", "bloque")),
        "degrade": degrade,
        "conversations_inconnues": sessions is None,
        # Le vocabulaire et l'ordre voyagent avec les donnees : le board ne doit
        # jamais inventer un libelle d'etat ni decider de leur priorite. C'est la
        # regle deja tenue par l'objet SESSION, qui porte son `glyphe` et son
        # `libelle` plutot que de laisser board.js les deduire.
        "ordre": list(ETATS),
        "libelles": {e: LIBELLE[e] for e in ETATS},
        "glyphes": {e: GLYPHE[e] for e in ETATS},
        "now": maintenant,
        "age_s": 0,
    }
    # ON NE MET PAS EN CACHE UN RELEVÉ DÉGRADÉ, et c'était un vrai bug.
    # La sonde de pastille du board interroge cette route au chargement de la
    # page, AVANT que le flux SSE ait produit son premier instantané : `sessions`
    # valait donc None, et ce relevé sans conversations restait servi pendant
    # 30 s. L'utilisateur qui ouvrait l'onglet dans la foulée voyait « aucun
    # instantané récent » alors que trois conversations tournaient.
    # Un résultat incomplet n'a pas droit au cache : il se recalcule (55 ms).
    if sessions is not None:
        with _VERROU:
            _CACHE["at"] = time.time()
            _CACHE["data"] = data
    return dict(data)


# ------------------------------------------------------------ auto-vérification
def _config_reelle():
    import json
    chemin = os.path.expanduser("~/.claude/board/config.json")
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"projects": [], "fallback_project": "AUTRE"}


def main():
    debut = time.time()
    d = scan(_config_reelle(), sessions=[], force=True)
    ms = (time.time() - debut) * 1000
    print("balayage : %d arbres en %.0f ms" % (d["total"], ms))
    if d.get("degrade"):
        print("dégradé  : %s" % d["degrade"])
    print("états    : " + " · ".join("%d %s" % (d["compteurs"][e], LIBELLE[e].lower())
                                     for e in ETATS if d["compteurs"].get(e)))
    print("à traiter: %d" % d.get("a_traiter", 0))
    print("libérables: %d dont %d branche terminée"
          % (d.get("liberables", 0), d.get("terminees", 0)))
    for g in d["groupes"]:
        print("\n%s — %d arbres" % (g["project"], g["count"]))
        for dep in g["depots"]:
            print("  %s (%d)" % (dep["repo"], dep["count"]))
            for a in dep["arbres"]:
                bouts = []
                if a["fichiers"]:
                    bouts.append("%d chg" % a["fichiers"])
                if a["ahead"]:
                    bouts.append("↑%d" % a["ahead"])
                if a["behind"]:
                    bouts.append("↓%d" % a["behind"])
                if a["pr"]:
                    bouts.append("PR #%s %s" % (a["pr"]["id"], a["pr"]["etat"]))
                print("    %-11s %-7s %-44s %-16s %s%s" % (
                    a["libelle"], a["us"] or "—", (a["branche"] or "(détachée)")[:44],
                    a["arbre"][:16], " ".join(bouts),
                    "  ⚠ " + a["alerte"] if a["alerte"] else ""))
    debut = time.time()
    scan(_config_reelle(), sessions=[])
    print("\nrelecture depuis le cache : %.2f ms" % ((time.time() - debut) * 1000))


if __name__ == "__main__":
    main()
