# -*- coding: utf-8 -*-
"""La file d'attente du bandeau — ce qui attend un geste, toutes sources confondues.

Ce module était l'onglet « Projets », un cinquième écran qui rangeait par projet
les trois relevés (conversations, arbres, pull requests) pour répondre à « sur
PROJET_A, où j'en suis ? ». L'axe était juste, l'écran ne l'était pas : mesuré le
02/09, il affichait UNE ligne pour deux projets — « 3 conversations à relire » —
entièrement dérivée des conversations, donc déjà dite deux fois ailleurs (les
trois jetons du bandeau, la pastille de chaque carte). Sa propre pastille
comptait les mêmes trois conversations que le bandeau. Un onglet, un sondage
réseau et 372 lignes pour un doublon.

CE QUI SURVIT EST SA DOCTRINE, PAS SON ÉCRAN. Le bandeau d'attention était une
file de CONVERSATIONS : une PR en conflit ou un arbre non commité n'y entrait
pas, et n'attendait donc nulle part. Ce module lui fournit exactement ce qui lui
manquait, et rien de plus.

    ce qui vient des conversations   -> déjà dans le bandeau, nommé une par une
    ce qui vient des arbres et des PR -> ici

UNE SEULE QUESTION, ET UNE SEULE RÉPONSE RETENUE. « Qui tient la balle ? » avait
trois réponses : à toi, chez les autres, à ranger. Seule la première monte dans
le bandeau — c'est ce qui l'empêche de se noyer, et c'est la contrainte posée en
choisissant cette fusion. Les deux autres lectures ne sont pas perdues : elles
vivent dans les onglets Chantier et Pull Requests, qui les portaient déjà.

Ce module n'invente aucune donnée et ne scanne rien : il consomme les relevés
déjà en cache. Comme partout ici, le vocabulaire voyage avec les données —
`texte` et `glyphe` descendent au client, qui n'a pas le droit d'inventer un
libellé.
"""

# Priorité de l'action suivante. Le plus petit gagne, et il n'y a pas d'ex aequo
# possible : deux sources différentes ne partagent jamais un poids.
#
# L'ordre n'est pas esthétique, il suit le coût de l'inaction :
#   ce qui est gelé  >  ce qui peut se perdre  >  ce qui bloque quelqu'un
#   >  ce qui pourrit  >  ce qui encombre.
#
# `P_MUETTE` A ÉTÉ INSÉRÉE en fusionnant l'onglet dans le bandeau, et les poids
# suivants ont été décalés. L'onglet ignorait délibérément l'état `silent` — une
# conversation muette n'est pas forcément une action — mais le bandeau, lui, l'a
# toujours affichée : il fallait donc lui donner un rang plutôt que de la faire
# disparaître au nom d'une échelle qui ne l'avait jamais prévue. Elle passe
# après ce qui peut se perdre (un travail non commité) et avant ce qui n'attend
# qu'un regard (une conversation qui a rendu la main), ce qui est exactement le
# rang qu'elle occupait dans `serveur.PRIORITE`.
P_BLOQUE      = 0    # une conversation gelée : une frappe la libère
P_ERREUR      = 1
P_CONFLIT     = 2    # ma PR ne peut plus être fusionnée
P_PRETE       = 3    # approuvée, sans conflit : il ne manque QUE mon geste
P_NON_COMMITE = 4    # la seule façon silencieuse de perdre du travail
P_A_CORRIGER  = 5
P_MUETTE      = 6    # une conversation qui ne dit plus rien
P_MON_VOTE    = 7    # je bloque quelqu'un d'autre
P_A_RELIRE    = 8    # une conversation a livré, personne n'a lu
P_NON_POUSSE  = 9

# Poids des CONVERSATIONS. Le bandeau les compose lui-même — il les nomme une
# par une, ce que ce module ne saurait pas faire — mais il prend leur rang ici :
# une seule échelle pour toute la file, sinon le tri n'a pas de sens.
POIDS_CONV = {"blocked": P_BLOQUE, "error": P_ERREUR,
              "silent": P_MUETTE, "review": P_A_RELIRE}


