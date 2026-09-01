# -*- coding: utf-8 -*-
"""L'onglet « Projets » — l'axe qui manquait.

Les quatre autres onglets sont organisés par TYPE D'OBJET : les conversations,
les pull requests, les arbres, l'historique. Aucun n'est organisé par projet, et
c'est pourtant la question qu'on se pose en s'asseyant : « sur PROJET_A, où j'en
suis, et qu'est-ce que je dois faire ? » Aujourd'hui la jointure se fait de tête,
en changeant d'onglet — l'aide l'avoue elle-même : la branche est la seule clé
qui relie les trois autres onglets entre eux.

Ce module n'invente aucune donnée et ne scanne rien. Il consomme les trois
relevés déjà calculés (instantané, chantier, pull requests) et les range par
projet selon UNE seule question : QUI TIENT LA BALLE ?

    à toi            — rien n'avance sans un geste de ta part
    chez les autres  — tu as livré, l'horloge tourne chez quelqu'un d'autre
    à ranger         — rien ne presse, mais ça encombre

Un projet porte UNE action suivante, choisie par priorité — jamais deux. C'est la
discipline de l'onglet Chantier (« un arbre n'a qu'un seul état, jamais deux
étiquettes à la fois »), appliquée au projet.

Comme partout ici, le vocabulaire voyage avec les données : `ordre`, `libelles`
et `glyphes` descendent au client, qui n'a pas le droit d'inventer un libellé.
"""

ORDRE = ("a_toi", "chez_les_autres", "a_ranger")
LIBELLE = {"a_toi": "À TOI",
           "chez_les_autres": "CHEZ LES AUTRES",
           "a_ranger": "À RANGER"}
GLYPHE = {"a_toi": "✋", "chez_les_autres": "➜", "a_ranger": "⌫"}

# Priorité de l'action suivante. Le plus petit gagne, et il n'y a pas d'ex aequo
# possible : deux sources différentes ne partagent jamais un poids.
#
# L'ordre n'est pas esthétique, il suit le coût de l'inaction :
#   ce qui est gelé  >  ce qui peut se perdre  >  ce qui bloque quelqu'un
#   >  ce qui pourrit  >  ce qui encombre.
P_BLOQUE      = 0    # une conversation gelée : une frappe la libère
P_ERREUR      = 1
P_CONFLIT     = 2    # ma PR ne peut plus être fusionnée
P_PRETE       = 3    # approuvée, sans conflit : il ne manque QUE mon geste
P_NON_COMMITE = 4    # la seule façon silencieuse de perdre du travail
P_A_CORRIGER  = 5
P_MON_VOTE    = 6    # je bloque quelqu'un d'autre
P_A_RELIRE    = 7    # une conversation a livré, personne n'a lu
P_NON_POUSSE  = 8
P_DORT        = 20   # chez les autres : ça pourrit, mais ce n'est pas à moi
P_EN_ATTENTE  = 21
P_BROUILLON   = 22
P_LIBERABLE   = 40   # à ranger
P_TERMINEE    = 41


def _item(cle, seau, poids, glyphe, texte, action, niveau="info",
          n=1, url=None, detail=None):
    return {"cle": cle, "seau": seau, "poids": poids, "glyphe": glyphe,
            "texte": texte, "action": action, "niveau": niveau, "n": n,
            "url": url, "detail": detail}


def _pluriel(n, singulier, pluriel=None):
    return singulier if n == 1 else (pluriel or singulier + "s")


# ------------------------------------------------------------ conversations
def _des_sessions(sessions):
    """Ce que les conversations vivantes réclament. Les états sont ceux de
    serveur.PRIORITE : on ne les redéfinit pas ici."""
    items = []
    par_etat = {}
    for s in sessions:
        par_etat.setdefault(s.get("state") or "working", []).append(s)

    for etat, poids, glyphe, mot, action, niveau in (
        ("blocked", P_BLOQUE,   "✋", "conversation bloquée",   "débloquer",  "bloque"),
        ("error",   P_ERREUR,   "✖", "conversation en erreur", "réparer",    "bloque"),
        ("review",  P_A_RELIRE, "➜", "conversation à relire",  "relire",     "agir"),
    ):
        lot = par_etat.get(etat) or []
        if not lot:
            continue
        n = len(lot)
        items.append(_item(
            "conv_" + etat, "a_toi", poids, glyphe,
            "%d %s" % (n, _pluriel(n, mot, mot.replace("conversation", "conversations"))),
            action, niveau, n=n,
            detail=", ".join(t for t in ((s.get("title") or s.get("ident")) for s in lot[:3]) if t)))
    return items


