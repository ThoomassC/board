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
# `node_modules` exclu, CONFIGURATION NUE (aucun projet déclaré, donc rien
# d'exclu par `_couvert` — c'est ce que fait l'auto-vérification en bas de
# fichier). La même mesure avec la configuration réelle du poste rend 14
# candidats au lieu de 21 : `docs/SCHEMA.md` publie celle-là, et les deux se
# lisent ensemble. Deuxième passe, cache disque chaud — la première mesure la
# lenteur du cache froid, pas celle de la profondeur :
#
#     profondeur   candidats   dossiers visités   durée    `degrade`
#          4          21              300          11 ms   interrompu
#          6          21              843          38 ms   interrompu
#          8          21            3 049          98 ms   interrompu
#         12          21            9 402         226 ms   interrompu
#         16          21            9 459         222 ms   null
#         40          21            9 459         253 ms   null
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

# ------------------------------------------------------ les bornes des familles
# DEUX SEUILS, et ils ne gardent pas le même risque. Le vrai danger du
# regroupement n'est pas de rater une famille — l'utilisateur adopte alors ses
# dépôts un par un, comme avant —, c'est d'en INVENTER une : proposer d'adopter
# un dossier parent, c'est proposer de troquer plusieurs colonnes contre une.
#
# MEMBRES_MIN à 2 : un dépôt seul se propose déjà très bien lui-même, et son
# parent contiendra demain autre chose que lui.
#
# PREFIXE_MIN à 3 caractères APRÈS rognage des séparateurs, et la mesure sur ce
# poste dit les deux côtés de ce choix :
#
#   · à 3, `~/Documents/CESI_MAALSI_cours` (dev_sec_ops_tp + blueprint) et
#     `~/Documents/Projets_Perso` (board + portfolio + dockshelf) sont refusés :
#     aucun préfixe commun, ce sont des projets sans rapport ;
#   · à 3, `~/Documents/CESI_MAALSI_Projects/goodfood` (APP-MOBILE + API-USER)
#     est refusé AUSSI, sur le préfixe « ap » de deux caractères. Cette famille
#     est légitime dans la réalité, et le refus est ASSUMÉ : l'utilisateur a
#     demandé le regroupement « quand le nom se répète », et ici il ne se répète
#     pas. Descendre à 2 attraperait « ap » — donc `api-*` et `application-*`
#     sans rapport — pour gagner ce seul cas, qui se règle à la main dans le
#     formulaire d'adoption, lequel accepte n'importe quelle racine.
MEMBRES_MIN = 2
PREFIXE_MIN = 3

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
# lire l'âge (`age_s`). Ici le contrat de retour est fermé — six clés, pas
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


def _racines_telles_qu_ecrites(config):
    """Les racines de config.json développées mais NON résolues.

    DEUX ESPACES DE CHEMINS, ET LE REFUS DOIT TENIR DANS LES DEUX. `_couvert`
    travaille sur des chemins résolus, ce qui est juste pour décider si un dépôt
    est déjà couvert. Mais le refus de chevauchement des familles, lui, protège
    `serveur.projet_de`, et `projet_de` compare des `normpath` SANS résoudre les
    liens : une racine déclarée qui est un lien symbolique vers l'extérieur du
    dossier parent sort de l'espace résolu — le chevauchement devient invisible
    ici alors qu'il continue d'exister là-bas. Deux mesures constatées sur ce
    genre de configuration : un `bricoloc-web` déclaré qui est un lien vers un
    autre disque, et un `~/documents/…` écrit sans capitale (macOS, système de
    fichiers insensible à la casse, que `realpath` ne canonise pas).

    On rend donc l'autre forme, et le refus regarde les deux. Rendre une racine
    de trop à un REFUS ne coûte qu'un regroupement non proposé — l'utilisateur
    adopte alors ses dépôts un par un, comme avant.
    """
    racines = []
    for p in (config or {}).get("projects") or []:
        brut = (p or {}).get("root") or ""
        if not brut:
            continue
        racines.append(os.path.normpath(os.path.expanduser(brut)))
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


def _abrege(chemin, maison):
    """`~/Documents/…` plutôt que `/Users/thomascaron/Documents/…`, pour l'œil.

    Une seule règle d'abréviation dans ce module, partagée par les candidats et
    par les familles : une ligne de la bande qui afficherait le chemin en entier
    quand ses voisines l'abrègent serait la seule à le faire, et l'utilisateur y
    lirait une différence qui n'existe pas.
    """
    if chemin == maison or chemin.startswith(maison + os.sep):
        return "~" + chemin[len(maison):]
    return chemin


