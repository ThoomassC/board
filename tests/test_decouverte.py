#!/usr/bin/env python3
"""Contrat de `decouverte.scan()` : proposer des dépôts, sans jamais mentir ni écrire.

La découverte est un geste à deux détentes. `scan()` PROPOSE — il balaie le
disque et rend une liste de dépôts git non déclarés. L'adoption, elle, est un
autre geste (`POST /api/projet`), humain, qui seul écrit dans config.json. Ces
tests gardent la première détente et l'empêchent de déborder sur la seconde.

Trois familles d'invariants, et ce sont elles qui donnent leur nom aux cas :

  · CE QU'ON PROPOSE — un dépôt git, et rien d'autre. Un dossier de code sans
    `.git` est un dossier de passage, pas un projet ; c'est déjà la règle écrite
    dans `serveur.candidats_projets` (« on ne propose QUE des dépôts git »), et
    la découverte ne doit pas en inventer une seconde.

  · CE QU'ON NE PROPOSE PAS — ce qui est déjà couvert par une racine déclarée
    (racine comprise, et TOUT ce qui vit dessous : une racine peut valoir
    `~/Documents/Projets_Perso` en entier), le répertoire personnel lui-même,
    et les zones mesurées comme bruit sur le poste réel : dossiers cachés à
    quelque niveau que ce soit, `node_modules`, `~/Library`, `~/.Trash`. Sans
    ces exclusions le balayage remonte `~/.codex/.tmp/…`, `~/.islands-dark-temp`
    et `~/.local/share/ruby-advisory-db`. Enfin, on ne descend jamais DANS un
    dépôt trouvé : sinon `antomappat` et `antomappat/antomappat-front` seraient
    proposés tous les deux.

  · CE QU'ON AVOUE — un dossier illisible est sauté, compté dans `illisibles`
    et raconté dans `degrade` ; une borne atteinte (profondeur, budget de temps)
    se DIT elle aussi, au lieu de rendre une liste tronquée qui aurait l'air
    complète. C'est la doctrine constante du dépôt (`_instantane_aveugle`,
    `sessions_indisponibles`) : un relevé incomplet se déclare.

L'ORDRE est un contrat, pas un détail d'affichage : `dernier_commit_at`
décroissant — ce qu'on a touché récemment se propose en premier — puis `name`
croissant à égalité de date. La date vient de la mtime de `.git/HEAD`, avec repli
sur celle de `.git` ; aucun appel à `git`, la contrainte « pas de binaire
externe » est ferme.

Hermétisme. Aucun test ne lit le disque de l'utilisateur : chaque cas fabrique
son arborescence sous `tempfile.TemporaryDirectory()` et la passe en `racine`.
Un `.git` de test est un simple dossier, avec au plus un `HEAD` dedans dont on
règle la mtime : rien ici ne dépend du binaire `git`, et le seul mock du fichier
est celui de l'horloge, une frontière, pour dépasser le budget de temps sur une
arborescence qui se balaie en trois millisecondes.
Les imports de `serveur` et `decouverte` se font HOME détourné, pour que
le chargement d'un module ne puisse pas aller lire `~/.claude/board`.
"""
import copy
import hashlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE_DEPOT, "server"))

# Détourne HOME LE TEMPS DES IMPORTS seulement : `serveur` construit un `Board()`
# au niveau module, qui lit config.json / seen.json / layout.json sous
# `~/.claude/board`. On ne veut ni lire ces fichiers, ni dépendre d'eux.
# Volontairement pas d'import au niveau module de CE fichier : `serveur` est
# importé au premier test qui en a besoin, pour ne pas voler à
# `test_serveur_sessions` le droit d'importer `serveur` sous SON faux HOME.
_FAUX_HOME = tempfile.TemporaryDirectory()


def _importer(nom):
    """Importe un module du serveur sans laisser son chargement toucher le poste."""
    reel = os.environ.get("HOME")
    os.environ["HOME"] = _FAUX_HOME.name
    try:
        return __import__(nom)
    finally:
        if reel is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = reel


