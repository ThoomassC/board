#!/usr/bin/env python3
"""Lecture du dossier d'états : un dossier illisible n'est pas un dossier vide.

C'est la frontière où l'invariant « None n'est pas zéro » se perdait réellement.
Tout le reste de la chaîne le respecte — `chantier.scan()` distingue les deux
avec `conversations` et `conversations_inconnues`, le client ne masque un projet
que sur un zéro — mais si la lecture du dossier d'états répond « aucune
conversation » quand elle n'a pas pu regarder, le zéro est FABRIQUÉ ici et tout
le monde en aval le croit. L'écran affirme alors qu'aucune conversation ne
tourne, et masque les projets, sur la foi d'une erreur avalée.

Le consommateur testé est `sessions_connues()` : c'est lui que l'onglet Chantier
appelle, et c'est sa valeur de retour qui devient le `sessions` de
`chantier.scan()`. `sessions()` est testée juste assez pour prouver que l'échec
est nommé (`EtatsIllisibles`) et non une panne quelconque — la distinction dont
`instantane()` se sert pour servir un instantané aveugle plutôt que de figer le
board.

Hermétisme : `serveur` calcule `RACINE` depuis `~` au chargement et construit un
`Board` au niveau module (`BOARD = Board()`), qui lit config.json, seen.json,
layout.json et archive.json. On détourne donc `HOME` vers un dossier temporaire
LE TEMPS DE L'IMPORT, puis on le restaure : aucun fichier du poste n'est lu, et
`Board()` reste le vrai constructeur — rien n'est échafaudé.
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

RACINE_DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE_DEPOT, "server"))

# Gardé vivant pour toute la session : `serveur.RACINE` pointe dedans.
_FAUX_HOME = tempfile.TemporaryDirectory()
_HOME_REEL = os.environ.get("HOME")
os.environ["HOME"] = _FAUX_HOME.name
try:
    import serveur  # noqa: E402
finally:
    if _HOME_REEL is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _HOME_REEL


class DossierDEtatsIllisible(unittest.TestCase):

    def setUp(self):
        self.assertTrue(serveur.RACINE.startswith(_FAUX_HOME.name),
                        "le serveur pointe vers le vrai poste : test non hermétique")
        os.makedirs(serveur.ETATS, exist_ok=True)
        # `_index_pr` appellerait `pullrequests.scan`, qui lance `gh` : on remet
        # le serveur dans son mode dégradé « module PR absent », un état qu'il
        # gère déjà nativement (cf. son bloc d'imports tolérants).
        correctif = mock.patch.object(serveur, "pullrequests", None)
        correctif.start()
        self.addCleanup(correctif.stop)
        self.board = serveur.Board()

    def _refuser_la_lecture(self):
        """Le dossier existe mais l'énumération échoue (droits, montage tombé)."""
        listdir_reel = os.listdir

        def listdir_refuse(chemin, *a, **kw):
            if os.path.normpath(chemin) == os.path.normpath(serveur.ETATS):
                raise PermissionError(13, "Permission denied", chemin)
            return listdir_reel(chemin, *a, **kw)

        return mock.patch.object(os, "listdir", listdir_refuse)

    def test_un_dossier_d_etats_vide_donne_zero_conversation(self):
        # Le dossier est lisible et ne contient rien : ZÉRO est une information
        # sûre. C'est le contraste qui donne son sens au cas suivant — sans lui,
        # une implémentation qui ne rendrait JAMAIS de liste passerait aussi.
        self.assertEqual(self.board.sessions_connues(), [])

    def test_un_dossier_d_etats_illisible_ne_donne_pas_zero_conversation(self):
        # L'invariant : l'onglet Chantier doit recevoir « je ne sais pas », donc
        # `None`, et surtout pas une liste vide qui lui ferait masquer tous les
        # projets en affirmant qu'aucune conversation ne tourne.
        with self._refuser_la_lecture():
            connues = self.board.sessions_connues()

        self.assertIsNone(connues)

    def test_l_echec_de_lecture_est_un_etat_degrade_nomme(self):
        # `instantane()` ne rattrape QUE `EtatsIllisibles` pour servir son
        # instantané aveugle : si l'échec remontait sous une autre forme, le
        # flux SSE se refermerait et le board se figerait.
        with self._refuser_la_lecture():
            with self.assertRaises(serveur.EtatsIllisibles):
                self.board.sessions(int(time.time()))


if __name__ == "__main__":
    unittest.main()
