#!/usr/bin/env python3
"""Détail d'une conversation, pour le panneau qui s'ouvre au clic sur un session.

Lit le transcript et en tire ce qu'on veut savoir avant de décider si on va au
pane : ce que l'agent a dit, ce qu'il fait en ce moment, et quels sous-agents
travaillent pour lui.

Le point non évident : les sous-agents n'apparaissent PAS dans le transcript
principal (`isSidechain` y reste à zéro, leurs transcripts sont des fichiers
séparés). En revanche chaque appel d'outil y figure, et un appel `tool_use`
SANS `tool_result` correspondant est un appel encore en vol. C'est de là que
vient la liste des agents en cours — vérifié sur un transcript réel de 1745
lignes : 240 appels, 239 résultats, et le seul non apparié était bien la
commande en cours d'exécution.

Lecture seule. Ce module n'écrit jamais rien.
"""
import json
import os
import time

ECHANGES_MAX = 8          # derniers échanges rendus
TEXTE_MAX = 420           # troncature d'un message
CACHE_MAX = 24

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


def _analyser(chemin):
    lignes = []
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            lignes = f.read().splitlines()
    except Exception:
        return None

    lances = {}          # tool_use_id -> {nom, desc, ts}
    rendus = set()
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
                    lances[b.get("id")] = {
                        "nom": b.get("name") or "outil",
                        "desc": " ".join(str(desc).split())[:120],
                        "ts": ts,
                    }
                elif b.get("type") == "tool_result":
                    rendus.add(b.get("tool_use_id"))

        txt = _texte(contenu)
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

    agents = []
    for i, v in lances.items():
        if v["nom"] != "Agent":
            continue
        agents.append({
            "desc": v["desc"] or "(sans description)",
            "en_cours": i not in rendus,
            "depuis_s": (maintenant - v["ts"]) if v["ts"] else None,
        })
    agents.sort(key=lambda a: (not a["en_cours"], -(a["depuis_s"] or 0)))

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
        "agents": agents[:8],
        "agents_en_cours": sum(1 for a in agents if a["en_cours"]),
        "outil": outil,
        "messages": n_msg,
        "debut_at": premier_ts,
        "fin_at": dernier_ts,
        "duree_s": (dernier_ts - premier_ts) if (premier_ts and dernier_ts) else None,
        "taille_ko": max(1, os.path.getsize(chemin) // 1024),
        "lignes": len(lignes),
    }


def detail(sid, transcript=None, projects_root=None):
    """Renvoie le détail d'une conversation, ou {"erreur": ...}.

    `transcript` évite la recherche quand le chemin est déjà connu.
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
        cle = (chemin, st.st_mtime, st.st_size)
    except Exception:
        return {"erreur": "transcript illisible"}

    ent = _cache.get(chemin)
    if ent and ent[0] == cle[1] and ent[1] == cle[2]:
        return ent[2]

    res = _analyser(chemin)
    if res is None:
        return {"erreur": "transcript illisible"}
    res["sid"] = sid
    res["transcript"] = chemin
    if len(_cache) > CACHE_MAX:
        _cache.clear()
    _cache[chemin] = (cle[1], cle[2], res)
    return res


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
    print("agents     : %d dont %d en cours" % (len(d["agents"]), d["agents_en_cours"]))
    for a in d["agents"][:5]:
        print("   %-9s %-50s %ss" % ("EN COURS" if a["en_cours"] else "fini",
                                     a["desc"][:50], a["depuis_s"]))
    print("échanges   :", len(d["echanges"]))
    for e in d["echanges"][-3:]:
        print("   %-5s %s" % (e["role"], e["texte"][:80]))
