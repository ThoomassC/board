#!/usr/bin/env python3
"""Contrat `conversations` de l'onglet Chantier : combien de conversations par projet.

Ce que ces tests gardent tient en deux phrases.

`conversations` doit distinguer ZÉRO de INCONNU. Le client se sert de cette
distinction pour choisir entre « ce projet n'a aucune conversation ouverte » et
« je ne sais pas, je ne masque rien » ; confondre les deux affiche une
contre-vérité.

`conversations` et `conversations_inconnues` N'ONT PAS LA MÊME PORTÉE, et c'est
le piège de ce module. `conversations_inconnues` décrit le RELEVÉ — ses états
d'arbres ont-ils été calculés sans instantané ? `conversations` décrit
l'APPEL — cet appelant-ci a-t-il fourni un instantané ? Les deux ne coïncident
que sur le chemin frais. Sur le chemin du cache, un appel `sessions=None` reçoit
un relevé qui, lui, a bien été calculé avec un instantané : `conversations` vaut
None, `conversations_inconnues` reste False.

Le point d'entrée testé est `scan()` et non `_habiller()` : `_habiller()` ne
reçoit pas `sessions` (cf. sa signature), le comptage ne peut donc pas s'y
observer. On teste la réponse publique, pas le découpage interne.

Aucune donnée du poste n'est lue. Les frontières — énumération des dossiers,
interrogation de git, cache des pull requests — sont remplacées par des valeurs
fabriquées ; tout le croisement de `chantier` reste, lui, réellement exercé.
"""
import copy
import os
import sys
import time
import unittest
from unittest import mock

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "server"))

import chantier  # noqa: E402


# Des racines qui n'existent sur aucun poste : si un stub venait à manquer, le
# test tomberait au lieu de balayer les vrais dépôts de la machine.
ALPHA = os.path.join(os.sep, "depots-de-test", "alpha")
BETA = os.path.join(os.sep, "depots-de-test", "beta")
AILLEURS = os.path.join(os.sep, "depots-de-test", "hors-config")

CONFIG = {
    "projects": [
        {"name": "Alpha", "root": ALPHA, "accent": "#112233"},
        {"name": "Beta", "root": BETA},
    ],
    "fallback_project": "AUTRE",
}

ARBRE_ALPHA = os.path.join(ALPHA, "depot-a")
ARBRE_BETA = os.path.join(BETA, "depot-b")
ARBRE_ORPHELIN = os.path.join(AILLEURS, "depot-x")
ARBRES = {ALPHA: [ARBRE_ALPHA], BETA: [ARBRE_BETA]}


def _parcours_fabrique(racine):
    """Ce que le parcours du disque trouverait sous cette racine."""
    return list(ARBRES.get(os.path.normpath(racine), []))


def _releve_fabrique(dossier):
    """Ce que git dirait de cet arbre. On ne le lui demande pas.

    `commun` vaut le chemin de l'arbre lui-même : l'arbre est alors le clone
    principal, ce qui évite au balayage de chercher si la branche est fusionnée
    — une question qui, elle, lance vraiment `git merge-base`.
    """
    return {"chemin": dossier, "branche": "feature/1000-travaux", "fichiers": 0,
            "ahead": 0, "behind": 0, "depot": os.path.basename(dossier),
            "commun": dossier, "commit_at": 1700000000}


def _session(cwd, sid, state="working"):
    """Une entrée de l'instantané, réduite aux champs que le balayage regarde."""
    return {"sid": sid, "cwd": cwd, "state": state, "title": sid,
            "glyphe": "●", "ctx_pct": 10, "since": 1700000000}


def _par_projet(releve):
    return {g["project"]: g for g in releve["groupes"]}


def _arbres_par_chemin(releve):
    """{chemin d'arbre -> l'arbre} — ce que le relevé DIT de chaque arbre.

    C'est la moitié de la réponse que le dédoublonnage peut rendre périmée.
    `conversations` se recalcule à chaque appel et ne peut donc pas mentir ;
    tout ce qui vient du relevé mémorisé — l'état, les occupants, le motif de
    rétention — ment dès qu'il est resservi à un appelant dont les
    conversations ne sont pas celles qui l'ont produit. Les tests de signature
    l'observent ici, pas ailleurs.
    """
    return {a["chemin"]: a
            for g in releve["groupes"] for d in g["depots"] for a in d["arbres"]}


