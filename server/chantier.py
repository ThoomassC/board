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

# DÉDOUBLONNAGE DES BALAYAGES FORCÉS — une fenêtre ET une signature.
#
# Ce que `force` doit garantir (cf. board.js, « POURQUOI `force` ») : que les
# ÉTATS D'ARBRES du relevé tiennent compte du changement de conversations qui
# vient d'être observé. Rien de plus. Ce n'est pas « refaire le travail », c'est
# « ne pas me servir un relevé d'avant l'événement ».
#
# Ce qu'il coûte, mesuré sur ce poste (18 arbres) : 155-164 ms de mur, 81
# processus git, 816 ms de CPU par balayage forcé. Avec 3 onglets du board
# ouverts, UNE conversation qui démarre a été mesurée à 349 ms, 243 processus,
# 2 699 ms de CPU — le même relevé, calculé trois fois.
#
# Un `force` est donc resservi depuis le relevé mémorisé À DEUX CONDITIONS, et
# il en faut deux parce qu'elles ne répondent pas à la même question.
#
#   ① LA SIGNATURE (`_signature_sessions`) — « ce relevé a-t-il vu MON
#     événement ? » C'est la condition exacte : le relevé mémorisé a été calculé
#     sur le même lot de sessions que celui de cet appel, donc un rebalayage ne
#     changerait aucun état d'arbre. Elle ne coûte rien au cas mesuré : N
#     onglets qui réagissent au même changement présentent par construction la
#     même signature. Elle rattrape ce que la fenêtre seule ratait — deux
#     changements DISTINCTS en moins de deux secondes, où le second appelant se
#     voyait resservir un relevé antérieur à son propre événement.
#
#   ② LA FENÊTRE (`FENETRE_FORCE`) — « ce relevé est-il encore assez frais ? »
#     Nécessaire parce que les états d'arbres ne dépendent pas QUE des
#     conversations : un `git status`, une PR ouverte, un commit poussé les
#     changent sans que l'instantané bouge. Sur la seule signature, un lot de
#     sessions stable dix minutes rendrait tout `force` définitivement
#     inopérant. La fenêtre borne les rafales à signature identique, elle ne les
#     décide plus.
#
# Pourquoi 2 s : les onglets ne réagissent pas en même temps. Chacun tient son
# propre flux SSE, dont la phase dépend de l'heure d'ouverture de la page ; le
# même changement de conversations est donc détecté sur une plage d'UNE seconde
# pleine (la période du flux), plus la durée d'un balayage (~0,16 s). Une
# fenêtre plus courte laisserait passer les onglets de fin de plage — c'est-à-
# dire raterait exactement le cas mesuré. Plus longue, elle ne recouvre plus
# d'événements distincts depuis que ① les départage, mais elle resservirait des
# états d'arbres vieillis pour des raisons étrangères aux conversations.
#
# Pourquoi pas le TTL de 30 s : le TTL répond à « ce relevé est-il encore
# utile ? », la fenêtre à « est-il assez frais pour un appelant qui a vu quelque
# chose bouger ? ». Deux questions, deux durées. Confondre les deux, c'est un
# `force` qui ne force jamais.
FENETRE_FORCE = 2.0

# Un balayage qui tourne déjà est attendu plutôt que doublé (cf. `scan`). Une
# borne, pour qu'un balayage bloqué sur un montage mort — `git status` a son
# propre TIMEOUT de 3 s, mais rien ne garantit qu'il soit le seul en cause — ne
# fige pas indéfiniment un appelant qui pourrait, lui, réussir.
ATTENTE_BALAYAGE = 1.0
ATTENTES_MAX = 3

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

# `data` ne contient JAMAIS `conversations` ni `jamais_servi` : voir
# `_deriver_a_l_appel`. Les deux appartiennent à l'APPEL, pas au relevé.
# `signature` est celle du lot de sessions qui a produit `data` — jamais None
# quand `data` ne l'est pas, puisque le cache refuse les relevés dégradés.
_CACHE = {"at": 0.0, "data": None, "signature": None}
# Une Condition et non un Lock : elle sert aussi de signal de fin de balayage,
# pour que deux balayages simultanés n'en fassent qu'un (cf. `scan`). Elle
# s'utilise comme un verrou partout ailleurs, `with _VERROU:` compris.
_VERROU = threading.Condition()
_EN_COURS = {"balayages": 0}


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