# ----------------------------------------------------------------- les familles
# REGROUPER DIX DÉPÔTS FRÈRES EN UNE SEULE PROPOSITION.
#
# Mesure qui justifie tout ce qui suit, `~` complet avec la vraie config.json :
# 14 candidats, dont 10 sont les dépôts `bricoloc-*` de
# `~/CESI_MAALSI_Projects/BricoLoc/`. La liste de propositions est donc à 71 %
# le bruit d'un SEUL projet, et l'utilisateur ne peut y répondre qu'en créant
# dix colonnes pour un projet qui n'en veut qu'une. Le moteur sait déjà vivre
# avec une racine qui couvre plusieurs dépôts (`_couvert` ici, `projet_de` dans
# le serveur, et TRAVELS_IN_WORLD dont la racine est `~/Documents/Projets_Perso`
# en entier) : ce qui manquait, c'était la PROPOSITION.
#
# UNE FAMILLE EST UNE PROPOSITION EN PLUS, JAMAIS UNE AMPUTATION. `candidats`
# garde ses dix membres : un relevé qui cacherait des dépôts derrière un
# regroupement mentirait sur le contenu du disque, et c'est le client qui décide
# de la présentation.
#
# COROLLAIRE DE `degrade`, à écrire parce qu'il ne se voit pas dans la forme du
# retour : quand `degrade` n'est pas None, une famille est établie sur une liste
# PARTIELLE de candidats — des dépôts frères ont pu échapper au balayage. C'est
# au client de le dire ; la détection, elle, ne change pas pour autant, sinon un
# unique dossier illisible à l'autre bout de `~` ferait disparaître un
# regroupement parfaitement établi.
SEPARATEURS_DE_NOM = "-_."


def _prefixe_commun(depots):
    """Le préfixe de nom partagé par ces dépôts, en minuscules et rogné, ou "".

    Comparé en MINUSCULES : sur ce poste le dossier parent s'écrit « BricoLoc »
    et ses dépôts « bricoloc-* ». La casse d'un nom de dossier est une décision
    d'auteur, pas une frontière de projet.

    Rogné de ses séparateurs de queue AVANT toute mesure de longueur, et
    l'ordre compte : « bricoloc- » n'est pas un nom de famille, le tiret
    appartient à la construction du nom du dépôt. Sans ce rognage-là, « ab- »
    passerait pour trois caractères significatifs alors qu'il n'en dit que deux
    — et deux lettres partagées sont une coïncidence de nommage.

    LE PRÉFIXE DOIT S'ARRÊTER SUR UNE FRONTIÈRE DE MOT, CHEZ CHAQUE MEMBRE, et
    c'est la condition qui empêche le regroupement d'inventer une famille.
    `os.path.commonprefix` compare CARACTÈRE À CARACTÈRE et ne sait rien des
    mots : `portail-front` et `portugal-x` partagent « port », quatre
    caractères, donc assez pour passer `PREFIXE_MIN` — et l'utilisateur se
    verrait proposer d'adopter leur parent commun sous le nom PORT. Un préfixe
    n'est un nom de projet que s'il est suivi, partout, d'un séparateur ou de
    la fin du nom. Le refus est franc : on ne rogne pas jusqu'à retomber sur une
    frontière plus courte, car ce serait chercher une famille dans deux noms qui
    n'en forment pas une.
    """
    noms = [d.lower() for d in depots]
    prefixe = os.path.commonprefix(noms).rstrip(SEPARATEURS_DE_NOM)
    if not prefixe:
        return ""
    for nom in noms:
        if nom != prefixe and nom[len(prefixe)] not in SEPARATEURS_DE_NOM:
            return ""
    return prefixe


def _nom_recevable(chemin_ou_nom):
    """Le nom de projet que `serveur._nom_devine` en tire, ou None.

    Un seul passage par la règle du serveur, ici comme pour les candidats : une
    seconde implémentation divergerait un jour sur les accents, la ponctuation
    ou la troncature à 32 caractères, et proposerait un nom que `creer_projet`
    refuserait. `serveur` peut être None (import tolérant) : on ne propose alors
    aucun nom plutôt qu'un nom deviné à côté de la règle.
    """
    if serveur is None:
        return None
    return serveur._nom_devine(chemin_ou_nom)


