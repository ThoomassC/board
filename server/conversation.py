#!/usr/bin/env python3
"""Détail d'une conversation, pour le panneau qui s'ouvre au clic sur un session.

Lit le transcript et en tire ce qu'on veut savoir avant de décider si on va au
pane : ce que l'agent a dit, ce qu'il fait en ce moment, et quels sous-agents
travaillent pour lui.

Le point non évident : les sous-agents n'apparaissent PAS dans le transcript
principal (`isSidechain` y reste à zéro, leurs transcripts sont des fichiers
séparés). En revanche chaque appel d'outil y figure, et un appel `tool_use`
SANS `tool_result` correspondant est un appel encore en vol. C'est de là que
vient la ligne « en ce moment » — vérifié sur un transcript réel de 1745
lignes : 240 appels, 239 résultats, et le seul non apparié était bien la
commande en cours d'exécution.

CETTE RÈGLE NE VAUT PAS POUR LES SOUS-AGENTS, et c'est le piège qui a rendu la
liste des agents muette pendant tout le mois d'août. Un `Agent` part en
arrière-plan : son `tool_result` revient en une seconde et demie, et ce n'est
pas son résultat, c'est un accusé de lancement — « Async agent launched
successfully », suivi d'un `agentId`. Tout appel d'agent est donc apparié
immédiatement, la liste affichait invariablement « 0 en cours sur N », et un
lot de huit agents au travail se présentait comme huit agents finis.

La fin réelle arrive bien plus tard, dans un message `user` qui porte un bloc
`<task-notification>` :

    <task-id>a4c058ae24be45a3d</task-id>   identité stable de l'agent
    <tool-use-id>toolu_019ke7…</tool-use-id>
    <status>completed</status>            ou `failed`
    <summary>Agent "B1 jeux de donnees sur DEV" finished</summary>

`task-id` est ÉGAL à l'`agentId` de l'accusé de lancement — vérifié sur trois
transcripts, 10 agents, appariement complet. C'est la seule clé fiable, pour
deux raisons mesurées :

  · `tool-use-id` change d'une notification à l'autre pour le MÊME agent. Un
    agent relancé par `SendMessage` renotifie en citant l'identifiant de ce
    `SendMessage`, pas celui de son lancement : sur un transcript, 3 des 8
    identifiants notifiés étaient introuvables parmi les lancements.
  · les commandes Bash en arrière-plan notifient dans le même format, avec des
    `task-id` qui ne sont pas des agents (`b18b69ok3`…). Sur un transcript, 17
    notifications pour 1 seul agent. Apparier par `agentId` les écarte sans
    avoir à deviner à quoi ressemble un identifiant d'agent.

Un agent est donc EN COURS tant qu'aucune notification n'est venue après son
dernier réveil — son lancement, ou le dernier `SendMessage` qui lui était
adressé. C'est chronologique et non booléen : un agent qui a fini, puis qu'on
relance, retravaille.

Lecture seule. Ce module n'écrit jamais rien.
"""
import json
import os
import re
import time

ECHANGES_MAX = 8          # derniers échanges rendus
TEXTE_MAX = 420           # troncature d'un message
CACHE_MAX = 24
AGENTS_MAX = 12           # agents rendus, les EN COURS d'abord et jamais coupés

# Accusé de lancement d'un agent en arrière-plan, et bloc de fin de tâche.
# `[0-9a-z]` et non `[0-9a-f]` : les identifiants observés ne sont pas tous
# hexadécimaux (`b18b69ok3`), et se restreindre à l'hexa laisserait passer un
# préfixe tronqué au lieu de l'identifiant entier.
RE_AGENT_ID = re.compile(r"agentId:\s*([0-9a-z]{6,})")
RE_TACHE_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
RE_TACHE_ETAT = re.compile(r"<status>\s*([^<\s]+)\s*</status>")

_cache = {}               # chemin -> (mtime, taille, resultat)


def _texte(contenu):
    """Aplatit le contenu d'un message en une chaîne lisible."""
    if isinstance(contenu, str):
        return " ".join(contenu.split())
    if not isinstance(contenu, list):
        return ""
    bouts = []
    for b in contenu:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            bouts.append(" ".join(str(b["text"]).split()))
        elif t == "thinking":
            continue                          # jamais affiché
        elif t == "tool_use":
            bouts.append("[%s]" % (b.get("name") or "outil"))
        elif t == "tool_result":
            continue
    return " ".join(bouts).strip()