def _item(cle, poids, glyphe, texte, action, onglet, niveau="info",
          n=1, url=None, detail=None):
    """`onglet` est la DESTINATION du clic, et c'est une donnée, pas une
    déduction : le board n'a pas à lire le préfixe d'une clé pour savoir où
    mener. Un item de PR porte en plus son `url`, qui gagne quand elle existe.
    """
    return {"cle": cle, "poids": poids, "glyphe": glyphe, "texte": texte,
            "action": action, "onglet": onglet, "niveau": niveau, "n": n,
            "url": url, "detail": detail}


def _pluriel(n, singulier, pluriel=None):
    return singulier if n <= 1 else (pluriel or singulier + "s")


# --------------------------------------------------------------- les arbres
def _des_arbres(arbres):
    """Ce que le chantier réclame, du plus dangereux au plus anodin.

    `non_commite` et `ahead` sont les deux seules pertes possibles : retirer un
    arbre laisse vivre la branche et ses commits, mais pas les fichiers modifiés
    ni les commits jamais poussés. C'est pourquoi ces deux-là seulement montent
    dans le bandeau — un arbre libérable ou une branche terminée n'attend
    personne, il encombre, et l'onglet Chantier est fait pour ça.
    """
    items = []
    non_commite = [a for a in arbres if (a.get("fichiers") or 0) > 0]
    if non_commite:
        f = sum(a.get("fichiers") or 0 for a in non_commite)
        items.append(_item(
            "arbre_non_commite", P_NON_COMMITE, "✋",
            "%d %s non %s dans %d %s" % (
                f, _pluriel(f, "fichier"), _pluriel(f, "commité"),
                len(non_commite), _pluriel(len(non_commite), "arbre")),
            "commiter", "chantier", "agir", n=len(non_commite),
            detail=", ".join(a.get("branche") or a.get("arbre") or "?"
                             for a in non_commite[:3])))

    non_pousse = [a for a in arbres if (a.get("ahead") or 0) > 0]
    if non_pousse:
        c = sum(a.get("ahead") or 0 for a in non_pousse)
        items.append(_item(
            "arbre_non_pousse", P_NON_POUSSE, "↑",
            "%d %s non %s" % (c, _pluriel(c, "commit"), _pluriel(c, "poussé")),
            "pousser", "chantier", "agir", n=len(non_pousse),
            detail=", ".join(a.get("branche") or "?" for a in non_pousse[:3])))
    return items


# ------------------------------------------------------------ pull requests
# état de PR -> (poids, glyphe, action, niveau)
#
# Les trois états qui ne sont PAS à moi — `dort`, `en_attente`, `brouillon` —
# n'ont pas d'entrée : ils ne montent pas dans le bandeau. Ce n'est pas un oubli,
# c'est la règle de cette fusion. L'onglet Pull Requests les affiche toujours.
_PR = {
    "conflit":    (P_CONFLIT,    "⚠", "résoudre le conflit", "bloque"),
    "prete":      (P_PRETE,      "✓", "fusionner",           "fait"),
    "a_corriger": (P_A_CORRIGER, "✋", "corriger",            "agir"),
    "a_relire":   (P_MON_VOTE,   "➜", "voter",               "agir"),
}
_PR_LIBELLE = {"conflit": "en conflit", "prete": "prête",
               "a_corriger": "à corriger", "a_relire": "à relire"}


def _des_pr(prs):
    """Une PR est déjà jugée par `pullrequests._etat` : on ne rejuge rien, on
    range. `a_relire` est la seule qui n'est pas à moi — c'est moi qui bloque."""
    items = []
    par_etat = {}
    for p in prs:
        par_etat.setdefault(p.get("etat") or "en_attente", []).append(p)

    for etat, lot in par_etat.items():
        regle = _PR.get(etat)
        if not regle:
            continue
        poids, glyphe, action, niveau = regle
        lot.sort(key=lambda p: -(p.get("age_j") or 0))
        n = len(lot)
        vieux = lot[0].get("age_j") or 0
        txt = "%d PR %s" % (n, _PR_LIBELLE[etat])
        if vieux:
            txt += " — %d j" % vieux
        items.append(_item(
            "pr_" + etat, poids, glyphe, txt,
            "%s %d PR" % (action, n) if n > 1 else action, "pr", niveau, n=n,
            url=lot[0].get("url"),
            detail=", ".join("#%s%s" % (p.get("id"),
                                        " (US %s)" % p["us"] if p.get("us") else "")
                             for p in lot[:4])))
    return items