def _compter_conversations(groupes, config, sessions):
    """Rend une COPIE des groupes, chacun portant `conversations`.

    `conversations` = combien de conversations vivantes travaillent dans ce
    projet, ou None quand l'appelant n'a pas fourni d'instantané. C'est une
    valeur DE RÉPONSE, dérivée au retour de `scan()` sur ses deux chemins ; elle
    n'entre jamais dans le relevé mémorisé, qui appartient à tout le monde.
    Quatre choses se jouent ici, et aucune ne se redevine à la relecture.

    1. LE RATTACHEMENT N'EST PAS CELUI DE `a["conv"]`. `_balayer` rattache une
       conversation à un arbre par égalité EXACTE des chemins : une conversation
       ouverte dans `.../board/server` n'occupe aucun arbre et n'apparaît donc
       dans le `conv` d'aucun d'eux — elle appartient pourtant bien au projet.
       `conversations` compte par RACINE DE PROJET, avec `_projet_de`, la règle
       que le serveur et `_habiller` appliquent déjà. Un
       `sum(len(a["conv"] or []) for ...)` paraîtrait équivalent et
       sous-compterait en silence tout travail mené dans un sous-dossier.

    2. None N'EST PAS ZÉRO. `sessions is None` dit « on ne sait pas », jamais
       « aucune ». Le client ne masque un projet que sur `conversations === 0`
       et jamais sur `null` : lui servir 0 au lieu de None ferait disparaître de
       l'écran des projets où trois conversations tournent. Cet invariant se
       tient jusqu'en amont : `serveur.sessions()` LÈVE quand le dossier d'états
       est illisible au lieu de rendre une liste vide, sans quoi tout ce
       raisonnement était contourné avant d'arriver ici.

    3. `conversations` N'EST PAS `conversations_inconnues`. Le premier décrit
       l'APPEL (cet appelant-ci a-t-il fourni un instantané ?), le second décrit
       le RELEVÉ (ses états d'arbres ont-ils été calculés sans instantané ?).
       Les deux ne coïncident que sur le chemin frais. Un appel `sessions=None`
       servi par le cache reçoit donc `conversations: null` et
       `conversations_inconnues: false` — et c'est juste : le relevé, lui, a
       bien vu un instantané.

    4. AUCUN GROUPE N'EST MUTÉ EN PLACE. Les groupes reçus sont ceux du relevé
       mémorisé, partagé entre tous les appelants : y écrire corromprait le
       cache pour le suivant. On rend des dicts neufs. La copie est
       volontairement de SURFACE : seul le premier niveau change, `depots` et
       les arbres qu'il contient ne sont ni lus ni touchés, les partager coûte
       zéro et évite de recopier 29 arbres à chaque appel.

    Une conversation `dead` ne compte pas : le serveur les écarte déjà en amont
    (`serveur.py`, construction de l'instantané), mais `chantier` reçoit une
    liste qu'il ne fabrique pas — il ne suppose pas ce filtrage, il le refait.
    Une entrée sans `cwd` est ignorée plutôt que rattachée au projet de repli :
    on ne sait pas où elle travaille, l'inventer gonflerait « AUTRE ».
    """
    if sessions is None:
        return [dict(g, conversations=None) for g in groupes]
    par_projet = {}
    for e in sessions:
        if not isinstance(e, dict) or e.get("state") == "dead":
            continue
        cwd = e.get("cwd")
        if not cwd:
            continue
        nom, _accent = _projet_de(cwd, config)
        par_projet[nom] = par_projet.get(nom, 0) + 1
    # `get(..., 0)` et non `get(...)` : un projet dont aucun arbre n'est occupé
    # a bien ZÉRO conversation, ce qui est une information sûre — pas un trou.
    return [dict(g, conversations=par_projet.get(g.get("project"), 0))
            for g in groupes]