def _texte_resultat(bloc):
    """Texte d'un `tool_result`, que son contenu soit une chaîne ou des blocs."""
    c = bloc.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text") or "" for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _que_des_outils(txt):
    """« [Bash] [Read] » -> vrai. Sert à écarter les messages sans prose."""
    reste = txt
    for bout in txt.split():
        if bout.startswith("[") and bout.endswith("]"):
            reste = reste.replace(bout, "", 1)
    return not reste.strip()


def _epoch(ts):
    """Horodatage ISO-8601 UTC -> epoch. Renvoie None si illisible."""
    if not isinstance(ts, str) or len(ts) < 19:
        return None
    try:
        import calendar
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _analyser(chemin, vivante=None):
    lignes = []
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            lignes = f.read().splitlines()
    except Exception:
        return None

    lances = {}          # tool_use_id -> {nom, desc, ts}
    rendus = set()
    # Un agent est suivi par DEUX index sur le même dictionnaire : par
    # tool_use_id, seule clé connue au moment du lancement, et par agentId dès
    # que l'accusé le révèle. Les notifications ne parlent que la seconde
    # langue, les réveils par SendMessage aussi.
    agents_par_appel = {}
    agents_par_id = {}
    echanges = []
    titre = None
    dernier_prompt = None
    premier_ts = dernier_ts = None
    n_msg = 0

    for l in lignes:
        if not l.strip():
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue

        if d.get("aiTitle"):
            titre = d["aiTitle"]
        if d.get("lastPrompt"):
            dernier_prompt = str(d["lastPrompt"])
        ts = _epoch(d.get("timestamp"))
        if ts:
            premier_ts = ts if premier_ts is None else min(premier_ts, ts)
            dernier_ts = ts if dernier_ts is None else max(dernier_ts, ts)

        typ = d.get("type")
        if typ not in ("user", "assistant"):
            continue
        n_msg += 1
        msg = d.get("message") or {}
        contenu = msg.get("content")

        # appariement des appels d'outil
        if isinstance(contenu, list):
            for b in contenu:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    ent = b.get("input") or {}
                    desc = ent.get("description") or ent.get("prompt") or ""
                    nom = b.get("name") or "outil"
                    lances[b.get("id")] = {
                        "nom": nom,
                        "desc": " ".join(str(desc).split())[:120],
                        "ts": ts,
                    }
                    if nom == "Agent":
                        agents_par_appel[b.get("id")] = {
                            "type": ent.get("subagent_type") or None,
                            "desc": " ".join(str(desc).split())[:120],
                            "modele": ent.get("model") or None,
                            "lance_ts": ts,
                            "reveil_ts": ts,
                            "fin_ts": None,
                            "etat": None,      # completed | failed, tel que reçu
                        }
                    elif nom == "SendMessage":
                        # Un message adressé à un agent connu le remet au
                        # travail. `to` porte aussi des sockets d'autres
                        # sessions (`uds:/run/…`) : on ne réveille que ce qu'on
                        # a soi-même lancé, plutôt que de deviner la forme d'un
                        # identifiant d'agent.
                        a = agents_par_id.get(str(ent.get("to") or ""))
                        if a is not None:
                            a["reveil_ts"] = ts
                            a["fin_ts"] = None
                            a["etat"] = None
                elif b.get("type") == "tool_result":
                    rendus.add(b.get("tool_use_id"))
                    a = agents_par_appel.get(b.get("tool_use_id"))
                    if a is not None:
                        m = RE_AGENT_ID.search(_texte_resultat(b))
                        if m:
                            agents_par_id[m.group(1)] = a
                        else:
                            # Pas d'accusé de lancement : agent synchrone, ce
                            # résultat EST son résultat. Le seul cas où
                            # l'appariement classique vaut pour un agent.
                            a["fin_ts"] = ts
                            a["etat"] = "completed"

        txt = _texte(contenu)

        if "<task-notification>" in txt:
            mid, met = RE_TACHE_ID.search(txt), RE_TACHE_ETAT.search(txt)
            a = agents_par_id.get(mid.group(1)) if mid else None
            if a is not None:
                a["fin_ts"] = ts
                a["etat"] = met.group(1) if met else "completed"

        # Un message qui ne contient QUE des marqueurs d'outil (« [Bash] ») ne
        # dit rien au lecteur : l'outil en cours a déjà sa ligne dédiée.
        if txt and txt.strip("[] ").replace(" ", "") and not _que_des_outils(txt) \
                and not d.get("isMeta"):
            echanges.append({
                "role": "agent" if typ == "assistant" else "toi",
                "texte": txt[:TEXTE_MAX] + ("…" if len(txt) > TEXTE_MAX else ""),
                "at": ts,
            })

    maintenant = int(time.time())
    en_vol = [(i, v) for i, v in lances.items() if i not in rendus]

    # DEUX DURÉES, ET PAS UNE : « depuis » et « en » ne veulent pas dire la même
    # chose, et un champ unique qui glisse de l'une à l'autre selon l'état est
    # exactement le genre d'ambiguïté que ce board paie ensuite à l'écran.
    #   · en cours -> `depuis_s`, comptée depuis le DERNIER RÉVEIL. Un agent
    #     relancé travaille depuis son dernier message, pas depuis son
    #     lancement d'il y a trois heures.
    #   · fini     -> `duree_s`, du lancement à la notification. Sa vie entière.
    agents = []
    for a in agents_par_appel.values():
        vivant = a["fin_ts"] is None
        agents.append({
            "type": a["type"],
            "desc": a["desc"] or "(sans description)",
            "modele": a["modele"],
            "statut": "en_cours" if vivant else
                      ("echoue" if a["etat"] == "failed" else "fini"),
            "depuis_s": (maintenant - a["reveil_ts"]) if (vivant and a["reveil_ts"]) else None,
            "duree_s": (a["fin_ts"] - a["lance_ts"])
                       if (not vivant and a["fin_ts"] and a["lance_ts"]) else None,
        })
    # Les agents au travail d'abord, et parmi eux le plus ancien en tête : c'est
    # celui qui peut être en peine. Puis les échecs, qui demandent un regard,
    # avant les réussites. La troncature mord donc sur les agents finis.
    RANG = {"en_cours": 0, "perdu": 1, "echoue": 2, "fini": 3}
    agents.sort(key=lambda a: (RANG[a["statut"]],
                               -(a["depuis_s"] or a["duree_s"] or 0)))

    # UN AGENT NE SURVIT PAS À SA CONVERSATION. « Lancé, jamais notifié » se lit
    # « au travail » dans une conversation qui tourne, et « jamais revenu » dans
    # une conversation éteinte — mesuré sur un transcript coupé par une limite
    # de quota : trois agents y sont restés sans notification, et les annoncer
    # « EN COURS depuis 17 h » aurait été un mensonge de plus en plus gros.
    #
    # `vivante` suit la doctrine de `serveur.session_vivante` : True, False, ou
    # None quand on ne sait pas — et alors on ne conclut RIEN, l'agent reste au
    # travail plutôt que d'être déclaré perdu sur une supposition.
    if vivante is False:
        for a in agents:
            if a["statut"] == "en_cours":
                a["statut"] = "perdu"

    outil = None
    non_agents = [v for i, v in en_vol if v["nom"] != "Agent"]
    if non_agents:
        v = max(non_agents, key=lambda x: x["ts"] or 0)
        outil = {"nom": v["nom"], "desc": v["desc"],
                 "depuis_s": (maintenant - v["ts"]) if v["ts"] else None}

    return {
        "titre": titre,
        "dernier_prompt": (dernier_prompt or "")[:300] or None,
        # Les derniers échanges ne sont plus affichés : la fiche doit tenir en
        # un coup d'œil, et le contenu de la conversation se lit dans son pane.
        # Le champ reste calculé mais vide, pour ne casser aucun appelant.
        "echanges": [],
        "agents": agents[:AGENTS_MAX],
        "agents_en_cours": sum(1 for a in agents if a["statut"] == "en_cours"),
        "agents_total": len(agents),
        "outil": outil,
        "messages": n_msg,
        "debut_at": premier_ts,
        "fin_at": dernier_ts,
        "duree_s": (dernier_ts - premier_ts) if (premier_ts and dernier_ts) else None,
        "taille_ko": max(1, os.path.getsize(chemin) // 1024),
        "lignes": len(lignes),
    }


def detail(sid, transcript=None, projects_root=None, vivante=None):
    """Renvoie le détail d'une conversation, ou {"erreur": ...}.

    `transcript` évite la recherche quand le chemin est déjà connu.
    `vivante` dit si la conversation tourne encore (True/False/None) : elle
    seule permet de distinguer un agent au travail d'un agent jamais revenu.
    """
    chemin = transcript
    if not chemin or not os.path.isfile(chemin):
        racine = projects_root or os.path.join(
            os.path.expanduser("~"), ".claude", "projects")
        chemin = None
        try:
            for d in os.listdir(racine):
                c = os.path.join(racine, d, "%s.jsonl" % sid)
                if os.path.isfile(c):
                    chemin = c
                    break
        except Exception:
            pass
    if not chemin:
        return {"erreur": "transcript introuvable pour cette conversation"}

    try:
        st = os.stat(chemin)
        cle = (st.st_mtime, st.st_size, vivante)
    except Exception:
        return {"erreur": "transcript illisible"}

    # `vivante` entre dans la clé : une conversation qui s'éteint sans que son
    # transcript bouge change le statut de ses agents, et un cache indexé sur le
    # seul fichier aurait continué à les afficher au travail.
    ent = _cache.get(chemin)
    if ent and ent[0] == cle:
        return ent[1]

    res = _analyser(chemin, vivante)
    if res is None:
        return {"erreur": "transcript illisible"}
    res["sid"] = sid
    res["transcript"] = chemin
    if len(_cache) > CACHE_MAX:
        _cache.clear()
    _cache[chemin] = (cle, res)
    return res


# --------------------------------------------------------- auto-vérification
# Le transcript est FABRIQUÉ, et c'est le point. L'auto-vérification d'origine
# lit le dernier transcript réel : la plupart n'ont aucun sous-agent, donc la
# détection pouvait rester muette sans que rien ne le signale — c'est
# exactement ce qui est arrivé pendant un mois. Ces sept cas tiennent la règle
# en place, y compris ses trois pièges : la notification d'une commande Bash en
# arrière-plan, le message adressé à la socket d'une autre session, et l'agent
# synchrone dont le `tool_result` est bien son résultat.
_CAS = (
    #  desc                   type                    statut vivant  durée
    ("toujours au travail", "Explore",              "en_cours", None),
    ("relance apres coup",  "difai-core:difai-dev", "en_cours", 600),
    ("tue par le quota",    "general-purpose",      "echoue",   180),
    ("termine proprement",  "general-purpose",      "fini",     300),
    ("agent synchrone",     "general-purpose",      "fini",     55),
)


def _faux_transcript(chemin):
    """Écrit un transcript de sept situations d'agents. Voir _CAS."""
    t0 = int(time.time()) - 3600
    def h(d):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t0 + d))
    lot = []
    def lancer(tid, desc, typ, d):
        lot.append({"type": "assistant", "timestamp": h(d), "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Agent",
             "input": {"description": desc, "subagent_type": typ,
                       "model": "opus", "prompt": "…"}}]}})
    def accuser(tid, aid, d):
        lot.append({"type": "user", "timestamp": h(d), "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text",
             "text": "Async agent launched successfully.\nagentId: %s (internal)" % aid}]}]}})
    def notifier(aid, etat, d):
        lot.append({"type": "user", "timestamp": h(d), "message": {"content":
            "<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>toolu_zz"
            "</tool-use-id>\n<status>%s</status>\n</task-notification>" % (aid, etat)}})
    def envoyer(dest, d, tid):
        lot.append({"type": "assistant", "timestamp": h(d), "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "SendMessage",
             "input": {"to": dest, "message": "continue"}}]}})

    lancer("t1", "toujours au travail", "Explore", 0);   accuser("t1", "a1111111111", 1)
    lancer("t2", "termine proprement", "general-purpose", 10)
    accuser("t2", "a2222222222", 11); notifier("a2222222222", "completed", 310)
    lancer("t3", "tue par le quota", "general-purpose", 20)
    accuser("t3", "a3333333333", 21); notifier("a3333333333", "failed", 200)
    lancer("t4", "relance apres coup", "difai-core:difai-dev", 30)
    accuser("t4", "a4444444444", 31); notifier("a4444444444", "completed", 400)
    envoyer("a4444444444", 3000, "t4b")          # réveil : il repart au travail
    lancer("t5", "agent synchrone", "general-purpose", 40)
    lot.append({"type": "user", "timestamp": h(95), "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t5",
         "content": [{"type": "text", "text": "Voici mon rapport."}]}]}})
    notifier("b18b69ok3", "completed", 500)      # une commande Bash, pas un agent
    envoyer("uds:/run/user/1000/cc-socks/1234.sock", 600, "t7")   # une autre session
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x) for x in lot))