class SocleChantier(unittest.TestCase):
    """Frontières fabriquées, cache remis à zéro, compteur de balayages.

    Le compteur porte sur `_parcours` — la frontière disque, déjà remplacée —
    et non sur `_balayer` : on compte le travail réellement coûteux qui a été
    déclenché, sans mocker une fonction interne du module testé.
    """

    def setUp(self):
        self.parcours_appels = []

        def parcours_compte(racine):
            self.parcours_appels.append(racine)
            return _parcours_fabrique(racine)

        for nom, remplacant in (("_parcours", parcours_compte),
                                ("_releve", _releve_fabrique),
                                ("_base_du_depot", lambda dossier: (None, None)),
                                ("_scan_pr", lambda config: {"groupes": []})):
            correctif = mock.patch.object(chantier, nom, remplacant)
            correctif.start()
            self.addCleanup(correctif.stop)
        self.addCleanup(self._vider_cache)
        self._vider_cache()

    def _vider_cache(self):
        # Le relevé est mémorisé dans un état de module partagé pendant 30 s :
        # sans remise à zéro, un test servirait la réponse fabriquée par le
        # précédent et passerait pour de mauvaises raisons.
        with chantier._VERROU:
            chantier._CACHE["at"] = 0.0
            chantier._CACHE["data"] = None
            chantier._CACHE["signature"] = None

    def _scanner(self, sessions, force=False):
        return chantier.scan(CONFIG, sessions=sessions, force=force,
                             us_de=lambda branche, cwd="": "")

    def _balayages(self):
        """Combien de balayages complets ont eu lieu depuis le début du test.

        Un balayage interroge chaque racine configurée une fois : le compte des
        appels à `_parcours` en est un multiple exact.
        """
        return len(self.parcours_appels) // len(CONFIG["projects"])


class ConversationsParProjet(SocleChantier):
    """`scan()["groupes"][*]["conversations"]` — comptage et cas « inconnu »."""

    def test_chaque_groupe_porte_la_cle_conversations(self):
        groupes = _par_projet(self._scanner([]))

        self.assertIn("conversations", groupes["Alpha"])
        self.assertIn("conversations", groupes["Beta"])

    def test_conversations_compte_celles_rattachees_au_projet(self):
        # La deuxième travaille dans un sous-dossier de l'arbre : elle n'occupe
        # aucun arbre au sens du balayage, mais elle appartient bien au projet.
        sessions = [_session(ARBRE_ALPHA, "a1"),
                    _session(os.path.join(ARBRE_ALPHA, "server"), "a2"),
                    _session(ARBRE_BETA, "b1")]

        groupes = _par_projet(self._scanner(sessions))

        self.assertEqual(groupes["Alpha"]["conversations"], 2)
        self.assertEqual(groupes["Beta"]["conversations"], 1)

    def test_une_conversation_hors_de_toute_racine_va_au_projet_de_repli(self):
        # Testée POSITIVEMENT : il ne suffit pas que `x1` n'apparaisse ni dans
        # Alpha ni dans Beta — n'importe quel rattachement erroné passerait ce
        # contrôle-là. On veut voir le chiffre atterrir dans « AUTRE ».
        # Pour qu'un groupe « AUTRE » existe, il faut qu'un ARBRE y soit rangé :
        # ce test fait donc remonter un arbre hors racine par la frontière
        # disque, et vérifie que les deux règles de rattachement du module —
        # celle de `_habiller` pour les arbres, celle du comptage pour les
        # conversations — désignent bien le même groupe. Si elles divergeaient,
        # `conversations` serait compté pour un projet qui n'a pas de ligne.
        arbres = dict(ARBRES, **{ALPHA: [ARBRE_ALPHA, ARBRE_ORPHELIN]})
        with mock.patch.object(chantier, "_parcours",
                               lambda r: list(arbres.get(os.path.normpath(r), []))):
            sessions = [_session(ARBRE_ALPHA, "a1"), _session(AILLEURS, "x1")]
            groupes = _par_projet(self._scanner(sessions))

        self.assertEqual(groupes["AUTRE"]["conversations"], 1)
        self.assertEqual(groupes["Alpha"]["conversations"], 1)
        self.assertEqual(groupes["Beta"]["conversations"], 0)

    def test_conversations_vaut_zero_quand_aucune_n_est_ouverte(self):
        releve = self._scanner([])
        groupes = _par_projet(releve)

        self.assertEqual(groupes["Alpha"]["conversations"], 0)
        self.assertEqual(groupes["Beta"]["conversations"], 0)
        self.assertIsInstance(groupes["Alpha"]["conversations"], int)
        self.assertIs(releve["conversations_inconnues"], False)

    def test_conversations_vaut_none_quand_l_instantane_est_indisponible(self):
        # L'invariant central sur le chemin frais : `None` n'est pas zéro. Ici
        # les deux champs coïncident, le relevé lui-même ayant été calculé sans
        # instantané — c'est le seul chemin où ils coïncident.
        releve = self._scanner(None)
        groupes = _par_projet(releve)

        self.assertIsNone(groupes["Alpha"]["conversations"])
        self.assertIsNone(groupes["Beta"]["conversations"])
        self.assertIs(releve["conversations_inconnues"], True)

    def test_conversations_ne_compte_pas_une_conversation_morte(self):
        # Le serveur exclut déjà les `dead` en amont ; ce test garde la
        # propriété si cette exclusion se déplace ou disparaît.
        sessions = [_session(ARBRE_ALPHA, "a1"),
                    _session(ARBRE_ALPHA, "a2", state="dead")]

        groupes = _par_projet(self._scanner(sessions))

        self.assertEqual(groupes["Alpha"]["conversations"], 1)