# --------------------------------------------------------------- les arbres
def _des_arbres(arbres):
    """Ce que le chantier réclame, du plus dangereux au plus anodin.

    `non_commite` et `ahead` sont les deux seules pertes possibles : retirer un
    arbre laisse vivre la branche et ses commits, mais pas les fichiers modifiés
    ni les commits jamais poussés.
    """
    items = []
    non_commite = [a for a in arbres if (a.get("fichiers") or 0) > 0]
    if non_commite:
        f = sum(a.get("fichiers") or 0 for a in non_commite)
        items.append(_item(
            "arbre_non_commite", "a_toi", P_NON_COMMITE, "✋",
            "%d %s non %s dans %d %s" % (
                f, _pluriel(f, "fichier"), _pluriel(f, "commité"),
                len(non_commite), _pluriel(len(non_commite), "arbre")),
            "commiter", "agir", n=len(non_commite),
            detail=", ".join(a.get("branche") or a.get("arbre") or "?"
                             for a in non_commite[:3])))

    non_pousse = [a for a in arbres if (a.get("ahead") or 0) > 0]
    if non_pousse:
        c = sum(a.get("ahead") or 0 for a in non_pousse)
        items.append(_item(
            "arbre_non_pousse", "a_toi", P_NON_POUSSE, "↑",
            "%d %s non %s" % (c, _pluriel(c, "commit"), _pluriel(c, "poussé")),
            "pousser", "agir", n=len(non_pousse),
            detail=", ".join(a.get("branche") or "?" for a in non_pousse[:3])))

    liberables = [a for a in arbres if a.get("liberable")]
    if liberables:
        n = len(liberables)
        items.append(_item(
            "arbre_liberable", "a_ranger", P_LIBERABLE, "⌫",
            "%d %s %s" % (n, _pluriel(n, "arbre"), _pluriel(n, "libérable")),
            "libérer %d %s" % (n, _pluriel(n, "arbre")), "info", n=n))

    terminees = [a for a in arbres if a.get("terminee")]
    if terminees:
        n = len(terminees)
        items.append(_item(
            "branche_terminee", "a_ranger", P_TERMINEE, "✓",
            "%d %s %s" % (n, _pluriel(n, "branche"), _pluriel(n, "terminée")),
            "supprimer %d %s" % (n, _pluriel(n, "branche")), "fait", n=n))
    return items


# ------------------------------------------------------------ pull requests
# état de PR -> (seau, poids, glyphe, action, niveau)
_PR = {
    "conflit":    ("a_toi",           P_CONFLIT,    "⚠", "résoudre le conflit", "bloque"),
    "prete":      ("a_toi",           P_PRETE,      "✓", "fusionner",           "fait"),
    "a_corriger": ("a_toi",           P_A_CORRIGER, "✋", "corriger",            "agir"),
    "a_relire":   ("a_toi",           P_MON_VOTE,   "➜", "voter",               "agir"),
    "dort":       ("chez_les_autres", P_DORT,       "⋯", "relancer",            "agir"),
    "en_attente": ("chez_les_autres", P_EN_ATTENTE, "➜", "attendre",            "attente"),
    "brouillon":  ("chez_les_autres", P_BROUILLON,  "·", "finir",               "info"),
}


