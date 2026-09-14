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


class _Arborescence(unittest.TestCase):
    """La fabrique commune : une arborescence jetable, et rien qui touche le poste.

    Elle ne porte AUCUN cas de test — c'est voulu. Les classes de cas en
    héritent pour partager une seule fabrique de dépôts : deux fabriques
    divergentes finiraient par ne plus fabriquer le même « dépôt de test », et
    la moitié de la suite garderait alors un invariant sur une arborescence que
    l'autre moitié ne connaît pas.
    """

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


class ScanDeDecouverte(_Arborescence):
    """Ce qu'on propose, ce qu'on ne propose pas, ce qu'on avoue."""

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
        # Les six clés sont TOUJOURS là : `degrade` à None est une affirmation
        # (« j'ai tout lu »), pas une clé oubliée. C'est la même règle que
        # `sessions_indisponibles` dans le serveur. `familles` est du même bois :
        # une liste vide dit « aucun regroupement à proposer », et le client la
        # lit sans avoir à tester la présence de la clé.
        self._depot("Code", "board")

        resultat = self._scan()

        self.assertEqual(sorted(resultat),
                         ["candidats", "degrade", "duree_ms", "familles",
                          "illisibles", "scannes"])
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


# Les dix dépôts réels de `~/CESI_MAALSI_Projects/BricoLoc/` sur ce poste, et
# leurs noms comptent : c'est sur EUX que le préfixe commun s'arrête à
# « bricoloc- » — backoffice, contracts, gateway, infra… divergent dès la lettre
# suivante. Trois noms inventés plus proches les uns des autres feraient sortir
# un préfixe plus long et le test ne mesurerait plus le cas du poste.
DEPOTS_BRICOLOC = ["bricoloc-backoffice", "bricoloc-contracts", "bricoloc-gateway",
                   "bricoloc-infra", "bricoloc-partner-portal",
                   "bricoloc-service-auth", "bricoloc-service-catalog",
                   "bricoloc-service-partners", "bricoloc-service-transactions",
                   "bricoloc-web"]

CLES_FAMILLE = ["collision", "depot", "depots", "dernier_commit_at", "membres",
                "name", "prefixe", "root", "root_court"]