class CheminDuCache(SocleChantier):
    """Le second appel, celui que le relevé mémorisé sert.

    Sans ces tests, supprimer la dérivation de `conversations` sur le chemin du
    cache laisse la suite verte pendant que la production sert des chiffres
    calculés pour un autre appelant, jusqu'à 30 s plus tôt.
    """

    def test_conversations_reflete_le_second_appel_et_non_le_premier(self):
        self._scanner([_session(ARBRE_ALPHA, "a1"), _session(ARBRE_ALPHA, "a2")])

        groupes = _par_projet(self._scanner([_session(ARBRE_BETA, "b1")]))

        self.assertEqual(self._balayages(), 1, "le second appel doit venir du cache")
        self.assertEqual(groupes["Alpha"]["conversations"], 0)
        self.assertEqual(groupes["Beta"]["conversations"], 1)

    def test_sur_le_cache_sessions_none_donne_conversations_none_sans_lever_l_inconnu(self):
        # LE test de la portée des deux champs. La sonde de pastille du board
        # interroge la route au chargement de la page, avant le premier
        # instantané SSE : elle passe `sessions=None` sur un cache chaud.
        #   · `conversations` décrit l'appel  -> None, on ne sait pas compter.
        #   · `conversations_inconnues` décrit le relevé, qui a bien été calculé
        #     avec un instantané réel (le cache refuse les relevés dégradés)
        #     -> False, ses états d'arbres ne sont pas en doute.
        self._scanner([_session(ARBRE_ALPHA, "a1")])

        releve = self._scanner(None)
        groupes = _par_projet(releve)

        self.assertEqual(self._balayages(), 1, "le second appel doit venir du cache")
        self.assertIsNone(groupes["Alpha"]["conversations"])
        self.assertIsNone(groupes["Beta"]["conversations"])
        self.assertIs(releve["conversations_inconnues"], False)

    def test_le_releve_memorise_ne_porte_pas_conversations(self):
        # `conversations` dépend de l'appelant, jamais du relevé : le mémoriser
        # serait mémoriser le contexte de quelqu'un d'autre. Ce que le cache ne
        # contient pas ne peut pas être servi périmé.
        self._scanner([_session(ARBRE_ALPHA, "a1")])

        memorise = chantier._CACHE["data"]

        self.assertIsNotNone(memorise, "un relevé complet doit être mémorisé")
        for groupe in memorise["groupes"]:
            self.assertNotIn("conversations", groupe)

    def test_un_appel_servi_par_le_cache_ne_mute_pas_le_releve_memorise(self):
        # Le relevé mémorisé est partagé par tous les appelants. Y écrire en
        # place — ou muter une réponse déjà rendue — corrompt le suivant.
        premier = self._scanner([_session(ARBRE_ALPHA, "a1"),
                                 _session(ARBRE_ALPHA, "a2")])
        avant = copy.deepcopy(chantier._CACHE["data"])

        self._scanner([_session(ARBRE_BETA, "b1")])

        self.assertEqual(chantier._CACHE["data"], avant)
        self.assertEqual(_par_projet(premier)["Alpha"]["conversations"], 2,
                         "la réponse déjà rendue a été modifiée rétroactivement")
        troisieme = self._scanner([_session(ARBRE_ALPHA, "a1"),
                                   _session(ARBRE_ALPHA, "a2")])
        self.assertEqual(_par_projet(troisieme)["Alpha"]["conversations"], 2)


