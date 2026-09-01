#!/usr/bin/env python3
"""Rouvre une conversation archivee dans un nouveau pane, au bon endroit.

L'onglet Historique montre 45 conversations et n'offrait aucun moyen d'y
retourner — alors que c'est exactement ce qu'on cherche en ouvrant un
historique. Un clic lance ici `claude --resume <session>` dans le dossier du
projet concerne, via un nouvel onglet Windows Terminal.

On reprend le patron du lanceur de l'utilisateur (lanceur-panes.vbs) :

    wt.exe -w <fenetre> new-tab wsl.exe -d <distro> --cd <dossier> -- bash -lic "claude --resume <sid>"

Aucun shell intermediaire : la commande est passee en liste a subprocess, donc
rien a echapper et aucune injection possible depuis un identifiant de session.
"""
import os
import re
import subprocess

RE_SID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
DISTRO_DEFAUT = "Ubuntu"


def _ouvrir_onglet(dossier, commande, cfg):
    """Lance `commande` dans un nouvel onglet Windows Terminal, sur `dossier`.

    Le coeur partage de `rouvrir` (reprise d'une conversation) et de `ouvrir`
    (conversation neuve depuis l'onglet Chantier) : une seule construction de
    commande, donc une seule chose a verifier. La commande est passee en LISTE a
    subprocess — rien a echapper, aucune injection possible.
    """
    fenetre = str(cfg.get("wt_window") or "0")
    distro = str(cfg.get("wsl_distro") or DISTRO_DEFAUT)

    if not any(os.access(os.path.join(d, "cmd.exe"), os.X_OK)
               for d in os.environ.get("PATH", "").split(os.pathsep) if d):
        return False, "cmd.exe introuvable — hors WSL ?"

    cmd = ["cmd.exe", "/c", "wt.exe", "-w", fenetre, "new-tab",
           "wsl.exe", "-d", distro, "--cd", dossier,
           "--", "bash", "-lic", commande]
    try:
        # detache : on ne bloque pas la requete du board sur le demarrage d'un
        # terminal (~600 ms cote Windows).
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as exc:
        return False, "lancement impossible : %s" % exc
    return True, None


def rouvrir(sid, cwd, cfg=None):
    """Renvoie (ok, message). N'attend pas la fin du terminal."""
    cfg = cfg or {}
    if not sid or not RE_SID.match(str(sid)):
        return False, "identifiant de session invalide"

    dossier = os.path.expanduser(cwd or "")
    if not dossier or not os.path.isdir(dossier):
        return False, "dossier introuvable : %s" % (cwd or "(vide)")

    ok, souci = _ouvrir_onglet(dossier, "claude --resume %s" % sid, cfg)
    if not ok:
        return False, souci
    return True, "conversation rouverte dans %s" % os.path.basename(dossier)


def ouvrir(cwd, cfg=None):
    """Ouvre une conversation NEUVE dans `cwd`. Renvoie (ok, message).

    Utilise par l'onglet Chantier : on y voit un arbre de travail qui dort, on
    veut y lancer une conversation. Rien a reprendre, donc pas de `--resume`.

    Le dossier doit etre SOUS une racine de projet declaree dans la config. Le
    chemin vient du navigateur : la contrainte n'est pas la pour proteger d'un
    utilisateur qui se tromperait, mais pour que la seule chose que cette route
    puisse faire soit ce que l'onglet montre deja.
    """
    cfg = cfg or {}
    # Teste l'entree AVANT de la normaliser : realpath("") rend le dossier
    # courant du processus, qui existe — un chemin vide serait donc refuse plus
    # loin, avec le mauvais motif.
    if not (cwd or "").strip():
        return False, "aucun dossier fourni"
    dossier = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(dossier):
        return False, "dossier introuvable : %s" % cwd

    racines = []
    for p in cfg.get("projects") or []:
        if isinstance(p, dict) and p.get("root"):
            racines.append(os.path.realpath(os.path.expanduser(p["root"])))
    if not any(dossier == r or dossier.startswith(r + os.sep) for r in racines):
        return False, ("ce dossier n'est sous aucune racine de projet declaree — "
                       "ajoute-le par « + projet » d'abord")

    ok, souci = _ouvrir_onglet(dossier, "claude", cfg)
    if not ok:
        return False, souci
    return True, "conversation ouverte dans %s" % os.path.basename(dossier)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: rouvrir.py <session_id> <dossier> [--pour-de-vrai]")
        raise SystemExit(2)
    sid, dossier = sys.argv[1], sys.argv[2]
    if "--pour-de-vrai" not in sys.argv:
        print("commande qui SERAIT lancee (ajoute --pour-de-vrai pour l'executer) :")
        print("  cmd.exe /c wt.exe -w 0 new-tab wsl.exe -d %s --cd %s "
              "-- bash -lic \"claude --resume %s\"" % (DISTRO_DEFAUT, dossier, sid))
        raise SystemExit(0)
    print(rouvrir(sid, dossier))