class FamillesDeDepotsFreres(_Arborescence):
    """Regrouper dix dépôts frères en UNE proposition, sans jamais en fabriquer une fausse.

    Le besoin est mesuré : `~/CESI_MAALSI_Projects/BricoLoc/` porte dix dépôts
    git, et la découverte en propose dix colonnes là où l'utilisateur en veut
    une, de racine le dossier parent. Le moteur sait déjà vivre avec une racine
    qui couvre plusieurs dépôts (`_couvert`, et TRAVELS_IN_WORLD dont la racine
    est `~/Documents/Projets_Perso` en entier) : ce qui manque, c'est la
    PROPOSITION.

    Une famille est une proposition EN PLUS, jamais une amputation : `candidats`
    garde ses dix membres, parce qu'un relevé qui cache des dépôts est un relevé
    qui ment, et parce que c'est le client qui décide de la présentation.

    Le vrai danger de cette fonctionnalité n'est pas de rater un regroupement,
    c'est d'en inventer un. Deux refus tiennent ce risque, et ce sont eux qui
    prennent le plus de cas ici :

      · le FAUX POSITIF de préfixe. Sous `~/Documents/Projets_Perso` vivent
        `board`, `portfolio` et `dockshelf` : trois dépôts frères qui n'ont
        aucun rapport entre eux. Regrouper sur la seule fraternité de dossier
        proposerait « adopte ce parent » à un utilisateur qui perdrait ses trois
        colonnes ;
      · le CHEVAUCHEMENT de racines. Adopter un parent qui contient déjà une
        racine déclarée donnerait deux racines emboîtées, et `serveur.projet_de`
        rend le PREMIER projet dont la racine préfixe le cwd : l'attribution de
        colonne dépendrait alors de l'ordre des lignes de config.json.
    """

    # ------------------------------------------------------------- fabrique
    def _dix_depots(self, *parent):
        """Les dix dépôts BricoLoc sous un même parent ; rend leurs roots."""
        return [self._depot(*parent, nom) for nom in DEPOTS_BRICOLOC]

    def _candidat(self, root, at):
        """Un candidat déjà formé, tel que `scan` le construirait.

        Sert aux seuls cas de date INCONNUE : un candidat à `dernier_commit_at`
        None n'est pas fabricable sur un vrai système de fichiers — pour être
        candidat, un dépôt doit porter un `.git` DOSSIER, donc un `.git` dont
        `os.stat` répond, donc une mtime lisible (cf. `_dernier_commit_at`, qui
        se replie dessus). La règle d'agrégation se garde donc sur `_familles`,
        dont le contrat fixe la signature, et non à travers `scan`.
        """
        serveur = _importer("serveur")
        return {"name": serveur._nom_devine(root), "root": root,
                "root_court": root, "depot": os.path.basename(root),
                "dernier_commit_at": at, "collision": None}

    # -------------------------------------------------------- ce qu'on regroupe
    def test_dix_depots_freres_de_prefixe_commun_font_une_seule_famille(self):
        # Le cas nominal, et le garde-fou de toute cette classe : sans lui, une
        # implémentation qui ne regrouperait JAMAIS rien passerait tous les cas
        # de refus qui suivent.
        roots = self._dix_depots("CESI_MAALSI_Projects", "BricoLoc")

        familles = self._scan()["familles"]

        self.assertEqual(len(familles), 1)
        famille = familles[0]
        self.assertEqual(os.path.realpath(famille["root"]),
                         os.path.join(self.racine, "CESI_MAALSI_Projects", "BricoLoc"))
        self.assertEqual(famille["name"], "BRICOLOC")
        self.assertEqual(famille["depot"], "BricoLoc")
        self.assertEqual(sorted(famille["membres"]), sorted(roots))
        self.assertEqual(sorted(famille["depots"]), sorted(DEPOTS_BRICOLOC))

    def test_une_famille_n_ampute_pas_le_releve_des_candidats(self):
        # La famille PROPOSE un regroupement, elle ne retire rien du relevé :
        # l'utilisateur peut vouloir adopter neuf dépôts sur dix, ou n'en
        # adopter qu'un. Un relevé qui cacherait ses membres derrière la
        # proposition serait un relevé qui mentirait sur le contenu du disque.
        self._dix_depots("CESI_MAALSI_Projects", "BricoLoc")

        resultat = self._scan()

        self.assertEqual(len(resultat["familles"]), 1)
        self.assertEqual(self._noms(resultat),
                         sorted(n.upper() for n in DEPOTS_BRICOLOC))

    def test_une_famille_porte_exactement_les_neuf_cles_du_contrat(self):
        # Forme fermée, comme celle d'un candidat : ni une clé de plus, ni une de
        # moins. `collision` à None est une affirmation (« ce nom ne heurte
        # rien »), pas une clé oubliée.
        self._dix_depots("CESI_MAALSI_Projects", "BricoLoc")

        famille = self._scan()["familles"][0]

        self.assertEqual(sorted(famille), CLES_FAMILLE)
        self.assertIsNone(famille["collision"])

    def test_familles_est_une_liste_vide_quand_il_n_y_a_rien_a_regrouper(self):
        # Le cas de tous les jours : un poste sans monorepo éclaté. La clé reste
        # là, vide — le client affiche zéro ligne de tête sans avoir à
        # distinguer « pas de famille » de « serveur d'une version antérieure ».
        self._depot("Documents", "board", commit_at=1_700_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["BOARD"])

    def test_le_prefixe_rendu_est_rogne_de_ses_separateurs_de_queue(self):
        # « bricoloc- » n'est pas un nom de famille : le séparateur appartient à
        # la construction du nom du dépôt, pas au préfixe. Il est rogné AVANT la
        # mesure de longueur — sinon un préfixe de deux lettres suivi d'un tiret
        # passerait pour trois caractères significatifs (cf. le cas « ab- »).
        self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-web")
        self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-api")

        famille = self._scan()["familles"][0]

        self.assertEqual(famille["prefixe"], "bricoloc")

    def test_la_comparaison_de_prefixe_ignore_la_casse(self):
        # Sur ce poste le dossier parent s'écrit « BricoLoc » et ses dépôts
        # « bricoloc-* » : la casse d'un nom de dossier est une décision
        # d'auteur, pas une frontière de projet. Le préfixe rendu, lui, est
        # normalisé en minuscules pour que le client n'ait pas à le recomparer.
        web = self._depot("CESI_MAALSI_Projects", "BricoLoc", "BricoLoc-web")
        api = self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-api")

        familles = self._scan()["familles"]

        self.assertEqual(len(familles), 1)
        self.assertEqual(familles[0]["prefixe"], "bricoloc")
        self.assertEqual(sorted(familles[0]["membres"]), sorted([api, web]))
        # `depots` rend les basenames TELS QU'ILS SONT sur le disque : c'est ce
        # qui s'affichera, et l'utilisateur doit retrouver ses dossiers.
        self.assertEqual(sorted(familles[0]["depots"]),
                         ["BricoLoc-web", "bricoloc-api"])

    def test_root_court_d_une_famille_abrege_le_repertoire_personnel(self):
        # Même règle d'affichage que pour un candidat : `~/Documents/BricoLoc`
        # dans le board, `root` absolu pour ce qui partira dans config.json. Une
        # famille qui afficherait `/Users/thomascaron/…` serait la seule ligne de
        # la bande à le faire.
        self._maison(self.racine)
        self._dix_depots("Documents", "BricoLoc")

        famille = self._scan()["familles"][0]

        self.assertEqual(famille["root_court"], "~/Documents/BricoLoc")
        self.assertTrue(os.path.isabs(famille["root"]))

    def test_un_nom_de_famille_en_collision_avec_un_projet_declare_est_signale(self):
        # Une famille est un candidat à l'adoption comme un autre : deux projets
        # du même nom dans config.json, c'est une colonne qui en avale une autre.
        # La découverte ne tranche pas — elle DIT la collision, pour que le
        # formulaire arrive avec le conflit visible. Le projet déclaré vit
        # AILLEURS que sous le parent : sous lui, c'est le chevauchement de
        # racines qui annulerait la famille avant qu'on parle de son nom.
        declare = self._depot("ailleurs", "bricoloc")
        self._dix_depots("CESI_MAALSI_Projects", "BricoLoc")
        config = {"projects": [{"name": "BRICOLOC", "root": declare}]}

        famille = self._scan(config)["familles"][0]

        self.assertEqual(famille["name"], "BRICOLOC")
        self.assertEqual(famille["collision"], "BRICOLOC")

    # ---------------------------------------------------- ce qu'on ne regroupe pas
    def test_des_depots_freres_sans_prefixe_commun_ne_font_aucune_famille(self):
        # LE faux positif à ne pas produire, et il existe pour de vrai :
        # `~/Documents/Projets_Perso` porte `board`, `portfolio` et `dockshelf`,
        # trois projets sans le moindre rapport. Les regrouper proposerait à
        # l'utilisateur d'adopter le parent, donc de troquer ses trois colonnes
        # contre une seule nommée PROJETS_PERSO.
        self._depot("Documents", "Projets_Perso", "board", commit_at=1_700_000_000)
        self._depot("Documents", "Projets_Perso", "portfolio", commit_at=1_690_000_000)
        self._depot("Documents", "Projets_Perso", "dockshelf", commit_at=1_680_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["BOARD", "DOCKSHELF", "PORTFOLIO"])

    def test_un_prefixe_commun_trop_court_ne_fait_aucune_famille(self):
        # « ab- » fait trois caractères mais ne dit rien : deux lettres
        # partagées, c'est une coïncidence de nommage, pas une famille. Le
        # rognage a donc lieu AVANT la mesure. Le parent porte ici un nom
        # parfaitement recevable (OUTILS) : c'est bien la règle de préfixe qui
        # doit refuser, et non un repli faute de nom proposable.
        self._depot("Outils", "ab-web", commit_at=1_700_000_000)
        self._depot("Outils", "ab-api", commit_at=1_690_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["AB-API", "AB-WEB"])

    def test_un_prefixe_qui_ne_s_arrete_pas_sur_un_separateur_ne_fait_aucune_famille(self):
        # LA FAMILLE INVENTÉE, et c'est le faux positif le plus vicieux du
        # regroupement : `os.path.commonprefix` compare caractère à caractère et
        # ne sait rien des mots. « portail-front » et « portugal-x » partagent
        # « port » — quatre caractères, donc au-dessus de PREFIXE_MIN —, et
        # l'utilisateur se verrait proposer d'adopter leur parent sous le nom
        # PORT, troquant deux projets sans rapport contre une seule colonne.
        # Un préfixe n'est un nom de projet que s'il s'arrête, chez CHAQUE
        # membre, sur un séparateur ou sur la fin du nom.
        self._depot("Code", "portail-front", commit_at=1_700_000_000)
        self._depot("Code", "portugal-x", commit_at=1_690_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["PORTAIL-FRONT", "PORTUGAL-X"])

    def test_un_membre_dont_le_nom_est_le_prefixe_entier_reste_dans_la_famille(self):
        # L'autre bord de la même règle : le membre qui EST le préfixe n'a aucun
        # caractère après lui, et c'est une frontière valable — la plus franche
        # qui soit. Le lire comme une rupture de frontière refuserait
        # `bricoloc` + `bricoloc-web`, c'est-à-dire le cas où le projet porte le
        # nom nu et ses satellites un suffixe.
        self._depot("Code", "bricoloc", commit_at=1_700_000_000)
        self._depot("Code", "bricoloc-web", commit_at=1_690_000_000)

        familles = self._scan()["familles"]

        self.assertEqual(len(familles), 1)
        self.assertEqual(familles[0]["prefixe"], "bricoloc")
        self.assertEqual(sorted(familles[0]["depots"]), ["bricoloc", "bricoloc-web"])

    def test_un_depot_sans_le_prefixe_sous_le_parent_annule_la_famille(self):
        # UNE PROPOSITION DIT CE QU'ELLE FAIT, ET UNE RACINE PREND TOUT CE
        # QU'ELLE COUVRE. `membres` ne liste que les frères de préfixe commun,
        # `root` ramasse tout ce qui vit sous le parent : sans ce refus, la
        # famille annonçait « regrouper 2 dépôts » pour une adoption qui en
        # avalait TROIS, et `scratchpad` quittait la bande sans avoir jamais
        # figuré dans la famille — donc sans que l'utilisateur ait vu partir la
        # colonne qu'il aurait pu adopter. Le dépôt intrus vit ici DEUX niveaux
        # sous le parent : c'est la couverture qui décide, pas la fratrie.
        self._depot("Code", "app-web", commit_at=1_700_000_000)
        self._depot("Code", "app-api", commit_at=1_690_000_000)
        self._depot("Code", "labo", "scratchpad", commit_at=1_680_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["APP-API", "APP-WEB", "SCRATCHPAD"])

    def test_deux_familles_emboitees_ne_sont_jamais_proposees_ensemble(self):
        # Adopter les deux racines créerait le chevauchement que le refus de la
        # racine déclarée existe pour interdire — ici entre deux PROPOSITIONS,
        # que rien n'avait encore confrontées. Le refus du dépôt non-préfixé s'en
        # charge : les `bricoloc-*` sont couverts par `Code` sans en être des
        # frères de préfixe, donc la famille haute tombe et la profonde reste.
        # C'est la profonde qu'on veut : elle est le vrai projet.
        self._depot("Code", "app-web", commit_at=1_700_000_000)
        self._depot("Code", "app-api", commit_at=1_690_000_000)
        self._depot("Code", "BricoLoc", "bricoloc-web", commit_at=1_680_000_000)
        self._depot("Code", "BricoLoc", "bricoloc-api", commit_at=1_670_000_000)

        familles = self._scan()["familles"]

        self.assertEqual([f["name"] for f in familles], ["BRICOLOC"])
        self.assertEqual(familles[0]["root"],
                         os.path.join(self.racine, "Code", "BricoLoc"))

    def test_une_racine_declaree_voisine_de_prefixe_du_parent_ne_bloque_pas_la_famille(self):
        # LE SÉPARATEUR EST OBLIGATOIRE DANS LE REFUS DE CHEVAUCHEMENT. Sans lui,
        # `Code-old` déclaré passerait pour vivre sous `Code` et ferait perdre un
        # regroupement parfaitement légitime — c'est la même faute qu'un
        # `startswith` nu côté client, et rien ne la gardait.
        self._depot("Code", "app-web", commit_at=1_700_000_000)
        self._depot("Code", "app-api", commit_at=1_690_000_000)
        self._depot("Code-old", "vieux-truc", commit_at=1_680_000_000)
        cfg = {"projects": [{"name": "VIEUX",
                             "root": os.path.join(self.racine, "Code-old")}]}

        familles = self._scan(cfg)["familles"]

        self.assertEqual([f["name"] for f in familles], ["CODE"])

    def test_une_racine_declaree_qui_est_un_lien_annule_la_famille(self):
        # LE REFUS DOIT TENIR DANS L'ESPACE DE CHEMINS OÙ VIT `projet_de`, et il
        # y en a DEUX. `_racines_declarees` résout les liens, `projet_de` compare
        # des `normpath` sans les résoudre : une racine déclarée qui est un lien
        # vers l'extérieur du parent sort de l'espace résolu, le chevauchement
        # devient invisible à la détection alors qu'il continue d'exister à
        # l'attribution — et après adoption, la colonne d'une conversation de ce
        # dépôt dépend de l'ordre des lignes de config.json.
        #
        # C'est le cas qui isole ce refus-là, et c'est pour ça qu'il est écrit
        # avec un LIEN plutôt qu'avec une casse divergente : le balayage ne suit
        # jamais les liens, donc `bricoloc-web` n'est pas candidat, donc le refus
        # du dépôt non-préfixé ne peut pas se déclencher à la place de celui-ci.
        self._depot("BricoLoc", "bricoloc-api", commit_at=1_700_000_000)
        self._depot("BricoLoc", "bricoloc-infra", commit_at=1_690_000_000)
        ailleurs = self._depot("ailleurs", "bricoloc-web", commit_at=1_680_000_000)
        lien = os.path.join(self.racine, "BricoLoc", "bricoloc-web")
        os.symlink(ailleurs, lien)
        cfg = {"projects": [{"name": "BRICOLOC-WEB", "root": lien}]}

        self.assertEqual(self._scan(cfg)["familles"], [])

    def test_un_seul_depot_sous_le_parent_ne_fait_aucune_famille(self):
        # Un dépôt tout seul se propose déjà très bien lui-même. Proposer en
        # plus d'adopter son dossier parent ajouterait une ligne qui déplacerait
        # la racine d'un cran vers le haut, pour rien — et ce parent contiendra
        # demain autre chose que ce projet.
        self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-web",
                    commit_at=1_700_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["BRICOLOC-WEB"])

    def test_une_racine_declaree_sous_le_parent_annule_la_famille(self):
        # LE REFUS LE PLUS IMPORTANT, et le moins évident.
        # `~/Documents/CESI_MAALSI_Projects` contient `projet_clients/Container-calcul`,
        # qui EST un projet déclaré. Adopter le parent créerait deux racines qui
        # se CHEVAUCHENT, et `serveur.projet_de` rend le PREMIER projet dont la
        # racine préfixe le cwd : la colonne d'affectation d'une conversation
        # dépendrait alors de l'ordre des lignes dans config.json, un fichier
        # écrit à la main. Une attribution qui dépend de l'ordre d'un fichier
        # édité à la main est un piège, pas une fonctionnalité — la famille
        # n'est donc pas proposée DU TOUT, pas même amputée du dépôt fautif.
        declare = self._depot("Documents", "CESI_MAALSI_Projects",
                              "projet_clients", "Container-calcul")
        self._depot("Documents", "CESI_MAALSI_Projects", "bricoloc-web",
                    commit_at=1_700_000_000)
        self._depot("Documents", "CESI_MAALSI_Projects", "bricoloc-api",
                    commit_at=1_690_000_000)
        config = {"projects": [{"name": "CONTAINER-CALCUL", "root": declare}]}

        resultat = self._scan(config)

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["BRICOLOC-API", "BRICOLOC-WEB"])

    def test_un_parent_deja_couvert_par_une_racine_declaree_ne_fait_aucune_famille(self):
        # Le parent EST déjà la racine d'un projet : proposer de l'adopter, c'est
        # proposer ce que config.json déclare. Aucun candidat non plus, tout ce
        # qui vit sous une racine déclarée étant couvert (`_couvert`) — et c'est
        # justement ce qui rend le piège discret : une détection bâtie sur les
        # dossiers plutôt que sur les candidats retenus reproposerait ici une
        # famille sans avoir un seul membre à lui donner.
        parent = self._dossier("CESI_MAALSI_Projects", "BricoLoc")
        self._dix_depots("CESI_MAALSI_Projects", "BricoLoc")
        config = {"projects": [{"name": "BRICOLOC", "root": parent}]}

        resultat = self._scan(config)

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(resultat["candidats"], [])

    def test_le_point_de_depart_du_balayage_ne_fait_jamais_famille(self):
        # Le parent des dépôts est le dossier d'où part la descente, c'est-à-dire
        # `~` en usage réel. « Adopte tout ton disque comme un projet » n'est pas
        # une proposition recevable : ce serait une racine qui couvrirait tout
        # dépôt à venir, et toute conversation atterrirait dans cette colonne.
        self._depot("bricoloc-web", commit_at=1_700_000_000)
        self._depot("bricoloc-api", commit_at=1_690_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["BRICOLOC-API", "BRICOLOC-WEB"])

    def test_le_repertoire_personnel_ne_fait_jamais_famille(self):
        # Même refus, mais par l'autre chemin : ici le balayage part d'un cran
        # PLUS HAUT que `~`, si bien que le répertoire personnel est un parent
        # ordinaire de la descente. Sans la règle écrite sur `maison`, la
        # structure du code ne suffirait plus à protéger `~`, et la découverte
        # proposerait d'adopter le dossier personnel entier.
        maison = self._dossier("maison")
        self._maison(maison)
        self._depot("maison", "bricoloc-web", commit_at=1_700_000_000)
        self._depot("maison", "bricoloc-api", commit_at=1_690_000_000)

        resultat = self._scan()

        self.assertEqual(resultat["familles"], [])
        self.assertEqual(self._noms(resultat), ["BRICOLOC-API", "BRICOLOC-WEB"])

    # ------------------------------------------------------------- date et ordre
    def test_la_date_d_une_famille_est_celle_de_son_membre_le_plus_recent(self):
        # La date sert à TRIER la proposition, et une famille vaut son membre le
        # plus vivant : dix services dont un seul bouge encore est un projet
        # actif. Prendre la plus ancienne, ou une moyenne, enterrerait la famille
        # en bas de la bande alors que c'est celle qu'on veut adopter. Le plus
        # récent n'est ni le premier ni le dernier créé sur le disque, pour qu'un
        # « je prends la date du premier membre rencontré » échoue ici.
        self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-api",
                    commit_at=1_600_000_000)
        self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-web",
                    commit_at=1_700_000_000)
        self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-infra",
                    commit_at=1_500_000_000)

        famille = self._scan()["familles"][0]

        self.assertEqual(famille["dernier_commit_at"], 1_700_000_000)

    def test_un_membre_sans_date_n_ecrase_pas_celle_de_la_famille(self):
        # « Inconnue » n'est pas « très ancienne » et surtout pas « aucune » :
        # un membre dont la date ne se lit pas ne doit pas faire perdre à la
        # famille le rang qu'un autre membre lui donne. C'est la même exigence
        # que `dernier_commit_at` du candidat, un cran plus haut.
        api = self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-api")
        web = self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-web")
        candidats = [self._candidat(web, 1_700_000_000), self._candidat(api, None)]

        familles = self.decouverte._familles(candidats, [], self.racine, self.racine)

        self.assertEqual(len(familles), 1)
        self.assertEqual(familles[0]["dernier_commit_at"], 1_700_000_000)

    def test_la_date_d_une_famille_est_le_maximum_et_non_le_premier_membre_venu(self):
        # LE CAS QUI DISCRIMINE VRAIMENT `max`. À travers `scan`, `_familles`
        # reçoit des candidats DÉJÀ triés par récence décroissante : « la date du
        # premier membre rencontré » y est toujours égale au maximum, et une
        # implémentation fausse passerait sans être vue. On appelle donc
        # `_familles` avec une liste volontairement désordonnée — c'est le seul
        # montage où l'agrégation est observable pour elle-même.
        parent = os.path.join(self.racine, "BricoLoc")
        membres = [self._candidat(os.path.join(parent, "bricoloc-web"), 1_680_000_000),
                   self._candidat(os.path.join(parent, "bricoloc-api"), 1_700_000_000),
                   self._candidat(os.path.join(parent, "bricoloc-infra"), 1_690_000_000)]

        familles = self.decouverte._familles(membres, [], self.racine, self.racine)

        self.assertEqual(len(familles), 1)
        self.assertEqual(familles[0]["dernier_commit_at"], 1_700_000_000)

    def test_une_famille_sans_date_se_trie_apres_celles_qui_en_ont_une(self):
        # CE QUE LE TERME « DATE INCONNUE » DE LA CLÉ DE TRI GARDE VRAIMENT, et
        # ce n'est pas ce qu'on croit : `-(None or 0)` vaut zéro, donc déjà le
        # maximum d'un tri croissant, et une famille sans date se rangerait en
        # queue même sans ce terme — TANT QUE toutes les autres dates sont
        # positives. La mtime antérieure à 1970 casse cet accident : sa clé
        # devient POSITIVE et passe derrière le zéro de l'inconnue, qui remonte
        # alors devant une date pourtant connue. D'où la famille de 1969 ici :
        # sans elle, ce cas ne discriminerait rien (vérifié par mutation).
        recente = os.path.join(self.racine, "Recente")
        ancienne = os.path.join(self.racine, "Ancienne")
        muette = os.path.join(self.racine, "Muette")
        membres = [self._candidat(os.path.join(recente, "recente-web"), 1_600_000_000),
                   self._candidat(os.path.join(recente, "recente-api"), 1_500_000_000),
                   self._candidat(os.path.join(ancienne, "ancienne-web"), -1_000),
                   self._candidat(os.path.join(ancienne, "ancienne-api"), -2_000),
                   self._candidat(os.path.join(muette, "muette-web"), None),
                   self._candidat(os.path.join(muette, "muette-api"), None)]

        familles = self.decouverte._familles(membres, [], self.racine, self.racine)

        self.assertEqual([f["name"] for f in familles],
                         ["RECENTE", "ANCIENNE", "MUETTE"])

    def test_une_famille_dont_aucun_membre_n_a_de_date_la_declare_inconnue(self):
        # L'autre côté de la même règle : on ne fabrique pas une date pour
        # remplir la case. Ni `0` — qui trierait la famille en queue comme une
        # abandonnée, mais avec l'assurance d'une date connue — ni maintenant,
        # qui la ferait passer pour la plus vivante du poste.
        api = self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-api")
        web = self._depot("CESI_MAALSI_Projects", "BricoLoc", "bricoloc-web")
        candidats = [self._candidat(web, None), self._candidat(api, None)]

        familles = self.decouverte._familles(candidats, [], self.racine, self.racine)

        self.assertEqual(len(familles), 1)
        self.assertIsNone(familles[0]["dernier_commit_at"])

    def test_l_ordre_des_familles_met_la_plus_recente_devant_et_departage_par_nom(self):
        # Même contrat que pour les candidats, et pour la même raison : ce qu'on
        # a touché récemment se propose en premier. Les deux familles ex æquo
        # sont là pour le départage par nom — sans lui l'ordre suivrait celui,
        # arbitraire, dans lequel le système rend les entrées d'un dossier (APFS
        # les rend par hachage sur ce poste), et la bande danserait d'un scan à
        # l'autre. AZTEK est fabriqué APRÈS PORTAIL pour qu'un ordre hérité de
        # la découverte, et non du tri, se voie ici.
        self._depot("Portail", "portail-front", commit_at=1_650_000_000)
        self._depot("Portail", "portail-back", commit_at=1_650_000_000)
        self._depot("Aztek", "aztek-un", commit_at=1_650_000_000)
        self._depot("Aztek", "aztek-deux", commit_at=1_650_000_000)
        self._depot("BricoLoc", "bricoloc-web", commit_at=1_700_000_000)
        self._depot("BricoLoc", "bricoloc-api", commit_at=1_500_000_000)

        noms = [f["name"] for f in self._scan()["familles"]]

        self.assertEqual(noms, ["BRICOLOC", "AZTEK", "PORTAIL"])

    def test_les_membres_d_une_famille_suivent_l_ordre_des_candidats(self):
        # `membres` et `depots` ne sont pas des ensembles : ils s'affichent, et
        # dans le même ordre que le relevé — récence décroissante, nom croissant
        # à égalité. Deux listes rangées différemment obligeraient le client à
        # retrier, ou lui feraient afficher le dépli d'une famille dans un ordre
        # qui contredit la liste juste au-dessus. Les deux ex æquo tiennent le
        # départage par nom ; `depots` est vérifié en regard pour qu'un index ne
        # puisse pas glisser entre les deux listes.
        self._depot("BricoLoc", "bricoloc-web", commit_at=1_700_000_000)
        self._depot("BricoLoc", "bricoloc-infra", commit_at=1_600_000_000)
        self._depot("BricoLoc", "bricoloc-api", commit_at=1_600_000_000)

        famille = self._scan()["familles"][0]

        self.assertEqual(famille["depots"],
                         ["bricoloc-web", "bricoloc-api", "bricoloc-infra"])
        self.assertEqual(famille["membres"],
                         [os.path.join(self.racine, "BricoLoc", d)
                          for d in famille["depots"]])

    # --------------------------------------------------------- ce qu'on n'écrit pas
    def test_le_scan_n_ecrit_rien_meme_quand_il_propose_une_famille(self):
        # Le doublon du cas des candidats, sur une arborescence à famille, parce
        # que le regroupement est exactement le genre de calcul qu'on est tenté
        # de mettre en cache sur disque — ou d'écrire dans la `config` reçue
        # pour « préparer » l'adoption. La découverte PROPOSE ; `POST /api/projet`
        # reste le seul écrivain de config.json.
        self._dix_depots("CESI_MAALSI_Projects", "BricoLoc")
        self._fichier('{"projects": []}\n', "config.json")
        avant = _empreinte(self.racine)
        config = {"projects": [{"name": "AUTRE", "root": "/nulle/part"}],
                  "fallback_project": "AUTRE"}
        temoin = copy.deepcopy(config)

        resultat = self._scan(config)

        self.assertEqual(len(resultat["familles"]), 1)
        self.assertEqual(_empreinte(self.racine), avant)
        self.assertEqual(config, temoin)


if __name__ == "__main__":
    unittest.main()