class DedoublonnageDuForce(SocleChantier):
    """Deux `force=True` rapprochés ET DE MÊME SIGNATURE ne paient qu'un balayage.

    LA RÈGLE, en une phrase : un `force` accepte le relevé mémorisé si celui-ci
    est récent ET s'il a été calculé sur le MÊME lot de sessions. La conjonction
    est le sujet de cette classe ; chacune de ses deux moitiés a ses tests.

    Pourquoi la signature, alors qu'une fenêtre de 2 s tuait déjà l'amplification
    par onglets : parce qu'elle ne la tuait que par approximation. `force`
    promet « ne pas me servir un relevé d'avant l'événement ». Deux changements
    de conversations distincts en moins de 2 s font deux événements, et le
    second appelant se voyait resservir un relevé antérieur au SIEN — un projet
    listé « en réserve » alors qu'une conversation venait d'y démarrer, ce que
    tout le reste de ce module s'interdit. La signature rend la promesse exacte
    sans rien coûter au cas mesuré : N onglets qui réagissent au même événement
    présentent par construction la même signature.

    On compte les balayages plutôt que de mesurer une durée : un test qui
    dépend du temps qui passe est instable, et celui-ci doit rester vrai quel
    que soit le seuil retenu côté serveur.
    """

    def test_deux_balayages_forces_rapproches_ne_declenchent_qu_un_balayage(self):
        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        self.assertEqual(self._balayages(), 1)

    def test_le_force_rebalaye_des_que_le_releve_n_est_plus_immediat(self):
        # Sans ce cas, un dédoublonnage qui dédoublonne TOUJOURS passerait le
        # test précédent et `force` ne forcerait plus rien. On vieillit le
        # relevé jusqu'au bord du TTL : il reste servable sans `force`, et
        # aucun seuil de dédoublonnage plus court que le TTL ne peut le couvrir.
        #
        # C'est aussi ce cas qui interdit de remplacer la fenêtre par la seule
        # signature : les états d'arbres ne dépendent pas QUE des conversations
        # — un `git status`, une PR ouverte, un commit poussé les changent sans
        # que l'instantané bouge d'une virgule. Un lot de sessions stable dix
        # minutes rendrait alors tout `force` définitivement inopérant.
        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)
        with chantier._VERROU:
            chantier._CACHE["at"] = time.time() - (chantier.TTL - 0.5)

        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        self.assertEqual(self._balayages(), 2)

    def test_deux_forces_rapproches_a_signatures_differentes_rebalaient(self):
        # LE cas qui manquait, et sans lequel un dédoublonnage permanent — ou
        # purement temporel, ce qu'il était — passe toute cette classe. Deux
        # conversations distinctes démarrent dans deux projets à moins de deux
        # secondes d'intervalle : le second appelant ne doit pas recevoir le
        # relevé du premier, qui ignore tout de son événement à lui.
        premier = self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        second = self._scanner([_session(ARBRE_BETA, "b1")], force=True)

        self.assertEqual(self._balayages(), 2)
        self.assertEqual(_arbres_par_chemin(premier)[ARBRE_ALPHA]["etat"],
                         "en_cours")
        # Sans rebalayage, ces deux lignes-là étaient fausses : l'arbre d'Alpha
        # serait resté « en cours » et celui de Beta « en réserve ».
        self.assertEqual(_arbres_par_chemin(second)[ARBRE_ALPHA]["etat"], "reserve")
        self.assertEqual(_arbres_par_chemin(second)[ARBRE_BETA]["etat"], "en_cours")

    def test_une_seconde_conversation_dans_le_meme_arbre_rebalaye(self):
        # LE cas qui interdit de réduire la signature aux seuls `cwd` occupés,
        # qui serait le strict minimum pour l'ÉTAT (l'arbre est « en cours » dans
        # les deux relevés). Le sid y est parce que le relevé ne porte pas qu'un
        # état : il porte la liste des occupants, et le board en affiche le titre
        # du premier, le « +N » des suivants, et le motif de rétention. Servir un
        # relevé qui ne connaît qu'une des deux conversations, c'est afficher
        # « 1 conversation y travaille » sur un arbre où il y en a deux — et
        # proposer de le libérer sur la foi de ce chiffre.
        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        releve = self._scanner([_session(ARBRE_ALPHA, "a1"),
                                _session(ARBRE_ALPHA, "a2")], force=True)

        self.assertEqual(self._balayages(), 2)
        # Les occupants, et non `retenu` : les arbres fabriqués ici sont des
        # clones principaux (cf. `_releve_fabrique`), dont le motif de rétention
        # est déjà pris par « jamais retiré ». C'est bien cette liste-là que
        # `_liberation` compte pour rédiger son motif, et que le board affiche.
        arbre = _arbres_par_chemin(releve)[ARBRE_ALPHA]
        self.assertEqual([c["sid"] for c in arbre["conv"]], ["a1", "a2"])

    def test_le_depart_de_la_derniere_conversation_rebalaye(self):
        # Le cas symétrique du précédent, et il n'est pas redondant : il oppose
        # une signature VIDE à une signature pleine. Une signature qui
        # confondrait « aucune session » avec « on ne sait pas » (cf. le test de
        # `sessions=None` plus bas) le raterait exactement ici.
        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        releve = self._scanner([], force=True)

        self.assertEqual(self._balayages(), 2)
        self.assertEqual(_arbres_par_chemin(releve)[ARBRE_ALPHA]["etat"], "reserve")

    def test_l_ordre_de_l_instantane_ne_compte_pas_dans_la_signature(self):
        # L'instantané est une LISTE, et son ordre dépend du tri d'affichage du
        # serveur, pas des arbres. Deux lots identiques présentés dans un autre
        # ordre décrivent le même monde : les comparer en tant que listes
        # rebalaierait sur un tri.
        a1, b1 = _session(ARBRE_ALPHA, "a1"), _session(ARBRE_BETA, "b1")
        self._scanner([a1, b1], force=True)

        self._scanner([b1, a1], force=True)

        self.assertEqual(self._balayages(), 1)

    def test_un_force_sans_instantane_ne_repeint_pas_sur_une_ignorance(self):
        # `sessions=None` dit « on ne sait pas quelles conversations tournent ».
        # Rebalayer là-dessus produirait un relevé où AUCUN arbre n'est occupé —
        # strictement moins vrai que celui du cache, calculé lui avec un
        # instantané réel — et il ne serait même pas mémorisé (le cache refuse
        # les relevés dégradés). On sert donc le cache : c'est le seul cas où la
        # signature ne peut pas départager, et le rebalayage y est un coût pur
        # payé pour une régression.
        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        releve = self._scanner(None, force=True)

        self.assertEqual(self._balayages(), 1)
        self.assertEqual(_arbres_par_chemin(releve)[ARBRE_ALPHA]["etat"],
                         "en_cours")
        self.assertIsNone(_par_projet(releve)["Alpha"]["conversations"])
        self.assertIs(releve["conversations_inconnues"], False)

    def test_le_dedoublonnage_du_force_rend_les_conversations_de_l_appel(self):
        # Un appel resservi par le dédoublonnage reste un chemin de cache : il
        # doit dériver `conversations` comme les autres, sinon `force` devient
        # la porte dérobée par laquelle un chiffre périmé ressort.
        #
        # Les deux appels portent la MÊME signature — même sid, même cwd — donc
        # un seul balayage, et pourtant ils ne comptent pas la même chose : le
        # second voit la conversation morte, que `conversations` écarte. C'est
        # exactement ce qui justifie de tenir `state` HORS de la signature : il
        # ne déplace aucun arbre (`_balayer` marque « en cours » tout arbre où
        # une entrée a son `cwd`, vivante ou non), il change seulement un
        # comptage qui, lui, est recalculé à chaque appel. L'y mettre ferait
        # rebalayer sur chaque battement d'état — c'est-à-dire sur presque tous
        # les `force` d'une rafale — et rendrait le dédoublonnage inopérant.
        self._scanner([_session(ARBRE_ALPHA, "a1")], force=True)

        groupes = _par_projet(self._scanner(
            [_session(ARBRE_ALPHA, "a1", state="dead")], force=True))

        self.assertEqual(self._balayages(), 1)
        self.assertEqual(groupes["Alpha"]["conversations"], 0)
        self.assertEqual(groupes["Beta"]["conversations"], 0)


