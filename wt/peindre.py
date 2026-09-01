#!/usr/bin/env python3
"""Peint les panes du terminal aux couleurs du board.

Le board sait a quel projet appartient chaque conversation ; le terminal ne le
sait pas. On le lui dit, en ecrivant des sequences d'echappement dans le tty de
chaque pane :

    OSC 11  -> couleur de fond      = LE PROJET   (grande surface, identite stable)
    OSC 0   -> titre du pane        = L'ETAT      (petit, change souvent)
    OSC 111 -> retour au profil     (a la mort de la session)

C'est la meme discipline que sur le board : le fond porte le projet, le glyphe
porte l'etat. Le vocabulaire ne se retraduit pas d'un ecran a l'autre.

La teinte reprend EXACTEMENT la formule de ~/.claude/claude-wt-theme.py :
melange de #0C0C0C vers l'accent du projet, a `intensite` pour cent. On ne
reinvente pas la palette de l'utilisateur.
"""
import json
import os
import time

BASE = (0x0C, 0x0C, 0x0C)          # = claude-wt-theme.py
INTENSITE_DEFAUT = 24              # idem : lisible et differenciable

# Windows Terminal n'expose AUCUNE sequence d'echappement pour la couleur
# d'onglet : elle n'existe qu'en reglage de profil ou a la creation du pane.
# Trois canaux restent pilotables a chaud, du plus discret au plus envahissant :
#
#   MODE        canal            visibilite                verdict
#   titre       OSC 0            onglet + survol           lisible, zero gene
#   curseur     OSC 12           la ou l'oeil se pose      discret, toujours utile
#   fond        OSC 11           tout le pane              efficace mais lourd
#
# Par defaut : titre + curseur. Le fond reste possible mais n'est plus l'option
# retenue — un pane entierement teinte se lit moins bien qu'une pastille.
MODES_CONNUS = ("titre", "curseur", "fond")
MODE_DEFAUT = "titre+curseur"

# Pastilles de couleur, reprises de claude-wt-theme.py ou l'utilisateur les
# emploie deja comme icones d'onglet : le vocabulaire existe, on ne l'invente pas.
PASTILLES = (
    (210, "\U0001F535"),   # bleu
    (155, "\U0001F7E2"),   # vert
    (275, "\U0001F7E3"),   # violet
    (355, "\U0001F534"),   # rouge
    (40,  "\U0001F7E1"),   # jaune
    (25,  "\U0001F7E0"),   # orange
)


def _teinte(rgb):
    """Teinte en degres (0-360). Sert a choisir la pastille la plus proche."""
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0.0
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60


def pastille(hexa):
    """Le rond de couleur le plus proche de l'accent du projet."""
    rgb = _rgb(hexa)
    if not rgb:
        return ""
    h = _teinte(rgb)
    return min(PASTILLES, key=lambda p: min(abs(h - p[0]), 360 - abs(h - p[0])))[1]

# (bg, titre) deja ecrits, par tty. On ne reecrit que ce qui change : un pane
# repeint chaque seconde clignoterait et polluerait le flux du terminal.
_dernier = {}


def _rgb(h):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _mix(src, dst, pct):
    return "#%02X%02X%02X" % tuple(
        round(a + (b - a) * pct / 100) for a, b in zip(src, dst))


def _racine():
    return os.environ.get("BOARD_HOME") or os.path.join(
        os.path.expanduser("~"), ".claude", "board")


def _panes_vivants():
    """{cwd -> tty} des processus claude qui ont un terminal de controle.

    Sert de repli : le hook SessionStart n'enregistre que les sessions ouvertes
    APRES son installation. Pour les autres, on retrouve le pane en croisant le
    dossier de travail. Un cwd partage par deux sessions est ambigu : on ecarte
    alors les deux plutot que de peindre le mauvais pane.
    """
    import subprocess
    trouves = {}
    doublons = set()
    try:
        r = subprocess.run(["ps", "-eo", "pid=,tty=,comm="],
                           capture_output=True, text=True, timeout=3)
        for ligne in r.stdout.splitlines():
            bouts = ligne.split()
            if len(bouts) < 3 or bouts[2] != "claude":
                continue
            pid, tty = bouts[0], bouts[1]
            if tty in ("?", "-"):
                continue
            try:
                cwd = os.path.realpath("/proc/%s/cwd" % pid)
            except Exception:
                continue
            if cwd in trouves and trouves[cwd] != "/dev/" + tty:
                doublons.add(cwd)
            trouves[cwd] = "/dev/" + tty
    except Exception:
        return {}
    for c in doublons:
        trouves.pop(c, None)
    return trouves


def _lire_tty(sid, cwd=None):
    """Renvoie (device, titre_libre) ou (None, False). Nettoie au passage un
    enregistrement dont le pane a disparu."""
    p = os.path.join(_racine(), "state", "%s.tty" % sid)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        dev = d.get("tty")
        if not dev or not os.path.exists(dev):
            os.unlink(p)                     # pane ferme : l'enregistrement ment
            _dernier.pop(dev, None)
            return None, False
        return dev, bool(d.get("titre_libre"))
    except Exception:
        pass
    # Repli : session ouverte avant l'installation du hook.
    #
    # Attention, le croisement n'est pas trivial : on compare le dossier du
    # PROCESSUS claude (celui du lancement, fige) au dossier COURANT de la
    # session (qui bouge des qu'on entre dans un worktree). Une egalite stricte
    # rate donc toutes les sessions qui se sont deplacees. On accepte donc aussi
    # qu'un pane soit l'ANCETRE du dossier courant — en prenant le plus proche,
    # et seulement s'il est unique : un pane lance depuis ~ serait sinon
    # l'ancetre de tout le monde.
    if cwd:
        vivants = _panes_vivants()
        cible = os.path.realpath(cwd)
        dev = vivants.get(cible)
        if not dev:
            candidats = [(len(racine), d) for racine, d in vivants.items()
                         if cible == racine or cible.startswith(racine.rstrip("/") + os.sep)]
            if candidats:
                profond = max(c[0] for c in candidats)
                retenus = {d for lg, d in candidats if lg == profond}
                if len(retenus) == 1:
                    dev = retenus.pop()
        if dev and os.path.exists(dev):
            # titre_libre inconnu par cette voie : on ne touche pas au titre,
            # Claude Code le reecrirait et on se battrait pour rien.
            return dev, False
    return None, False


