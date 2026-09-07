#!/usr/bin/env python3
"""Découverte des dépôts git du poste : PROPOSER, et rien de plus.

Le board ne connaît que les projets déclarés à la main dans config.json. Ce
module regarde le disque et rend la liste des dépôts git qui n'y sont pas
encore. Il PROPOSE — c'est la première détente d'un geste qui en a deux : la
seconde, l'adoption, est humaine et passe par `POST /api/projet`, seul endroit
du dépôt qui écrive dans config.json. `scan()` n'écrit rien, nulle part, pas
même un cache : un module qui propose n'a pas à laisser de trace.

TROIS RÈGLES DE PRUDENCE, et elles comptent plus que la détection elle-même.

  · ON NE PROPOSE QUE DES DÉPÔTS GIT. C'est déjà la règle de
    `serveur.candidats_projets` (« on ne propose QUE des dépôts git ») et la
    découverte n'en invente pas une seconde : un dossier de code sans `.git`
    est un dossier de passage. Le nom proposé vient de `serveur._nom_devine`,
    la règle qui existe déjà — la réimplémenter ici, c'est se garantir de
    proposer un jour un nom que le formulaire d'adoption refusera.

  · AUCUN BINAIRE EXTERNE, `git` COMPRIS. Contrainte de contrat, et pas
    seulement de portabilité : la date de dernière activité se lit dans la
    mtime de `.git/HEAD`, ce qui rend un balayage de tout `~` gratuit en
    processus (mesure : 2,9 s pour 16 dépôts, contre autant de `git log` à
    lancer) et surtout testable sans dépôt git réel.

  · UN RELEVÉ INCOMPLET SE DÉCLARE. Un dossier illisible est compté dans
    `illisibles` et raconté dans `degrade` ; une borne atteinte le dit aussi.
    Rendre une liste tronquée avec `degrade` à None serait la faire passer pour
    exhaustive — c'est la faute que `_instantane_aveugle` et
    `sessions_indisponibles` corrigent partout ailleurs dans ce dépôt. Même
    exigence sur `dernier_commit_at` : None quand la date est inconnue, jamais
    `0` (qui trierait le projet en queue comme un abandonné) ni maintenant (qui
    le ferait passer pour actif).
"""
import collections
import os
import threading
import time

# `serveur` importe `decouverte` dans son bloc d'imports tolérants, et
# `decouverte` a besoin de `serveur._nom_devine` : le cycle est assumé, et il
# tient parce que rien n'est lu ici au chargement — seulement le nom du module.
# Un `from serveur import _nom_devine` casserait, lui : quand c'est `serveur`
# qui amorce le cycle, la fonction n'est pas encore définie au moment où il
# nous importe. L'import reste au niveau module (et non dans `scan`) pour que
# le chargement de `serveur` — qui construit un `Board()` lisant
# `~/.claude/board` — ait lieu à l'import et non au milieu d'une requête.
try:
    import serveur
except Exception:
    serveur = None


# ------------------------------------------------------------------ les bornes
# DEUX BORNES, parce qu'aucune des deux ne suffit seule : la profondeur protège
# d'une arborescence pathologique (un dossier de build à quarante niveaux), le
# budget protège d'un disque lent ou d'un montage réseau qui répond au ralenti
# — et là aucune profondeur ne borne le temps.
#
# LA PROFONDEUR N'EST PAS UN RÉGLAGE DE VITESSE : c'est le budget qui tient le
# temps. La choisir trop basse ne fait pas gagner un balayage, elle fait perdre
# un signal. Mesures sur ce poste, `~` entier, arrêt sur chaque dépôt et
# `node_modules` exclu :
#
#     profondeur   candidats   dossiers visités   durée    `degrade`
#          4           4              295          36 ms   interrompu
#          6           4              838         112 ms   interrompu
#          8           4            3 043         324 ms   interrompu
#         12           4            9 396         276 ms   interrompu
#         16           4            9 453         263 ms   null
#         40           4            9 453         220 ms   null
#
# La liste est complète dès 4 niveaux et ne bouge plus ; en revanche le poste
# porte des arbres hors dépôt qui descendent jusqu'à ~15 niveaux, si bien qu'une
# borne à 8 rendrait un « balayage interrompu » À CHAQUE APPEL sans jamais rien
# ajouter à la liste. Un avertissement qu'on voit toujours n'est plus lu, et le
# jour où la descente serait vraiment tronquée personne ne le remarquerait.
# PROFONDEUR_MAX est donc à 16 : le premier palier où ce poste se balaie en
# entier, avec le budget pour tenir le temps si un autre poste est plus profond.
#
# BUDGET_S à 10 s : trente fois la durée mesurée ici (324 ms), et trois fois la
# pire mesure connue sur ce poste à cache froid (2,9 s). Il n'est pas là pour
# arbitrer le cas nominal, seulement pour que le cas anormal finisse.
PROFONDEUR_MAX = 16
BUDGET_S = 10.0

