#!/usr/bin/env python3
"""Les PROJETS NEUFS : un projet adopté reste visible jusqu'à sa première conversation.

Le défaut mesuré. L'interrupteur « avec conversation » masque les projets où
aucune conversation ne travaille. On découvre un dépôt du poste, on l'adopte —
et il disparaît aussitôt, puisqu'il n'a évidemment aucune conversation. Or la
colonne vide est ce qui porte le nom du lanceur `claude-<projet>` : le filtre
retire la porte d'entrée du projet qu'on vient d'ajouter.

Le comportement gardé ici. Un projet adopté par la route reste visible, filtre
allumé ou non, jusqu'à ce qu'une conversation Claude y ait tourné au moins une
fois ; ensuite il rejoint le lot commun et redevient masquable.

C'est un FAIT DE CYCLE DE VIE, pas une préférence d'affichage — d'où sa
persistance dans `~/.claude/board/layout.json`, sous la clé `neufs` : la liste
des noms de projets en attente de leur première conversation. La préférence,
elle (`avecConv` côté client), reste volontairement non persistée ; ces tests
n'y touchent pas.

LE CHAMP PUBLIÉ S'APPELLE `jamais_servi`, et c'est un booléen, toujours présent
sur chaque groupe. Il ne s'appelle PAS `neuf` : `neufs` (la liste persistée) et
`neuf` (le drapeau publié) ne diffèrent que d'une lettre, et une lecture
`g.neufs` côté client rendrait `undefined`, c'est-à-dire faux, c'est-à-dire
exactement le bug qu'on corrige — en silence. Le dépôt s'est déjà fait prendre
une fois à ce jeu (`convs` contre `conversations`, cf. board.js). Deux noms
franchement différents pour deux choses différentes.

Ce qui est testé et où. Ce fichier garde le côté SERVEUR : l'inscription à
l'adoption, le retrait auto-guérissant, la persistance, la lecture défensive de
`layout.json` et le drapeau sur les groupes de l'instantané. Le drapeau sur les
groupes de `/api/chantier` est gardé dans `test_chantier.py`, avec les autres
invariants du contrat de ce relevé (classe `ProjetsNeufs`).

Hermétisme. `serveur` calcule `RACINE` depuis `~` AU CHARGEMENT et construit un
`Board` au niveau module : on l'importe donc HOME détourné, et jamais au niveau
module de ce fichier — sinon on volerait à `test_serveur_sessions` le droit
d'importer `serveur` sous SON faux HOME. HOME reste détourné pendant chaque
test : `creer_projet` pose un lanceur dans `~/.local/bin`, une écriture qui n'a
rien à faire sur le poste de l'utilisateur. Les frontières remplacées sont
toutes extérieures au sujet : `git status` (`etat_git`), `gh` (`pullrequests`),
la peinture des panes, les notifications, et `confiance`, qui écrit dans
`~/.claude.json`.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE_DEPOT, "server"))

# Le vrai répertoire personnel, capturé avant tout détournement : il ne sert
# qu'à PROUVER qu'on n'y touche pas.
_HOME_REEL = os.path.realpath(os.path.expanduser("~"))

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


def _releve_fabrique(dossier):
    """Ce que `git` dirait de cet arbre de travail. On ne le lui demande pas.

    `commun` vaut le chemin de l'arbre lui-même : l'arbre est alors le clone
    principal, ce qui évite au balayage de chercher si la branche est fusionnée
    — une question qui, elle, lance vraiment `git merge-base`.
    """
    return {"chemin": dossier, "branche": "feature/1000-travaux", "fichiers": 0,
            "ahead": 0, "behind": 0, "depot": os.path.basename(dossier),
            "commun": dossier, "commit_at": 1700000000}


class SocleNeufs(unittest.TestCase):
    """Un serveur qui ne lit et n'écrit que sous un faux HOME, et rien d'autre."""

    def setUp(self):
        self.srv = _importer("serveur")
        self.assertFalse(self.srv.RACINE.startswith(_HOME_REEL),
                         "le serveur pointe vers le vrai poste : test non hermétique")
        # RACINE vaut <faux home>/.claude/board : on remonte au faux home pour
        # que `expanduser("~")` désigne le même monde pendant tout le test.
        self.maison = os.path.dirname(os.path.dirname(self.srv.RACINE))
        self._patcher_dict(os.environ, {"HOME": self.maison})

        # Les frontières, et rien qu'elles : un binaire (`git`, `gh`), un effet
        # sur le terminal, une notification, et le fichier de confiance de
        # Claude Code. Aucune règle de `serveur` n'est remplacée.
        for nom, valeur in (("pullrequests", None), ("peindre", None),
                            ("resolve_pane", None), ("Notifier", None),
                            ("confiance", None),
                            ("etat_git", lambda cwd: None)):
            self._patcher(self.srv, nom, valeur)

        os.makedirs(self.srv.ETATS, exist_ok=True)
        self.addCleanup(self._nettoyer)
        self._nettoyer()

        # Le poste de travail : les dossiers que les projets déclarent comme
        # racine. Hors du faux HOME, pour qu'un chemin de projet ne puisse pas
        # être confondu avec un fichier du board.
        self.travail = tempfile.TemporaryDirectory()
        self.addCleanup(self.travail.cleanup)

    # -- outillage ------------------------------------------------------
    def _patcher(self, cible, nom, valeur):
        c = mock.patch.object(cible, nom, valeur)
        c.start()
        self.addCleanup(c.stop)

    def _patcher_dict(self, cible, valeurs):
        c = mock.patch.dict(cible, valeurs)
        c.start()
        self.addCleanup(c.stop)

    def _nettoyer(self):
        """Le dossier du board revient à l'état où ce test l'a trouvé.

        `test_serveur_sessions` partage ce faux HOME et attend un dossier
        d'états VIDE : un fichier oublié ici lui ferait compter une
        conversation qui n'existe pas.
        """
        for f in ("config.json", "layout.json"):
            try:
                os.remove(os.path.join(self.srv.RACINE, f))
            except OSError:
                pass
        for f in os.listdir(self.srv.ETATS):
            try:
                os.remove(os.path.join(self.srv.ETATS, f))
            except OSError:
                pass

    def _dossier(self, nom):
        chemin = os.path.join(self.travail.name, nom)
        os.makedirs(chemin, exist_ok=True)
        return chemin

    def _ecrire(self, fichier, obj):
        with open(os.path.join(self.srv.RACINE, fichier), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def _config(self, projets, repli="AUTRE"):
        self._ecrire("config.json", {"projects": projets, "fallback_project": repli,
                                     "notify": {"enabled": False}})

    def _layout(self, obj):
        self._ecrire("layout.json", obj)

    def _layout_sur_disque(self):
        """Le fichier tel qu'un redémarrage du serveur le relira.

        Un fichier absent rend `{}` plutôt que de lever : « rien n'a été
        persisté » est une RÉPONSE à l'invariant testé, pas un accident du
        harnais, et l'échec doit se lire dans l'assertion.
        """
        try:
            with open(os.path.join(self.srv.RACINE, "layout.json"),
                      encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _neufs_sur_disque(self):
        return self._layout_sur_disque().get("neufs")

    def _conversation(self, cwd, sid="c1"):
        """Une conversation vivante dans ce dossier, telle qu'un hook l'écrit."""
        maintenant = int(time.time())
        with open(os.path.join(self.srv.ETATS, sid + ".event.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": sid, "state": "working", "cwd": cwd,
                       "state_since": maintenant, "updated_at": maintenant}, f)

    def _groupes(self, board):
        return {g["project"]: g for g in board.instantane()["groupes"]}

    def _aveugler(self):
        """Rend le dossier d'états illisible : c'est l'instantané AVEUGLE.

        Droits retirés, montage tombé, dossier supprimé en cours de lecture —
        `docs/SCHEMA.md` appelle ça « l'échec le plus probable de tout le
        serveur ». `plainte` n'écrit que sur stderr, une ligne par minute : la
        museler garde la sortie du runner lisible sans changer un comportement.
        """
        listdir_reel = os.listdir

        def listdir_refuse(chemin, *a, **kw):
            if os.path.normpath(chemin) == os.path.normpath(self.srv.ETATS):
                raise PermissionError(13, "Permission denied", chemin)
            return listdir_reel(chemin, *a, **kw)

        self._patcher(os, "listdir", listdir_refuse)
        self._patcher(self.srv, "plainte", lambda *a, **kw: None)


class AdoptionInscritLeProjet(SocleNeufs):
    """Invariant 1 et 7 : `creer_projet` inscrit, une seule fois, et lui seul."""

    def test_creer_projet_inscrit_le_nouveau_projet_dans_neufs(self):
        self._config([])
        board = self.srv.Board()

        ok, message, _lanceur = board.creer_projet("Neuf", self._dossier("neuf"))

        self.assertTrue(ok, message)
        # Persisté : le fait « ce projet n'a jamais servi » doit survivre au
        # redémarrage du serveur, sinon le projet redevient masquable à la
        # première relance et le défaut revient tel quel.
        self.assertEqual(self._neufs_sur_disque(), ["Neuf"])

    def test_un_projet_declare_a_la_main_dans_la_config_n_est_pas_neuf(self):
        # On n'invente pas un état de cycle de vie pour un projet qu'on n'a pas
        # vu naître : sans passage par la route d'adoption, rien à dire.
        self._config([{"name": "AncienProjet", "root": self._dossier("ancien")}])
        board = self.srv.Board()

        self.assertIs(self._groupes(board)["AncienProjet"]["jamais_servi"], False)

    def test_une_adoption_refusee_n_inscrit_personne(self):
        self._config([])
        board = self.srv.Board()
        dossier = self._dossier("meme-dossier")
        board.creer_projet("Premier", dossier)

        ok, _message, _lanceur = board.creer_projet("Second", dossier)

        self.assertFalse(ok, "un second projet sur le même dossier doit être refusé")
        self.assertEqual(self._neufs_sur_disque(), ["Premier"])

    def test_un_projet_deja_inscrit_ne_l_est_pas_deux_fois(self):
        # `neufs` n'est pas une liste de faveurs : elle ne doit pas grandir
        # indéfiniment. Un nom déjà présent — layout édité à la main, config
        # remise à zéro, adoption rejouée — reste présent UNE fois.
        self._config([])
        self._layout({"neufs": ["Neuf"]})
        board = self.srv.Board()

        board.creer_projet("Neuf", self._dossier("neuf"))

        self.assertEqual(self._neufs_sur_disque(), ["Neuf"])


class PremiereConversation(SocleNeufs):
    """Invariants 2 et 6 : le retrait est automatique, persisté, et définitif."""

    def _adopter(self):
        self._config([])
        board = self.srv.Board()
        self.racine = self._dossier("neuf")
        ok, message, _ = board.creer_projet("Neuf", self.racine)
        self.assertTrue(ok, message)
        return board

    def test_une_conversation_vue_retire_le_projet_de_neufs(self):
        board = self._adopter()
        self._conversation(self.racine)

        board.instantane()

        self.assertIs(self._groupes(board)["Neuf"]["jamais_servi"], False)

    def test_le_retrait_est_persiste(self):
        # Auto-guérissant veut dire : aucun geste utilisateur, aucune commande
        # de nettoyage, et rien à refaire au prochain démarrage.
        board = self._adopter()
        self._conversation(self.racine)

        board.instantane()

        self.assertEqual(self._neufs_sur_disque(), [])

    def test_une_conversation_dans_un_autre_projet_ne_retire_pas_le_neuf(self):
        board = self._adopter()
        autre = self._dossier("autre")
        board.creer_projet("Autre", autre)
        self._conversation(autre)

        board.instantane()

        self.assertEqual(self._neufs_sur_disque(), ["Neuf"])

    def test_le_projet_ne_redevient_pas_neuf_quand_sa_conversation_s_arrete(self):
        # « A déjà servi » est IRRÉVERSIBLE. Sans cet invariant, un projet
        # ressortirait du filtre chaque fois qu'on ferme sa dernière
        # conversation — exactement le contraire du besoin.
        board = self._adopter()
        self._conversation(self.racine)
        board.instantane()
        os.remove(os.path.join(self.srv.ETATS, "c1.event.json"))

        board.instantane()

        self.assertEqual(self._neufs_sur_disque(), [])
        self.assertIs(self._groupes(board)["Neuf"]["jamais_servi"], False)

    def test_sessions_connues_seule_retire_le_projet_servi(self):
        # LE CHEMIN SANS FLUX SSE. `instantane()` n'est produit que tant qu'un
        # onglet du board est ouvert ; l'onglet Chantier, lui, passe par
        # `sessions_connues()`. Si le retrait ne vivait que dans `instantane()`,
        # un projet servi resterait neuf — donc non masquable — pour quiconque
        # n'ouvre jamais l'écran d'accueil. Aucun appel à `instantane()` ici :
        # c'est tout le test.
        board = self._adopter()
        self._conversation(self.racine)

        board.sessions_connues()

        self.assertEqual(self._neufs_sur_disque(), [])

    def test_un_instantane_aveugle_ne_retire_personne_de_neufs(self):
        # Le dossier d'états illisible ne prouve PAS qu'aucune conversation
        # tourne : déclencher sur une ignorance un retrait irréversible, c'est
        # perdre pour de bon la visibilité d'un projet qu'on vient d'adopter.
        board = self._adopter()
        self._aveugler()

        instantane = board.instantane()

        self.assertIsNotNone(instantane["sessions_indisponibles"],
                             "cet instantané devait être l'instantané aveugle")
        self.assertEqual(self._neufs_sur_disque(), ["Neuf"])


class PurgeDesFantomes(SocleNeufs):
    """Un nom que la configuration ne porte plus quitte `neufs` — SAUF si c'est
    la configuration qu'on n'a pas pu lire.

    Les deux cas vont ensemble et ne valent rien séparés. Sans le premier, une
    implémentation qui ne purge jamais passerait le second sans rien garder.
    Sans le second, la purge se déclenche sur une ignorance : `fusion_config()`
    retombe silencieusement sur une configuration à zéro projet quand le fichier
    est illisible, et TOUS les projets neufs passeraient alors pour des
    fantômes — c'est-à-dire qu'un `config.json` momentanément illisible
    effacerait, définitivement, la visibilité de ce qu'on vient d'adopter.
    """

    def test_un_neufs_fantome_est_purge_quand_la_config_est_lue(self):
        self._config([{"name": "Alpha", "root": self._dossier("alpha")}])
        self._layout({"neufs": ["Fantome", "Alpha"]})
        board = self.srv.Board()

        board.instantane()

        self.assertEqual(self._neufs_sur_disque(), ["Alpha"])

    def test_une_config_illisible_ne_purge_aucun_neuf(self):
        self._config([{"name": "Alpha", "root": self._dossier("alpha")}])
        self._layout({"neufs": ["Alpha"]})
        board = self.srv.Board()
        # Le fichier devient illisible APRÈS la construction du board : c'est le
        # cas réel — un serveur qui tourne, une configuration qu'on abîme. La
        # configuration fusionnée retombe à zéro projet au prochain
        # `recharge_config`, et c'est exactement le piège.
        with open(os.path.join(self.srv.RACINE, "config.json"),
                  "w", encoding="utf-8") as f:
            f.write("{ceci n'est pas du JSON")

        board.instantane()

        self.assertEqual(self._neufs_sur_disque(), ["Alpha"])


class PoserLayout(SocleNeufs):
    """`neufs` N'EST PAS UNE CLÉ QUE LE CLIENT PEUT POSER, et c'est une
    propriété de sécurité, pas un détail de liste blanche.

    Le cycle de vie est piloté de bout en bout par le serveur : `creer_projet`
    inscrit, l'observation d'une conversation retire. Rien dans `board/` ne lit
    ni n'écrit cette clé. L'accepter n'ouvrirait qu'une capacité, celle de
    réinscrire par un `curl` un projet qui a déjà servi et de le faire ressortir
    du filtre à volonté — c'est-à-dire de défaire l'irréversibilité gardée par
    `PremiereConversation`. Une capacité qui ne sert qu'à contourner un
    invariant se supprime, elle ne se borde pas.

    La liste blanche a donc trois obligations, et chacune a son cas : elle
    écrit ce qu'elle possède, elle n'invente pas `neufs`, et elle ne laisse
    personne toucher un `neufs` existant.
    """

    def test_poser_layout_pose_toujours_l_ordre_des_colonnes(self):
        # Le contraste qui donne leur sens aux deux cas suivants : sans lui, un
        # `poser_layout` qui n'écrirait PLUS RIEN les passerait tous les deux.
        self._config([])
        board = self.srv.Board()

        board.poser_layout({"projects": ["Beta", "Alpha"]})

        self.assertEqual(self._layout_sur_disque().get("projects"),
                         ["Beta", "Alpha"])

    def test_poser_layout_n_invente_pas_la_cle_neufs(self):
        self._config([])
        board = self.srv.Board()

        board.poser_layout({"neufs": ["Neuf"], "projects": ["Alpha"]})

        # Pas même une liste vide : la clé ne doit pas apparaître du tout.
        self.assertNotIn("neufs", self._layout_sur_disque())

    def test_poser_layout_ne_modifie_pas_un_neufs_existant(self):
        # Le payload tente les deux sens à la fois — retirer « Servi » et
        # inscrire « Fantome ». Aucun des deux ne doit passer : le retrait
        # appartient à l'observation, l'inscription à l'adoption.
        self._config([])
        self._layout({"neufs": ["Servi"]})
        board = self.srv.Board()

        board.poser_layout({"neufs": ["Fantome"]})

        self.assertEqual(self._neufs_sur_disque(), ["Servi"])

    def test_poser_layout_refuse_une_cle_inconnue(self):
        # La même règle, sur n'importe quelle autre clé : le corps de la requête
        # vient du navigateur, `layout.json` est relu à chaque démarrage, et une
        # clé inconnue y resterait pour toujours.
        self._config([])
        board = self.srv.Board()

        board.poser_layout({"cards": {}, "n_importe_quoi": {"cle": "valeur"}})

        self.assertNotIn("n_importe_quoi", self._layout_sur_disque())


class DrapeauSurLInstantane(SocleNeufs):
    """Invariants 4 et 5 : le drapeau descend au client, et `layout.json` se lit
    défensivement — c'est un fichier que l'utilisateur peut éditer ou perdre."""

    def test_chaque_groupe_de_l_instantane_porte_le_drapeau(self):
        # Toujours présent, comme `sessions_indisponibles` : « pas neuf » doit
        # être une valeur du contrat, jamais l'absence d'une clé. Le client ne
        # doit pas avoir à joindre deux sources pour savoir s'il peut masquer.
        self._config([{"name": "Alpha", "root": self._dossier("alpha")},
                      {"name": "Beta", "root": self._dossier("beta")}])
        board = self.srv.Board()

        groupes = self._groupes(board)

        self.assertIn("jamais_servi", groupes["Alpha"])
        self.assertIn("jamais_servi", groupes["Beta"])

    def test_le_drapeau_distingue_le_projet_neuf_des_autres(self):
        self._config([{"name": "Alpha", "root": self._dossier("alpha")},
                      {"name": "Beta", "root": self._dossier("beta")}])
        self._layout({"neufs": ["Beta"]})
        board = self.srv.Board()

        groupes = self._groupes(board)

        self.assertIs(groupes["Beta"]["jamais_servi"], True)
        self.assertIs(groupes["Alpha"]["jamais_servi"], False)

    def test_un_neufs_nommant_un_projet_disparu_ne_fait_rien_apparaitre(self):
        # LE FANTÔME EST TESTÉ LÀ OÙ LA PURGE NE PEUT PAS L'ATTEINDRE, sinon ce
        # cas ne garde rien : quand `config.json` est lisible, le nom est retiré
        # de `neufs` avant même qu'on regarde les colonnes (cf.
        # `PurgeDesFantomes`), et n'importe quelle règle de colonnes passerait.
        #
        # Ici le fichier est SUPPRIMÉ pendant que le serveur tourne — un
        # déplacement, une restauration ratée. `recharge_config` échoue sur la
        # date du fichier et garde la configuration précédente, donc Alpha garde
        # sa colonne ; mais la purge, elle, refuse de conclure quoi que ce soit
        # d'un fichier illisible et laisse « Fantome » dans la liste. C'est la
        # seule situation où un nom mort survit dans `neufs`, et c'est donc la
        # seule où « ne fait rien apparaître d'inexistant » se prouve.
        self._config([{"name": "Alpha", "root": self._dossier("alpha")}])
        self._layout({"neufs": ["Fantome"]})
        board = self.srv.Board()
        os.remove(os.path.join(self.srv.RACINE, "config.json"))

        self.assertEqual(list(self._groupes(board)), ["Alpha"])

    def test_un_neufs_ecrit_en_chaine_ne_marque_aucun_projet(self):
        # `layout.json` s'édite à la main et se perd. Une chaîne au lieu d'une
        # liste ne doit ni faire tomber l'instantané, ni marquer un projet dont
        # le nom n'est qu'une SOUS-CHAÎNE de ce qui traîne là — `"Alpha" in
        # "Alphabet"` est vrai en Python, et c'est tout ce qu'il faut pour
        # qu'un `in` posé sur la valeur brute désigne le mauvais projet.
        self._config([{"name": "Alpha", "root": self._dossier("alpha")}])
        self._layout({"neufs": "Alphabet"})
        board = self.srv.Board()

        self.assertIs(self._groupes(board)["Alpha"]["jamais_servi"], False)

    def test_l_instantane_aveugle_publie_le_drapeau(self):
        # L'instantané aveugle garde ses colonnes pour distinguer « je ne sais
        # pas » d'un board effacé. Ses groupes doivent porter le champ comme les
        # autres : « pas neuf » est une valeur du contrat, jamais l'absence
        # d'une clé, et un client qui lirait `undefined` ici retomberait sur le
        # défaut qu'on corrige le jour où le prédicat d'aveuglement bougerait.
        self._config([{"name": "Alpha", "root": self._dossier("alpha")}])
        self._layout({"neufs": ["Alpha"]})
        board = self.srv.Board()
        self._aveugler()

        groupes = self._groupes(board)

        self.assertIs(groupes["Alpha"]["jamais_servi"], True)

    def test_un_neufs_ecrit_en_dictionnaire_ne_marque_aucun_projet(self):
        # L'autre forme plausible, et celle qui piège vraiment : un jour où
        # l'on voudra dater l'adoption, `neufs` deviendra tentant en
        # dictionnaire. Un `in` sur la valeur brute interroge alors ses CLÉS et
        # marque le projet — sur un fichier que le contrat dit être une liste.
        # On lit le type qu'on a promis, ou on ne lit rien.
        self._config([{"name": "Alpha", "root": self._dossier("alpha")}])
        self._layout({"neufs": {"Alpha": 1700000000}})
        board = self.srv.Board()

        self.assertIs(self._groupes(board)["Alpha"]["jamais_servi"], False)


class RouteChantier(SocleNeufs):
    """`GET /api/chantier` — le CÂBLAGE, la seule chose que ce cas prouve.

    `chantier.scan()` sait marquer les groupes (gardé dans `test_chantier.py`),
    et le `Board` sait quels projets sont neufs (gardé plus haut). Reste la
    jointure : si la route oublie de passer `neufs` au relevé, l'onglet Chantier
    publie `jamais_servi: false` sur TOUS ses groupes — et les deux suites
    restent vertes, chacune gardant sa moitié. Ce trou-là ne se bouche qu'en
    interrogeant la route pour de bon.

    Un vrai serveur HTTP sur un port éphémère de la boucle locale, une vraie
    requête, la réponse JSON telle que le navigateur la reçoit. L'échafaudage
    tient en dix lignes et n'attend aucune durée : `serve_forever` dans un fil,
    `shutdown` au démontage.
    """

    def setUp(self):
        super(RouteChantier, self).setUp()
        self.chantier = _importer("chantier")
        # Les frontières du module Chantier, remplacées exactement comme dans
        # `test_chantier.SocleChantier` : le parcours du disque, ce que `git`
        # dirait de l'arbre, et `_scan_pr` qui lance `gh` (un binaire et le
        # réseau). Le croisement de `chantier`, lui, reste réellement exercé —
        # et il le faut : un projet n'a de GROUPE sur cet onglet que s'il porte
        # au moins un arbre de travail.
        self._patcher(self.chantier, "_parcours",
                      lambda racine: [os.path.join(racine, "depot")])
        self._patcher(self.chantier, "_releve", _releve_fabrique)
        self._patcher(self.chantier, "_base_du_depot", lambda dossier: (None, None))
        self._patcher(self.chantier, "_scan_pr", lambda config: {"groupes": []})
        self.addCleanup(self._vider_cache_chantier)
        self._vider_cache_chantier()

    def _vider_cache_chantier(self):
        # Le relevé est mémorisé 30 s dans un état de module partagé avec
        # `test_chantier` : sans remise à zéro, cette route servirait des
        # groupes fabriqués par un autre fichier de tests.
        with self.chantier._VERROU:
            self.chantier._CACHE["at"] = 0.0
            self.chantier._CACHE["data"] = None
            self.chantier._CACHE["signature"] = None

    def _demander(self, chemin, board):
        import http.server
        import threading
        import urllib.request

        # La route lit le `BOARD` du module, construit à l'import : on lui
        # substitue celui du test, qui pointe sur la même RACINE mais dont on
        # connaît la configuration et la liste `neufs`.
        self._patcher(self.srv, "BOARD", board)
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.srv.Handler)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d%s" % (httpd.server_address[1], chemin)
        with urllib.request.urlopen(url, timeout=10) as reponse:
            return json.loads(reponse.read().decode("utf-8"))

    def test_la_route_du_chantier_publie_le_drapeau_du_projet_neuf(self):
        self._config([{"name": "Alpha", "root": self._dossier("alpha")},
                      {"name": "Beta", "root": self._dossier("beta")}])
        self._layout({"neufs": ["Beta"]})
        board = self.srv.Board()

        releve = self._demander("/api/chantier", board)

        groupes = {g["project"]: g for g in releve["groupes"]}
        self.assertIs(groupes["Beta"]["jamais_servi"], True)
        self.assertIs(groupes["Alpha"]["jamais_servi"], False)

    def test_la_route_publie_le_drapeau_d_apres_l_observation(self):
        # L'ORDRE DES DEUX LECTURES DE LA ROUTE. `sessions_connues()` est une
        # observation vivante : elle retire Beta de `neufs` en passant. Lire la
        # liste AVANT cet appel publierait le drapeau d'AVANT le retrait, et
        # l'onglet Chantier garderait à l'écran, un relevé de plus, un projet
        # dont la première conversation vient d'être vue. Les deux lignes se
        # relisent comme interchangeables ; elles ne le sont pas.
        beta = self._dossier("beta")
        self._config([{"name": "Alpha", "root": self._dossier("alpha")},
                      {"name": "Beta", "root": beta}])
        self._layout({"neufs": ["Beta"]})
        board = self.srv.Board()
        self._conversation(os.path.join(beta, "depot"))

        releve = self._demander("/api/chantier", board)

        groupes = {g["project"]: g for g in releve["groupes"]}
        self.assertIs(groupes["Beta"]["jamais_servi"], False)


if __name__ == "__main__":
    unittest.main()