def _des_pr(prs):
    """Une PR est déjà jugée par pullrequests._etat : on ne rejuge rien, on
    range. `a_relire` est la seule qui n'est pas à moi — c'est moi qui bloque."""
    items = []
    par_etat = {}
    for p in prs:
        par_etat.setdefault(p.get("etat") or "en_attente", []).append(p)

    for etat, lot in par_etat.items():
        regle = _PR.get(etat)
        if not regle:
            continue
        seau, poids, glyphe, action, niveau = regle
        lot.sort(key=lambda p: -(p.get("age_j") or 0))
        n = len(lot)
        vieux = lot[0].get("age_j") or 0
        libelle = {"conflit": "en conflit", "prete": "prête", "a_corriger": "à corriger",
                   "a_relire": "à relire", "dort": "sans relecteur",
                   "en_attente": "en attente", "brouillon": "en brouillon"}[etat]
        txt = "%d PR %s" % (n, libelle)
        if vieux:
            txt += " — %d j" % vieux
        items.append(_item(
            "pr_" + etat, seau, poids, glyphe, txt,
            "%s %d PR" % (action, n) if n > 1 else action, niveau, n=n,
            url=lot[0].get("url"),
            detail=", ".join("#%s%s" % (p.get("id"),
                                        " (US %s)" % p["us"] if p.get("us") else "")
                             for p in lot[:4])))
    return items


# ------------------------------------------------------------------- le tout
def _projets_de(config):
    """L'ordre des projets est celui de la configuration : c'est celui que
    l'utilisateur a choisi, et le board le respecte déjà partout ailleurs."""
    out = []
    for p in (config or {}).get("projects") or []:
        nom = p.get("name")
        if nom:
            out.append((nom, p.get("accent")))
    return out


def synthese(config, instantane, chantier_data, pr_data):
    """Range les trois relevés par projet. Aucun appel réseau, aucun git."""
    sessions = [s for g in (instantane or {}).get("groupes") or []
                for s in g.get("sessions") or []]

    arbres_par = {}
    for g in (chantier_data or {}).get("groupes") or []:
        arbres_par.setdefault(g.get("project"), []).extend(
            a for d in g.get("depots") or [] for a in d.get("arbres") or [])

    # Une PR appartient au projet de son arbre quand on le connaît, sinon à
    # celui que le scan PR a déduit. On ne devine jamais deux fois.
    pr_par = {}
    depot_projet = {}
    for proj, arbres in arbres_par.items():
        for a in arbres:
            if a.get("depot"):
                depot_projet[a["depot"]] = proj
    for g in (pr_data or {}).get("groupes") or []:
        for p in g.get("prs") or g.get("items") or []:
            proj = depot_projet.get(p.get("repo")) or g.get("project") \
                or p.get("ado_project") or "AUTRE"
            pr_par.setdefault(proj, []).append(p)

    accents = dict(_projets_de(config))
    noms = [n for n, _ in _projets_de(config)]
    for n in list(arbres_par) + list(pr_par) + [s.get("project") for s in sessions]:
        if n and n not in noms:
            noms.append(n)

    projets, total_a_toi = [], 0
    for nom in noms:
        mes_sessions = [s for s in sessions if s.get("project") == nom]
        arbres = arbres_par.get(nom) or []
        prs = pr_par.get(nom) or []
        if not (mes_sessions or arbres or prs):
            continue

        items = _des_sessions(mes_sessions) + _des_arbres(arbres) + _des_pr(prs)
        items.sort(key=lambda i: i["poids"])

        seaux = {c: [i for i in items if i["seau"] == c] for c in ORDRE}
        a_toi = sum(i["n"] for i in seaux["a_toi"])
        total_a_toi += a_toi

        # L'action suivante : le premier item, tous seaux confondus. Un projet
        # où il ne reste que du rangement le dit — il ne se tait pas.
        suivante = items[0] if items else None
        projets.append({
            "project": nom,
            "accent": accents.get(nom),
            "seaux": seaux,
            "a_toi": a_toi,
            "prochaine": {"texte": suivante["action"],
                          "glyphe": suivante["glyphe"],
                          "niveau": suivante["niveau"]} if suivante else None,
            "compte": {"sessions": len(mes_sessions),
                       "arbres": len(arbres), "prs": len(prs)},
        })

    # Un relevé incomplet doit le dire plutôt que d'afficher un projet calme.
    degrade = " ; ".join(filter(None, (
        (chantier_data or {}).get("degrade"), (pr_data or {}).get("degrade"))))
    return {"projets": projets, "a_traiter": total_a_toi,
            "ordre": list(ORDRE), "libelles": dict(LIBELLE), "glyphes": dict(GLYPHE),
            "degrade": degrade or None,
            "conversations_inconnues": (chantier_data or {}).get("conversations_inconnues"),
            "age_s": max((chantier_data or {}).get("age_s") or 0,
                         (pr_data or {}).get("age_s") or 0)}