# Dossiers écartés à toute profondeur. `node_modules` : une dépendance
# installée n'est pas un projet de l'utilisateur, même quand elle embarque son
# propre `.git`. Les dossiers cachés sont écartés par une règle et non par une
# liste (cf. `_ecarte`) : sans elle le balayage remonte `~/.codex/.tmp/…`,
# `~/.islands-dark-temp` et `~/.local/share/ruby-advisory-db`, les trois faux
# positifs mesurés. `~/.Trash` en fait partie — un projet supprimé ne se
# repropose pas.
EXCLUS = {"node_modules"}

# Écarté au PREMIER niveau du balayage seulement : `~/Library` pèse des
# dizaines de milliers de dossiers et ne contient aucun projet de
# l'utilisateur, mais un dossier nommé `Library` plus bas peut parfaitement en
# être un.
EXCLUS_RACINE = {"Library"}


# ------------------------------------------------------------- la concurrence
# UN SEUL BALAYAGE À LA FOIS, et les concurrents sont REFUSÉS, pas mis en file.
#
# C'est l'opération la plus lente du serveur (2,9 s) et elle tourne dans un
# thread de requête. Le scénario réel n'est pas le polling — la découverte
# demande `autorise=1`, donc un geste humain — mais l'impatience : trois
# secondes sans retour visuel et l'utilisateur reclique. Dix clics, ce sont dix
# parcours de `~` en parallèle, et l'I/O disque ne se partage pas : ils
# s'écroulent tous ensemble.
#
# Pourquoi PAS la coalescence de `chantier.scan` (attendre le balayage en cours
# et resservir son relevé) : elle repose sur un cache dont l'appelant peut
# lire l'âge (`age_s`). Ici le contrat de retour est fermé — cinq clés, pas
# une de plus — donc un relevé resservi serait indiscernable d'un relevé frais,
# et la découverte se mettrait à affirmer l'état d'un disque qu'elle n'a pas
# regardé. Un refus nommé est moins agréable et plus honnête : `candidats` vide
# AVEC un `degrade` qui dit pourquoi, ce que le client sait déjà lire partout
# ailleurs.
_VERROU = threading.Lock()

REFUS_CONCURRENT = "balayage déjà en cours : réessayez dans quelques secondes"


# ------------------------------------------------------------------- outillage
def _mtime(chemin):
    """La mtime d'un chemin en secondes entières, ou None si elle est illisible."""
    try:
        return int(os.stat(chemin).st_mtime)
    except OSError:
        return None


def _dernier_commit_at(root):
    """La date de dernière activité du dépôt, ou None.

    `.git/HEAD` est réécrit à chaque commit, chaque merge et chaque changement
    de branche : c'est la trace disque la plus proche de « dernière activité »
    accessible sans lancer `git`. Repli sur la mtime du `.git` lui-même, qui
    couvre le worktree et le sous-module (où `.git` est un FICHIER
    `gitdir: …`, sans HEAD à côté de lui). None si aucune des deux ne se lit :
    on ne fabrique pas une date pour remplir la case.
    """
    marque = os.path.join(root, ".git")
    at = _mtime(os.path.join(marque, "HEAD"))
    if at is None:
        at = _mtime(marque)
    return at


def _marque_git(chemin):
    """(porte un `.git`, ce `.git` est un dossier) — deux réponses, pas une.

    La nuance est celle qui sépare un dépôt d'un worktree, et sans binaire
    `git` pour la trancher : un dépôt principal porte un `.git` DOSSIER, un
    worktree ou un sous-module porte un `.git` FICHIER qui contient
    `gitdir: …`. Les deux arrêtent la descente ; seul le premier se propose.

    Mesuré sur ce poste : ne pas faire la différence remontait neuf worktrees de
    `APP-MOBILE.worktrees/` et `API-USER.worktrees/` comme autant de projets
    distincts, aux côtés de leur dépôt principal. C'est déjà la règle écrite
    dans `serveur.candidats_projets` (« on propose le dépôt principal et non le
    worktree ») ; le dépôt principal, lui, est proposé de son côté.
    """
    marque = os.path.join(chemin, ".git")
    # `isdir` / `exists` avalent l'erreur et rendent False : un `.git` illisible
    # est traité comme absent, ce qui fait descendre dans le dossier plutôt que
    # de le proposer sur une preuve qu'on n'a pas.
    if os.path.isdir(marque):
        return True, True
    return os.path.exists(marque), False