# ------------------------------------------------------------------- le tout
def _par_projet(config, chantier_data, pr_data):
    """Range arbres et PR par projet. Aucun appel réseau, aucun git.

    Une PR appartient au projet de son arbre quand on le connaît, sinon à celui
    que le scan PR a déduit. On ne devine jamais deux fois.
    """
    arbres_par = {}
    for g in (chantier_data or {}).get("groupes") or []:
        arbres_par.setdefault(g.get("project"), []).extend(
            a for d in g.get("depots") or [] for a in d.get("arbres") or [])

    depot_projet = {}
    for proj, arbres in arbres_par.items():
        for a in arbres:
            if a.get("depot"):
                depot_projet[a["depot"]] = proj

    pr_par = {}
    for g in (pr_data or {}).get("groupes") or []:
        for p in g.get("prs") or g.get("items") or []:
            proj = depot_projet.get(p.get("repo")) or g.get("project") \
                or p.get("ado_project") or "AUTRE"
            pr_par.setdefault(proj, []).append(p)

    # L'ordre des projets est celui de la configuration : c'est celui que
    # l'utilisateur a choisi, et le board le respecte déjà partout ailleurs.
    accents, noms = {}, []
    for p in (config or {}).get("projects") or []:
        if p.get("name"):
            noms.append(p["name"])
            accents[p["name"]] = p.get("accent")
    for n in list(arbres_par) + list(pr_par):
        if n and n not in noms:
            noms.append(n)

    return [(n, accents.get(n), arbres_par.get(n) or [], pr_par.get(n) or [])
            for n in noms]


def attente(config, chantier_data, pr_data):
    """Ce qui attend un geste et que le bandeau ne sait pas voir seul.

    Rend une liste d'items triés par poids, chacun portant son projet et son
    accent — le bandeau est groupé par projet comme le reste du board. Les
    conversations n'y sont PAS : le bandeau les nomme déjà une par une, et les
    reprendre ici les compterait deux fois.
    """
    out = []
    for nom, accent, arbres, prs in _par_projet(config, chantier_data, pr_data):
        for it in _des_arbres(arbres) + _des_pr(prs):
            it = dict(it)
            it["project"] = nom
            it["accent"] = accent
            out.append(it)
    out.sort(key=lambda i: i["poids"])
    return out


def degrade(chantier_data, pr_data):
    """Un relevé incomplet doit le dire plutôt que d'afficher un board calme."""
    return " ; ".join(filter(None, (
        (chantier_data or {}).get("degrade"),
        (pr_data or {}).get("degrade")))) or None


if __name__ == "__main__":
    # Auto-vérification : l'ordre des poids est la seule chose que ce module
    # décide, et deux sources ne doivent jamais partager un rang.
    poids = {n: v for n, v in sorted(globals().items())
             if n.startswith("P_") and isinstance(v, int)}
    doublons = len(poids) != len(set(poids.values()))
    print("poids : %s" % ", ".join("%s=%d" % (n, v) for n, v in
                                   sorted(poids.items(), key=lambda x: x[1])))
    print("ex aequo : %s" % ("OUI — À CORRIGER" if doublons else "aucun"))
    faux = [e for e in POIDS_CONV if e not in ("blocked", "error", "silent", "review")]
    print("etats de conversation couverts : %s%s"
          % (", ".join(sorted(POIDS_CONV)), " !! inconnu: %s" % faux if faux else ""))
    arbres = [{"fichiers": 3, "branche": "feat-40004", "ahead": 0},
              {"fichiers": 4, "branche": "feat-40003", "ahead": 2}]
    prs = [{"etat": "conflit", "id": 31004, "age_j": 5, "url": "http://x"},
           {"etat": "dort", "id": 31009, "age_j": 9}]
    for it in attente({"projects": [{"name": "PROJET_A", "accent": "#4EC9A0"}]},
                      {"groupes": [{"project": "PROJET_A",
                                    "depots": [{"arbres": arbres}]}]},
                      {"groupes": [{"project": "PROJET_A", "prs": prs}]}):
        print("  %2d %-2s %-44s %s" % (it["poids"], it["glyphe"],
                                       it["texte"], it["detail"] or ""))
    print("« dort » ne doit PAS apparaitre ci-dessus : c'est chez les autres.")