def autoverif():
    """Vérifie la détection des agents sur un transcript fabriqué. 0 = tout va."""
    import tempfile
    ecarts = []
    chemin = os.path.join(tempfile.mkdtemp(), "faux.jsonl")
    _faux_transcript(chemin)
    attendu = {d: (st, du) for d, _, st, du in _CAS}
    types = {d: t for d, t, _, _ in _CAS}

    for vivante, mot in ((True, "vivante"), (False, "éteinte")):
        _cache.clear()
        d = detail("faux", chemin, vivante=vivante)
        print("  conversation %-8s : %d agents, %d au travail"
              % (mot, d["agents_total"], d["agents_en_cours"]))
        if d["agents_total"] != len(_CAS):
            ecarts.append("%d agents au lieu de %d — la notification d'une "
                          "commande Bash a-t-elle créé un agent ?"
                          % (d["agents_total"], len(_CAS)))
        for a in d["agents"]:
            st, du = attendu[a["desc"]]
            if vivante is False and st == "en_cours":
                st = "perdu"
            vu = a["depuis_s"] if a["depuis_s"] is not None else a["duree_s"]
            print("     %-9s %-22s %-22s %ss"
                  % (a["statut"], (a["type"] or "-")[:22], a["desc"][:22], vu))
            if a["statut"] != st:
                ecarts.append("%s : statut %s au lieu de %s" % (a["desc"], a["statut"], st))
            if (a["type"] or "-") != types[a["desc"]]:
                ecarts.append("%s : type %s au lieu de %s"
                              % (a["desc"], a["type"], types[a["desc"]]))
            # Le réveil d'un agent relancé se compte depuis son SendMessage, pas
            # depuis son lancement : 600 s attendues, et non 3570.
            if du is not None and vu is not None and abs(vu - du) > 40:
                ecarts.append("%s : %ss au lieu de ~%ss" % (a["desc"], vu, du))
    try:
        os.remove(chemin)
    except Exception:
        pass
    for e in ecarts:
        print("  !! %s" % e)
    print("  %s" % ("tout va" if not ecarts else "%d écart(s)" % len(ecarts)))
    return len(ecarts)