def _racines_declarees(config):
    """Les racines de config.json, développées et résolues.

    `expanduser` n'est pas un détail : config.json est écrit à la main et
    `~/Documents/…` y est la forme normale. Une racine laissée sous sa forme
    tildée ne couvrirait rien, et la découverte reproposerait des projets déjà
    déclarés.
    """
    racines = []
    for p in (config or {}).get("projects") or []:
        brut = (p or {}).get("root") or ""
        if not brut:
            continue
        racines.append(os.path.realpath(os.path.expanduser(brut)))
    return racines


def _couvert(chemin, racines):
    """Le chemin est-il une racine déclarée, ou vit-il sous l'une d'elles ?

    Une racine déclarée peut être un dossier PARENT qui contient plusieurs
    dépôts (`~/Documents/Projets_Perso` chez cet utilisateur) : tout ce qui vit
    dessous est déjà couvert, et n'a donc rien à faire dans une liste de
    propositions.
    """
    return any(chemin == r or chemin.startswith(r + os.sep) for r in racines)


def _ecarte(nom, profondeur):
    """Faut-il refuser d'entrer dans ce sous-dossier, sur son seul nom ?"""
    if nom.startswith("."):
        return True                       # caché, à quelque profondeur que ce soit
    if nom in EXCLUS:
        return True
    return profondeur == 1 and nom in EXCLUS_RACINE


def _phrase_degrade(illisibles, borne):
    """La phrase lisible qui dit ce qui manque au relevé, ou None si rien.

    `illisibles` reste À CÔTÉ, en entier : le client ne doit jamais avoir à
    parser une phrase pour obtenir un compte. Cette phrase-ci est pour l'œil.
    """
    motifs = []
    if illisibles:
        motifs.append("%d dossier%s illisible%s, sauté%s"
                      % (illisibles, "s" if illisibles > 1 else "",
                         "s" if illisibles > 1 else "", "s" if illisibles > 1 else ""))
    if borne == "profondeur":
        motifs.append("balayage interrompu : profondeur maximale de %d niveaux "
                      "atteinte, ce qui vit plus bas n'a pas été regardé"
                      % PROFONDEUR_MAX)
    elif borne == "budget":
        motifs.append("balayage interrompu : budget de %.0f s dépassé, "
                      "la liste est incomplète" % BUDGET_S)
    return " ; ".join(motifs) or None


# --------------------------------------------------------------- le balayage
def _balayer(depart, racines):
    """Descend depuis `depart` et rend (dépôts, dossiers visités, illisibles, borne).

    ON NE SUIT JAMAIS LES LIENS SYMBOLIQUES — `is_dir(follow_symlinks=False)`,
    écrit explicitement parce que c'est le défaut qu'une réécriture réintroduit
    sans y penser : un lien vers `~` ou vers un parent fait boucler la descente
    sans fin, et un lien vers un dossier déjà balayé le proposerait deux fois
    sous deux chemins. Corollaire utile : tous les chemins produits ici sont
    déjà des chemins réels, puisqu'aucun n'a traversé de lien.

    Parcours en LARGEUR, et non en profondeur : si une borne coupe le balayage,
    ce qui manque est ce qui vivait le plus loin de `~`, pas une branche entière
    tirée au sort par l'ordre de `scandir`.
    """
    depots = []
    visites = 0
    illisibles = 0
    borne = None
    t0 = time.monotonic()
    file = collections.deque([(depart, 0)])

    while file:
        chemin, profondeur = file.popleft()
        try:
            with os.scandir(chemin) as entrees:
                sous = []
                for e in entrees:
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        # L'entrée existait à la lecture du dossier et ne
                        # répond plus : elle est illisible, pas absente.
                        illisibles += 1
                        continue
                    sous.append(e.name)
        except OSError:
            # Droits, montage tombé, dossier supprimé pendant la descente : on
            # saute et on le COMPTE. Laisser l'exception remonter ferait échouer
            # toute la découverte à cause d'un seul dossier.
            illisibles += 1
            continue

        # Visité = on y est entré. Ni les entrées lues, ni les dépôts trouvés.
        visites += 1

        for nom in sous:
            enfant = os.path.join(chemin, nom)
            if _ecarte(nom, profondeur + 1):
                continue
            if _couvert(enfant, racines):
                continue                  # déjà déclaré, ou sous une racine déclarée
            porte_git, principal = _marque_git(enfant)
            if porte_git:
                # ON NE DESCEND PAS DANS UN DÉPÔT TROUVÉ. Sinon `antomappat` ET
                # `antomappat/antomappat-front` seraient tous deux proposés, et
                # l'utilisateur devrait deviner lequel des deux adopter.
                if principal:
                    depots.append(enfant)
                continue
            if profondeur + 1 > PROFONDEUR_MAX:
                borne = borne or "profondeur"
                continue
            file.append((enfant, profondeur + 1))

        # Le budget se vérifie APRÈS un dossier et non avant : le premier tour
        # doit avoir lieu, sinon un budget déjà consommé rendrait un relevé
        # vide sans même avoir regardé la racine.
        if time.monotonic() - t0 >= BUDGET_S:
            borne = "budget"              # écrase la profondeur : c'est plus grave
            break

    return depots, visites, illisibles, borne