def _empreinte(racine):
    """Le contenu exact de l'arborescence : chemins, tailles, octets.

    Sert à prouver qu'un scan n'écrit rien — pas même un cache, pas même un
    fichier de marquage. On ne compare pas les mtimes : les lire suffit à
    documenter l'intention, les comparer rendrait le test dépendant de la
    granularité horaire du système de fichiers.
    """
    vu = {}
    for dossier, sous, fichiers in os.walk(racine):
        for f in sorted(fichiers):
            chemin = os.path.join(dossier, f)
            with open(chemin, "rb") as fh:
                octets = fh.read()
            vu[os.path.relpath(chemin, racine)] = hashlib.sha256(octets).hexdigest()
        for d in sorted(sous):
            vu[os.path.relpath(os.path.join(dossier, d), racine) + os.sep] = "dossier"
    return vu


class ScanDeDecouverte(unittest.TestCase):

    def setUp(self):
        # Import tardif : le module n'existe pas encore, chaque cas doit donc
        # échouer POUR CETTE RAISON-LÀ, nommément, et non faire sauter la
        # découverte des 21 tests déjà verts.
        self.decouverte = _importer("decouverte")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.racine = os.path.realpath(self.tmp.name)

    # ------------------------------------------------------------- fabrique
    def _dossier(self, *segments):
        chemin = os.path.join(self.racine, *segments)
        os.makedirs(chemin, exist_ok=True)
        return chemin

    def _depot(self, *segments, tete=True, commit_at=None):
        """Un dépôt git de test : un dossier qui contient un `.git`. Rien de plus.

        `commit_at` fabrique la date que le scan doit lire : la mtime de
        `.git/HEAD`. `tete=False` laisse le `.git` vide, pour exercer le repli
        sur la mtime du dossier `.git` lui-même.
        """
        chemin = self._dossier(*segments)
        git = os.path.join(chemin, ".git")
        os.makedirs(git, exist_ok=True)
        if tete:
            chemin_tete = os.path.join(git, "HEAD")
            with open(chemin_tete, "w") as fh:
                fh.write("ref: refs/heads/main\n")
            if commit_at is not None:
                os.utime(chemin_tete, (commit_at, commit_at))
        elif commit_at is not None:
            os.utime(git, (commit_at, commit_at))
        return chemin

    def _horloge_qui_saute(self, bond=3600.0):
        """Une horloge qui avance d'une heure à chaque coup d'œil.

        Frontière, et frontière seulement : c'est le seul moyen de dépasser un
        budget de temps sur une arborescence de test qui se balaie en quelques
        millisecondes. Les trois horloges de la bibliothèque standard sont
        détournées ensemble pour que le test ne présume pas laquelle est
        utilisée. Le premier appel rend le temps de départ : le budget ne peut
        pas être déclaré dépassé avant même d'avoir commencé.
        """
        etat = {"n": 0}

        def maintenant():
            etat["n"] += 1
            return 1_700_000_000.0 + bond * (etat["n"] - 1)

        correctifs = [mock.patch.object(time, nom, maintenant)
                      for nom in ("time", "monotonic", "perf_counter")]
        for c in correctifs:
            c.start()
            self.addCleanup(c.stop)

    def _fichier(self, contenu, *segments):
        chemin = os.path.join(self.racine, *segments)
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w") as fh:
            fh.write(contenu)
        return chemin

    def _maison(self, chemin):
        """Fait passer HOME par le dossier voulu pour la durée du test."""
        reel = os.environ.get("HOME")
        os.environ["HOME"] = chemin

        def rendre():
            if reel is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = reel

        self.addCleanup(rendre)

    def _noms(self, resultat):
        return sorted(c["name"] for c in resultat["candidats"])

    def _scan(self, config=None):
        return self.decouverte.scan(config or {"projects": []}, racine=self.racine)

    # ------------------------------------------------------- ce qu'on propose
    def test_un_depot_git_non_declare_est_propose(self):
        # Le cas nominal, et le garde-fou de tous les autres : sans lui, une
        # implémentation qui ne proposerait JAMAIS rien passerait toute la suite.
        self._depot("Documents", "mon-projet")

        resultat = self._scan()

        self.assertEqual(len(resultat["candidats"]), 1)
        candidat = resultat["candidats"][0]
        self.assertEqual(candidat["name"], "MON-PROJET")
        self.assertEqual(os.path.realpath(candidat["root"]),
                         os.path.join(self.racine, "Documents", "mon-projet"))
        self.assertEqual(candidat["depot"], "mon-projet")

    def test_la_forme_du_retour_respecte_le_contrat(self):
        # Les quatre clés sont TOUJOURS là : `degrade` à None est une affirmation
        # (« j'ai tout lu »), pas une clé oubliée. C'est la même règle que
        # `sessions_indisponibles` dans le serveur.
        self._depot("Code", "board")

        resultat = self._scan()

        self.assertEqual(sorted(resultat),
                         ["candidats", "degrade", "duree_ms", "illisibles", "scannes"])
        self.assertIsInstance(resultat["scannes"], int)
        self.assertGreater(resultat["scannes"], 0)
        self.assertIsInstance(resultat["duree_ms"], int)
        self.assertGreaterEqual(resultat["duree_ms"], 0)
        # Un scan sain l'affirme deux fois, et les deux comptent : aucun dossier
        # sauté, et rien à raconter. `illisibles` est un ENTIER de premier
        # niveau — le client ne doit pas parser une phrase pour obtenir un compte.
        self.assertEqual(resultat["illisibles"], 0)
        self.assertIsNone(resultat["degrade"])
        # `collision` fait partie du contrat au même titre que les autres : elle
        # est TOUJOURS présente, à None quand le nom proposé ne heurte aucun
        # projet déclaré (cf. le cas de collision plus bas).
        self.assertEqual(sorted(resultat["candidats"][0]),
                         ["collision", "depot", "dernier_commit_at", "name",
                          "root", "root_court"])

    def test_un_dossier_sans_git_n_est_jamais_propose(self):
        # Trois fichiers de code, zéro `.git` : c'est un dossier de travail, pas
        # un projet. Le dépôt voisin prouve que le balayage a bien eu lieu.
        self._fichier("print(1)\n", "Documents", "scripts", "outil.py")
        self._fichier("{}\n", "Documents", "scripts", "package.json")
        self._depot("Documents", "vrai-projet")

        self.assertEqual(self._noms(self._scan()), ["VRAI-PROJET"])

    def test_root_court_abrege_le_repertoire_personnel(self):
        # Le chemin affiché dans le board : `~/Documents/…`, pas
        # `/Users/thomascaron/Documents/…`. `root` reste absolu, lui, puisque
        # c'est ce qui partira dans config.json.
        self._maison(self.racine)
        self._depot("Documents", "board")

        candidat = self._scan()["candidats"][0]

        self.assertEqual(candidat["root_court"], "~/Documents/board")
        self.assertTrue(os.path.isabs(candidat["root"]))

    # --------------------------------------------------- ce qu'on ne propose pas
    def test_une_racine_declaree_n_est_pas_reproposee(self):
        depot = self._depot("Documents", "board")
        config = {"projects": [{"name": "BOARD", "root": depot}]}

        self.assertEqual(self._scan(config)["candidats"], [])

    def test_un_depot_sous_une_racine_declaree_n_est_pas_propose(self):
        # Le piège réel : une racine déclarée peut être un dossier PARENT qui
        # contient plusieurs dépôts (`~/Documents/Projets_Perso` chez cet
        # utilisateur). Tout ce qui vit dessous est déjà couvert.
        parent = self._dossier("Documents", "Projets_Perso")
        self._depot("Documents", "Projets_Perso", "travels_in_world")
        self._depot("Documents", "Projets_Perso", "portfolio")
        self._depot("Documents", "ailleurs")
        config = {"projects": [{"name": "TRAVELS_IN_WORLD", "root": parent}]}

        self.assertEqual(self._noms(self._scan(config)), ["AILLEURS"])

    def test_une_racine_declaree_ecrite_avec_un_tilde_couvre_ce_qui_est_dessous(self):
        # config.json est écrit à la main : `~/Documents/…` y est la forme
        # normale. Une racine non étendue ne couvrirait rien et la découverte
        # reproposerait des projets déjà déclarés.
        self._maison(self.racine)
        self._depot("Documents", "Projets_Perso", "board")
        self._depot("Documents", "ailleurs")
        config = {"projects": [{"name": "BOARD", "root": "~/Documents/Projets_Perso"}]}

        self.assertEqual(self._noms(self._scan(config)), ["AILLEURS"])

    def test_le_repertoire_personnel_n_est_jamais_un_candidat(self):
        # `~` porte souvent un `.git` (dotfiles versionnés). « UTILISATEUR »
        # n'est pas un projet — c'est déjà l'un des trois refus explicites de
        # `serveur.candidats_projets`.
        self._maison(self.racine)
        os.makedirs(os.path.join(self.racine, ".git"), exist_ok=True)
        self._depot("Code", "vrai-projet")

        self.assertEqual(self._noms(self._scan()), ["VRAI-PROJET"])

    def test_les_dossiers_caches_ne_sont_jamais_explores(self):
        # Les trois faux positifs mesurés sur le poste réel. Le dossier caché
        # peut apparaître à N'IMPORTE QUEL niveau de la descente : ici la racine
        # (`.islands-dark-temp`), un cran plus bas (`.codex/.tmp/…`) et deux
        # crans plus bas (`Documents/.local/share/…`).
        self._depot(".islands-dark-temp")
        self._depot(".codex", ".tmp", "sandbox")
        self._depot("Documents", ".local", "share", "ruby-advisory-db")
        self._depot("Documents", "visible")

        self.assertEqual(self._noms(self._scan()), ["VISIBLE"])

    def test_node_modules_n_est_jamais_explore(self):
        # Une dépendance installée n'est pas un projet de l'utilisateur, même
        # quand elle embarque son propre `.git`.
        self._depot("app", "node_modules", "une-lib")
        self._depot("lib")

        self.assertEqual(self._noms(self._scan()), ["LIB"])

    def test_library_et_la_corbeille_du_repertoire_personnel_sont_exclus(self):
        self._maison(self.racine)
        self._depot("Library", "Caches", "un-depot")
        self._depot(".Trash", "ancien-projet")
        self._depot("Code", "garde")

        self.assertEqual(self._noms(self._scan()), ["GARDE"])

    def test_le_balayage_ne_descend_pas_dans_un_depot_trouve(self):
        # Worktree ou sous-module imbriqué : un dépôt trouvé arrête la descente,
        # sinon `antomappat` ET `antomappat/antomappat-front` seraient proposés,
        # et l'utilisateur devrait deviner lequel des deux adopter.
        self._depot("antomappat")
        self._depot("antomappat", "antomappat-front")
        self._depot("antomappat", "paquets", "coeur")

        resultat = self._scan()

        self.assertEqual(self._noms(resultat), ["ANTOMAPPAT"])
        self.assertEqual(os.path.realpath(resultat["candidats"][0]["root"]),
                         os.path.join(self.racine, "antomappat"))

    # ------------------------------------------------------------- le nommage
    def test_le_nom_propose_est_celui_de_la_regle_du_serveur(self):
        # La règle de nommage existe déjà (`serveur._nom_devine`) et elle est
        # contraignante : elle doit produire un nom que `creer_projet`
        # ACCEPTERA. Ce test compare les noms proposés à ceux que rend cette
        # fonction — une seconde implémentation qui divergerait sur les accents,
        # la ponctuation ou la troncature à 32 caractères échouerait ici.
        serveur = _importer("serveur")
        bases = ["Éditeur", "board.v2", "mon projet", "a" * 40]
        for base in bases:
            self._depot("Code", base)
        attendus = sorted(serveur._nom_devine(os.path.join(self.racine, "Code", b))
                          for b in bases)

        self.assertEqual(self._noms(self._scan()), attendus)
        self.assertNotIn(None, attendus, "cas de test mal choisi : rien à comparer")

    def test_un_dossier_dont_le_nom_est_irrecevable_n_est_pas_propose(self):
        # `_nom_devine` rend None quand le nom ne passerait pas
        # `RE_NOM_PROJET` (ici : un seul caractère). Proposer un candidat que le
        # formulaire d'adoption refusera est une impasse offerte à l'utilisateur.
        serveur = _importer("serveur")
        self._depot("Code", "x")
        self._depot("Code", "correct")
        self.assertIsNone(serveur._nom_devine(os.path.join(self.racine, "Code", "x")),
                          "cas de test mal choisi : ce nom est recevable")

        self.assertEqual(self._noms(self._scan()), ["CORRECT"])

    def test_un_nom_en_collision_avec_un_projet_declare_est_signale(self):
        # Deux projets du même nom dans config.json, c'est une colonne qui en
        # avale une autre. La découverte n'a pas à trancher — mais elle doit
        # DIRE la collision, pour que le formulaire d'adoption arrive pré-rempli
        # avec le conflit visible plutôt qu'avec un doublon silencieux.
        # Forme figée : `collision` vaut le nom déjà déclaré, sinon None.
        declare = self._depot("ailleurs", "board")
        self._depot("Code", "board")
        self._depot("Code", "portfolio")
        config = {"projects": [{"name": "BOARD", "root": declare}]}

        par_nom = {c["name"]: c for c in self._scan(config)["candidats"]}

        self.assertEqual(sorted(par_nom), ["BOARD", "PORTFOLIO"])
        self.assertEqual(par_nom["BOARD"]["collision"], "BOARD")
        self.assertIsNone(par_nom["PORTFOLIO"]["collision"])

    # --------------------------------------------------------- ce qu'on avoue
    def test_dernier_commit_at_est_la_mtime_de_head(self):
        # La date se lit sur `.git/HEAD`, que git réécrit à chaque commit et à
        # chaque changement de branche. Pas de `git log` : aucun binaire externe.
        self._depot("Code", "board", commit_at=1_700_000_000)

        candidat = self._scan()["candidats"][0]

        self.assertEqual(candidat["dernier_commit_at"], 1_700_000_000)

    def test_dernier_commit_at_retombe_sur_le_dossier_git_quand_head_manque(self):
        # Un dépôt fraîchement cloné, un `.git` en cours d'écriture : HEAD peut
        # manquer. On rend alors la date du dossier `.git`, qui est une
        # approximation HONNÊTE — et surtout pas maintenant, qui ferait passer
        # le dépôt pour le plus récemment touché de la liste.
        self._depot("Code", "sans-tete", tete=False, commit_at=1_600_000_000)

        candidat = self._scan()["candidats"][0]

        self.assertEqual(candidat["dernier_commit_at"], 1_600_000_000)

    def test_l_ordre_met_le_plus_recent_devant_et_departage_par_nom(self):
        # L'ordre est un contrat : ce qu'on a touché récemment se propose en
        # premier, parce que c'est très probablement le projet qu'on cherche à
        # adopter. À égalité de date, le nom tranche — sinon l'ordre dépendrait
        # de celui, arbitraire, dans lequel le système rend les entrées d'un
        # dossier, et la liste danserait d'un scan à l'autre.
        #
        # LES TROIS EX ÆQUO SONT À TROIS PROFONDEURS DIFFÉRENTES, ET CE N'EST
        # PAS DÉCORATIF. Mesuré sur ce poste : le système ne rend pas les
        # entrées d'un dossier dans l'ordre alphabétique (APFS les rend par
        # hachage). Trois dépôts frères auraient donc pu sortir déjà triés par
        # accident, et un tri qui ignore le nom aurait passé le test — c'est
        # arrivé. Aux profondeurs 1, 2 et 3, en revanche, aucun parcours ne peut
        # produire AA, BB, CC : un balayage de haut en bas rend BB avant CC avant AA,
        # un balayage qui remonterait rendrait CC avant BB. Seul un tri PAR NOM
        # donne cet ordre. Remettre les trois au même niveau reperdrait le
        # pouvoir de discrimination du test.
        self._depot("ancien", commit_at=1_500_000_000)
        self._depot("recent", commit_at=1_700_000_000)
        self._depot("bb", commit_at=1_600_000_000)
        self._depot("enveloppe", "cc", commit_at=1_600_000_000)
        self._depot("enveloppe", "tunnel", "aa", commit_at=1_600_000_000)

        noms = [c["name"] for c in self._scan()["candidats"]]

        self.assertEqual(noms, ["RECENT", "AA", "BB", "CC", "ANCIEN"])

    def test_une_date_tres_ancienne_ne_passe_pas_pour_une_date_manquante(self):
        # 1970 est une DATE, pas une absence : un dépôt à la mtime rasée doit
        # finir en queue de liste sans disparaître ni prendre la place d'un
        # candidat sans date. C'est le pendant de « None n'est pas zéro » vu de
        # l'autre côté : zéro n'est pas None non plus.
        self._depot("epoque-zero", commit_at=1)
        self._depot("normal", commit_at=1_700_000_000)

        candidats = self._scan()["candidats"]

        self.assertEqual([c["name"] for c in candidats], ["NORMAL", "EPOQUE-ZERO"])
        self.assertEqual(candidats[1]["dernier_commit_at"], 1)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root traverse les droits : l'échec de lecture n'est pas simulable")
    def test_un_dossier_illisible_est_saute_et_reporte_dans_degrade(self):
        # Un montage tombé, un dossier à 000 : le scan rend ce qu'il a pu voir et
        # le DIT. Une exception qui remonterait ferait échouer toute la
        # découverte à cause d'un seul dossier ; un `degrade` à None ferait
        # passer un relevé troué pour un relevé complet.
        self._depot("Code", "lisible")
        mur = self._dossier("Code", "interdit")
        self._depot("Code", "interdit", "cache-derriere-le-mur")
        os.chmod(mur, 0o000)
        self.addCleanup(os.chmod, mur, 0o755)

        resultat = self._scan()

        self.assertEqual(self._noms(resultat), ["LISIBLE"])
        self.assertEqual(resultat["illisibles"], 1)
        self.assertIsInstance(resultat["degrade"], str)
        self.assertTrue(resultat["degrade"].strip(),
                        "un dégradé vide ne dit rien : le motif doit être nommé")

    def test_la_profondeur_maximale_atteinte_est_dite_dans_degrade(self):
        # La borne elle-même appartient au module, pas au test : on la LIT
        # (comme `test_chantier` lit `chantier.TTL`) et on fabrique une
        # arborescence qui la dépasse à coup sûr. Ce que le test garde n'est pas
        # la valeur, c'est le refus de rendre une liste tronquée qui aurait
        # l'air complète.
        borne = getattr(self.decouverte, "PROFONDEUR_MAX", None)
        self.assertIsInstance(borne, int,
                              "le module doit exposer PROFONDEUR_MAX ; le test "
                              "ne codera pas la valeur en dur")
        self._depot("proche", commit_at=1_700_000_000)
        self._depot(*(["gouffre"] * (borne + 3)), commit_at=1_700_000_000)

        resultat = self._scan()

        self.assertEqual(self._noms(resultat), ["PROCHE"])
        self.assertIsInstance(resultat["degrade"], str)
        self.assertTrue(resultat["degrade"].strip(),
                        "une descente écourtée qui se taît fait passer un relevé "
                        "tronqué pour un relevé complet")

    def test_le_budget_de_temps_depasse_est_dit_dans_degrade(self):
        # Même invariant, l'autre borne. L'horloge est détournée pour que le
        # budget soit dépassé quelle qu'en soit la valeur : le test ne présume
        # ni la durée choisie, ni l'horloge utilisée pour la mesurer.
        self._dossier("a", "b", "c")
        self._dossier("d", "e", "f")
        self._depot("g", commit_at=1_700_000_000)
        self._horloge_qui_saute()

        resultat = self._scan()

        self.assertIsInstance(resultat["degrade"], str)
        self.assertTrue(resultat["degrade"].strip())
        # Un balayage écourté par le temps n'est pas un échec de lecture : les
        # deux dégradés ne se confondent pas, sinon le client compterait des
        # dossiers illisibles qui n'existent pas.
        self.assertEqual(resultat["illisibles"], 0)

    def test_les_liens_symboliques_ne_sont_jamais_suivis(self):
        # Le risque n'est pas théorique : un lien vers `~` ou vers un parent
        # fait descendre le balayage en rond jusqu'à la limite du système. Un
        # lien vers un dépôt, lui, produirait un DOUBLON du même dépôt sous deux
        # chemins, et l'utilisateur devrait deviner lequel adopter.
        reel = self._depot("reel", commit_at=1_700_000_000)
        os.symlink(self.racine, os.path.join(self.racine, "boucle"))
        os.symlink(reel, os.path.join(self.racine, "alias-du-depot"))
        os.symlink(os.path.join(self.racine, "nulle-part"),
                   os.path.join(self.racine, "lien-casse"))

        resultat = self._scan()

        self.assertEqual(self._noms(resultat), ["REEL"])
        self.assertEqual(os.path.realpath(resultat["candidats"][0]["root"]), reel)

    def test_scannes_compte_les_dossiers_visites_et_rien_d_autre(self):
        # `scannes` est un compte de dossiers VISITÉS. Plutôt que de figer une
        # convention sur la racine (compte-t-elle pour un ?), on mesure le
        # DELTA : un dossier ordinaire de plus coûte exactement une visite, un
        # dossier exclu ou enfoui dans un dépôt n'en coûte aucune. C'est ce
        # delta qui dit si le compte est un compte, ou une valeur d'ambiance.
        self._dossier("ordinaire")
        self._depot("depot", commit_at=1_700_000_000)
        avant = self._scan()["scannes"]

        self._dossier("ordinaire", "un-de-plus")
        apres_un_dossier = self._scan()["scannes"]

        self._dossier(".cache")
        self._dossier("ordinaire", "node_modules", "paquet")
        self._dossier("depot", "src", "profond")
        apres_les_exclus = self._scan()["scannes"]

        self.assertEqual(apres_un_dossier, avant + 1)
        self.assertEqual(apres_les_exclus, apres_un_dossier)

    def test_le_scan_n_ecrit_rien(self):
        # La découverte PROPOSE ; l'adoption écrit, et c'est un autre geste
        # (`POST /api/projet`). Pas de cache sur disque, pas de config.json
        # retouché, pas de fichier de marquage — et pas de config mutée en
        # mémoire non plus.
        self._depot("Code", "board")
        self._fichier('{"projects": []}\n', "config.json")
        avant = _empreinte(self.racine)
        config = {"projects": [{"name": "AUTRE", "root": "/nulle/part"}],
                  "fallback_project": "AUTRE"}
        temoin = copy.deepcopy(config)

        self._scan(config)

        self.assertEqual(_empreinte(self.racine), avant)
        self.assertEqual(config, temoin)


if __name__ == "__main__":
    unittest.main()