def _ecrire(dev, sequences):
    """Ecriture directe dans le tty. Un pane ferme entre-temps ne doit jamais
    faire remonter d'exception : le board continue."""
    try:
        with open(dev, "w", encoding="utf-8", errors="replace") as f:
            f.write("".join(sequences))
            f.flush()
        return True
    except Exception:
        return False


def peindre(sessions, cfg):
    """Repeint les panes des sessions donnes. Idempotent, silencieux, borne."""
    term = (cfg or {}).get("terminal") or {}
    if not term.get("enabled"):
        return 0
    pct = term.get("intensite", INTENSITE_DEFAUT)
    try:
        pct = max(0.0, min(100.0, float(pct)))
    except Exception:
        pct = INTENSITE_DEFAUT
    mode = str(term.get("mode") or MODE_DEFAUT)
    # rétro-compatibilité : l'ancien réglage booléen « titre »
    if "titre" in term and "mode" not in term:
        mode = "titre+curseur" if term.get("titre") else "curseur"
    actifs = {m for m in MODES_KNOWN_SPLIT(mode)}
    veut_titre = "titre" in actifs

    peints = 0
    for e in sessions or []:
        sid = e.get("sid")
        if not sid:
            continue
        dev, titre_libre = _lire_tty(sid, e.get("cwd"))
        if not dev:
            continue

        # Pas d'accent (projet de repli) = pas de couleur. On prefere ne rien
        # dire plutot que d'inventer une identite de projet.
        acc_hex = e.get("accent")
        accent = _rgb(acc_hex)

        bg = _mix(BASE, accent, pct) if (accent and "fond" in actifs) else None
        curseur = acc_hex if (accent and "curseur" in actifs) else None

        titre = None
        if veut_titre and titre_libre:
            bout = [pastille(acc_hex) if accent else "",
                    e.get("glyphe") or "", e.get("ident") or ""]
            if e.get("project"):
                bout.append("· " + e["project"])
            titre = " ".join(x for x in bout if x)

        etat = (bg, curseur, titre)
        if _dernier.get(dev) == etat:
            continue

        seq = []
        # Si le fond n'est plus voulu mais l'a ete, il faut le RENDRE : sinon la
        # teinte precedente reste collee au pane.
        ancien = _dernier.get(dev)
        if bg:
            seq.append("\033]11;%s\007" % bg)
        elif ancien and ancien[0]:
            seq.append("\033]111\007")
        if curseur:
            seq.append("\033]12;%s\007" % curseur)
        if titre:
            seq.append("\033]0;%s\007" % titre.replace("\007", "").replace("\033", ""))
        if seq and _ecrire(dev, seq):
            _dernier[dev] = etat
            peints += 1
    return peints


def MODES_KNOWN_SPLIT(mode):
    """« titre+curseur » -> {"titre","curseur"}. Tout mode inconnu est ignore."""
    return [m for m in str(mode).replace(",", "+").split("+")
            if m.strip() in MODES_CONNUS for m in [m.strip()]]


def effacer(sid, cwd=None):
    """Rend le pane a son profil : fond ET curseur d'origine. A appeler a la mort
    d'une session, sinon le pane garde les couleurs du board pour toujours."""
    dev, _ = _lire_tty(sid, cwd)
    if not dev:
        return False
    _dernier.pop(dev, None)
    # OSC 111 = fond par defaut, OSC 112 = curseur par defaut
    return _ecrire(dev, ["\033]111\007", "\033]112\007"])


def effacer_tout():
    """Filet de securite : rend TOUS les panes enregistres a leur profil."""
    n = 0
    d = os.path.join(_racine(), "state")
    try:
        noms = [f[:-4] for f in os.listdir(d) if f.endswith(".tty")]
    except Exception:
        return 0
    for sid in noms:
        if effacer(sid):
            n += 1
    return n


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--effacer":
        print("panes rendus a leur profil :", effacer_tout())
        raise SystemExit(0)

    d = os.path.join(_racine(), "state")
    try:
        ttys = sorted(f for f in os.listdir(d) if f.endswith(".tty"))
    except Exception:
        ttys = []
    print("panes enregistres : %d" % len(ttys))
    for f in ttys:
        sid = f[:-4]
        dev, libre = _lire_tty(sid)
        print("  %-38s %-12s titre_libre=%s" % (sid, dev or "(disparu)", libre))
    print("\nformule de teinte, intensite %d%% :" % INTENSITE_DEFAUT)
    for nom, acc in (("PROJET_A", "#4EC9A0"), ("PROJET_B", "#D8A657"),
                     ("terraform", "#9B6DD6"), ("frontend", "#4A9EFF"),
                     ("ansible", "#E06C75")):
        print("  %-10s %s -> fond %s" % (nom, acc,
              _mix(BASE, _rgb(acc), INTENSITE_DEFAUT)))