def _familles(candidats, racines_declarees, depart, maison):
    """Les regroupements de dépôts frères à proposer, du plus récent au plus ancien.

    Groupe les candidats par dossier PARENT IMMÉDIAT et ne retient un parent
    que si les HUIT conditions tiennent. Cinq d'entre elles sont des REFUS, et
    c'est normal : rater un regroupement coûte à l'utilisateur les quelques
    clics qu'il faisait déjà hier, en inventer un lui coûte des colonnes. Un
    neuvième garde — le parent ne porte pas de `.git` — est structurellement
    toujours vrai ici ; il s'écrit quand même plus bas, et se lit sur place.

      1. `MEMBRES_MIN` candidats au moins sous ce parent ;
      2. un préfixe de nom commun d'au moins `PREFIXE_MIN` caractères ;
      3. ce préfixe s'arrête sur une FRONTIÈRE DE MOT chez CHAQUE membre, sinon
         `portail-front` et `portugal-x` feraient une famille nommée PORT sur
         quatre caractères de coïncidence — voir `_prefixe_commun` ;
      4. le parent n'est ni le point de départ du balayage, ni le répertoire
         personnel, ni la racine du système. Regrouper à `~` reviendrait à
         proposer « adopte tout ton disque comme un projet » : une racine qui
         couvrirait tout dépôt à venir, et dans laquelle toute conversation
         atterrirait ;
      5. le parent n'est pas déjà couvert par une racine déclarée — sinon on
         propose ce que config.json déclare déjà ;
      6. AUCUN candidat non-membre ne vit sous le parent, à quelque profondeur
         que ce soit : `membres` ne liste que les frères de préfixe commun, mais
         la racine adoptée ramasse tout ce qu'elle couvre — une famille qui
         annonce deux dépôts pour une adoption qui en avale trois se tairait sur
         le troisième, qui quitterait la bande sans avoir été vu ;
      7. AUCUNE racine déclarée ne vit SOUS le parent. C'est le refus le plus
         important et le moins évident : `~/Documents/CESI_MAALSI_Projects`
         contient `projet_clients/Container-calcul`, qui EST un projet déclaré.
         Adopter le parent créerait deux racines qui se CHEVAUCHENT, et
         `serveur.projet_de` rend le PREMIER projet dont la racine préfixe le
         cwd : la colonne d'affectation d'une conversation dépendrait alors de
         l'ordre des lignes de config.json, un fichier écrit à la main. Une
         attribution qui dépend de l'ordre d'un fichier édité à la main est un
         piège, pas une fonctionnalité — la famille n'est donc pas proposée DU
         TOUT, pas même amputée du dépôt fautif ;
      8. un nom recevable se trouve, sinon rien. Proposer un nom que le
         formulaire d'adoption refusera est une impasse — c'est déjà la règle
         écrite pour les candidats.

    LA DÉTECTION PART DES CANDIDATS RETENUS, ET NON DES DOSSIERS DU DISQUE.
    C'est ce qui rend gratuits les refus que `scan` a déjà tranchés : un parent
    dont tous les dépôts sont couverts par une racine déclarée n'a plus un seul
    candidat, donc plus de famille. Une détection bâtie sur les dossiers
    reproposerait ici une famille sans avoir un membre à lui donner.

    `membres` et `depots` suivent l'ORDRE DE `candidats` — l'appelant les passe
    déjà triés. Ce ne sont pas des ensembles : ils s'affichent, et dans le même
    ordre que la liste juste au-dessus, sinon le dépli d'une famille
    contredirait le relevé.

    `collision` est rendue à None : cette fonction ne reçoit pas les noms
    déclarés, et c'est `scan` qui la renseigne, exactement comme pour les
    candidats. None y vaut « aucune collision », pas « clé oubliée ».
    """
    par_parent = collections.OrderedDict()
    for c in candidats:
        par_parent.setdefault(os.path.dirname(c["root"]), []).append(c)

    familles = []
    for parent, membres in par_parent.items():
        if len(membres) < MEMBRES_MIN:
            continue
        if not parent or parent in (depart, maison, os.sep):
            continue
        if _couvert(parent, racines_declarees):
            continue
        # Une racine déclarée STRICTEMENT sous le parent : le chevauchement.
        # `racines_declarees` porte les deux formes d'une même racine, résolue et
        # telle qu'écrite (cf. `_racines_telles_qu_ecrites`) — le refus doit
        # tenir dans l'espace de chemins où vit `projet_de`, pas seulement dans
        # celui où vit `_couvert`.
        if any(r.startswith(parent + os.sep) for r in racines_declarees):
            continue
        # LE PARENT NE PORTE JAMAIS DE `.git` LUI-MÊME. S'il en portait un, le
        # balayage se serait arrêté sur LUI et aucun de ses enfants ne serait
        # candidat : la condition est structurellement toujours vraie ici. Elle
        # s'écrit quand même, pour qu'une réécriture du balayage ne la perde pas
        # en silence — proposer un parent versionné, c'est proposer d'adopter un
        # dépôt comme s'il était un dossier de projets.
        if _marque_git(parent)[0]:
            continue

        depots = [c["depot"] for c in membres]
        prefixe = _prefixe_commun(depots)
        if len(prefixe) < PREFIXE_MIN:
            continue

        # UNE PROPOSITION DIT CE QU'ELLE FAIT, ET UNE RACINE PREND TOUT CE
        # QU'ELLE COUVRE. `membres` ne liste que les frères de préfixe commun,
        # alors que `root` ramasse tout ce qui vit sous le parent, à quelque
        # profondeur que ce soit. Un `~/Code` qui porte `app-web`, `app-api` et
        # `labo/scratchpad` faisait donc annoncer « regrouper 2 dépôts » à une
        # adoption qui en avalait TROIS — et `scratchpad` quittait la bande sans
        # avoir jamais figuré dans la famille, donc sans que l'utilisateur ait vu
        # partir la colonne qu'il aurait pu adopter. C'est « on affirme sur une
        # ignorance », un cran plus loin : l'affirmation est un compte.
        # Le refus est franc plutôt qu'un élargissement de `membres` : ces
        # dépôts-là n'ont PAS le préfixe commun, les faire entrer dans la famille
        # dirait « le nom se répète » là où il ne se répète pas.
        if any(_couvert(c["root"], [parent]) and c not in membres
               for c in candidats):
            continue

        # Le nom du dossier parent d'abord — c'est celui que l'utilisateur
        # reconnaît (« BricoLoc ») —, le préfixe en repli quand ce dossier
        # s'appelle par exemple « v2 » ou « ~/Code/2024 ».
        nom = _nom_recevable(parent) or _nom_recevable(prefixe)
        if not nom:
            continue

        # LE PLUS RÉCENT DES MEMBRES, et rien d'autre : une famille vaut son
        # membre le plus vivant. Dix services dont un seul bouge encore est un
        # projet actif, et c'est celui-là qu'on veut adopter ; la moyenne ou le
        # plus ancien l'enterreraient en bas de la bande. Un membre sans date ne
        # doit pas faire perdre à la famille le rang qu'un autre lui donne — et
        # si aucun n'a de date, elle reste inconnue : ni `0`, qui la trierait en
        # queue avec l'assurance d'une date connue, ni maintenant.
        dates = [c["dernier_commit_at"] for c in membres
                 if c["dernier_commit_at"] is not None]
        familles.append({
            "name": nom,
            "root": parent,
            "root_court": _abrege(parent, maison),
            "depot": os.path.basename(parent),
            "prefixe": prefixe,
            "membres": [c["root"] for c in membres],
            "depots": depots,
            "dernier_commit_at": max(dates) if dates else None,
            "collision": None,
        })

    # Même contrat d'ordre que les candidats, et pour la même raison : ce qu'on
    # a touché récemment se propose en premier. Le départage par `name` n'est
    # pas cosmétique — sans lui l'ordre suivrait celui, arbitraire, dans lequel
    # le système rend les entrées d'un dossier (APFS les rend par hachage sur ce
    # poste), et la bande danserait d'un scan à l'autre.
    familles.sort(key=lambda f: (f["dernier_commit_at"] is None,
                                 -(f["dernier_commit_at"] or 0),
                                 f["name"]))
    return familles


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

        {"candidats": [...], "familles": [...], "scannes": int,
         "illisibles": int, "duree_ms": int, "degrade": str|None}

    Un candidat porte exactement `name`, `root`, `root_court`, `depot`,
    `dernier_commit_at` et `collision` — toujours les six, `collision` et
    `dernier_commit_at` à None valant « aucune » et « inconnue », pas « clé
    oubliée ».

    `familles` regroupe des dépôts frères en une proposition unique (cf. « les
    familles »). Elle est TOUJOURS présente, sur tous les chemins de retour de
    ce module, refus de concurrence compris : une liste vide dit « aucun
    regroupement à proposer », et un client qui devrait tester la présence de la
    clé selon le chemin d'erreur devinerait mal un jour. Les familles sont une
    proposition EN PLUS — `candidats` garde tous leurs membres.

    L'ORDRE EST UN CONTRAT, pas un détail d'affichage : `dernier_commit_at`
    décroissant, les dates inconnues en dernier, puis `name` croissant à
    égalité. Ce qu'on a touché récemment se propose en premier — c'est ce qui
    rend la liste utile plutôt qu'alphabétique. Il vaut pour les familles comme
    pour les candidats, et `membres` suit celui du relevé.

    `racine` vaut le répertoire personnel quand il est None. Le paramètre
    existe pour que le balayage soit dirigeable : les tests fabriquent une
    arborescence sous `tempfile` et n'ont ainsi jamais à lire le vrai disque.

    Ne mute ni `config`, ni le disque.
    """
    t0 = time.monotonic()
    if not _VERROU.acquire(False):
        # Refus immédiat plutôt qu'une mise en file : cf. « la concurrence ».
        return {"candidats": [], "familles": [], "scannes": 0, "illisibles": 0,
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
            nom = _nom_recevable(root)
            if not nom:
                # `_nom_devine` refuse ce qui ne passerait pas `RE_NOM_PROJET`.
                # Proposer un nom que le formulaire d'adoption rejettera, c'est
                # offrir une impasse à l'utilisateur.
                continue
            candidats.append({
                "name": nom,
                "root": root,
                "root_court": _abrege(root, maison),
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

        # LE TRI D'ABORD, LE REGROUPEMENT ENSUITE : `_familles` n'ordonne pas
        # ses membres, elle hérite de l'ordre reçu. Regrouper avant de trier
        # rendrait un dépli de famille rangé autrement que le relevé au-dessus.
        familles = _familles(candidats,
                             racines + _racines_telles_qu_ecrites(config),
                             depart, maison)
        for f in familles:
            # LA COLLISION SE CALCULE ICI, ET NON DANS `_familles` : cette
            # fonction ne reçoit pas les noms déclarés, et lui passer la config
            # entière pour un seul test d'appartenance mélangerait la détection
            # d'un regroupement avec l'état de config.json. Même geste que pour
            # les candidats : la découverte ne tranche pas un doublon de nom,
            # elle le DIT, pour que le formulaire d'adoption arrive avec le
            # conflit visible plutôt qu'avec un doublon silencieux.
            f["collision"] = f["name"] if f["name"] in declares else None

        return {"candidats": candidats,
                "familles": familles,
                "scannes": visites,
                "illisibles": illisibles,
                "duree_ms": int((time.monotonic() - t0) * 1000),
                "degrade": _phrase_degrade(illisibles, borne)}
    finally:
        _VERROU.release()


# ------------------------------------------------------------- essai à la main
def _config_reelle():
    """La config.json du poste, ou une config nue si elle ne se lit pas.

    Même lecture que `chantier._config_reelle`. L'auto-vérification tourne sur
    la VRAIE config en plus de la config nue, et ce n'est pas du confort : les
    refus qui comptent — parent déjà couvert, racine déclarée sous le parent —
    ne s'exercent que s'il y a des racines déclarées. Sur `{"projects": []}` ils
    dorment tous, et l'auto-vérification les déclarerait bons sans les avoir
    regardés.
    """
    import json
    chemin = os.path.expanduser("~/.claude/board/config.json")
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"projects": [], "fallback_project": "AUTRE"}


def _montrer(titre, config):
    d = scan(config)
    print("\n=== %s — %d projet%s déclaré%s" % (
        titre, len(config.get("projects") or []),
        "s" if len(config.get("projects") or []) > 1 else "",
        "s" if len(config.get("projects") or []) > 1 else ""))
    print("candidats : %d   familles : %d   scannés : %d   illisibles : %d   "
          "durée : %d ms" % (len(d["candidats"]), len(d["familles"]),
                             d["scannes"], d["illisibles"], d["duree_ms"]))
    print("dégradé   : %s" % d["degrade"])
    for f in d["familles"]:
        print("  ▸ %-24s %-14s %s" % (f["name"], f["dernier_commit_at"],
                                      f["root_court"]))
        # Le préfixe et les membres sont montrés parce que c'est là que se
        # jugerait un faux positif : une famille dont le préfixe ne dit rien, ou
        # dont les membres n'ont visiblement aucun rapport, se voit d'un coup
        # d'œil ici et nulle part ailleurs.
        print("    préfixe « %s » · %d membres : %s"
              % (f["prefixe"], len(f["depots"]), ", ".join(f["depots"])))
        if f["collision"]:
            print("    collision : %s est déjà déclaré" % f["collision"])
    for c in d["candidats"]:
        print("  %-32s %-14s %s" % (c["name"], c["dernier_commit_at"],
                                    c["root_court"]))


if __name__ == "__main__":
    _montrer("config nue", {"projects": []})
    _montrer("config réelle du poste", _config_reelle())