if __name__ == "__main__":
    import glob
    import sys
    motif = os.path.expanduser("~/.claude/projects/*/*.jsonl")
    f = sorted(glob.glob(motif), key=os.path.getmtime, reverse=True)[0]
    sid = os.path.basename(f)[:-6]
    t0 = time.time()
    d = detail(sid, f)
    froid = (time.time() - t0) * 1000
    t0 = time.time()
    detail(sid, f)
    chaud = (time.time() - t0) * 1000
    print("transcript : %s (%d lignes, %d Ko)" % (os.path.basename(f), d["lignes"], d["taille_ko"]))
    print("froid %.0f ms · chaud %.2f ms" % (froid, chaud))
    print("titre      :", d["titre"])
    print("messages   : %s · durée %ss" % (d["messages"], d["duree_s"]))
    print("outil      :", d["outil"])
    print("agents     : %d dont %d en cours" % (d["agents_total"], d["agents_en_cours"]))
    for a in d["agents"][:8]:
        print("   %-9s %-18s %-40s %ss" % (
            a["statut"].upper(), (a["type"] or "-")[:18], a["desc"][:40],
            a["depuis_s"] if a["depuis_s"] is not None else a["duree_s"]))
    print("échanges   :", len(d["echanges"]))
    for e in d["echanges"][-3:]:
        print("   %-5s %s" % (e["role"], e["texte"][:80]))
    print("\ndétection des agents, sur transcript fabriqué :")
    sys.exit(1 if autoverif() else 0)