def scan(config, racine=None):
    """Les dépôts git du poste que config.json ne déclare pas encore.

        {"candidats": [...], "scannes": int, "illisibles": int,
         "duree_ms": int, "degrade": str|None}

    Un candidat porte exactement `name`, `root`, `root_court`, `depot`,
    `dernier_commit_at` et `collision` — toujours les six, `collision` et
    `dernier_commit_at` à None valant « aucune » et « inconnue », pas « clé
    oubliée ».

    L'ORDRE EST UN CONTRAT, pas un détail d'affichage : `dernier_commit_at`
    décroissant, les dates inconnues en dernier, puis `name` croissant à
    égalité. Ce qu'on a touché récemment se propose en premier — c'est ce qui
    rend la liste utile plutôt qu'alphabétique.

    `racine` vaut le répertoire personnel quand il est None. Le paramètre
    existe pour que le balayage soit dirigeable : les tests fabriquent une
    arborescence sous `tempfile` et n'ont ainsi jamais à lire le vrai disque.

    Ne mute ni `config`, ni le disque.
    """
    t0 = time.monotonic()
    if not _VERROU.acquire(False):
        # Refus immédiat plutôt qu'une mise en file : cf. « la concurrence ».
        return {"candidats": [], "scannes": 0, "illisibles": 0,
                "duree_ms": 0, "degrade": REFUS_CONCURRENT}
    try:
        maison = os.path.realpath(os.path.expanduser("~"))
        depart = maison if racine is None else os.path.realpath(
            os.path.expanduser(racine))
        racines = _racines_declarees(config)
        declares = {(p or {}).get("name") for p in (config or {}).get("projects") or []}

        depots, visites, illisibles, borne = _balayer(depart, racines)

        candidats = []
        for root in depots:
            # `depart` n'est jamais candidat, même s'il porte un `.git` : `~`
            # est souvent un dépôt de dotfiles, et « UTILISATEUR » n'est pas un
            # projet. C'est structurel ici — la descente commence PAR lui et ne
            # peut donc pas le trouver comme enfant — mais la règle est écrite
            # pour qu'une réécriture ne la perde pas.
            if root == depart:
                continue
            nom = serveur._nom_devine(root) if serveur is not None else None
            if not nom:
                # `_nom_devine` refuse ce qui ne passerait pas `RE_NOM_PROJET`.
                # Proposer un nom que le formulaire d'adoption rejettera, c'est
                # offrir une impasse à l'utilisateur.
                continue
            court = root
            if court == maison or court.startswith(maison + os.sep):
                court = "~" + court[len(maison):]
            candidats.append({
                "name": nom,
                "root": root,
                "root_court": court,
                "depot": os.path.basename(root),
                "dernier_commit_at": _dernier_commit_at(root),
                # La découverte ne tranche pas un doublon de nom — elle le DIT,
                # pour que le formulaire d'adoption arrive avec le conflit
                # visible plutôt qu'avec un doublon silencieux.
                "collision": nom if nom in declares else None,
            })

        candidats.sort(key=lambda c: (c["dernier_commit_at"] is None,
                                      -(c["dernier_commit_at"] or 0),
                                      c["name"]))
        return {"candidats": candidats,
                "scannes": visites,
                "illisibles": illisibles,
                "duree_ms": int((time.monotonic() - t0) * 1000),
                "degrade": _phrase_degrade(illisibles, borne)}
    finally:
        _VERROU.release()


# ------------------------------------------------------------- essai à la main
if __name__ == "__main__":
    d = scan({"projects": []})
    print("candidats : %d   scannés : %d   illisibles : %d   durée : %d ms"
          % (len(d["candidats"]), d["scannes"], d["illisibles"], d["duree_ms"]))
    print("dégradé   : %s" % d["degrade"])
    for c in d["candidats"]:
        print("  %-32s %-14s %s" % (c["name"], c["dernier_commit_at"], c["root_court"]))