class ProjetsNeufs(SocleChantier):
    """`jamais_servi` — le projet adopté qu'aucune conversation n'a encore vu.

    Le filtre « avec conversation » masquait un projet dès son adoption, lui
    retirant la colonne qui porte le nom de son lanceur. Un projet adopté reste
    donc visible jusqu'à sa première conversation. Le fait vit dans
    `layout.json` (clé `neufs`, gardée par `test_neufs.py`) ; le SERVEUR le
    descend jusqu'ici, et cet onglet le republie par groupe pour que le client
    n'ait pas à joindre deux sources avant de décider s'il peut masquer.

    Même règle que `conversations`, pour la même raison : le drapeau est DÉRIVÉ
    à chaque appel et n'entre jamais dans le relevé mémorisé — celui-ci est
    partagé par tous les appelants pendant 30 s, et un projet peut cesser
    d'être neuf entre deux.
    """

    def _scanner_neufs(self, neufs, sessions=(), force=False):
        return chantier.scan(CONFIG, sessions=list(sessions), neufs=neufs,
                             force=force, us_de=lambda branche, cwd="": "")

    def test_chaque_groupe_porte_le_drapeau_jamais_servi(self):
        # Toujours présent, et faux par défaut : « pas neuf » est une valeur du
        # contrat, pas l'absence d'une clé.
        groupes = _par_projet(self._scanner([]))

        self.assertIs(groupes["Alpha"]["jamais_servi"], False)
        self.assertIs(groupes["Beta"]["jamais_servi"], False)

    def test_le_drapeau_distingue_le_projet_neuf_des_autres(self):
        groupes = _par_projet(self._scanner_neufs(["Beta"]))

        self.assertIs(groupes["Beta"]["jamais_servi"], True)
        self.assertIs(groupes["Alpha"]["jamais_servi"], False)

    def test_le_drapeau_du_chemin_du_cache_est_celui_de_l_appel(self):
        # Second appel servi par le relevé mémorisé (aucun rebalayage) : le
        # drapeau doit être celui de CET appel.
        #
        # L'ORDRE DES DEUX APPELS EST LE TEST. Faux puis vrai — jamais
        # l'inverse : un chemin de cache qui ignorerait `neufs` rendrait faux,
        # et un drapeau rangé dans le relevé mémorisé rendrait le faux du
        # premier appel. Les deux erreurs passent inaperçues dans l'autre sens.
        self._scanner_neufs([])

        groupes = _par_projet(self._scanner_neufs(["Beta"]))

        self.assertEqual(self._balayages(), 1)
        self.assertIs(groupes["Beta"]["jamais_servi"], True)


if __name__ == "__main__":
    unittest.main()