def _neufs_valides(neufs):
    """L'ensemble des noms de projets « jamais servis », lu DÉFENSIVEMENT.

    `neufs` traverse tout le chemin depuis `layout.json`, un fichier que
    l'utilisateur édite à la main et que le serveur relit à chaque démarrage.
    On lit le type promis par le contrat — une liste de chaînes — ou on ne lit
    rien : une chaîne (`"Alphabet"`) ou un dictionnaire répondraient à `in` sans
    lever, et marqueraient le mauvais projet en silence. `None` (« l'appelant ne
    passe pas la liste ») rend le même ensemble vide qu'une liste vide : ici,
    contrairement à `conversations`, il n'y a rien à distinguer — le drapeau dit
    un FAIT de cycle de vie, et « on ne sait pas » se dit « pas neuf », soit la
    valeur qui ne fait rien apparaître.
    """
    if not isinstance(neufs, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(n for n in neufs if isinstance(n, str) and n)


def _marquer_jamais_servi(copies, neufs):
    """Pose `jamais_servi` sur des groupes DÉJÀ COPIÉS — écriture EN PLACE.

    Ce que le drapeau dit : ce projet a été adopté et aucune conversation Claude
    n'y a encore été observée. Le filtre « avec conversation » masquait un projet
    dès son adoption — il n'a évidemment aucune conversation —, lui retirant la
    colonne vide qui porte le nom de son lanceur `claude-<projet>`. Le serveur
    publie donc le FAIT ; c'est le client qui décide de ne pas masquer dessus.
    La POLITIQUE d'affichage ne descend pas ici.

    Toujours présent et toujours booléen, comme `conversations_inconnues` :
    « pas neuf » est une valeur du contrat, jamais l'absence d'une clé. Un client
    qui lirait une clé absente obtiendrait `undefined`, donc faux, donc le bug
    qu'on corrige — en silence.

    ÉCRIT EN PLACE, et c'est sûr uniquement parce que l'appelant unique est
    `_deriver_a_l_appel`, qui vient de recevoir de `_compter_conversations` des
    dicts neufs lui appartenant. Ne jamais appeler cette fonction sur les groupes
    du relevé mémorisé : ils sont partagés par tous les appelants (cf.
    `_compter_conversations`, point 4).
    """
    ensemble = _neufs_valides(neufs)
    for g in copies:
        g["jamais_servi"] = g.get("project") in ensemble
    return copies


def _deriver_a_l_appel(groupes, config, sessions, neufs):
    """Les deux champs qui appartiennent à l'APPEL et non au relevé.

    `conversations` et `jamais_servi` sont dérivés au retour de `scan()`, sur ses
    DEUX chemins — le froid comme celui du cache — et n'entrent jamais dans le
    relevé mémorisé. Celui-ci est partagé pendant 30 s par tous les appelants,
    et les deux valeurs changent d'un appelant à l'autre : un projet peut cesser
    d'être neuf entre deux appels, exactement comme son nombre de conversations
    peut changer. Ce que le cache ne contient pas ne peut pas être servi périmé.

    Un seul point d'entrée pour les deux, parce que l'ORDRE est un invariant :
    `_compter_conversations` produit les copies, `_marquer_jamais_servi` y écrit.
    Les enchaîner ici évite une seconde copie de surface et empêche qu'un futur
    appelant marque par erreur les groupes du cache.
    """
    return _marquer_jamais_servi(
        _compter_conversations(groupes, config, sessions), neufs)


def _signature_sessions(sessions):
    """Ce dont les ÉTATS D'ARBRES dépendent dans l'instantané, et rien d'autre.

    Deux appels de même signature produiraient, arbre par arbre, le même `etat`,
    le même `liberable`, le même `retenu`, la même `alerte` et les mêmes
    `compteurs`. C'est la condition qu'un `force` doit vérifier avant d'accepter
    le relevé mémorisé de quelqu'un d'autre.

    CE QU'ELLE RETIENT, et pourquoi c'est exactement ça. `_balayer` ne fait
    qu'UNE chose de `sessions` : il indexe les entrées par `normpath(cwd)` et
    accroche à chaque arbre celles dont le chemin coïncide. Le `cwd` normalisé
    décide donc de l'arbre occupé, et le `sid` distingue deux conversations dans
    le même arbre — un nombre qui se lit à l'écran (« 2 conversations y
    travaillent ») et qu'un ensemble de cwd seuls écraserait.

    CE QU'ELLE IGNORE : `state`, `title`, `glyphe`, `ctx_pct`, `since`. Ils
    voyagent bien jusqu'au relevé, dans `a["conv"]`, mais ils ne déplacent aucun
    arbre — et ils changent à chaque seconde. Les inclure ferait rebalayer sur
    un pourcentage de contexte qui monte, c'est-à-dire sur presque tous les
    `force` d'une rafale : le dédoublonnage serait mort de sa précision. C'est
    la même coupe que fait `majVeilleChantier` côté board pour décider quand
    forcer, et les deux doivent rester d'accord.
    (Si `_balayer` venait un jour à écarter les entrées `dead` — il ne le fait
    pas, il les traite comme des occupants —, `state` devrait entrer ici.)

    Un TUPLE TRIÉ plutôt qu'un `set` : indifférent à l'ordre de la liste, qui
    dépend du tri d'affichage du serveur et non des arbres, mais sensible à la
    multiplicité, qu'un ensemble effacerait. Les entrées sans `cwd` sont
    ignorées, comme `_balayer` les ignore : on ne sait pas où elles travaillent,
    les compter ferait rebalayer pour un changement qui ne touche aucun arbre.

    `None` (« on ne sait pas quelles conversations tournent ») n'a pas de
    signature — voir `_meme_instantane`, qui décide de ce cas-là.
    """
    if sessions is None:
        return None
    paires = []
    for e in sessions:
        cwd = e.get("cwd") if isinstance(e, dict) else None
        if cwd:
            paires.append((os.path.normpath(cwd), str(e.get("sid") or "")))
    return tuple(sorted(paires))


def _meme_instantane(signature):
    """Le relevé mémorisé a-t-il vu le même lot de sessions ?

    À appeler sous `_VERROU`.

    LE CAS `None` — c'est-à-dire « l'appelant ne sait pas quelles conversations
    tournent » — se tranche ici, et il ne se tranche pas par égalité. Le rendre
    égal à la signature vide le confondrait avec « aucune session » et ferait
    rebalayer dès que le cache en connaît une ; en faire une valeur qui ne
    s'égale à rien ferait rebalayer TOUJOURS. Or dans les deux cas ce
    rebalayage produirait un relevé où AUCUN arbre n'est occupé — strictement
    moins vrai que celui du cache, calculé lui avec un instantané réel — et qui
    ne serait même pas mémorisé, `_balayer_et_memoriser` refusant les relevés
    dégradés. On paierait 160 ms et 81 processus git pour régresser.

    Un appelant qui ne sait rien n'a rien à apporter au relevé : il ne peut pas
    le rapprocher de son événement, seulement l'en éloigner. On lui sert donc le
    cache, avec le `conversations: null` et le `conversations_inconnues: false`
    qui disent exactement ce qui est su et par qui (cf. `_compter_conversations`,
    point 3). C'est la même doctrine que `serveur._instantane_aveugle` : une
    ignorance ne déclenche pas d'effet visible.
    """
    if signature is None:
        return True
    return signature == _CACHE["signature"]


def dernier():
    """Le dernier relevé SI le cache est frais, sinon None. NE BALAIE JAMAIS.

    Existe pour le bandeau d'attention, qui se construit à chaque instantané —
    une fois par seconde. `scan()` y est interdit : il lance un balayage git de
    tous les arbres dès que le cache expire, et la boucle SSE n'a pas à payer
    ça. None veut dire « on ne sait pas encore », et le bandeau se contente
    alors de ce qu'il sait — il ne prétend rien.

    Rien à retirer du relevé mémorisé : ni `conversations` ni `jamais_servi` n'y
    entrent (cf. `_deriver_a_l_appel`), donc `dernier()` ne peut pas les servir
    périmés. C'était douze lignes de commentaire et un filtrage de cinq dicts
    par seconde pour défaire un travail qu'on venait de faire.
    """
    with _VERROU:
        if _CACHE["data"] is None or (time.time() - _CACHE["at"]) >= TTL:
            return None
        d = dict(_CACHE["data"])
        d["age_s"] = int(time.time() - _CACHE["at"])
        return d


def _servir_du_cache(config, sessions, maintenant, neufs):
    """Le relevé mémorisé, habillé pour CET appelant. À appeler sous `_VERROU`.

    `conversations` et `jamais_servi` sont dérivés ici, jamais lus : le cache ne
    les porte pas. `conversations_inconnues`, lui, vient du relevé tel quel —
    c'est une propriété du relevé, pas de l'appel (cf. `_compter_conversations`,
    point 3), et le cache ne mémorise que des relevés complets.
    """
    d = dict(_CACHE["data"])
    d["age_s"] = int(time.time() - _CACHE["at"])
    d["now"] = maintenant
    d["groupes"] = _deriver_a_l_appel(d.get("groupes") or [], config, sessions,
                                      neufs)
    return d


def scan(config, sessions=None, us_de=None, force=False, neufs=None):
    """L'inventaire complet, servi depuis un cache de 30 s.

    `sessions` : la liste des sessions du dernier instantané. `None` signifie
    « on ne sait pas » et NON « aucune » : dans ce cas aucun arbre ne peut être
    marqué `en_cours`, ce que le drapeau `conversations_inconnues` dit à
    l'écran. Étiqueter en silence un arbre occupé comme `réserve` serait faux,
    pas dégradé. Le `conversations` de chaque groupe porte la même distinction :
    un entier, ou None quand on ne sait pas. Il est DÉRIVÉ à chaque appel, sur
    les deux chemins, et n'entre jamais dans le cache.

    `force` court-circuite le TTL. Il n'est resservi depuis le relevé mémorisé
    que si celui-ci est récent ET a vu le même lot de sessions : voir
    `FENETRE_FORCE` et `_signature_sessions`.

    `us_de` : la règle d'extraction du n° d'US, injectée par le serveur pour
    qu'il n'en existe qu'une seule implémentation (cf. docs/SCHEMA.md).

    `neufs` : les noms des projets adoptés qu'aucune conversation n'a encore vus
    (`layout.json`, clé `neufs`). Passé À CHAQUE APPEL par le serveur, comme
    `sessions`, et jamais mémorisé dans le relevé : ce module ne lit pas
    `layout.json` et n'a rien à retenir de ce fait-là. Il en dérive le
    `jamais_servi` de chaque groupe (cf. `_deriver_a_l_appel`).
    """
    maintenant = int(time.time())
    # Un `force` accepte un relevé de moins de 2 s, un appel normal de moins de
    # 30 s. Une seule expression, pour qu'il n'y ait qu'un endroit où se tromper.
    fenetre = FENETRE_FORCE if force else TTL
    # Calculée une fois, hors du verrou et hors de la boucle d'attente : elle ne
    # dépend que de l'argument.
    signature = _signature_sessions(sessions)
    with _VERROU:
        attentes = 0
        while True:
            # LA SIGNATURE NE CONDITIONNE QUE LE CHEMIN `force`. Un appel normal
            # sert le relevé mémorisé quel que soit le lot qui l'a produit :
            # c'est tout le sens d'un TTL, et `conversations`, lui, est de toute
            # façon recalculé pour cet appelant-ci. Étendre la signature au
            # chemin normal ferait balayer une fois par conversation qui démarre
            # même quand personne ne regarde l'onglet.
            if (_CACHE["data"] is not None
                    and (time.time() - _CACHE["at"]) < fenetre
                    and (not force or _meme_instantane(signature))):
                return _servir_du_cache(config, sessions, maintenant, neufs)
            if not _EN_COURS["balayages"] or attentes >= ATTENTES_MAX:
                break
            # UN BALAYAGE TOURNE DÉJÀ : on attend son résultat au lieu d'en
            # lancer un second identique. La fenêtre de temps seule ne suffisait
            # pas — elle ne dédoublonne que ce qui arrive APRÈS la fin du
            # premier balayage, or le cas mesuré (3 onglets, une conversation
            # qui démarre : 243 processus git) est fait d'appels qui se
            # recouvrent. Au réveil on repasse par le test ci-dessus : le relevé
            # tout juste mémorisé a 0 s, il entre dans la fenêtre même la plus
            # étroite — et s'il a vu un AUTRE lot de sessions que le nôtre, on
            # sort balayer, ce qui est le comportement voulu : la coalescence
            # mutualise le travail, elle ne fait pas passer un relevé pour un
            # autre. Le prix est une attente de la durée du balayage en cours
            # (~0,16 s) avant de lancer le nôtre, et c'est le seul cas où elle
            # ne sert à rien.
            attentes += 1
            _VERROU.wait(ATTENTE_BALAYAGE)
        # Un COMPTEUR et non un booléen : celui qui sort de la boucle par
        # épuisement de sa patience balaie alors qu'un autre balaie encore, et
        # un booléen remis à False par le premier des deux qui finit rouvrirait
        # la porte à un troisième balayage. La coalescence dégrade ici — deux
        # balayages au lieu d'un — mais elle ne se désarme pas.
        _EN_COURS["balayages"] += 1

    try:
        return _balayer_et_memoriser(config, sessions, us_de, maintenant,
                                     signature, neufs)
    finally:
        # Y COMPRIS SUR ÉCHEC : un balayage qui lève doit réveiller ceux qui
        # l'attendaient, sinon ils patientent pour rien avant de repartir.
        with _VERROU:
            _EN_COURS["balayages"] -= 1
            _VERROU.notify_all()


def _balayer_et_memoriser(config, sessions, us_de, maintenant, signature, neufs):
    """Le chemin froid : balayage git, mise en cache, réponse de l'appelant.

    Tourne HORS du verrou — un balayage dure 155-164 ms et tient 8 processus
    git de front ; le tenir sous verrou figerait `dernier()`, appelé une fois
    par seconde par la boucle SSE. L'exclusion des balayages concurrents est
    assurée en amont par `_EN_COURS`, pas par le verrou.
    """
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

    # LES GROUPES MÉMORISÉS NE PORTENT NI `conversations` NI `jamais_servi`, et
    # c'est ce qui rend l'invariant vrai PAR CONSTRUCTION plutôt que par
    # vigilance : ce que le cache ne contient pas ne peut pas être servi périmé
    # à l'appelant suivant. Les deux sont dérivés au retour, ici comme sur le
    # chemin du cache — une seule fonction, deux chemins, la même règle.
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
            # Écrite dans le MÊME bloc que le relevé : une signature qui ne
            # décrirait pas le `data` d'à côté ferait resservir à un `force` un
            # relevé calculé sur un autre monde, ce que tout ceci existe pour
            # empêcher. Non-None par construction, `sessions` ne l'étant pas.
            _CACHE["signature"] = signature
    # La réponse de CET appelant, dérivée du relevé qu'on vient de mémoriser.
    # `dict(data)` puis remplacement de `groupes` : le relevé mémorisé ne doit
    # pas hériter des clés qu'on vient de refuser de lui donner.
    reponse = dict(data)
    reponse["groupes"] = _deriver_a_l_appel(groupes, config, sessions, neufs)
    return reponse


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
    config = _config_reelle()
    debut = time.time()
    avec = scan(config, sessions=[])
    print("\nrelecture depuis le cache : %.2f ms" % ((time.time() - debut) * 1000))

    # Le dédoublonnage des balayages forcés, sur la config réelle : le premier
    # `force` de cette auto-vérification date de moins de FENETRE_FORCE et a vu
    # le même lot de sessions (`[]`), celui-ci doit donc être resservi. S'il
    # rebalaie, on le voit au temps (165 ms).
    debut = time.time()
    scan(config, sessions=[], force=True)
    ms = (time.time() - debut) * 1000
    print("second `force` sous %.0f s      : %.2f ms — %s"
          % (FENETRE_FORCE, ms, "dédoublonné" if ms < 20 else "REBALAYÉ, À CORRIGER"))

    # LA RAFALE, le cas qui motive tout le dédoublonnage : N onglets réagissent
    # au même changement, donc présentent la MÊME signature. Un seul balayage
    # doit être payé pour tous — ici zéro, celui du premier `force` servant
    # encore.
    rafale = 5
    at_avant = _CACHE["at"]
    debut = time.time()
    for _ in range(rafale):
        scan(config, sessions=[], force=True)
    ms_rafale = (time.time() - debut) * 1000
    # La DATE du relevé mémorisé, et non le chronomètre : un poste sans dépôt
    # sous ses racines balaierait en 2 ms et un seuil de temps le déclarerait
    # dédoublonné à tort. `at` ne bouge que quand un balayage a vraiment eu lieu.
    rafale_gratuite = _CACHE["at"] == at_avant
    print("%d `force` de même signature   : %.2f ms au total (%.2f ms/appel)"
          % (rafale, ms_rafale, ms_rafale / rafale))

    # Et la garantie que la fenêtre seule ne donnait pas : un lot de sessions
    # DIFFÉRENT rebalaie, même sous la fenêtre. Le cwd est réel pour que le
    # rattachement se fasse comme en production ; le sid ne peut appartenir à
    # aucune conversation.
    faux = [{"sid": "auto-verification", "cwd": os.getcwd(), "state": "working"}]
    debut = time.time()
    scan(config, sessions=faux, force=True)
    ms_autre = (time.time() - debut) * 1000
    autre_rebalaye = _CACHE["signature"] == _signature_sessions(faux)
    print("`force` à signature différente : %.2f ms — %s"
          % (ms_autre, "rebalayé" if autre_rebalaye else "DÉDOUBLONNÉ, À CORRIGER"))

    # « None n'est pas zéro », sur les deux chemins, plus l'invariant de cache.
    # Ces trois lignes-là sont la raison d'être du champ : les vérifier ici
    # coûte deux lectures de cache et attrape une régression que l'affichage
    # ci-dessus, tout vert, ne montrerait jamais.
    sans = scan(config, sessions=None)

    # `jamais_servi` : même doctrine que `conversations` — dérivé à l'appel,
    # jamais mémorisé. On marque un projet RÉEL de la config pour que le
    # rattachement soit exercé pour de bon ; sans projet, le contrôle
    # s'auto-déclare vide plutôt que de passer pour de mauvaises raisons.
    premier = (avec["groupes"][0]["project"] if avec["groupes"] else None)
    marque = scan(config, sessions=[], neufs=[premier] if premier else [])
    # LU EN DERNIER, après l'appel qui marque : c'est le seul moment où une
    # écriture en place dans les groupes du cache serait visible.
    memorise = (_CACHE["data"] or {}).get("groupes") or []
    controles = [
        ("un entier quand on sait",
         all(isinstance(g.get("conversations"), int) for g in avec["groupes"])),
        ("None quand on ne sait pas",
         all(g.get("conversations") is None for g in sans["groupes"])),
        ("le relevé mémorisé ne porte pas la clé",
         all("conversations" not in g for g in memorise)),
        ("le cache-hit ne lève pas `conversations_inconnues`",
         sans.get("conversations_inconnues") is False),
        ("une rafale de `force` à même signature ne rebalaie pas",
         rafale_gratuite),
        ("un `force` à signature différente rebalaie", autre_rebalaye),
        ("`jamais_servi` est présent et booléen sur chaque groupe",
         all(g.get("jamais_servi") in (True, False) for g in avec["groupes"])),
        ("faux par défaut, quand aucun projet n'est neuf",
         all(g["jamais_servi"] is False for g in avec["groupes"])),
        ("le relevé mémorisé ne porte pas `jamais_servi`",
         all("jamais_servi" not in g for g in memorise)),
        ("un `neufs` nommant un projet ne marque QUE celui-là",
         premier is None or [g["project"] for g in marque["groupes"]
                             if g["jamais_servi"]] == [premier]),
    ]
    for libelle, ok in controles:
        print("  %s %s" % ("OK  " if ok else "ÉCHEC", libelle))


if __name__ == "__main__":
    main()
