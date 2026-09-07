/* Le board — client.
   Deux principes qui gouvernent tout ce fichier :
   1. On ne réécrit jamais le DOM en entier. Réconciliation par clé, sinon
      l'écran scintille, le focus se perd et le glisser-déposer casse.
   2. Aucun compteur n'est incrémenté localement. Tout vient de l'instantané
      serveur : si le flux meurt, l'affichage GÈLE et le voile apparaît.
      Un board grisé est honnête, un board figé ne l'est pas. */
"use strict";

// PIGMENTS D'ETAT — remappes avec la direction Ardoise (01/09).
//   blocked + error -> --red  : les deux disent « arretee, il faut toi ». Elles se
//                               distinguent par le glyphe (✋ / ✖), par le mot de la
//                               pastille, et par le pouls que seul blocked porte.
//   review          -> --amber (etait --sky)
//   working         -> --green : l'etat neutre prend enfin une encre, parce que la
//                               pastille ecrit un mot et qu'un mot a besoin d'une
//                               couleur. Le fond de la carte, lui, reste neutre :
//                               TEINTE ne contient toujours que blocked et error.
//   --sky n'est PLUS un pigment d'etat : il est l'accent du chrome, et rien d'autre.
const EDGE = { blocked:"var(--red)", error:"var(--red)", silent:"var(--grey)",
               review:"var(--amber)", working:"var(--green)" };
const TEINTE = new Set(["blocked", "error"]);
const FENETRE = { five_hour: 5*3600, seven_day: 7*24*3600 };

let dernier = null;          // dernier instantané reçu
let dernierAt = 0;           // horodatage local de réception (fraîcheur du flux)
let ongletActif = "board";
let layout = { projects: [], cards: {} };
const survol = new Map();    // sid -> timer de marquage « vu »
let ordreProjets = [];       // ordre COMPLET des colonnes, filtre ignoré : voir
                             // le `drop` de brancherGlisserColonne

/* ══════════ « avec conversation » : UN SEUL ÉTAT POUR DEUX ÉCRANS ════════════
   `avecConv` allumé, on n'affiche que les projets où une conversation Claude
   travaille en ce moment. Deux écrans obéissent :

     ACCUEIL   une colonne par projet DÉCLARÉ, y compris à zéro conversation.
               C'est l'écran d'arrivée, et c'est là que les projets sur lesquels
               on ne travaille pas encombrent le plus. Critère : le groupe de
               l'instantané n'a aucune `sessions`.
     CHANTIER  un bloc par projet ayant des arbres de travail.
               Critère : `conversations === 0` dans /api/chantier.

   DEUX DONNÉES DIFFÉRENTES, UN SEUL ÉTAT. Les deux critères ne viennent pas de
   la même source — l'instantané SSE d'un côté, un relevé git de l'autre — et
   peuvent donc ne pas masquer exactement les mêmes projets au même instant. Ça
   n'est pas une divergence à corriger : ce sont deux écrans qui ne listent pas
   les mêmes objets. Ce qui ne doit PAS diverger, c'est la volonté de
   l'utilisateur, et elle tient dans cette seule variable, pilotée par un seul
   contrôle, dans le chrome (board.html, `.ch-ctrl`).

   ALLUMÉ AU DÉMARRAGE, JAMAIS PERSISTÉ, et c'est la même règle que les plis de
   l'onglet Chantier — voir la doctrine au-dessus de `chReplie`. Une préférence
   d'affichage qui survit à la nuit finirait par cacher, un lundi matin, le
   projet où l'on a laissé vendredi soir trois fichiers non commités, sans
   qu'aucune trace du geste de la semaine dernière n'explique pourquoi. Ni
   localStorage, ni /api/layout — et `autocomplete="off"` sur la case, sinon le
   navigateur la restaurerait tout seul après un rechargement, ce qui
   persisterait la préférence par la bande. */
let avecConv = true;

/* CE QUE CHAQUE ÉCRAN A MASQUÉ AU DERNIER RENDU, et ce que ce retrait emporte.
   Chaque écran dépose ici son propre bilan ; l'aveu du chrome affiche celui de
   l'onglet VISIBLE. Un seul aveu à l'écran, donc jamais deux nombres à tenir
   d'accord — mais chacun reste calculé par l'écran qui masque, seul à savoir ce
   qu'il retire (des colonnes ici, des arbres là). */
const bilanFiltre = {
  board:    { projets: 0, noms: [], perils: 0, nomsPeril: [], inconnu: false },
  chantier: { projets: 0, noms: [], perils: 0, nomsPeril: [], inconnu: false },
};

function pluriel(n, un, plusieurs) { return n + " " + (n > 1 ? plusieurs : un); }

/* ── L'AVEU, ET LE TEXTE QUI LE PORTE AILLEURS QUE DANS UN `title` ────────────
   Trois publics, une seule rédaction : l'aveu visible à côté de l'interrupteur,
   son `title` pour la souris, et la description liée à la case pour le clavier
   et le lecteur d'écran (`aria-describedby` -> #fc-desc). Un `title` sur un
   `<span>` n'est atteignable ni au clavier ni au lecteur d'écran (WCAG 1.3.1),
   d'où le texte hors-vue qui existe pour de bon dans le document.

   Le nom du contrôle reste COURT dans le label — c'est le nom accessible de la
   case, et y verser trois phrases les ferait relire à chaque tabulation. */
function texteFiltre(onglet) {
  const b = bilanFiltre[onglet] || bilanFiltre.board;
  let t = "Allumé, n'affiche que les projets où une conversation Claude "
        + "travaille en ce moment. Vaut pour l'onglet Conversations, qui masque "
        + "alors la colonne du projet, et pour l'onglet Chantier, qui masque son "
        + "bloc d'arbres de travail.";
  if (b.inconnu) {
    /* L'IGNORANCE NE MASQUE RIEN, et c'est le cas qui compte le plus.
       Côté accueil, un instantané AVEUGLE (`sessions_indisponibles`) publie
       toutes les colonnes à zéro conversation : sans ce garde-fou, la moindre
       lecture ratée du dossier d'états viderait l'écran d'accueil en entier.
       docs/SCHEMA.md le nomme comme « l'échec le plus probable de tout le
       serveur ». Côté chantier, c'est `conversations` à null. */
    return t + " En ce moment le serveur ne sait pas quelles conversations "
             + "tournent : RIEN n'est masqué tant qu'on ne sait pas.";
  }
  if (!b.projets) return t + " En ce moment, aucun projet n'est masqué.";
  t += " En ce moment : " + pluriel(b.projets, "projet masqué", "projets masqués")
     + " — " + b.noms.join(", ") + ".";
  if (b.perils)
    t += " " + pluriel(b.perils, "contient du travail qu'on peut perdre",
                       "contiennent du travail qu'on peut perdre")
       + " — non commité, non poussé ou incohérent : " + b.nomsPeril.join(", ") + ".";
  return t + " Éteins « avec conversation » pour les revoir.";
}

/* Repeint l'aveu du chrome à partir du bilan de l'onglet VISIBLE. Appelé par les
   deux rendus, par `ongler` et par la bascule elle-même : c'est le seul endroit
   qui écrit dans #fc-aveu, donc le seul à pouvoir mentir. */
function majAveuFiltre() {
  const aveu = $("#fc-aveu"), desc = $("#fc-desc");
  if (!aveu) return;
  texte(desc, texteFiltre(ongletActif));
  const b = bilanFiltre[ongletActif];
  // Les onglets PR et Historique ne filtrent rien : un aveu y parlerait d'un
  // écran qu'on ne regarde pas.
  const concerne = ongletActif === "board" || ongletActif === "chantier";
  if (!concerne || !avecConv || !b || !b.projets) { aveu.hidden = true; return; }
  aveu.hidden = false;
  aveu.textContent = "";
  aveu.append(el("b", null, String(b.projets)),
              document.createTextNode(b.projets > 1 ? " masqués" : " masqué"));
  if (b.perils) {
    const p = el("b", "peril", "⚠" + b.perils);
    attr(p, "aria-hidden", "true");   // la description le dit en toutes lettres
    aveu.append(p);
  }
  attr(aveu, "title", texteFiltre(ongletActif));
}

/* ─────────────────────────────── utilitaires ───────────────────────────── */
const $ = (s, r = document) => r.querySelector(s);

function poste(route, corps) {
  return fetch(route, { method:"POST", headers:{"Content-Type":"application/json"},
                        body: JSON.stringify(corps) }).catch(() => {});
}

function texte(el, valeur) {           // n'écrit que si ça change : zéro reflow inutile
  const v = valeur == null ? "" : String(valeur);
  if (el.textContent !== v) el.textContent = v;
}

function attr(el, nom, valeur) {
  if (valeur == null || valeur === false) { if (el.hasAttribute(nom)) el.removeAttribute(nom); }
  else if (el.getAttribute(nom) !== String(valeur)) el.setAttribute(nom, valeur);
}

function classe(el, nom, actif) { el.classList.toggle(nom, !!actif); }

/* Reste avant réinitialisation, format adaptatif. Le mot « reset » n'apparaît pas. */
function reste(secondes, longue) {
  if (secondes == null) return { txt:"—", etat:"" };
  const s = Math.floor(secondes);
  if (s <= 0) return { txt:"SESSION NEUVE", etat:"neuve" };
  if (longue && s >= 86400) {
    const j = Math.floor(s/86400), h = Math.floor((s%86400)/3600);
    return { txt:`${j}j${String(h).padStart(2,"0")}`, etat:"" };
  }
  if (s >= 3600) {
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
    return { txt:`${h}h${String(m).padStart(2,"0")}`, etat:"" };
  }
  const m = Math.floor(s/60), sec = s%60;
  if (m < 10) return { txt:`${m}m${String(sec).padStart(2,"0")}`, etat:"urgent" };
  return { txt:`${m}m`, etat:"" };
}

function heureAbsolue(epoch, longue) {
  if (!epoch) return "";
  const d = new Date(epoch*1000);
  const hh = String(d.getHours()).padStart(2,"0"), mm = String(d.getMinutes()).padStart(2,"0");
  if (!longue) return `(${hh}:${mm})`;
  const jours = ["dim.","lun.","mar.","mer.","jeu.","ven.","sam."];
  return `(${jours[d.getDay()]} ${hh}:${mm})`;
}

/* ─────────────────────────── bandeau de compte ─────────────────────────── */
/* L'horloge a été retirée : le système en a une, et elle occupait la place la
   plus visible de la barre pour l'information la moins liée à l'écran.
   Les deux jauges n'affichent plus que trois choses ; le pourcentage exact et
   l'heure de réinitialisation vivent dans le title de la jauge. */
function rendChrome(snap) {
  for (const [cle, sel, longue] of [["five_hour","#lim-5h",false], ["seven_day","#lim-7j",true]]) {
    const el = $(sel), rl = (snap.account || {})[cle] || null;
    const remplissage = $("i", el), tick = $(".rythme", el);
    if (!rl) {
      remplissage.style.width = "0%"; tick.style.display = "none";
      texte($(".reste", el), "—");
      attr(el, "title", "quota inconnu — aucune mesure reçue de la statusline");
      continue;
    }
    const pct = Math.max(0, Math.min(100, Number(rl.used_pct) || 0));
    const restant = rl.resets_at ? rl.resets_at - snap.now : null;
    const fen = FENETRE[cle];
    // Le repère de rythme : où l'on devrait en être si la consommation était linéaire.
    let attendu = null;
    if (restant != null) attendu = Math.max(0, Math.min(100, ((fen - restant) / fen) * 100));

    remplissage.style.width = pct.toFixed(1) + "%";
    // La couleur encode la VITESSE, pas le niveau — sauf plafond proche.
    let col = "var(--green)";
    if (pct >= 90) col = "var(--red)";
    else if (attendu != null && pct > attendu) col = "var(--amber)";
    remplissage.style.background = col;

    if (attendu == null) tick.style.display = "none";
    else { tick.style.display = ""; tick.style.left = attendu.toFixed(1) + "%"; }

    const r = reste(restant, longue);
    const elReste = $(".reste", el);
    texte(elReste, r.txt);
    elReste.className = "reste" + (r.etat ? " " + r.etat : "");

    // L'infobulle porte ce que la barre n'affiche plus. Elle dit aussi la
    // CONCLUSION et pas seulement les nombres : « tu consommes plus vite que le
    // rythme » est la seule phrase que ce bloc a jamais eu à dire.
    const abs = heureAbsolue(rl.resets_at, longue).replace(/[()]/g, "");
    attr(el, "title",
      (cle === "five_hour" ? "Quota 5 heures" : "Quota 7 jours")
      + ` · ${Math.round(pct)} % consommés`
      + (attendu != null ? ` · repère de rythme à ${Math.round(attendu)} %` : "")
      + (abs ? ` · réinitialisation à ${abs}` : "")
      + (attendu != null
          ? (pct > attendu ? " — tu consommes PLUS VITE que le rythme"
                           : " — tu es dans le rythme")
          : ""));
  }
}

function rendFlux() {
  // DEUX liens à surveiller, et le second était ignoré :
  //   · navigateur <- serveur : le SSE. Sa mort gèle l'écran, d'où le voile.
  //   · capteurs   -> serveur : les hooks et la statusline. Leur mort ne gelait
  //     RIEN : le board continuait d'afficher des chiffres morts, tout vert.
  // On affiche donc le pire des deux, et on dit lequel.
  const ageSSE = dernierAt ? Math.floor((Date.now() - dernierAt) / 1000) : null;
  const ageCap = dernier && dernier.capteurs_age_s != null
    ? Math.floor(dernier.capteurs_age_s) : null;
  const el = $("#flux");

  const pire = Math.max(ageSSE == null ? -1 : ageSSE, ageCap == null ? -1 : ageCap);
  const cause = (ageCap != null && ageCap === pire && (ageSSE == null || ageCap > ageSSE))
    ? "capteurs" : "flux";

  texte($("span", el), pire < 0 ? "—" : pire + "s");
  attr(el, "title", pire < 0 ? "flux non établi"
    : cause === "capteurs"
      ? `capteurs muets depuis ${pire} s — hook ou statusline en panne ?`
      : `dernier instantané reçu il y a ${pire} s`);
  // Un capteur au repos est normal : la statusline bat à 10 s, on tolère 45 s
  // avant de s'inquiéter. Le SSE, lui, doit arriver chaque seconde.
  const seuilAmbre = cause === "capteurs" ? 45 : 10;
  const seuilPerdu = cause === "capteurs" ? 180 : 30;
  classe(el, "vieux", pire >= seuilAmbre);
  classe(document.body, "perdu", pire >= seuilPerdu);
  const v = $("#voile div");
  if (v) texte(v, cause === "capteurs" ? "CAPTEURS MUETS" : "FLUX PERDU");
}

/* ───────────────────────── bandeau d'attention ───────────────────────────────
   La zone la plus stratégique de l'écran, et elle était vide 90 % du temps : en
   régime calme, ses 40 px portaient UNE ligne de 11,5 px en gris pâle, ferrée à
   droite — l'endroit où l'œil arrive en dernier. La cause est un accident de
   code : `.att-aide` servait à la fois au message calme ET à l'indice tertiaire
   du régime alerte, donc le message le plus important de l'écran calme héritait
   du style prévu pour la donnée la moins importante du régime alerte. Personne
   n'avait jamais CHOISI un style pour le régime calme.

   Il a maintenant sa classe, `.att-msg`, à 15 px / 700 / --t1.

   POURQUOI PAS CENTRÉ, malgré la demande. En régime alerte, la lecture commence
   au bord gauche de la barre. Un message calme centré obligerait à apprendre
   DEUX points de fixation selon le régime qu'on découvre — or la réponse la plus
   rapide est celle qu'on trouve toujours au même endroit. Le centre du viewport
   est déjà pris, une ligne au-dessus, par les onglets ; le dupliquer affaiblirait
   les deux. Le message est donc ferré à gauche, à l'origine exacte des boutons
   du régime alerte.

   LA COULEUR SUIT LA GRAVITÉ RÉELLE, ce qui corrige un mensonge : le fond était
   ambre — « ton action est attendue » — même quand la liste ne contenait que du
   travail à relire. Le mapping reprend exactement la restriction que l'écran
   s'est déjà donnée au niveau des cartes (TEINTE = {blocked, error}) : seuls ces
   deux états teintent un fond. `silent` et `review` gardent un fond neutre, leur
   pigment restant porté par le glyphe de chaque bouton. */
let attTout = false;      // « +N autres » déplié — geste de l'utilisateur, jamais du système

/* ─────────────────────── LES DEUX JETONS DU BANDEAU ─────────────────────────
   Le bandeau porte deux genres d'entrées depuis que l'onglet Projets y a été
   fusionné (02/09) : les CONVERSATIONS, qu'il nomme une par une, et les ITEMS
   — une PR en conflit, des fichiers non commités — qu'il ne savait pas voir.
   Le serveur dit lequel par `genre` : rien ici ne le devine.

   Ils partagent la géométrie de `.att` et ne se distinguent que par ce qu'un
   clic fait, parce que c'est la seule chose qui diffère vraiment : une
   conversation ouvre sa fiche, un item mène là où il se traite. */
function jetonConv(e) {
  const b = document.createElement("button");
  b.className = "att";
  b.type = "button";
  // L'identifiant quitte le jeton et rejoint l'infobulle : c'est lui qui
  // rendait deux conversations d'un même dépôt indiscernables ici.
  b.title = `${e.project} · ${e.libelle}` + (e.ident ? ` · ${e.ident}` : "");
  // Le glyphe garde le pigment d'état ; le NOM prend l'encre primaire. Le
  // libellé reste, atténué : le contrat interdit qu'une couleur de statut
  // voyage sans son glyphe ET son libellé, et « ✋ » seul demanderait au
  // lecteur de connaître quatre glyphes par cœur.
  const g = el("b", null, e.glyphe);
  g.style.color = EDGE[e.state] || "var(--t1)";
  b.append(g, el("span", "nom", nomLisible(e).nom),
           el("span", "quoi",
              e.state === "blocked" ? "attend ton OK"
            : e.state === "error" ? "erreur"
            : e.state === "silent" ? "muette" : "a rendu la main"),
           el("span", "d", e.since));
  b.onclick = () => ouvrir(e.sid);
  return b;
}

// Pigment d'un item : le NIVEAU vient du serveur, la couleur en découle ici —
// la même table que partout, pour qu'un ambre veuille dire la même chose dans
// le bandeau et sur une carte.
const ATT_NIVEAU = { bloque:"var(--red)", agir:"var(--amber)",
                     attente:"var(--amber)", fait:"var(--green)", info:"var(--t3)" };

function jetonItem(e) {
  // Une PR porte une URL : le jeton devient un lien, et le clic sort du board.
  // Sinon il mène à l'onglet qui détaille la chose — `onglet` est envoyé par le
  // serveur, le board ne lit pas le préfixe d'une clé pour le deviner.
  const lien = !!e.url;
  const b = document.createElement(lien ? "a" : "button");
  b.className = "att item";
  if (lien) { b.href = e.url; b.target = "_blank"; b.rel = "noopener"; }
  else b.type = "button";
  b.title = `${e.project || ""} · ${e.action || ""}`.trim().replace(/^· /, "");
  const g = el("b", null, e.glyphe || "·");
  g.style.color = ATT_NIVEAU[e.niveau] || "var(--t3)";
  b.append(g, el("span", "nom", e.texte || ""));
  if (e.detail) b.append(el("span", "quoi", e.detail));
  // Pas de chrono : un item n'a pas d'âge propre — c'est un agrégat. À la
  // place, ce qu'il faut faire, qui est la seule chose qu'on veut savoir de
  // plus. Le serveur l'a déjà rédigé (`action`).
  if (e.action) b.append(el("span", "d", e.action));
  if (!lien && e.onglet) b.onclick = () => ongler(e.onglet);
  return b;
}

function rendAttention(snap) {
  const zone = $("#attention");
  const liste = snap.attention || [];
  zone.textContent = "";
  if (liste.length <= 3) attTout = false;
  // La gravité maximale présente décide du fond, et rien d'autre.
  const etats = new Set(liste.map(e => e.state));
  classe(zone, "grave", etats.has("blocked"));
  classe(zone, "casse", !etats.has("blocked") && etats.has("error"));
  classe(zone, "tout", attTout && liste.length > 3);
  if (!liste.length) {
    classe(zone, "calme", true);
    // Compter par ÉTAT, pas le total : annoncer « 3 sessions au travail » quand
    // un seul travaille, c'était le mensonge le plus visible du bandeau.
    const tous = (snap.groupes || []).flatMap(g => g.sessions || []);
    const par = t => tous.filter(e => e.state === t).length;
    const bosse = par("working"), vues = tous.length - bosse;

    // UN SEUL VERDICT, et c'est « rien ne t'attend ».
    // Il y avait deux formules — « rien ne t'attend » quand tout travaillait,
    // « rien de nouveau » dès qu'une conversation avait déjà été vue. La
    // nuance était juste mais elle se payait cher : la phrase du repos changeait
    // de sens selon un détail que le décompte dit déjà, et « rien de nouveau »
    // est plus faible que ce que le bandeau sait vraiment. Une file d'attente
    // vide, c'est exactement « rien ne t'attend », dans les quatre cas.
    const verdict = tous.length ? "Rien ne t'attend" : "Aucune conversation ouverte";
    const morceaux = [];
    if (bosse) morceaux.push(`${bosse} au travail`);
    if (vues)  morceaux.push(`${vues} déjà vue${vues > 1 ? "s" : ""}`);

    // Le point d'état, le verdict, le décompte : trois éléments au lieu d'une
    // phrase, parce que les trois n'ont pas le même poids (cf. board.css).
    const bloc = el("span", "att-repos");
    bloc.append(el("span", "att-pt" + (bosse ? "" : " creux")),
                el("span", "att-msg", verdict));
    if (morceaux.length) bloc.append(el("span", "att-det", morceaux.join(", ")));
    zone.append(bloc);
    return;
  }
  classe(zone, "calme", false);
  // Plafonnée à 3 entrées PAR DÉFAUT : au-delà, le bandeau crie et ne dit plus
  // rien. Mais le plafond se lève sur un geste, ce que la doctrine autorise —
  // c'est le système qui n'a pas le droit de décider, pas l'utilisateur.
  for (const e of liste.slice(0, attTout ? liste.length : 3))
    zone.append(e.genre === "item" ? jetonItem(e) : jetonConv(e));
  if (liste.length > 3) {
    // C'était un <span> sans clic, sans survol et non focusable : un texte qui
    // ressemble à une troncature et promet une action qu'il ne tient pas.
    const p = document.createElement("button");
    p.type = "button";
    p.className = "att-plus";
    const reste = liste.length - 3;
    p.textContent = attTout ? "◂ replier"
                            : `+${reste} autre${reste > 1 ? "s" : ""}`;
    p.title = attTout ? "revenir aux trois plus urgentes"
                      : `montrer les ${reste} conversation${reste > 1 ? "s" : ""} restante${reste > 1 ? "s" : ""}`;
    p.onclick = () => { attTout = !attTout; if (dernier) rendAttention(dernier); };
    zone.append(p);
  }
  const aide = document.createElement("span");
  aide.className = "att-aide";
  aide.textContent = "clic = fiche de la conversation";
  zone.append(aide);
}

/* ──────────────────────────────── la carte ─────────────────────────────── */
function creerCarte(sid) {
  const c = document.createElement("article");
  c.className = "carte";
  c.dataset.sid = sid;
  c.tabIndex = 0;
  c.draggable = true;
  // ORDRE DE LECTURE : titre, identifiant, meta, phrase, etat + jauge.
  // Le titre monte en premiere ligne (D1 de docs/AMELIORATIONS.md) et
  // l'identifiant descend juste dessous, en mono. La pastille d'etat vit dans
  // .bas et NON dans .ctx : .ctx est masquee des que ctx_pct est nul, et la
  // carte perdrait son etat avec sa jauge.
  c.innerHTML =
    '<div class="titre"></div>' +
    '<div class="tech"><span class="id"></span><span class="meta"></span></div>' +
    '<div class="dit"></div>' +
    '<div class="bas"><span class="pill"><i class="gl"></i><b class="lib"></b></span>' +
      '<button class="valider" type="button" hidden>' +
        '<i aria-hidden="true">\u2713</i>lu</button>' +
      '<span class="relue" hidden><i aria-hidden="true">\u2713</i>relue</span>' +
      '<span class="agents" hidden></span>' +
      '<span class="pr-puce" hidden></span>' +
      '<span class="chrono"></span></div>' +
    '<div class="ctx"><span class="ctx-tr"><i></i>' +
      '<u style="left:50%"></u><u style="left:70%"></u></span>' +
      '<span class="ctx-n"></span></div>' +
    '<button class="jeter" type="button" title="Mettre à la poubelle — la conversation quitte le board et reste dans l\'historique">⌫</button>';

  c.addEventListener("click", () => ouvrir(sid));
  c.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); ouvrir(sid); }
  });
  // Survol soutenu = « vu », SAUF pour « à relire ».
  //
  // « À relire » a désormais un bouton : la conversation attend une décision de
  // l'utilisateur, pas un regard. Or le survol la marquait au bout de 600 ms —
  // c'est-à-dire pendant le trajet de la souris VERS le bouton. Le clic serait
  // arrivé sur une carte déjà marquée, et le bouton n'aurait rien voulu dire.
  // Les autres états gardent la règle du survol : elle est ce qui empêche le
  // bandeau de crier au loup en permanence.
  c.addEventListener("mouseenter", () => {
    const e = sessionDe(sid);
    if (e && e.state === "review") return;
    survol.set(sid, setTimeout(() => marquerVu(sid), 600));
  });
  c.addEventListener("mouseleave", () => {
    clearTimeout(survol.get(sid)); survol.delete(sid);
  });
  // « lu » : la conversation a été relue et n'attend plus rien de personne.
  $(".valider", c).addEventListener("click", ev => {
    ev.stopPropagation();          // sinon le clic ouvre aussi la fiche
    validerRelue(sid);
  });

  // La poubelle ne détruit rien : elle sort la conversation du board vivant.
  $(".jeter", c).addEventListener("click", ev => {
    ev.stopPropagation();
    poste("/api/archiver", { sid });
    c.remove();                       // retrait immédiat, sans attendre le flux
  });
  brancherGlisserCarte(c);
  return c;
}

/* ─────────────────── LE TITRE PORTE, L'IDENTIFIANT SUIT ─────────────────────
   Une seule implémentation, parce que deux consommateurs la partagent : la
   carte et le bandeau d'attention. docs/SCHEMA.md l'exige nommément pour
   l'extraction du n° d'US, et pour la même raison — cette règle-ci a déjà
   divergé une fois, le bandeau étant resté sur l'ancienne hiérarchie pendant
   que la carte passait au titre (D1). Le bandeau y affichait alors deux fois
   « ProjetA » pour deux conversations différentes.

   Rend { nom, appoint } : `nom` est ce qui doit se lire en premier, `appoint`
   ce qui le complète, ou "" quand il n'apprendrait rien.
     · pas de titre exploitable  -> l'identifiant devient le nom, et ne se
       répète pas en appoint ;
     · titre égal à l'identifiant -> on n'écrit pas deux fois la même chaîne ;
     · identifiant égal au dépôt  -> `meta` finit déjà par le dépôt, donc
       l'appoint le dirait une troisième fois. */
function nomLisible(e) {
  const titre = (e.title || "").trim();
  const ident = (e.ident || "").trim();
  const repo  = (e.repo  || "").trim();
  const titreUtile = titre && titre !== "(sans titre)" && titre !== ident;
  return {
    nom: titreUtile ? titre : ident,
    appoint: (titreUtile && ident && ident !== repo) ? ident : "",
  };
}

function majCarte(c, e) {
  c.className = "carte " + e.state
    + (TEINTE.has(e.state) ? " teinte" : "")
    + (e.aging ? " age" : "") + (e.seen ? " vu" : "");
  // --edge alimente le glyphe, la pastille, la bordure teintee et le chrono
  // vieillissant. Le repli --edge-off (et non une valeur en dur : elle avait deja
  // fait afficher un rail ardoise fonce sur fond blanc en theme clair) ne sert
  // plus qu'aux etats absents d'EDGE, c'est-a-dire a aucun aujourd'hui.
  c.style.setProperty("--edge", EDGE[e.state] || "var(--edge-off)");
  texte($(".gl", c), e.glyphe);

  // La regle vit dans nomLisible(), partagee avec le bandeau d'attention.
  const nl = nomLisible(e);
  texte($(".titre", c), nl.nom);
  texte($(".id", c),    nl.appoint);
  $(".id", c).hidden = !nl.appoint;

  texte($(".chrono", c), e.since);
  // La pastille ecrit l'etat en clair : c'est le canal STATUT depuis Ardoise.
  texte($(".lib", c), e.libelle);

  // ── LE CANAL STATUT NE PORTE QU'UNE SEULE PASTILLE À LA FOIS ───────────────
  // Une conversation acquittée n'est plus « à relire » : elle est « relue ». La
  // pastille d'état s'efface donc au profit du témoin, qui prend sa place et sa
  // géométrie. Garder les deux affichait « À RELIRE ✓ RELUE » côte à côte — une
  // question et sa réponse au même instant, c'est-à-dire un écran qui demande
  // encore ce qu'on vient de lui donner.
  //
  // Ce n'est pas un mensonge sur l'état : le serveur maintient `review`, et il a
  // raison — la conversation attend toujours quelque chose de la MACHINE. Ce que
  // la carte affiche, c'est où en est L'UTILISATEUR, et l'infobulle porte les
  // deux lectures pour qui veut la vérité brute.
  const arelire = e.state === "review";
  const acquittee = arelire && e.seen;
  $(".pill", c).hidden    = acquittee;
  $(".valider", c).hidden = !(arelire && !e.seen);
  $(".relue", c).hidden   = !acquittee;
  attr(c, "title", `${e.libelle}${acquittee ? " · relue, acquittée par toi" : ""}`
                   + ` · ${e.project} · ${e.repo || ""}`);

  // ── LES SOUS-AGENTS AU TRAVAIL POUR CETTE CONVERSATION
  // Ne s'affiche que s'il y en a : zéro agent et « on ne sait pas » (le serveur
  // envoie null) ne méritent aucun pixel, et une puce permanente ne voudrait
  // plus rien dire. Elle vaut aussi comme avertissement — une conversation qui
  // a trois agents en vol n'est pas une conversation qu'on interrompt.
  const ag = $(".agents", c);
  const nag = e.agents;
  ag.hidden = !nag;
  if (nag) {
    texte(ag, "\u2699 " + nag + " agent" + (nag > 1 ? "s" : ""));
    attr(ag, "title", nag + " sous-agent" + (nag > 1 ? "s" : "") +
        " au travail pour cette conversation — ouvre la fiche pour savoir lesquels");
  }

  // ── la PR de cette branche, s'il en existe une
  const puce = $(".pr-puce", c);
  if (e.pr) {
    puce.hidden = false;
    puce.className = "pr-puce " + e.pr.etat;
    texte(puce, "PR #" + e.pr.id);
    attr(puce, "title", `PR #${e.pr.id} — ${PR_ETAT[e.pr.etat]?.txt || e.pr.etat}` +
        `, ouverte depuis ${e.pr.age_j} j` +
        (e.pr.commentaires ? `, ${e.pr.commentaires} commentaire(s)` : "") +
        (e.pr.non_resolus ? `, ${e.pr.non_resolus} fil(s) non résolu(s)` : ""));
  } else {
    puce.hidden = true;
  }

  const meta = $(".meta", c);
  meta.textContent = "";
  if (e.stale) {
    // Le reste de la ligne reste `meta`, comme partout ailleurs. Ce chemin
    // ajoutait `repo` en dur, ce qui redonnait le nom de la colonne au moment
    // même où `meta` a cessé de le répéter — deux règles pour une seule ligne.
    const h = document.createElement("span");
    h.className = "hachure"; h.textContent = "oubliée ?";
    meta.append(h);
    if (e.meta) meta.append(document.createTextNode(" · " + e.meta));
  } else {
    meta.textContent = e.meta || "";
  }
  // Le coût ferme la ligne technique, et prend l'ambre au-delà du second seuil.
  // Les deux décisions — l'afficher, et le teinter — viennent du serveur : le
  // board ne connaît aucun seuil.
  if (e.cout) {
    const sou = el("b", "sou" + (e.cout_fort ? " fort" : ""), e.cout);
    sou.title = "coût cumulé de cette conversation";
    if (meta.textContent) meta.append(document.createTextNode(" · "));
    meta.append(sou);
  }

  // ── l'état du worktree : la question « lequel a du travail non commité ? »
  const g = e.git;
  if (g && (g.fichiers || g.ahead || g.behind)) {
    const z = el("span", "git");
    if (g.fichiers) {
      const x = el("b", "chg", g.fichiers + " chg");
      x.title = g.fichiers + " fichier(s) modifié(s) et non commité(s)";
      z.append(x);
    }
    if (g.ahead) {
      const x = el("b", "av", "↑" + g.ahead);
      x.title = g.ahead + " commit(s) à pousser";
      z.append(x);
    }
    if (g.behind) {
      const x = el("b", "re", "↓" + g.behind);
      x.title = g.behind + " commit(s) à récupérer";
      z.append(x);
    }
    meta.append(z);
  }

  texte($(".dit", c), e.say ? `« ${e.say} »` : "");
  $(".dit", c).hidden = !e.say;

  const ctx = $(".ctx", c), barre = $(".ctx-tr i", c);
  if (e.ctx_pct == null) { ctx.hidden = true; }
  else {
    ctx.hidden = false;
    barre.style.width = Math.max(0, Math.min(100, e.ctx_pct)).toFixed(1) + "%";
    barre.style.background = e.ctx_level === "crit" ? "var(--red)"
                           : e.ctx_level === "warn" ? "var(--amber)" : "var(--green)";
    texte($(".ctx-n", c), Math.round(e.ctx_pct) + "%");
  }
}

/* ─────────────────── adopter un projet détecté ──────────────────────────────
   Le bouton « + projet » a quitté la barre du haut, où il vivait à côté du
   mot-marque sans lien avec quoi que ce soit à l'écran. L'adoption se propose
   maintenant EXACTEMENT là où le manque se voit : en tête de la colonne de
   repli, au-dessus des conversations qui n'ont pas de projet.

   Le serveur devine le nom et la racine (cf. `candidats_projets`) ; le clic
   ouvre le formulaire pré-rempli. Rien n'est écrit sans validation humaine :
   config.json reste le fichier de l'humain. */
function majAdoption(col, snap) {
  const cands = snap.candidats || [];
  let z = $(".adopt", col);
  if (!cands.length) { if (z) z.remove(); return; }
  if (!z) {
    z = el("div", "adopt");
    col.insertBefore(z, $(".cartes", col));
  }
  // Rendu complet : la liste est courte et ne change qu'à l'ouverture d'une
  // conversation dans un dossier neuf. Aucun risque de scintillement.
  const signature = cands.map(c => c.root).join("|");
  if (z.dataset.sig === signature) return;
  z.dataset.sig = signature;
  z.textContent = "";

  const t = el("div", "adopt-t");
  t.append(el("b", null, String(cands.length)),
           el("span", null, cands.length > 1 ? "dossiers non déclarés"
                                             : "dossier non déclaré"));
  z.append(t);

  for (const c of cands) {
    const b = el("button", "adopt-l");
    b.type = "button";
    b.append(el("b", null, "+ " + c.name),
             el("span", null, c.root_court || c.root),
             el("em", null, c.convs + (c.convs > 1 ? " conversations" : " conversation")));
    attr(b, "title", `Adopter « ${c.root_court || c.root} » comme projet : `
      + `une colonne à lui, sa couleur, et un lanceur claude-${(c.name||"").toLowerCase()}. `
      + `Le formulaire s'ouvre pré-rempli — tu peux corriger le nom avant de valider.`);
    b.onclick = () => ouvrirFormProjet(c.name, c.root_court || c.root);
    z.append(b);
  }
}

/* ──────────────────────────── les colonnes ─────────────────────────────── */
function creerColonne(nom) {
  const col = document.createElement("section");
  col.className = "col";
  col.dataset.projet = nom;
  // Le canal PROJET passe du filet de 2 px à un POINT de 9 px : sur un panneau
  // arrondi de 18 px, un filet droit posé en haut se fait couper par la courbe
  // aux deux extrémités, et il faut le regarder pour le voir. Un point se lit
  // du premier coup d'œil, et il vit sur la même ligne que le nom qu'il
  // qualifie. La couleur reste posée par board.js via --acc, comme avant.
  col.innerHTML = '<header class="col-hd" draggable="true">' +
                  '<i class="pt"></i><span class="nm"></span>' +
                  '<span class="ct"></span></header>' +
                  '<div class="cartes"></div>';
  brancherGlisserColonne(col);
  return col;
}

function rendBoard(snap) {
  const board = $("#board");
  board.className = "board mode-" + (snap.mode || "M");
  const tous = snap.groupes || [];

  /* ── « avec conversation » SUR L'ÉCRAN D'ACCUEIL ──────────────────────────
     Cet écran affiche une colonne par projet DÉCLARÉ, pas par projet occupé :
     un projet sans aucune conversation y occupe une colonne pleine largeur pour
     y écrire « Aucune conversation ouverte ». C'est précisément ce que
     l'interrupteur du chrome retire.

     LE CRITÈRE EST `sessions`, ET PAS `count`. Les deux sont publiés et
     coïncident aujourd'hui (`count: len(lot)`, serveur.py), mais c'est le
     tableau `sessions` qui décide de ce que la colonne CONTIENT. Filtrer sur ce
     qui remplit la colonne, et non sur un nombre publié à côté, c'est ce qui
     garantit qu'on ne masque jamais une colonne qui aurait eu des cartes.

     L'IGNORANCE NE MASQUE RIEN — et ici ce n'est pas une précaution théorique.
     Quand `Board.sessions()` n'arrive pas à lire ~/.claude/board/state — droits,
     montage tombé, dossier supprimé pendant la lecture — le serveur répond par
     un instantané AVEUGLE : toutes les colonnes, `count: 0`, `sessions: []`,
     et le motif dans `sessions_indisponibles`. docs/SCHEMA.md appelle ça
     « l'échec le plus probable de tout le serveur » et raconte le bug qu'il a
     déjà causé une fois. Sans ce garde-fou, une lecture ratée viderait l'écran
     d'ACCUEIL en entier, en affirmant qu'aucune conversation ne tourne. La
     règle est donc la même que celle de `conversations === 0` sur le Chantier :
     on ne fait pas disparaître un projet sur une ignorance.

     LA COLONNE DE REPLI EST ÉPARGNÉE QUAND ELLE PORTE UNE ADOPTION. Vide, elle
     ne dit rien et part comme les autres ; mais c'est la SEULE colonne où
     peuvent apparaître les dossiers non déclarés à adopter (`majAdoption`), et
     ce bloc-là n'est pas une conversation : c'est une action qui n'a aucun
     autre endroit où vivre. */
  const aveugle = !!snap.sessions_indisponibles;
  const actif = g => (g.sessions || []).length > 0
                  || (g.project === snap.fallback && (snap.candidats || []).length > 0);
  const filtre = avecConv && !aveugle;
  const groupes = filtre ? tous.filter(actif) : tous;
  const caches = filtre ? tous.filter(g => !actif(g)) : [];
  bilanFiltre.board = { projets: caches.length, noms: caches.map(g => g.project),
                        perils: 0, nomsPeril: [], inconnu: aveugle };
  // L'ordre COMPLET, avant filtrage : c'est lui que le glisser-déposer d'une
  // colonne doit réécrire, sinon un projet masqué disparaîtrait de layout.json.
  ordreProjets = tous.map(g => g.project);
  majAveuFiltre();

  if (!groupes.length) {
    board.classList.add("vide");
    /* LE VIDE A DEUX CAUSES, ET ELLES NE SE DISENT PAS PAREIL. Sans filtre,
       c'est un poste au repos. Avec le filtre, c'est l'écran qui a tout retiré —
       et c'est le cas de TOUS LES SOIRS. Un écran vide qui ne nomme ni la cause
       de son vide ni le geste qui le remplit est un écran cassé ; le compteur du
       chrome ne suffit pas ici, puisqu'il n'y a rien en dessous pour lui donner
       une échelle. On reconstruit donc à chaque fois dans ce cas, le
       `dataset.vide` ne gardant que la version invariable. */
    // `dataset.vide` sert de SIGNATURE et pas de drapeau : le flux repasse ici
    // une fois par seconde, et reconstruire ce paragraphe à chaque tour serait
    // un scintillement pour rien.
    const sign = caches.length ? "f" + caches.length : "0";
    if (board.dataset.vide !== sign) {
      board.dataset.vide = sign;
      board.textContent = "";
      if (caches.length) {
        const p = el("p", "board-vide");
        p.append(document.createTextNode("Aucune conversation Claude ouverte. "),
                 el("b", null, pluriel(caches.length, "projet est masqué",
                                       "projets sont masqués")),
                 document.createTextNode(" — éteins « avec conversation », en haut "
                                         + "à droite, pour les revoir."));
        board.append(p);
      } else {
        board.textContent = "Aucune conversation Claude Code ouverte.";
      }
    }
    return;
  }
  if (board.dataset.vide) { board.textContent = ""; delete board.dataset.vide; }
  board.classList.remove("vide");

  // On écrit une VARIABLE, pas grid-template-columns : la feuille peut ainsi
  // reprendre la main en étroit sans avoir besoin de !important.
  if (snap.mode !== "L")
    board.style.setProperty("--cols", `repeat(${groupes.length}, minmax(0,1fr))`);
  else board.style.setProperty("--cols", "none");

  const muet = $("#legende-muet");
  if (muet) {
    const off = dernier && dernier.notify_actif === false;
    muet.hidden = !off;
    if (off) {
      texte(muet, "notifications éteintes");
      attr(muet, "title", "Aucun toast Windows ne sera émis. " +
           "Passe notify.enabled à true dans ~/.claude/board/config.json.");
    }
  }
  // LE PIED DE PAGE EST UN MODE D'EMPLOI, PAS UN AVEU. Il portait « aucun pane
  // suivi — la fiche reste lisible », coincé entre deux raccourcis, dans la
  // ligne la moins lue de l'écran. D6 avait demandé cet aveu et il était juste ;
  // il était seulement au mauvais endroit. Il vit désormais dans la fiche, sous
  // le bouton qu'il concerne — le même geste que l'adoption de projet, qui a
  // quitté la barre du haut pour la colonne où le manque se voit.
  const fin = $("#legende-fin");
  if (fin) texte(fin, "clic = fiche · ⌫ = poubelle · glisser pour réordonner");

  const vues = new Set();
  groupes.forEach((g, i) => {
    vues.add(g.project);
    let col = board.querySelector(`.col[data-projet="${CSS.escape(g.project)}"]`);
    if (!col) { col = creerColonne(g.project); board.append(col); }
    if (col.style.order !== String(i)) col.style.order = String(i);
    col.style.setProperty("--acc", g.accent || "var(--t3)");
    texte($(".nm", col), g.project);
    // « SESSION » DANS LE CODE, « CONVERSATION » À L'ÉCRAN. Le mot « session »
    // est le nom de l'objet du contrat (docs/SCHEMA.md) et il y reste ; à
    // l'écran il désigne autre chose pour qui lit — une session de travail, pas
    // un fil de discussion. L'onglet, l'aide et la fiche disaient déjà
    // « conversation » ; cet en-tête était le seul endroit où le vocabulaire du
    // code affleurait.
    texte($(".ct", col), `${g.count} conversation${g.count > 1 ? "s" : ""}`);
    // Seule la colonne de repli peut porter une proposition d'adoption : c'est
    // la seule où « ce projet n'est pas déclaré » est vrai.
    if (g.project === snap.fallback) majAdoption(col, snap);

    const hote = $(".cartes", col);
    const vuesC = new Set();
    (g.sessions || []).forEach((e, j) => {
      vuesC.add(e.sid);
      let c = hote.querySelector(`.carte[data-sid="${CSS.escape(e.sid)}"]`);
      if (!c) { c = creerCarte(e.sid); hote.append(c); }
      if (c.style.order !== String(j)) c.style.order = String(j);
      majCarte(c, e);
      c._e = e;
    });
    // Mort d'une carte : on la retire (le serveur a déjà tenu son fantôme).
    hote.querySelectorAll(".carte").forEach(c => {
      if (!vuesC.has(c.dataset.sid)) c.remove();
    });
    // Colonne vide : on dit que le projet est PRÊT, et comment l'ouvrir. Sans
    // ça, une colonne sans carte ressemble à une panne d'affichage. Le nom du
    // lanceur se déduit du nom de projet — c'est la règle qu'applique
    // _poser_lanceur() côté serveur (« claude-» + nom en minuscules).
    let vide = $(".vide-col", hote);
    if (!(g.sessions || []).length && g.project !== snap.fallback) {
      if (!vide) {
        vide = el("div", "vide-col");
        vide.append(document.createTextNode("Aucune conversation ouverte"),
                    el("code", null, "claude-" + g.project.toLowerCase()));
        hote.append(vide);
      }
      vide.style.order = "9999";
    } else if (vide) {
      vide.remove();
    }
    hote.style.display = "flex"; hote.style.flexDirection = "column";
  });
  board.querySelectorAll(".col").forEach(col => {
    if (!vues.has(col.dataset.projet)) col.remove();
  });
}

/* ──────────────────────────── actions ──────────────────────────────────── */
function sessionDe(sid) {
  for (const g of (dernier?.groupes || []))
    for (const e of (g.sessions || [])) if (e.sid === sid) return e;
  return null;
}

/* ── « RELUE » ────────────────────────────────────────────────────────────────
   Ce que le bouton fait, et surtout ce qu'il NE fait pas.

   Il ne touche pas à la conversation : aucun message n'est envoyé, aucun état
   de session n'est modifié. Il écrit un accusé de lecture dans seen.json, par
   la route /api/vu qui existait déjà — la même que le survol utilisait seul.
   Le contrat reste intact : les capteurs sont les seuls producteurs d'état, le
   navigateur ne dit que ce que L'UTILISATEUR a fait, jamais ce que la
   conversation fait.

   La clé est `cle_vu` = sid:état:horodatage. Donc l'accusé porte sur CE
   passage en « à relire » : si la conversation retravaille puis redemande une
   relecture, elle réapparaît dans le bandeau. On ne fait pas taire une
   conversation, on acquitte un tour.

   Le repeint est LOCAL et immédiat, alors que le flux confirmera dans la
   seconde. C'est le patron que marquerVu() suivait déjà (il posait `e.seen`
   avant la réponse) : un clic sans réponse visible passe pour un clic perdu.
   Aucun compteur n'est inventé pour autant — on affiche l'accusé qu'on vient
   d'écrire, pas un état de conversation deviné. */
function validerRelue(sid) {
  const e = sessionDe(sid);
  if (!e || e.seen) return;
  marquerVu(sid);

  const c = document.querySelector(`.carte[data-sid="${CSS.escape(sid)}"]`);
  if (c) majCarte(c, e);

  // Le bandeau d'attention est calculé par le serveur : il ignore encore
  // l'accusé. On retire l'entrée pour que la file d'attente se vide sous le
  // doigt, plutôt qu'une seconde plus tard.
  if (dernier && Array.isArray(dernier.attention)) {
    dernier.attention = dernier.attention.filter(a => a.sid !== sid);
    rendAttention(dernier);
  }
}

function marquerVu(sid) {
  const e = sessionDe(sid);
  if (e && !e.seen && e.cle_vu) { e.seen = true; poste("/api/vu", { cle_vu: e.cle_vu }); }
}

async function ouvrir(sid) {
  const e = sessionDe(sid);
  if (!e) return;
  marquerVu(sid);
  ouvrirFiche(sid);
}

async function allerAuPane(sid) {
  const e = sessionDe(sid);
  if (!e) return;
  if (e.pane == null) {
    // Silence = mensonge. On dit pourquoi le clic n'a pas focalisé.
    avis(`<b>Pane inconnu</b> pour ${e.ident} — aucun pane suivi pour ` +
         `<code>${e.repo || "ce dépôt"}</code>. Lance la fenêtre par ton raccourci ` +
         `de bureau, ou déclare son index dans <code>panes</code> de config.json. ` +
         `La conversation est marquée « vu ».`);
    return;
  }
  const r = await (await fetch("/api/focus", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ pane: e.pane })
  }).catch(() => ({ json:async()=>({ok:false,raison:"serveur injoignable"}) }))).json();
  if (!r.ok) avis(`<b>Focus impossible</b> — ${r.raison || "raison inconnue"}.`);
}

/* ──────────────────────── glisser-déposer ──────────────────────────────── */
/* Les places sont fixes : SEUL l'utilisateur peut les changer, jamais le système.
   Son geste est mémorisé côté serveur dans layout.json. */
let glisse = null;

function brancherGlisserColonne(col) {
  const hd = $(".col-hd", col);
  hd.addEventListener("dragstart", ev => {
    glisse = { type:"col", projet: col.dataset.projet };
    col.classList.add("glisse");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", col.dataset.projet);
  });
  hd.addEventListener("dragend", () => {
    col.classList.remove("glisse"); glisse = null;
    document.querySelectorAll(".cible").forEach(x => x.classList.remove("cible"));
  });
  col.addEventListener("dragover", ev => {
    if (glisse?.type !== "col" || glisse.projet === col.dataset.projet) return;
    ev.preventDefault(); col.classList.add("cible");
  });
  col.addEventListener("dragleave", () => col.classList.remove("cible"));
  col.addEventListener("drop", ev => {
    if (glisse?.type !== "col") return;
    ev.preventDefault(); col.classList.remove("cible");
    const vus = [...$("#board").querySelectorAll(".col")]
      .sort((a, b) => (+a.style.order || 0) - (+b.style.order || 0))
      .map(c => c.dataset.projet);
    const de = vus.indexOf(glisse.projet), vers = vus.indexOf(col.dataset.projet);
    if (de < 0 || vers < 0) return;
    vus.splice(vers, 0, vus.splice(de, 1)[0]);
    /* ── CE QUI EST MASQUÉ NE DOIT PAS ÊTRE EFFACÉ ────────────────────────────
       Cet ordre était construit à partir des seules colonnes PRÉSENTES DANS LE
       DOM, et il partait tel quel dans layout.json. Depuis que « avec
       conversation » peut en retirer, déplacer une colonne aurait supprimé du
       fichier tous les projets masqués : une préférence durable détruite par un
       filtre d'AFFICHAGE, côté serveur, sans un mot. C'est le seul endroit du
       diff où un état écran pouvait abîmer un état persistant.
       On réécrit donc l'ordre COMPLET : chaque emplacement occupé par un projet
       visible reçoit le suivant de la liste réordonnée, les projets masqués
       gardent le leur. Les visibles bougent entre eux, les masqués ne bougent
       pas — ce qui est exactement ce que l'utilisateur vient de demander. */
    const reste = vus.slice();
    const ordre = ordreProjets.length
      ? ordreProjets.map(p => vus.includes(p) ? reste.shift() : p)
      : vus;
    layout.projects = ordre;
    poste("/api/layout", { projects: ordre });
  });
}

function brancherGlisserCarte(c) {
  c.addEventListener("dragstart", ev => {
    glisse = { type:"carte", sid: c.dataset.sid, projet: c.closest(".col").dataset.projet };
    c.classList.add("glisse");
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", c.dataset.sid);
    ev.stopPropagation();
  });
  c.addEventListener("dragend", () => {
    c.classList.remove("glisse"); glisse = null;
    document.querySelectorAll(".cible").forEach(x => x.classList.remove("cible"));
  });
  c.addEventListener("dragover", ev => {
    // Une conversation appartient à son projet par son chemin de travail :
    // on réordonne DANS une colonne, on ne déménage pas entre colonnes.
    if (glisse?.type !== "carte") return;
    if (glisse.projet !== c.closest(".col").dataset.projet) return;
    if (glisse.sid === c.dataset.sid) return;
    ev.preventDefault(); ev.stopPropagation(); c.classList.add("cible");
  });
  c.addEventListener("dragleave", () => c.classList.remove("cible"));
  c.addEventListener("drop", ev => {
    if (glisse?.type !== "carte") return;
    ev.preventDefault(); ev.stopPropagation(); c.classList.remove("cible");
    const hote = c.closest(".cartes"), projet = c.closest(".col").dataset.projet;
    const ordre = [...hote.querySelectorAll(".carte")]
      .sort((a, b) => (+a.style.order || 0) - (+b.style.order || 0))
      .map(x => x.dataset.sid);
    const de = ordre.indexOf(glisse.sid), vers = ordre.indexOf(c.dataset.sid);
    if (de < 0 || vers < 0) return;
    ordre.splice(vers, 0, ordre.splice(de, 1)[0]);
    layout.cards[projet] = ordre;
    poste("/api/layout", { cards: layout.cards });
  });
}

/* ───────────────────────────── historique ─────────────────────────────── */
let histoData = null;

async function chargerHistorique() {
  const zone = $("#h-liste");
  zone.textContent = "";
  const attente = document.createElement("p");
  attente.className = "h-vide"; attente.textContent = "lecture des transcripts…";
  zone.append(attente);
  let d;
  try { d = await (await fetch("/api/historique")).json(); }
  catch { d = { groupes: [], total: 0, erreur: "serveur injoignable" }; }
  histoData = d;
  rendHistorique(d);
}

/* Rendu séparé du chargement : le filtre le rejoue sur les données déjà en
   mémoire, sans rappeler le serveur — la réponse est donc instantanée à la
   frappe, y compris sur les 45 conversations. */
function rendHistorique(d) {
  // Le pavé d'explication a été déplacé dans le bouton « ? » de la barre de
  // filtre. Il coûtait cinq lignes de hauteur en tête de liste ET il était
  // reconstruit à CHAQUE frappe dans le filtre, puisque rendHistorique() est
  // rejoué à l'input — le seul texte de l'écran qu'on relisait vingt fois par
  // recherche sans jamais en avoir besoin.
  const zone = $("#h-liste");
  zone.textContent = "";
  if (d.erreur && !(d.groupes || []).length) {
    const p = document.createElement("p");
    p.className = "h-vide"; p.textContent = "Historique indisponible : " + d.erreur;
    return zone.append(p);
  }
  if (!(d.groupes || []).length) {
    const p = document.createElement("p");
    p.className = "h-vide"; p.textContent = "Aucune conversation trouvée.";
    return zone.append(p);
  }
  const q = ($("#h-filtre")?.value || "").trim().toLowerCase();
  const garde = c => !q || [c.title, c.repo, c.branch, c.last_prompt,
                           ...(c.us || [])].some(
      v => v && String(v).toLowerCase().includes(q));
  let vus = 0, total = 0;
  for (const g of d.groupes) {
    const convs = (g.convs || []).filter(c => { total++; return garde(c) && ++vus; });
    if (!convs.length) continue;
    const sec = document.createElement("section");
    sec.className = "h-grp";
    if (g.accent) sec.style.setProperty("--acc", g.accent);
    const hd = document.createElement("header");
    hd.className = "h-hd";
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = g.project;
    const ct = document.createElement("span"); ct.className = "ct";
    ct.textContent = `${convs.length} conversation${convs.length > 1 ? "s" : ""}`;
    hd.append(nm, ct); sec.append(hd);
    for (const c of convs) {
      const r = document.createElement("div");
      r.className = "h-row" + (c.alive ? " vivant" : "");
      const f = (cl, t, ti) => {
        const s = document.createElement("span"); s.className = cl;
        s.textContent = t == null ? "" : t; if (ti) s.title = ti; return s;
      };
      const d2 = c.last_at ? new Date(c.last_at * 1000) : null;
      const us = document.createElement("span");
      us.className = "hus";
      for (const n of (c.us || []).slice(0, 3)) {
        const b = document.createElement("b");
        b.textContent = "#" + n;
        us.append(b);
      }
      if (c.archived) r.classList.add("arch");
      const act = document.createElement("span");
      act.className = "hact";
      if (c.restaurable) {
        const b = document.createElement("button");
        b.className = "reprendre";
        b.type = "button";
        b.textContent = "↩";
        b.title = "Sortir de la poubelle — la remettre dans les projets en cours";
        b.onclick = async ev => {
          ev.stopPropagation();
          await poste("/api/restaurer", { sid: c.sid });
          chargerHistorique();
        };
        act.append(b);
      }
      // Réintégrer pour de vrai : rouvrir la conversation dans son projet.
      // Disponible sur TOUTE ligne d'historique, pas seulement les archivées :
      // une conversation d'il y a trois semaines se reprend aussi.
      if (c.cwd) {
        const r = document.createElement("button");
        r.className = "reprendre rouvrir";
        r.type = "button";
        r.textContent = "↺";
        r.title = "Rouvrir dans " + c.cwd + " (nouvel onglet du terminal)";
        r.onclick = async ev => {
          ev.stopPropagation();
          r.disabled = true;
          let res;
          try {
            res = await (await fetch("/api/rouvrir", {
              method:"POST", headers:{"Content-Type":"application/json"},
              body: JSON.stringify({ sid: c.sid, cwd: c.cwd })
            })).json();
          } catch { res = { ok:false, message:"serveur injoignable" }; }
          r.disabled = false;
          avis(res.ok
            ? `<b>Rouverte</b> — ${res.message}. Elle apparaîtra dans la board dès ` +
              `que ses capteurs répondront.`
            : `<b>Impossible de rouvrir</b> — ${res.message}.`);
        };
        act.append(r);
      }
      r.append(
        f("hdate", d2 ? d2.toLocaleDateString("fr-FR", { day:"2-digit", month:"2-digit" }) : "—",
          d2 ? d2.toLocaleString("fr-FR") : ""),
        f("ht", c.title, [c.title, c.repo, c.branch].filter(Boolean).join(" · ")),
        f("hp", c.last_prompt || "", c.last_prompt || ""),
        us,
        f("hm", c.messages != null ? c.messages + " msg" : ""),
        act
      );
      sec.append(r);
    }
    zone.append(sec);
  }
  const cpt = $("#h-compte");
  if (cpt) texte(cpt, q ? `${vus} sur ${total}` : `${total} conversations`);
  if (q && !vus) {
    const p2 = el("p", "h-vide", `Aucune conversation ne correspond à « ${q} ».`);
    zone.append(p2);
  }
}

/* UNE SEULE table pour les onglets : panneau ET bouton. Le bug qu'elle corrige
   valait la peine d'être structurellement empêché — `ongler()` posait
   `aria-selected` en itérant sur une liste de trois paires écrite à la main, à
   côté de cette table. L'onglet Chantier a été ajouté ici et oublié là : son
   panneau s'affichait, mais aucun onglet ne paraissait sélectionné, puisque les
   trois autres passaient tous à `false`. Un quatrième oubli est maintenant
   impossible : la table est la seule source, et le câblage des clics en découle. */
const PANNEAUX = {
  board:    { panneau:"#board",    onglet:"#ong-board" },
  pr:       { panneau:"#prs",      onglet:"#ong-pr" },
  chantier: { panneau:"#chantier", onglet:"#ong-chantier" },
  histo:    { panneau:"#histo",    onglet:"#ong-histo" },
};

/* ═════════════════════════ Pull Requests ═════════════════════════════════
   Les données coûtent 12 appels réseau : le serveur rend son cache tout de
   suite et rafraîchit derrière. On affiche donc toujours l'âge de ce qu'on
   montre, et on se re-synchronise tant qu'un rafraîchissement tourne. */
// Les pigments des PR suivent EXACTEMENT la doctrine des cartes depuis Ardoise,
// pour qu'une couleur veuille dire la meme chose dans les deux onglets :
//   --red   = c'est arrete, il faut TON ACTE      -> conflit, a corriger
//   --amber = ca attend un regard ou une relance  -> a relire, dort
//   --green = c'est bon                           -> prete
//   --t3    = rien a faire pour l'instant         -> en attente, brouillon
// --sky ne parait plus ici : il est l'accent du chrome (focus, liens), pas un etat.
const PR_ETAT = {
  conflit:     { txt:"CONFLIT",     couleur:"var(--red)" },
  a_corriger:  { txt:"À CORRIGER",  couleur:"var(--red)" },
  a_relire:    { txt:"À RELIRE",    couleur:"var(--amber)" },
  dort:        { txt:"DORT",        couleur:"var(--amber)" },
  prete:       { txt:"PRÊTE",       couleur:"var(--green)" },
  en_attente:  { txt:"EN ATTENTE",  couleur:"var(--grey)" },
  brouillon:   { txt:"BROUILLON",   couleur:"var(--grey)" },
};
let prTimer = null;

function majBadgePR(n, d) {
  const b = $("#pr-badge");
  if (!b) return;
  // `hidden` retirerait la pastille du flux et ferait rétrécir l'onglet : on
  // masque sans libérer la place, pour que la barre ne bouge jamais.
  b.hidden = false;
  if (!(n > 0)) { classe(b, "vide", true); texte(b, "0"); return; }
  classe(b, "vide", false);
  texte(b, String(n));
  // Le détail dans l'infobulle : « 5 à traiter » ne dit pas de quoi il s'agit.
  const par = {};
  for (const g of (d && d.groupes) || [])
    for (const p of g.prs || [])
      if (["conflit","a_corriger","a_relire","dort"].includes(p.etat))
        par[p.etat] = (par[p.etat] || 0) + 1;
  const noms = { conflit:"en conflit", a_corriger:"à corriger",
                 a_relire:"à relire", dort:"qui dorment" };
  const detail = Object.entries(par).map(([k, v]) => `${v} ${noms[k]}`).join(", ");
  attr(b, "title", n + " PR à traiter" + (detail ? " — " + detail : ""));
}

async function chargerPR(force) {
  const zone = $("#prs");
  if (!zone.dataset.charge) {
    zone.textContent = "";
    const a = document.createElement("p");
    a.className = "pr-vide"; a.textContent = "interrogation d'Azure DevOps…";
    zone.append(a);
  }
  let d;
  try { d = await (await fetch("/api/pr" + (force ? "?force=1" : ""))).json(); }
  catch { d = { groupes: [], total: 0, a_traiter: 0, degrade: "serveur injoignable" }; }
  zone.dataset.charge = "1";
  rendPR(d);
  // Un rafraîchissement tourne : on revient le chercher, sans marteler.
  clearTimeout(prTimer);
  if (d.rafraichit && ongletActif === "pr") prTimer = setTimeout(() => chargerPR(false), 3000);
}

function rendPR(d) {
  const zone = $("#prs");
  zone.textContent = "";
  majBadgePR(d.a_traiter || 0, d);

  const tete = document.createElement("div");
  tete.className = "pr-tete";
  const mk = (cl, txt) => { const e = document.createElement("span"); e.className = cl;
                            e.textContent = txt; return e; };
  tete.append(mk("gros", String(d.total ?? 0)), mk("lbl", (d.total === 1 ? "PR ouverte" : "PR ouvertes")));
  const agir = mk("agir" + ((d.a_traiter || 0) === 0 ? " zero" : ""), String(d.a_traiter ?? 0));
  tete.append(agir, mk("lbl", "à traiter"));

  const droite = document.createElement("span");
  droite.className = "droite";
  const frais = document.createElement("span");
  frais.className = "pr-frais";
  if (d.rafraichit) frais.textContent = "rafraîchissement en cours…";
  else if (d.age_s == null) frais.textContent = "";
  else {
    frais.textContent = "données d'il y a " + (d.age_s < 60 ? d.age_s + " s"
                        : Math.floor(d.age_s / 60) + " min");
    if (d.age_s > 600) frais.classList.add("vieux");
  }
  const btn = document.createElement("button");
  btn.className = "pr-relire"; btn.type = "button";
  btn.textContent = "rafraîchir"; btn.disabled = !!d.rafraichit;
  btn.onclick = () => { btn.disabled = true; chargerPR(true); };
  droite.append(frais, btn);
  tete.append(droite);
  zone.append(tete);

  if (d.degrade) {
    const w = document.createElement("p");
    w.className = "pr-degrade"; w.textContent = d.degrade;
    zone.append(w);
  }
  if (!(d.groupes || []).length) {
    if (!d.degrade) {
      const p = document.createElement("p");
      p.className = "pr-vide"; p.textContent = "Aucune pull request ouverte.";
      zone.append(p);
    }
    return;
  }

  for (const g of d.groupes) {
    const sec = document.createElement("section");
    sec.className = "pr-grp";
    if (g.accent) sec.style.setProperty("--acc", g.accent);
    const hd = document.createElement("header");
    hd.className = "pr-hd";
    hd.append(mk("nm", g.project),
              mk("ct", g.count + (g.count > 1 ? " PR" : " PR")));
    sec.append(hd);

    for (const pr of (g.prs || [])) {
      const e = PR_ETAT[pr.etat] || PR_ETAT.en_attente;
      const r = document.createElement("div");
      r.className = "pr-row" + (pr.draft ? " brouillon" : "");
      r.style.setProperty("--pr-edge", e.couleur);

      r.append(mk("pr-etat", e.txt), mk("pr-id", "#" + pr.id));

      const t = document.createElement("span");
      t.className = "pr-titre"; t.textContent = pr.titre || "";
      t.title = pr.titre || "";
      if (pr.us) { const u = mk("pr-us", "#" + pr.us); t.append(u); }
      r.append(t);

      const br = document.createElement("span");
      br.className = "pr-br";
      br.title = (pr.src || "") + " → " + (pr.dst || "");
      const em = document.createElement("em"); em.textContent = pr.src || "?";
      br.append(em, document.createTextNode(" → " + (pr.dst || "?")));
      r.append(br);

      const v = pr.votes || {};
      const votes = document.createElement("span");
      votes.className = "pr-votes";
      const paire = (cl, glyphe, n, titre) => {
        if (!n) return;
        const s2 = document.createElement("span");
        s2.className = cl; s2.title = titre;
        s2.textContent = glyphe + n;
        votes.append(s2);
      };
      paire("ok",  "✔", (v.approuve || 0) + (v.suggestions || 0), "approbations");
      paire("att", "↩", v.attente_auteur || 0, "en attente de l'auteur");
      paire("no",  "✖", v.rejete || 0, "rejets");
      paire("",    "·", v.sans_avis || 0, "sans avis");
      r.append(votes);

      const com = document.createElement("span");
      com.className = "pr-com";
      if (pr.commentaires) {
        const b = document.createElement("b"); b.textContent = pr.commentaires;
        com.append(b, document.createTextNode(" com."));
        if (pr.non_resolus) {
          const o = document.createElement("span");
          o.className = "ouvert"; o.textContent = " " + pr.non_resolus + "↯";
          o.title = pr.non_resolus + " fil(s) non résolu(s)";
          com.append(o);
        }
      } else com.textContent = "—";
      r.append(com);

      const age = mk("pr-age", pr.age_j != null ? pr.age_j + "j" : "");
      if ((pr.age_j || 0) >= 7) age.classList.add("vieille");
      r.append(age);

      const a = document.createElement("a");
      a.className = "pr-lien"; a.href = pr.url || "#"; a.target = "_blank";
      a.rel = "noopener"; a.textContent = "↗"; a.title = "Ouvrir dans Azure DevOps";
      r.append(a);

      sec.append(r);
    }
    zone.append(sec);
  }
  const cpt = $("#h-compte");
  if (cpt) texte(cpt, q ? `${vus} sur ${total}` : `${total} conversations`);
  if (q && !vus) {
    const p2 = el("p", "h-vide", `Aucune conversation ne correspond à « ${q} ».`);
    zone.append(p2);
  }
}

/* ═══════════════════════════ Chantier ═══════════════════════════════════
   L'inventaire des arbres de travail — 29 sur ce poste, dont 1 seul porte une
   conversation. Les trois autres onglets regardent le travail par la
   conversation ou par la PR ; celui-ci le regarde par la BRANCHE, qui est la
   seule clé commune aux trois.

   Deux règles héritées du reste du produit :
   · aucun libellé d'état n'est écrit ici. Le serveur envoie `libelles`,
     `glyphes` et `ordre` avec les données, exactement comme l'objet SESSION
     porte son propre glyphe. Un vocabulaire dupliqué finit par diverger.
   · les données sont locales et rapides (55 ms pour 29 arbres, cache 30 s),
     donc pas de rafraîchissement de fond compliqué : on charge à l'ouverture
     de l'onglet, et le bouton « rafraîchir » force le balayage. */
let chData = null;
const chReplie = new Set();      // dépôts dont la RÉSERVE est repliée
const chFerme = new Set();       // dépôts entièrement REFERMÉS par l'utilisateur
const chVus = new Set();         // dépôts déjà vus : le repli PAR DÉFAUT ne
                                 // s'applique qu'au premier rendu, sinon chaque
                                 // rafraîchissement annulerait le geste de
                                 // l'utilisateur et refermerait ce qu'il ouvre.

/* POURQUOI CES TROIS ENSEMBLES VIVENT DANS LA PAGE ET PAS AILLEURS.
   localStorage ne sert qu'au thème, et layout.json (côté serveur) ne mémorise que
   l'ordre voulu des colonnes et des cartes — deux préférences durables et
   coûteuses à refaire. Un pli est une aide de lecture du moment. Le persister
   créerait le pire cas de cet écran : un dépôt refermé la semaine dernière qui
   contient aujourd'hui du travail non commité frais, et qu'on ne verrait pas au
   chargement. Chaque ouverture de page montre donc l'état complet au moins une
   fois — sauf la réserve, dont le repli repose sur un état FIXE du contrat de
   données (`etat === "reserve"`) et non sur un jugement du moment.

   `avecConv` — l'interrupteur du chrome, déclaré en tête de fichier — SUIT LA
   MÊME RÈGLE, et pour la même raison, en pire : il ne plie pas un dépôt, il
   retire un projet entier de deux écrans. Sa doctrine complète est là-haut,
   avec lui ; elle n'a pas sa place ici depuis qu'il ne s'applique plus qu'à cet
   onglet. */

/* CE QUI EMPÊCHE UN PROJET DE PARTIR EN SILENCE : IL NE PART PAS.
   Première version de ce filtre : on masquait un projet sans conversation, puis
   on AVERTISSAIT en ambre s'il contenait du travail en péril. Un avertissement
   est un pis-aller — il demande d'être lu, d'être compris, et d'être suivi d'un
   geste (rallumer le filtre) pour retrouver ce qu'on vient de cacher. Il ne
   protégeait rien : il documentait la perte.

   Désormais l'exception est STRUCTURELLE. Un projet qui porte du travail en
   péril n'est jamais masqué, point. Le prédicat complet est plus haut, dans
   `rendChantier` :

     masquer  ⟺  filtre allumé  ET  conversations === 0  ET  aucun arbre en péril

   Ce que ça change, et c'est le fond du sujet : la doctrine de repli juste
   au-dessus interdit de faire disparaître au chargement « un dépôt qui contient
   du travail non commité frais ». Tant que le filtre pouvait masquer un tel
   projet, il reproduisait exactement le pire cas que cette doctrine nomme, en
   pire (un projet entier, pas un pli), et se contentait de l'avouer à côté.
   Il ne le peut PLUS : le filtre est devenu incapable de produire ce cas. Une
   propriété est plus forte qu'un avertissement — et elle ne coûte rien à lire.

   Conséquence assumée : le compteur ambre « N masqués demandent un geste » a été
   supprimé, en JS comme en CSS. Il compterait toujours zéro par construction, et
   un jeton qui ne s'allume jamais est du bruit dans une barre qui déborde déjà.
   Le compteur « N projets masqués », lui, RESTE : il dit ce que l'écran ne
   montre pas, et ça, aucune propriété structurelle ne le rend inutile.

   CE QUI COMPTE COMME « EN PÉRIL ».
   Trois faits du contrat de données, lus dans docs/SCHEMA.md et dans
   `chantier._liberation()` — aucun n'est deviné :

     `fichiers`       fichiers modifiés ET non suivis, non commités. C'est le
                      seul travail qui ne survivrait pas au retrait de l'arbre,
                      et c'est nommément le pire cas que la doctrine ci-dessus
                      interdit de faire disparaître au chargement.
     `ahead`          commits qui n'existent que dans ce répertoire tant qu'on
                      ne les a pas poussés. Même famille de perte que
                      `fichiers` : `_liberation()` les cite tous les deux comme
                      LES deux choses qui ne se retrouvent nulle part ailleurs.
     `alerte_niveau`  "agir" ou "bloque" — les contradictions, exactement ce que
                      compte `a_traiter` et ce que le ⚠ d'un dépôt replié refuse
                      déjà de taire. Le niveau "info" est EXCLU : c'est du
                      rangement possible, pas un geste dû.

   `liberable` n'entre pas dans le test, et c'est volontaire : il dit le
   contraire de ce qu'on cherche. Un arbre libérable est précisément celui qu'on
   peut perdre sans rien perdre — le masquer ne coûte rien. */
function chArbreEnPeril(a) {
  return !!(a.fichiers || a.ahead
            || a.alerte_niveau === "agir" || a.alerte_niveau === "bloque");
}

/* LES TEXTES DE L'INTERRUPTEUR ONT SUIVI L'INTERRUPTEUR. Cet onglet rédigeait
   son infobulle, sa description liée et son annonce ; les trois vivent
   maintenant en tête de fichier, avec `texteFiltre` et l'aveu du chrome, parce
   qu'elles doivent parler des DEUX écrans. Ce que cet onglet garde, c'est ce
   qu'il est seul à savoir : quels projets il a masqués, et lesquels contenaient
   du travail qu'on peut perdre. Il le dépose dans `bilanFiltre.chantier`. */

function majBadgeChantier(n) {
  const b = $("#ch-badge");
  if (!b) return;
  b.hidden = false;
  if (!(n > 0)) { classe(b, "vide", true); texte(b, "0"); return; }
  classe(b, "vide", false);
  texte(b, String(n));
  // La pastille ne compte QUE les contradictions — une branche en revue dont
  // la PR ne contient pas le dernier commit, une PR bloquée par une autre.
  // « non commité » est un état normal ici : le compter allumerait la pastille
  // en permanence, et une pastille permanente ne veut plus rien dire.
  attr(b, "title", n + (n > 1 ? " arbres incohérents" : " arbre incohérent")
       + " — la ligne paraît actionnable mais ne l'est pas, ou la PR ne montre "
       + "pas ton dernier état");
}

let chEnVol = false;             // un relevé est en route : voir majVeilleChantier

/* ── CE QU'UN RE-RENDU N'A PAS LE DROIT D'EMPORTER ────────────────────────────
   `rendChantier` vide `#chantier` et reconstruit tout. Deux choses n'y survivent
   pas, et ce sont les deux positions de l'UTILISATEUR, pas des données : le
   défilement, et le FOCUS.

   Le focus est le plus grave — WCAG 2.4.3. Mesuré en Chromium instrumenté avant
   correctif : `focus avant re-rendu auto : ch-relire` → `focus après : BODY`.
   Tant qu'un relevé ne partait que d'un clic, on pouvait rustiner au cas par
   cas. Depuis que le flux déclenche un relevé SANS AUCUN GESTE (voir
   `majVeilleChantier`), la rustine par chemin ne tient plus : un utilisateur au
   clavier posé sur un chevron de dépôt perd sa place parce qu'une conversation
   a démarré ailleurs sur la machine, dans un projet qu'il ne regardait même pas.

   L'INTERRUPTEUR N'EST PLUS DANS CETTE LISTE, et c'est un gain : depuis qu'il
   vit dans le chrome, il n'est plus reconstruit, donc plus jamais perdu. Ce qui
   reste à rattraper, ce sont les contrôles que CE PANNEAU fabrique.

   La restauration porte donc sur le RENDU lui-même, et sur l'élément focalisé
   quel qu'il soit : on note son `id` s'il est dans la zone, on refocalise après.
   C'est ce qui oblige tout contrôle reconstruit à porter un `id` stable —
   `ch-relire`, `ch-aide`, et les deux chevrons de chaque dépôt
   (`…-plier`, `…-reserve`). Un contrôle sans `id` n'est pas rattrapable : c'est
   le prix de cette approche, et il est écrit ici pour qu'on s'en souvienne en
   ajoutant le prochain bouton.

   `preventScroll` n'est pas un détail : sans lui, le navigateur ramènerait la
   zone sur l'élément refocalisé et défferait la ligne du dessus. */
function chGarderLaPlace(rendu) {
  const zone = $("#chantier");
  const actif = document.activeElement;
  const idFocus = actif && actif !== document.body && zone.contains(actif)
                ? (actif.id || "") : "";
  // Le navigateur borne la valeur si la liste a raccourci.
  const defile = zone.scrollTop || 0;
  rendu();
  zone.scrollTop = defile;
  if (idFocus) document.getElementById(idFocus)?.focus({ preventScroll: true });
}

async function chargerChantier(force) {
  const zone = $("#chantier");
  if (!zone.dataset.charge) {
    zone.textContent = "";
    zone.append(el("p", "ch-vide", "relevé des arbres de travail…"));
  }
  let d;
  chEnVol = true;
  try { d = await (await fetch("/api/chantier" + (force ? "?force=1" : ""))).json(); }
  catch { d = { groupes: [], total: 0, compteurs: {}, degrade: "serveur injoignable" }; }
  finally { chEnVol = false; }
  chData = d;
  chGarderLaPlace(() => rendChantier(d));
  // APRÈS le rendu, et c'est voulu : écrire dans `dataset` invalide le style de
  // la zone, et c'est la seule invalidation de ce chemin. Le poser avant ferait
  // de la lecture de `scrollTop`, deux lignes plus loin, un calcul de mise en
  // page forcé — pour rien, puisque rien ici ne lit `charge` entre-temps.
  zone.dataset.charge = "1";
}

/* ── LE RÉVEIL DE L'ONGLET QUAND UNE CONVERSATION NAÎT OU MEURT ───────────────
   L'onglet ne demandait un relevé qu'à son ouverture et sur clic. Tant qu'il
   listait tout, ça se défendait : le contenu bougeait peu. Avec le filtre, ce
   silence devient un mensonge — démarrer une conversation dans un projet masqué
   ne le ferait pas réapparaître, et la quitter n'en ferait pas disparaître un
   autre, tant qu'on n'aurait pas pensé à cliquer « rafraîchir ». Un filtre qui
   ment sur ce qu'il filtre est pire que pas de filtre du tout.

   POURQUOI CE DÉCLENCHEUR-LÀ, ET PAS UN AUTRE.
   · Pas un re-rendu par message du flux. Il en arrive un par seconde ; on
     reconstruirait .ch-tete et toutes les sections 86 400 fois par jour pour un
     écran qui change deux ou trois fois, et l'utilisateur perdrait le focus au
     milieu de chaque geste.
   · Pas un minuteur non plus. `sondeChantier` en tient déjà un, toutes les
     3 minutes, et il ne sert QUE la pastille : lui faire re-rendre le panneau
     ferait bouger l'écran sous les mains de l'utilisateur sans qu'il ait rien
     demandé, ce que la doctrine de cet onglet interdit — et trois minutes de
     retard sur un projet qui réapparaît, c'est trois minutes de mensonge.
   · Le bon déclencheur est l'ÉVÉNEMENT lui-même : la RÉPARTITION des
     conversations vivantes PAR PROJET a changé. On la lit dans l'instantané
     qu'`appliquer` reçoit déjà, sans rien demander à personne : même endroit et
     même raison que le réveil de `sondeChantier` juste au-dessus, qui attend
     lui aussi le flux plutôt que d'interroger le serveur à l'aveugle.

   LA SIGNATURE RETIENT LE COUPLE `sid` + `project`, trié. Le `sid` seul ne
   suffit pas, et c'est un vrai cas, pas une hypothèse : `conversations` est
   compté PAR RACINE DE PROJET (docs/SCHEMA.md), donc une conversation qui change
   de `cwd` d'un projet vers un autre déplace DEUX compteurs — celui qu'elle
   quitte peut tomber à zéro, celui qu'elle rejoint peut quitter zéro — sans
   qu'aucun `sid` apparaisse ni disparaisse. Le filtre mentirait alors jusqu'au
   prochain démarrage ou arrêt ailleurs dans la flotte, ce qui peut être une
   demi-journée. L'objet SESSION porte `project`, résolu par la même règle que
   `conversations` (préfixe de `projects[].root`) : c'est exactement la donnée
   qu'il faut, et elle est déjà là.

   Ce que la signature IGNORE, en revanche : l'état, le contexte, le titre, le
   coût — ils changent à chaque seconde et ne déplacent aucun projet. C'est toute
   la différence entre « quelques relevés par jour » et « un par seconde ». Le
   tri rend la comparaison indifférente à l'ordre des colonnes du board, qui n'a
   rien à voir avec le chantier. */
let chSignConv = null;           // conversations vivantes par projet, au dernier relevé
let chPerime = false;            // le relevé affiché a été calculé avec une autre liste

function majVeilleChantier(snap) {
  const sign = (snap.groupes || [])
                 .flatMap(g => (g.sessions || [])
                   .map(e => e.sid + "@" + (e.project || g.project || "")))
                 .sort().join("|");
  if (chSignConv === null) { chSignConv = sign; return; }  // première référence
  if (sign === chSignConv) return;
  // Un relevé est déjà en route : on ne prend PAS la nouvelle référence, sinon
  // le changement serait avalé. L'instantané suivant, une seconde plus tard,
  // repassera ici et le rattrapera.
  if (chEnVol) return;
  chSignConv = sign;
  if (ongletActif === "chantier" && $("#chantier").dataset.charge) {
    /* POURQUOI `force`, ALORS QU'UN RELEVÉ EN CACHE SUFFIRAIT AU FILTRE.
       `conversations` et `conversations_inconnues` sont recalculés à CHAQUE
       appel, y compris quand la réponse sort du cache de 30 s (docs/SCHEMA.md) :
       un simple GET rendrait donc déjà le bon `conversations`, et le filtre
       serait juste pour zéro balayage git. Ce qui ne serait pas juste, c'est le
       RESTE de la ligne. L'état d'un arbre, lui, vient du relevé mis en cache :
       le projet réapparaîtrait avec ses arbres étiquetés « réserve » alors
       qu'une conversation y travaille — exactement ce que cet écran s'interdit
       ailleurs, au point d'avoir un drapeau (`conversations_inconnues`) pour ne
       jamais le faire en silence.

       CE QUE COÛTE CE `force`, MESURÉ SUR CE POSTE et non estimé : 155 à 164 ms
       de mur, 81 processus git lancés, 816 ms de CPU cumulés par relevé forcé.
       Ce n'est pas rien — c'est trois fois le chiffre qu'annonçait la première
       rédaction de ce commentaire. Ça reste le bon choix parce que le
       déclencheur est RARE (quelques fois par jour, uniquement l'onglet ouvert,
       et jamais deux fois pour le même changement, cf. `chEnVol`) : réapparaître
       à moitié faux pendant 30 s coûte plus cher à qui lit l'écran. Le nombre de
       processus, lui, est un sujet SERVEUR (dédoublonnage du balayage) et pas un
       sujet de cette fonction. */
    chargerChantier(true);
  } else {
    // Onglet fermé : rien à re-rendre, mais le prochain coup d'œil ne doit pas
    // tomber sur un relevé d'avant le changement.
    chPerime = true;
  }
}

// Ce que le dernier rendu de CET ONGLET a retiré, et ce que ce retrait emporte.
// Depuis que l'aveu vit dans le chrome, ce bilan-là ne sert plus qu'à l'état vide
// du panneau — le seul texte que cet onglet écrit encore lui-même sur le sujet.
// Ce qui part vers le chrome est `bilanFiltre.chantier`, calculé au même endroit.
let chDernierMasque = { projets: 0, arbres: 0, perils: 0, nomsPeril: [],
                        inconnus: 0 };

function rendChantier(d) {
  const zone = $("#chantier");
  zone.textContent = "";
  // La pastille d'onglet compte TOUT le chantier, filtre allumé ou non : elle
  // doit rester juste sans même ouvrir l'onglet (cf. sondeChantier), et un
  // filtre d'AFFICHAGE n'a pas le droit d'éteindre une alerte qui existe.
  majBadgeChantier(d.a_traiter || 0);

  const ordre = d.ordre || [];
  const libelles = d.libelles || {};
  const compteurs = d.compteurs || {};

  /* ── TRIER AVANT DE COMPTER, et non l'inverse ─────────────────────────────
     Ce dépliage des dépôts vivait dans la boucle de rendu, plus bas. Il remonte
     ici parce que l'en-tête doit compter EXACTEMENT ce que la liste affichera :
     c'était déjà la règle pour les lignes socle — « un tableau qui cache des
     lignes sans le dire ment sur ce qu'il montre » — et le filtre de
     conversations ne peut pas y échapper. */
  const projets = [];
  for (const g of (d.groupes || [])) {
    // Un dépôt qui n'avait qu'une ligne socle disparaît de l'onglet : un en-tête
    // sans contenu ne renseigne sur rien.
    const depots = (g.depots || [])
      .map(dep => ({ repo: dep.repo,
                     arbres: (dep.arbres || []).filter(a => a.etat !== "socle") }))
      .filter(dep => dep.arbres.length)
      .map(dep => ({ ...dep, count: dep.arbres.length }));
    if (!depots.length) continue;
    const arbres = depots.flatMap(dep => dep.arbres);
    /* LE PRÉDICAT DE MASQUAGE — `avecConv` ET `g.conversations === 0`.
       `avecConv` est l'état GLOBAL du chrome (voir sa doctrine en tête de
       fichier) : cet onglet ne possède plus son interrupteur, il obéit à celui
       de la barre du haut, qui gouverne aussi l'écran d'accueil.

       `g.conversations === 0`. La comparaison est STRICTE, jamais
           `!g.conversations`. Le champ vaut `null` quand l'instantané des
           conversations était indisponible au moment du relevé — le serveur dit
           alors « je ne sais pas », et non « aucune » — et `undefined` face à un
           serveur qui ne publie pas encore la clé. Les deux sont des IGNORANCES,
           et on ne fait pas disparaître un projet sur une ignorance. Même règle
           que `conversations_inconnues`, qui refuse déjà d'étiqueter « réserve »
           un arbre dont on ne sait pas s'il est occupé.

           LE CHAMP S'APPELLE `conversations`, PAS `convs`, ET C'EST UN PIÈGE
           ÉVITÉ DE JUSTESSE. Ce fichier lit déjà `g.convs` en un autre endroit
           (rendu de l'historique) où c'est une LISTE venue de /api/historique,
           sur une collection qui s'appelle elle aussi `groupes`. Deux types
           derrière un même nom, dans un même fichier : `0 || []` coerce en
           silence, et la première lecture inversée n'aurait jamais levé
           d'erreur. /api/chantier publie donc `conversations`, en toutes
           lettres. Un serveur antérieur au renommage rend `undefined` ici,
           c'est-à-dire une ignorance, c'est-à-dire zéro projet masqué : la
           dégradation va dans le sens sûr.

       ET RIEN D'AUTRE. Il y avait une seconde condition — « aucun arbre du
       groupe n'est en péril » — qui exemptait de masquage tout projet portant
       du travail non commité. Elle a été RETIRÉE sur demande explicite de
       l'utilisateur, redemandée après l'avoir vue à l'œuvre : elle gardait à
       l'écran un projet sans aucune conversation, ce qui est exactement ce que
       cet interrupteur existe pour retirer. Une protection qu'on n'a pas
       demandée et qui rend le contrôle infidèle à son libellé n'est pas une
       protection, c'est une surprise.

       CE QUE CE RETRAIT COÛTE, ET COMMENT IL EST PAYÉ. Le filtre redevient
       capable de masquer un dépôt contenant du travail non commité — le pire
       cas que nomme la doctrine de `chReplie`. `chArbreEnPeril` n'est donc pas
       supprimé : il ne décide plus, il DÉNONCE. Le compteur « N projets
       masqués » porte désormais, en ambre, le nombre de projets masqués qui
       contiennent du travail qu'on peut perdre, et les nomme. Rien ne disparaît
       en silence : ce qui disparaît, l'écran le dit. */
    const inconnu = typeof g.conversations !== "number";
    const masque = avecConv && g.conversations === 0;
    projets.push({ g, depots, arbres, masque, inconnu,
                   peril: masque && arbres.some(chArbreEnPeril) });
  }
  const montres = projets.filter(p => !p.masque);
  const caches = projets.filter(p => p.masque);
  const arbresMontres = montres.flatMap(p => p.arbres);
  const arbresCaches = caches.flatMap(p => p.arbres);
  const perils = caches.filter(p => p.peril);
  const nomsCaches = [...new Set(caches.map(p => p.g.project))];
  chDernierMasque = { projets: caches.length, arbres: arbresCaches.length,
                      perils: perils.length,
                      nomsPeril: perils.map(p => p.g.project),
                      inconnus: projets.filter(p => p.inconnu).length };
  // Ce que cet onglet vient de retirer part vers l'aveu du chrome, seul endroit
  // de la page où le masquage se dit désormais. `inconnu` n'est vrai que si
  // AUCUN projet n'a de compte connu : un seul compte connu suffit à ce que le
  // filtre ait un sens, et les inconnus restent affichés de toute façon.
  bilanFiltre.chantier = {
    projets: caches.length, noms: nomsCaches,
    perils: perils.length, nomsPeril: perils.map(p => p.g.project),
    inconnu: projets.length > 0 && projets.every(p => p.inconnu),
  };
  majAveuFiltre();

  // `main` et `develop` ne sont pas des chantiers : on n'y travaille pas, on n'y
  // a rien à libérer, et leur ligne occupait un tiers des dépôts PROJET_A. Elles
  // sont masquées — mais COMPTÉES à part dans l'en-tête : un tableau qui cache
  // des lignes sans le dire ment sur ce qu'il montre. Ce compteur-là vient bien
  // du serveur : les lignes socle ne sont dans aucun des tableaux ci-dessus.
  const socles = compteurs.socle || 0;
  /* LES NOMBRES DE L'EN-TÊTE SONT RECOMPTÉS SUR CE QUI EST LISTÉ.
     Une version antérieure les obtenait en SOUSTRAYANT les arbres masqués des
     totaux du serveur, au motif qu'« une soustraction ne peut pas diverger de ce
     que le serveur publie ». L'argument est exact et il est à l'envers : diverger
     du serveur est précisément ce qu'on VEUT ici. L'en-tête ne décrit pas le
     chantier, il décrit LA LISTE — et la liste applique déjà un filtre que le
     serveur ignore (`a.etat !== "socle"`, en dur quelques lignes plus haut). La
     soustraction n'était juste que tant que ce filtre-là restait le seul ; le
     jour où un second état cesse d'être listé, elle annoncerait un compteur pour
     un état sans une seule ligne en dessous. Recompter supprime l'hypothèse. */
  const parEtat = {};
  for (const a of arbresMontres) parEtat[a.etat] = (parEtat[a.etat] || 0) + 1;
  const visible = arbresMontres.length;

  // ── en-tête : le total, puis un compteur par état
  const tete = el("div", "ch-tete");
  tete.append(el("span", "gros", String(visible)),
              el("span", "lbl", (visible === 1 ? "arbre de travail" : "arbres de travail")));
  for (const e of ordre) {
    if (e === "socle" || !parEtat[e]) continue;
    const c = el("span", "ch-cpt " + e);
    c.append(el("b", null, String(parEtat[e])),
             el("span", null, (libelles[e] || e).toLowerCase()));
    tete.append(c);
  }
  // « Quand puis-je supprimer un worktree ? » — deux nombres, parce que ce sont
  // deux gestes différents : retirer l'arbre, et retirer l'arbre ET sa branche.
  const liberables = arbresMontres.filter(a => a.liberable).length;
  const terminees = arbresMontres.filter(a => a.terminee).length;
  if (liberables > 0) {
    const c = el("span", "ch-cpt lib");
    c.append(el("b", null, String(liberables)),
             el("span", null, liberables > 1 ? "libérables" : "libérable"));
    attr(c, "title", "arbres de travail retirables sans rien perdre : rien de non "
      + "commité, rien à pousser, aucune conversation dedans, aucune PR ouverte");
    tete.append(c);
  }
  if (terminees > 0) {
    const c = el("span", "ch-cpt fini");
    c.append(el("b", null, String(terminees)),
             el("span", null, terminees > 1 ? "branches terminées" : "branche terminée"));
    attr(c, "title", "en plus déjà dans la base : la branche peut partir avec l'arbre");
    tete.append(c);
  }

  if (socles) {
    const c = el("span", "ch-cpt masque");
    c.append(el("b", null, String(socles)),
             el("span", null, socles > 1 ? "socles masqués" : "socle masqué"));
    attr(c, "title", "les branches main / master / develop ne sont pas des chantiers : "
      + "rien à y libérer, rien à y suivre. Elles ne sont pas listées.");
    tete.append(c);
  }

  /* ── L'AVEU A DÉMÉNAGÉ DANS LE CHROME, AVEC L'INTERRUPTEUR ────────────────
     Cette barre a porté le jeton `#ch-masque-conv` — « N projets masqués ⚠N » —
     tant que l'interrupteur vivait ici. Il gouverne maintenant DEUX écrans
     depuis le bandeau du haut, et son aveu l'a suivi : le laisser ici en aurait
     fait un second nombre à tenir d'accord avec celui du chrome, sur un écran
     où les deux auraient été visibles ensemble. Rien n'est perdu — le compte,
     les noms, le ⚠ des projets masqués contenant du travail qu'on peut perdre,
     tout est publié dans `bilanFiltre.chantier` quelques lignes plus haut et
     rendu par `majAveuFiltre`, y compris pour le lecteur d'écran.
     Ce que cette barre garde, c'est l'aveu qui n'appartient qu'à elle : les
     lignes socle, juste au-dessus. */
  const droite = el("span", "droite");
  const frais = el("span", "ch-frais");
  if (d.age_s == null) frais.textContent = "";
  else if (d.age_s < 5) frais.textContent = "à l'instant";
  else {
    frais.textContent = "relevé d'il y a " + (d.age_s < 60 ? d.age_s + " s"
                        : Math.floor(d.age_s / 60) + " min");
    if (d.age_s > 300) frais.classList.add("vieux");
  }
  // Le pavé qui annonçait « aucun instantané récent » a été retiré du panneau :
  // il coûtait trois lignes de hauteur pour un cas qui, depuis la correction du
  // cache côté serveur, ne devrait plus se produire. Il ne disparaît pas pour
  // autant — l'écran ne doit jamais étiqueter « réserve » en silence un arbre où
  // une conversation travaille. Le doute passe donc ici, dans une ligne qui
  // existe déjà, à coût vertical nul.
  if (d.conversations_inconnues) {
    frais.classList.add("doute");
    frais.textContent = (frais.textContent ? frais.textContent + " · " : "")
                      + "conversations inconnues";
    attr(frais, "title",
      "Aucun instantané récent au moment du relevé : les arbres portant une "
      + "conversation vivante ne sont pas distingués. Ils apparaissent en réserve "
      + "ou en non commité au lieu d'en cours. Rafraîchis le relevé.");
  }
  /* ── L'INTERRUPTEUR A QUITTÉ CETTE BARRE POUR LE CHROME ───────────────────
     Il a vécu ici, entre la fraîcheur du relevé et les deux boutons, tant qu'il
     ne filtrait que cet onglet. Or l'écran qu'on regarde en arrivant est
     l'accueil, qui affiche une colonne par projet DÉCLARÉ — conversation ou
     pas — et c'est là que les projets sur lesquels on ne travaille pas
     encombrent le plus. Un même besoin sur deux écrans ne se règle pas avec
     deux contrôles : il se règle avec un contrôle global. Il est donc écrit en
     dur dans board.html, dans `.ch-ctrl`, à côté de la bascule de thème.

     TROIS CHOSES QUE CE DÉMÉNAGEMENT SIMPLIFIE, et c'est pourquoi il est bon
     au-delà de la demande :
       · le chrome n'est JAMAIS reconstruit, donc l'interrupteur ne peut plus
         emporter le focus avec lui à chaque re-rendu — `chGarderLaPlace` n'a
         plus à le rattraper, il n'a plus jamais disparu ;
       · une barre de moins à faire déborder : `.ch-tete` rendait 168 px de
         contrôle et 125 px d'aveu ;
       · un seul état pour les deux écrans, donc aucun moyen de les faire
         diverger. */
  const btn = el("button", "ch-relire", "rafraîchir");
  btn.type = "button";
  btn.id = "ch-relire";            // rattrapable par chGarderLaPlace
  btn.onclick = () => { btn.disabled = true; chargerChantier(true); };
  // L'aide de cet onglet rejoint le dialogue GLOBAL au lieu d'ouvrir le sien :
  // il n'y a qu'un endroit où chercher de l'aide dans ce produit, et 90 % du
  // contenu y était déjà. Deux rédactions du même sujet finiraient par diverger.
  const aide = el("button", "aide-onglet", "?");
  aide.type = "button";
  aide.id = "ch-aide";             // rattrapable par chGarderLaPlace
  attr(aide, "title", "À quoi sert cet onglet ?");
  attr(aide, "aria-label", "Aide sur l'onglet Chantier");
  aide.onclick = () => {
    dlgAide.showModal();
    // showModal d'abord : la position dans .corps, qui défile, n'est calculable
    // que dialogue ouvert.
    requestAnimationFrame(() =>
      $("#aide-chantier")?.scrollIntoView({ block: "start" }));
  };
  droite.append(frais, btn, aide);
  tete.append(droite);
  zone.append(tete);

  if (d.degrade) zone.append(el("p", "ch-degrade", d.degrade));
  if (!(d.groupes || []).length) {
    if (!d.degrade)
      zone.append(el("p", "ch-vide",
        "Aucun arbre de travail sous les racines déclarées."));
    return;
  }
  if (!montres.length && caches.length) {
    /* Tout est masqué. Un écran vide qui ne nomme ni la cause de son vide ni le
       geste qui le remplit est un écran cassé — et c'est le seul moment où le
       compteur de la barre ne suffit pas, puisqu'il n'y a rien en dessous pour
       lui donner une échelle.
       Ce cas est FRÉQUENT — c'est tous les soirs, dès qu'aucune conversation ne
       tourne. L'exemption le rendait rare ; son retrait le remet au premier plan,
       et c'est le moment de la journée où l'on ouvre cet onglet pour savoir où
       l'on en était. L'état vide doit donc porter, avant tout le reste, ce que le
       vide cache de récupérable. */
    const p = el("p", "ch-vide");
    p.append(document.createTextNode(
               "Aucune conversation Claude ne travaille en ce moment. "),
             el("b", null, caches.length
                + (caches.length > 1 ? " projets sont masqués" : " projet est masqué")),
             document.createTextNode(
               " — éteins « avec conversation », en haut à droite, pour les "
               + "revoir."));
    if (chDernierMasque.perils) {
      const a = el("b", "peril");
      a.textContent = "⚠ " + pluriel(chDernierMasque.perils,
                                       "de ces projets contient du travail",
                                       "de ces projets contiennent du travail")
                    + " qu'on peut perdre : " + chDernierMasque.nomsPeril.join(", ") + ".";
      p.append(el("br"), a);
    }
    zone.append(p);
    return;
  }

  for (const p of montres) {
    const n = p.arbres.length;
    const sec = el("section", "ch-grp");
    if (p.g.accent) sec.style.setProperty("--acc", p.g.accent);
    const hd = el("header", "ch-hd");
    hd.append(el("span", "nm", p.g.project),
              el("span", "ct", n + (n > 1 ? " arbres" : " arbre")));
    sec.append(hd);

    for (const dep of p.depots) sec.append(blocDepot(p.g, dep, d));
    zone.append(sec);
  }
}

/* ─────────────────── un dépôt, pliable ───────────────────────────────────────
   DEUX niveaux de pliage et pas trois. Le PROJET ne plie pas : ils sont deux, un
   accordéon sur deux éléments ne fait rien gagner, et son en-tête porte la
   couleur d'accent qui sert de repère de navigation. Le DÉPÔT plie, parce qu'un
   seul dépôt porte 17 des 19 arbres PROJET_B. La RÉSERVE d'un dépôt plie, mais
   seulement si ce dépôt contient AUSSI autre chose — sinon les deux boutons
   feraient exactement la même chose, et on n'en garde qu'un.

   États par défaut, et la nuance est la doctrine de l'écran :
     · dépôt   OUVERT toujours. Décider qu'un dépôt « semble calme » serait un
               jugement du système, et rien ne se replie ici sans un geste.
     · réserve REPLIÉE au premier rendu. Seule exception tolérée, parce qu'elle
               s'appuie sur un état explicite du contrat de données et non sur
               une heuristique : elle ne varie jamais d'un rendu à l'autre.
   Conséquence voulue : au premier chargement, l'écran est exactement celui
   d'avant l'accordéon. Le pli est un outil, pas une opinion. */
function blocDepot(g, dep, d) {
  const arbres = dep.arbres || [];
  const cle = g.project + "/" + dep.repo;
  const ident = ("ch-d-" + cle).replace(/[^A-Za-z0-9_-]+/g, "-");
  const reserve = arbres.filter(a => a.etat === "reserve").length;
  const toutReserve = arbres.length > 0 && reserve === arbres.length;

  /* ÉTAT PAR DÉFAUT, décidé UNE FOIS par dépôt (chVus), jamais rejoué :
     · la réserve est repliée — règle inchangée ;
     · le dépôt est refermé s'il ne porte AUCUNE PR ouverte.
     Ce second point est une décision du système, ce que cette feuille interdit
     par ailleurs. Il est admis pour la même raison que le repli de réserve : le
     critère est un FAIT du contrat de données (`etat === "en_revue"`), pas un
     jugement sur ce qui « semble calme ». Il ne varie donc pas d'un rendu à
     l'autre, et l'en-tête replié continue d'annoncer ce qu'il contient — y
     compris son ⚠, qu'aucun pli n'a le droit de taire. */
  if (!chVus.has(cle)) {
    chVus.add(cle);
    if (reserve && !toutReserve) chReplie.add(cle);
    if (!arbres.some(a => a.etat === "en_revue")) chFerme.add(cle);
  }
  const ferme = chFerme.has(cle);

  const bloc = el("div", "ch-depot" + (ferme ? " ferme" : ""));
  bloc.id = ident;

  const dhd = el("div", "ch-dhd");

  const plierD = el("button", "ch-dplier");
  plierD.type = "button";
  // `id` stable : c'est ce qui permet à chGarderLaPlace de rendre le focus à ce
  // chevron précis après un re-rendu — y compris un re-rendu que l'utilisateur
  // n'a pas demandé (voir majVeilleChantier).
  plierD.id = ident + "-plier";
  attr(plierD, "aria-expanded", ferme ? "false" : "true");
  attr(plierD, "aria-controls", ident + "-corps");
  attr(plierD, "aria-describedby", ident + "-resume");
  attr(plierD, "title", (ferme ? "déplier " : "replier ") + dep.repo);
  // Le chevron ne porte PLUS de caractère : ▾ et ▸ sont absents d'Arial (mesuré
  // sur arial.ttf de ce poste), ils étaient donc rendus par une police de repli
  // inconnue. La forme est maintenant dessinée en CSS et pivotée par
  // `aria-expanded` — une seule source de vérité pour l'état et l'apparence.
  const car = el("i", "car");
  attr(car, "aria-hidden", "true");
  plierD.append(car, el("span", "rp", dep.repo),
                el("span", "ct", dep.count + (dep.count > 1 ? " arbres" : " arbre")));
  // On reconstruit le bloc plutôt que de basculer des classes : les trois
  // ensembles de pliage sont de portée module, donc rien n'est perdu, et il n'y
  // a qu'un seul chemin de rendu à garder juste.
  plierD.onclick = () => {
    if (chFerme.has(cle)) chFerme.delete(cle); else chFerme.add(cle);
    // Le bouton qu'on vient d'actionner fait partie de ce qui est détruit : même
    // passage que les re-rendus complets, et pour la même raison (WCAG 2.4.3).
    chGarderLaPlace(() => bloc.replaceWith(blocDepot(g, dep, d)));
  };
  dhd.append(plierD);

  // ── le résumé, visible SEULEMENT replié : replier ne doit rien faire perdre
  const resume = el("span", "ch-resume");
  resume.id = ident + "-resume";
  for (const e of (d.ordre || [])) {
    if (e === "socle") continue;              // le socle n'a rien à signaler
    const n = arbres.filter(a => a.etat === e).length;
    if (!n) continue;
    resume.append(el("b", "etat-" + e,
                     n + " " + ((d.libelles || {})[e] || e).toLowerCase()));
  }
  const incoherents = arbres.filter(
    a => a.alerte_niveau === "agir" || a.alerte_niveau === "bloque").length;
  if (incoherents) {
    // Un dépôt replié ne doit JAMAIS taire une incohérence : c'est la seule
    // chose de cet écran qui demande un acte.
    const w = el("i", "alerte", "⚠");
    attr(w, "title", incoherents + (incoherents > 1
      ? " arbres incohérents dans ce dépôt" : " arbre incohérent dans ce dépôt"));
    resume.append(w);
  }
  resume.hidden = !ferme;
  dhd.append(resume);

  // ── la réserve, si elle coexiste avec autre chose
  if (reserve && !toutReserve) {
    const plier = el("button", "ch-plier");
    plier.type = "button";
    plier.id = ident + "-reserve";     // même raison que le chevron de dépôt
    attr(plier, "aria-controls", ident + "-corps");
    // Même chevron dessiné, mais plus petit : le pliage de dépôt est
    // l'interaction primaire de la ligne, celui de la réserve est accessoire.
    // L'écart de taille EST le signal de cette hiérarchie.
    const pcar = el("i", "car");
    attr(pcar, "aria-hidden", "true");
    const ptxt = el("span", "txt");
    plier.append(pcar, ptxt);
    // La réserve est repliée par défaut, et c'est là que dorment les arbres
    // libérables : replier sans le dire cacherait exactement ce qu'on est venu
    // chercher quand on fait du ménage. Le bouton porte donc le compte.
    const libRes = arbres.filter(a => a.etat === "reserve" && a.liberable).length;
    const majPlier = () => {
      const replie = chReplie.has(cle);
      classe(bloc, "replie", replie);
      attr(plier, "aria-expanded", replie ? "false" : "true");
      texte(ptxt, reserve + " en réserve" + (libRes ? " · " + libRes + " libérable"
                                                    + (libRes > 1 ? "s" : "") : ""));
      classe(plier, "avec-lib", !!libRes);
      attr(plier, "title", replie
        ? "montrer les " + reserve + " arbres au repos de ce dépôt"
          + (libRes ? ", dont " + libRes + " qui peuvent être retiré"
                    + (libRes > 1 ? "s" : "") + " sans rien perdre" : "")
        : "replier la réserve");
    };
    plier.onclick = () => {
      if (chReplie.has(cle)) chReplie.delete(cle); else chReplie.add(cle);
      majPlier();
    };
    majPlier();
    dhd.append(plier);
  }
  // Un dépôt fait UNIQUEMENT de réserve montre ses lignes : les replier
  // laisserait un en-tête sans contenu, ce qui ne renseigne sur rien.

  bloc.append(dhd);
  const lignes = el("div", "ch-lignes");
  lignes.id = ident + "-corps";
  for (const a of arbres) lignes.append(ligneChantier(a));
  bloc.append(lignes);
  return bloc;
}

function ligneChantier(a) {
  const r = el("div", "ch-row " + a.etat);

  const etat = el("span", "ch-etat");
  etat.append(el("i", null, a.glyphe || ""), el("span", null, a.libelle || a.etat));
  r.append(etat);

  r.append(el("span", "ch-us", a.us || "—"));

  const br = el("span", "ch-br", a.branche || "(tête détachée)");
  br.title = a.branche || "tête détachée — aucune branche courante";
  r.append(br);

  const arbre = el("span", "ch-arbre" + (a.arbre === "(dépôt)" ? " depot" : ""),
                   a.arbre || "");
  arbre.title = a.chemin || "";
  r.append(arbre);

  // Jetons git : mêmes formes et mêmes encres que sur la carte de la Board.
  const git = el("span", "ch-git");
  if (a.fichiers) {
    const x = el("b", "chg", a.fichiers + " chg");
    x.title = a.fichiers + " fichier(s) modifié(s) ou non suivi(s), non commité(s)";
    git.append(x);
  }
  if (a.ahead) {
    const x = el("b", "av", "↑" + a.ahead);
    x.title = a.ahead + " commit(s) à pousser";
    git.append(x);
  }
  if (a.behind) {
    const x = el("b", "re", "↓" + a.behind);
    x.title = a.behind + " commit(s) à récupérer";
    git.append(x);
  }
  if (!git.childElementCount) git.append(el("b", "vide", "—"));
  r.append(git);

  const age = el("span", "ch-age", a.commit_j == null ? "—" : a.commit_j + "j");
  age.title = a.commit_j == null ? "aucun commit daté"
            : "dernier commit il y a " + a.commit_j + " jour(s)";
  if ((a.commit_j || 0) >= 7) age.classList.add("vieux");
  r.append(age);

  // ── le détail : la conversation si elle existe, sinon la PR, sinon rien
  const det = el("span", "ch-det");
  if (a.conv && a.conv.length) {
    const c = a.conv[0];
    det.append(el("em", null, "« " + (c.title || "sans titre") + " »"));
    if (a.conv.length > 1) det.append(document.createTextNode(" +" + (a.conv.length - 1)));
    if (c.ctx_pct != null) det.append(document.createTextNode(
      " · ctx " + Math.round(c.ctx_pct) + "%"));
    // L'état est exclusif, le détail ne l'est pas : un arbre où une conversation
    // travaille peut aussi porter une PR ouverte, et le taire serait perdre la
    // moitié de la ligne.
    if (a.pr) {
      const e = PR_ETAT[a.pr.etat] || PR_ETAT.en_attente;
      det.append(document.createTextNode(" · "), el("b", null, "PR #" + a.pr.id));
      const s2 = el("span", "pr-etat-txt", " " + e.txt);
      s2.style.color = e.couleur;
      det.append(s2);
    }
    det.title = a.conv.map(x => x.title || x.sid).join(" · ")
              + (a.pr ? " · PR #" + a.pr.id : "");
  } else if (a.pr) {
    const e = PR_ETAT[a.pr.etat] || PR_ETAT.en_attente;
    det.append(el("b", null, "PR #" + a.pr.id), document.createTextNode(" "));
    const s = el("span", "pr-etat-txt", e.txt);
    s.style.color = e.couleur;
    det.append(s);
    if (a.pr.commentaires) det.append(document.createTextNode(
      " · " + a.pr.commentaires + " com."));
    det.title = "PR #" + a.pr.id + " — " + e.txt
              + (a.pr.age_j != null ? ", ouverte depuis " + a.pr.age_j + " j" : "");
  } else if (a.liberable) {
    // Ni conversation ni PR : c'est exactement la condition d'un arbre libérable,
    // donc la colonne de détail est libre pour le dire.
    const p = el("b", "ch-lib" + (a.terminee ? " fini" : ""),
                 a.terminee ? "BRANCHE TERMINÉE" : "ARBRE LIBÉRABLE");
    attr(p, "title", a.terminee
      ? "Rien de non commité, rien à pousser, aucune PR ouverte, et la branche est "
        + "déjà dans " + (a.base || "la base") + ". L'arbre ET la branche peuvent "
        + "partir : « git branch -d » refusera de lui-même si jamais elle ne "
        + "l'était pas."
      : "Rien de non commité, rien à pousser, aucune PR ouverte : retirer l'arbre "
        + "ne perd rien, et la branche survit de toute façon dans le dépôt.\n"
        + "Elle n'est pas encore vue dans " + (a.base || "la base")
        + (a.base_j != null ? " (référence locale de " + a.base_j + " j)" : "")
        + " — un « git fetch » pourrait la faire passer en TERMINÉE. Ce tableau "
        + "n'en fait jamais.");
    det.append(p);
  }
  r.append(det);

  // La raison qui retient une ligne est LA question qu'on se pose en la
  // regardant. Elle est sur la ligne entière, pas sur un jeton : on survole où
  // l'on veut. Tous les faits qui la motivent sont déjà visibles à l'écran
  // (jetons git, PR, « (dépôt) »), l'infobulle ne fait que les conclure.
  if (a.retenu) attr(r, "title", "Non libérable : " + a.retenu);

  // ── les gestes. Aucun n'écrit dans un dépôt.
  const act = el("span", "ch-act");
  const bcp = el("button", "ch-lien", "⧉");
  bcp.type = "button";
  bcp.title = "copier « cd " + (a.chemin || "") + " »";
  bcp.onclick = async ev => {
    ev.stopPropagation();
    const txt = "cd " + (a.chemin || "");
    try {
      await navigator.clipboard.writeText(txt);
      avis("<b>Copié</b> — <code>" + txt + "</code>");
    } catch {
      // Le presse-papier peut être refusé : on le dit et on montre le chemin
      // plutôt que de faire croire que c'est copié.
      avis("<b>Copie refusée</b> par le navigateur — le chemin est&nbsp;: " +
           "<code>" + txt + "</code>");
    }
  };
  act.append(bcp);

  if (!(a.conv && a.conv.length)) {
    const bo = el("button", "ch-lien rouvrir", "↺");
    bo.type = "button";
    bo.title = "ouvrir une conversation Claude Code dans " + (a.chemin || "cet arbre");
    bo.onclick = async ev => {
      ev.stopPropagation();
      bo.disabled = true;
      let res;
      try {
        res = await (await fetch("/api/ouvrir", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cwd: a.chemin })
        })).json();
      } catch { res = { ok: false, message: "serveur injoignable" }; }
      bo.disabled = false;
      avis(res.ok
        ? "<b>Ouverte</b> — " + res.message + ". Elle apparaîtra dans la board dès " +
          "que ses capteurs répondront."
        : "<b>Impossible d'ouvrir</b> — " + res.message + ".");
    };
    act.append(bo);
  }

  if (a.liberable && a.depot_chemin) {
    // On PROPOSE la commande, on ne l'exécute pas. Ce tableau ne touche à aucun
    // dépôt — et supprimer un arbre de travail depuis une page web serait le
    // seul geste de cet écran qu'on ne pourrait pas défaire.
    const cmd = "git -C " + a.depot_chemin + " worktree remove " + a.chemin
      + (a.terminee ? " && git -C " + a.depot_chemin + " branch -d " + a.branche : "");
    const b = el("button", "ch-lien liberer", "⌫");
    b.type = "button";
    attr(b, "title", "copier la commande qui libère cet arbre"
      + (a.terminee ? " et supprime sa branche" : "") + " — rien n'est exécuté ici");
    b.onclick = async ev => {
      ev.stopPropagation();
      try {
        await navigator.clipboard.writeText(cmd);
        avis("<b>Commande copiée</b> — à coller dans un terminal&nbsp;:<br>"
             + "<code>" + cmd.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</code>");
      } catch {
        avis("<b>Copie refusée</b> par le navigateur — la commande est&nbsp;:<br>"
             + "<code>" + cmd.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</code>");
      }
    };
    act.append(b);
  }

  if (a.pr && a.pr.url) {
    const lien = el("a", "ch-lien", "↗");
    lien.href = a.pr.url; lien.target = "_blank"; lien.rel = "noopener";
    lien.title = "ouvrir la PR #" + a.pr.id + " dans Azure DevOps";
    lien.onclick = ev => ev.stopPropagation();
    act.append(lien);
  }
  r.append(act);

  // ── l'alerte, sur toute la largeur : c'est une phrase, pas une donnée
  if (a.alerte) {
    const al = el("div", "ch-alerte " + (a.alerte_niveau || "info"));
    al.append(el("i", null, a.alerte_niveau === "info" ? "⌫" : "⚠"),
              el("span", null, a.alerte));
    r.append(al);
  }
  return r;
}

/* ── LE GLISSEMENT D'ONGLET ───────────────────────────────────────────────────
   Le panneau qui arrive entre par le côté d'où il vient : on va vers la droite
   dans la barre, il entre par la droite. Ça donne à cinq panneaux empilés au
   même endroit une géographie — « l'Historique est à droite de tout » — que le
   basculement instantané ne donnait pas.

   SEUL LE PANNEAU QUI ARRIVE EST ANIMÉ, et c'est un choix, pas une facilité.
   Animer aussi celui qui part suppose de le garder visible pendant la sortie :
   les cinq panneaux sont des frères en flux, donc deux visibles en même temps
   s'empilent verticalement et l'écran saute. Il faudrait les sortir du flux en
   position absolue le temps de la transition — c'est-à-dire recalculer leur
   hauteur à chaque bascule, sur un écran dont la doctrine est que rien ne bouge
   sans qu'on l'ait demandé. L'entrée seule suffit à dire le sens ; la sortie
   coûterait un risque de saut pour une nuance.

   190 ms : au-dessus, on attend l'interface ; en dessous, on ne perçoit plus la
   direction, donc autant ne rien animer. La courbe est décélérée (le mouvement
   arrive et se pose, il ne rebondit pas).

   `prefers-reduced-motion` désactive tout, plus bas dans board.css. */
function sensOnglet(de, vers) {
  if (!de || de === vers) return 0;
  // Les rangs sont lus dans le DOM, pas dans PANNEAUX : l'ordre des onglets est
  // une décision de board.html, et une table JS qui le duplique finirait un jour
  // par le contredire en silence.
  const ids = [...document.querySelectorAll(".onglets .onglet")].map(b => b.id);
  const a = ids.indexOf((PANNEAUX[de] || {}).onglet?.slice(1));
  const b = ids.indexOf((PANNEAUX[vers] || {}).onglet?.slice(1));
  if (a < 0 || b < 0) return 0;
  return b > a ? 1 : -1;
}

function glisser(panneau, sens) {
  const classe = sens > 0 ? "entre-d" : "entre-g";
  panneau.classList.remove("entre-d", "entre-g");
  // Reflow forcé : retirer puis remettre la même classe dans le même tour de
  // boucle ne rejoue PAS l'animation — le navigateur ne voit qu'un état final
  // identique. Lire offsetWidth le force à recalculer entre les deux.
  void panneau.offsetWidth;
  panneau.classList.add(classe);
  panneau.addEventListener("animationend",
    () => panneau.classList.remove("entre-d", "entre-g"), { once: true });
}

/* ── LA PLAQUE DE VERRE DE LA BARRE D'ONGLETS ────────────────────────────────
   Ce code ne fait qu'UNE chose : mesurer l'onglet actif et pousser trois valeurs
   dans le style de la plaque (--x, sa largeur, et --sx pendant l'étirement).
   Toute la matière et toute la courbe vivent dans board.css — on peut changer
   l'allure du verre sans relire une ligne de JS.

   `offsetLeft` est mesuré depuis la piste elle-même, qui est `position:relative`
   pour cette raison. La plaque part de `left:0`, donc translateX(offsetLeft) les
   aligne, padding de la piste compris.

   POURQUOI UN ResizeObserver ET PAS UN APPEL DANS rendChrome(). Trois choses
   déplacent l'onglet actif sans qu'on ait cliqué : le redimensionnement de la
   fenêtre, la bascule de la barre en deux rangées sous 1415 px, et un compteur
   de pastille qui passe de 9 à 10 (l'onglet s'élargit). Les trois changent la
   taille de la piste, donc un observateur sur la piste les attrape toutes les
   trois. Le faire dans rendChrome() coûterait une lecture de mise en page
   forcée par seconde, pour un événement qui arrive trois fois par jour. */
function mouvementReduit() {
  try { return matchMedia("(prefers-reduced-motion: reduce)").matches; }
  catch { return false; }
}

let curseurPose = false;

function placerCurseur(anime) {
  const piste = $(".onglets"), plaque = $(".onglets .curseur");
  const actif = $(".onglets .onglet[aria-selected=\"true\"]");
  if (!piste || !plaque || !actif) return;
  const x = actif.offsetLeft, large = actif.offsetWidth;
  // Barre pas encore mesurable : polices en cours de chargement, ou onglet
  // dans un panneau masqué. On ne pose RIEN plutôt que de poser une plaque de
  // zéro pixel qui traverserait ensuite l'écran.
  if (!large) return;

  const sec = !anime || !curseurPose || mouvementReduit();
  if (sec) plaque.classList.add("sec");

  const depuis = parseFloat(plaque.dataset.x || "0");
  plaque.style.setProperty("--x", x + "px");
  plaque.style.width = large + "px";
  plaque.style.opacity = "1";
  plaque.dataset.x = String(x);

  if (sec) {
    // Reflow forcé avant de rendre la transition : sans lui, retirer la classe
    // dans le même tour de boucle laisserait le navigateur animer quand même.
    void plaque.offsetWidth;
    plaque.classList.remove("sec");
    curseurPose = true;
    return;
  }

  // L'étirement liquide, proportionnel au voyage et borné à 10 %.
  const saut = Math.abs(x - depuis);
  if (saut > 4) {
    plaque.style.setProperty("--sx", Math.min(1.10, 1 + saut / 900).toFixed(3));
    clearTimeout(plaque._detente);
    plaque._detente = setTimeout(() => plaque.style.setProperty("--sx", "1"), 110);
    // FILET : si le minuteur est perdu — onglet fermé puis rouvert, machine qui
    // décroche, minuteur écrasé par une bascule rapide — la plaque resterait
    // étirée pour toujours. La fin du voyage la remet à plat dans tous les cas.
    plaque.addEventListener("transitionend", ev => {
      if (ev.propertyName === "--x") plaque.style.setProperty("--sx", "1");
    }, { once: true });
  }
}

function ongler(quel) {
  const sens = sensOnglet(ongletActif, quel);
  ongletActif = quel;
  for (const [nom, o] of Object.entries(PANNEAUX)) {
    const p = $(o.panneau);
    p.hidden = nom !== quel;
    attr($(o.onglet), "aria-selected", nom === quel);
    if (nom === quel && sens) glisser(p, sens);
  }
  placerCurseur(true);
  // Le bandeau d'attention ne concerne que les conversations : ailleurs il
  // mentirait sur ce que l'écran montre.
  $("#attention").hidden = quel !== "board";
  // L'aveu du chrome décrit l'onglet VISIBLE : il change donc de sujet ici, avant
  // même que le nouvel onglet ait rendu quoi que ce soit.
  majAveuFiltre();
  /* La bande « chercher les projets du poste » est un FRÈRE de #board dans le
     flux, pas un enfant : masquer #board ne la masque pas. Sans cette ligne elle
     resterait posée au-dessus du panneau Pull Requests, à parler d'un écran
     qu'on ne regarde plus. */
  majDecouv();
  if (quel === "histo") chargerHistorique();
  if (quel === "pr") chargerPR(false);
  if (quel === "chantier") {
    // La répartition des conversations a bougé pendant que l'onglet était
    // fermé : on force, pour la même raison que dans majVeilleChantier — le
    // cache rendrait le bon `conversations` mais des ÉTATS d'avant, donc un
    // projet listé « en réserve » alors qu'on y travaille. Sans changement, on
    // se contente du cache : c'est le comportement d'origine de l'onglet.
    const force = chPerime;
    chPerime = false;
    chargerChantier(force);
  }
}

/* ──────────────────────────────── flux ────────────────────────────────── */
function appliquer(snap) {
  // Les sondes de pastille ont besoin des conversations pour être justes, donc
  // elles attendaient le premier instantané en se rappelant toutes les 1,5 s.
  // Or il arrive en 10 ms : la pastille Chantier restait vide une seconde et
  // demie pour rien. On les réveille ici, à la milliseconde où l'attente cesse.
  const premier = !dernier;
  dernier = snap; dernierAt = Date.now();
  if (premier) sondeChantier();
  // Le seul lien entre le flux et l'onglet Chantier : il ne re-rend rien de
  // lui-même, il regarde si l'ENSEMBLE des conversations vivantes a changé.
  majVeilleChantier(snap);
  rendChrome(snap);
  if (ongletActif === "board") { rendAttention(snap); rendBoard(snap); }
  rendFlux();
}

function brancherFlux() {
  let es;
  const ouvrirFlux = () => {
    es = new EventSource("/flux");
    es.onmessage = ev => { try { appliquer(JSON.parse(ev.data)); } catch {} };
    es.onerror = () => { es.close(); setTimeout(ouvrirFlux, 2000); };
  };
  ouvrirFlux();
}

/* ─────────────────────── création de projet ────────────────────────────── */
const dlg = $("#nouveau");

function normaliserNom(v) {
  return (v || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "")
                  .replace(/[^A-Za-z0-9_-]/g, "").slice(0, 32);
}

$("#np-nom").addEventListener("input", ev => {
  const n = normaliserNom(ev.target.value);
  if (ev.target.value !== n) ev.target.value = n;
  texte($("#np-apercu"), "claude-" + (n.toLowerCase() || "nomprojet"));
});

/* Le bouton « + projet » de la barre a disparu : le serveur détecte lui-même les
   dossiers non déclarés et les propose en tête de la colonne AUTRE. Le formulaire
   reste, et s'ouvre PRÉ-REMPLI avec le nom et la racine devinés — l'utilisateur
   corrige s'il veut, mais il n'a plus rien à taper dans le cas courant.

   UN SEUL CHEMIN D'ADOPTION, DEUX APPELANTS. `majAdoption` (colonne de repli) et
   la recherche de projets du poste (`#decouv`) ouvrent ce MÊME formulaire et ne
   connaissent aucune autre route que /api/projet — écrire une seconde adoption
   aurait dédoublé la validation du nom, le lanceur et le message d'échec.
   Ce que l'un des deux a besoin de savoir en plus, c'est QUAND ça a marché, pour
   retirer sa ligne : d'où `apres`, un crochet à un coup. Il est posé à
   l'ouverture, désarmé à la fermeture QUOI QU'IL ARRIVE (`close`, natif, couvre
   Échap, le clic sur le fond et le bouton Annuler), et n'est donc jamais rejoué
   par l'adoption suivante. */
let adoptionSuite = null;
function ouvrirFormProjet(nom, racine, apres) {
  const n = normaliserNom(nom || "");
  $("#np-nom").value = n;
  $("#np-racine").value = racine || "";
  texte($("#np-msg"), ""); $("#np-msg").className = "msg";
  texte($("#np-apercu"), "claude-" + (n.toLowerCase() || "nomprojet"));
  adoptionSuite = typeof apres === "function" ? apres : null;
  dlg.showModal();
  // Le nom est deviné, la racine est un fait : c'est le nom qu'on vient relire.
  $("#np-nom").focus();
  $("#np-nom").select();
}
dlg.addEventListener("close", () => { adoptionSuite = null; });
$("#np-annuler").onclick = () => dlg.close();

$("#form-projet").addEventListener("submit", async ev => {
  ev.preventDefault();
  const nom = normaliserNom($("#np-nom").value);
  const racine = $("#np-racine").value.trim();
  const msg = $("#np-msg");
  if (!nom || !racine) { msg.className = "msg"; texte(msg, "nom et dossier sont requis"); return; }
  $("#np-creer").disabled = true;
  let r;
  try {
    r = await (await fetch("/api/projet", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: nom, root: racine })
    })).json();
  } catch { r = { ok: false, message: "serveur injoignable" }; }
  $("#np-creer").disabled = false;
  if (!r.ok) { msg.className = "msg"; texte(msg, r.message || "échec"); return; }
  msg.className = "msg ok";
  texte(msg, r.lanceur ? "créé · lanceur " + r.lanceur : (r.message || "créé"));
  /* Le crochet est CAPTURÉ ici et joué APRÈS `close()`. Deux raisons, et les
     deux sont des bugs qu'on évite : `close()` déclenche l'écouteur ci-dessus
     qui remet `adoptionSuite` à null, donc le lire après serait le lire vide ;
     et `close()` rend le focus au bouton qui a ouvert le formulaire — or c'est
     précisément ce bouton que le crochet va retirer du document. Le jouer
     ensuite lui laisse la main sur un focus déjà rendu, et il peut le replacer
     là où il faut au lieu de le laisser retomber sur <body>. */
  const suite = adoptionSuite;
  setTimeout(() => { dlg.close(); if (suite) suite(); }, 1400);
});

/* ─────────────────────────── thème ────────────────────────────────────────
   Deux thèmes explicites, mémorisés localement. Au premier affichage on suit
   la préférence du système. L'accès au stockage peut lever (navigation privée,
   site data bloqué) : tout est sous try/catch et la page s'affiche quand même. */
function litTheme() {
  try { const v = localStorage.getItem("board-theme");
        if (v === "clair" || v === "sombre") return v; } catch {}
  // SOMBRE PAR DÉFAUT, sans consulter le système. Le board suivait
  // `prefers-color-scheme`, donc sur un Windows réglé en clair il s'ouvrait en
  // clair — alors que c'est un écran de surveillance posé à côté d'un terminal
  // sombre, et que la direction Ardoise est pensée pour le sombre (les néons
  // ci-dessous n'existent pas en clair, un halo sur du blanc se lit comme un
  // flou d'impression). Le thème clair reste entier et à un clic.
  return "sombre";
}

function poseTheme(t) {
  document.documentElement.dataset.theme = t;
  const b = $("#btn-theme");
  if (b) {
    // Plus de glyphe à écrire : ☾ et ☀ sont absents de Plex Sans comme ils
    // l'étaient d'Arial. Le bouton porte un commutateur dessiné, dont la
    // position découle du `data-theme` posé juste au-dessus — donc rien à
    // synchroniser ici, et aucun état qui puisse diverger de l'apparence.
    attr(b, "title", t === "clair" ? "Passer au thème sombre" : "Passer au thème clair");
    attr(b, "aria-label", b.title);
  }
  try { localStorage.setItem("board-theme", t); } catch {}
}

poseTheme(litTheme());
$("#btn-theme").onclick = () =>
  poseTheme(document.documentElement.dataset.theme === "clair" ? "sombre" : "clair");

/* ─────────── L'INTERRUPTEUR « avec conversation », CÂBLÉ UNE FOIS ────────────
   Le contrôle est statique (board.html) : ce câblage a donc lieu au chargement
   et jamais plus. C'est toute la différence avec la version qui vivait dans
   `.ch-tete`, recâblée à chaque rendu.

   LA CASE EST FORCÉE À L'ALLUMAGE, et ce n'est pas redondant avec l'attribut
   `checked` du HTML. Les navigateurs restaurent l'état des formulaires après un
   rechargement (F5, retour arrière) : sans cette ligne, éteindre l'interrupteur
   puis recharger la page le rendrait éteint, c'est-à-dire PERSISTÉ — ce que la
   doctrine d'`avecConv` interdit. `autocomplete="off"` le dit déjà au
   navigateur ; cette ligne le garantit quoi qu'il en fasse.

   LES DEUX ÉCRANS SE RE-RENDENT, y compris celui qui est caché. Ni l'un ni
   l'autre ne demande quoi que ce soit au serveur : le filtre est un tri de
   données déjà en mémoire (`dernier` pour l'accueil, `chData` pour le
   Chantier). Les re-rendre tous les deux tout de suite coûte deux passes de DOM
   et supprime toute possibilité qu'un onglet montre un état et l'autre un
   autre — c'est moins cher, et plus sûr, qu'un drapeau de péremption de plus. */
const fcCase = $("#fc-case");
fcCase.checked = true;
avecConv = true;
fcCase.onchange = () => {
  avecConv = fcCase.checked;
  if (dernier) rendBoard(dernier);
  if (chData) chGarderLaPlace(() => rendChantier(chData));
  majAveuFiltre();
  /* LE CHANGEMENT DOIT S'ENTENDRE, pas seulement se voir : c'est le nombre de
     projets affichés qui vient de changer, et rien dans la page ne l'annonce à
     qui ne la regarde pas. `#avis` est une région live que `avis()` maintient
     en permanence dans l'arbre d'accessibilité — voir son commentaire, qui porte
     la mesure. Ce n'est pas la confirmation vide que ce commentaire interdit :
     la charge utile, c'est ce qui vient de quitter l'écran.
     L'annonce décrit l'onglet VISIBLE, comme l'aveu : annoncer le bilan d'un
     écran qu'on ne regarde pas serait un chiffre de plus à ne pas pouvoir
     vérifier. Aucun nom de projet n'est interpolé dans `avis()`, qui écrit en
     innerHTML — les noms vivent dans l'aveu et dans sa description, en texte. */
  const b = bilanFiltre[ongletActif] || bilanFiltre.board;
  if (!avecConv) {
    avis("<b>« avec conversation » éteint</b> — tous les projets sont affichés.");
  } else if (b.inconnu) {
    avis("<b>« avec conversation » allumé</b> — rien n'est masqué : le serveur "
         + "ne sait pas quelles conversations tournent.");
  } else if (!b.projets) {
    avis("<b>« avec conversation » allumé</b> — aucun projet masqué : une "
         + "conversation travaille dans chacun d'eux.");
  } else {
    avis("<b>« avec conversation » allumé</b> — "
         + pluriel(b.projets, "projet masqué", "projets masqués")
         + (b.perils ? ", dont " + b.perils + " avec du travail qu'on peut perdre"
                     : "") + ".");
  }
  majDecouv();
};
// L'aveu doit dire ce que montre l'onglet qu'on vient d'ouvrir, pas le précédent.
majAveuFiltre();


/* ╔══════════════════════════════════════════════════════════════════════════╗
   ║  CHERCHER LES PROJETS DU POSTE                                           ║
   ╚══════════════════════════════════════════════════════════════════════════╝

   LE BESOIN. L'écran d'accueil affiche une colonne par projet DÉCLARÉ dans
   config.json. Rien, jusqu'ici, ne disait ce que ce fichier ignore : un dépôt
   cloné il y a trois mois et jamais déclaré n'existait tout simplement pas pour
   le board, et le seul moyen de l'y faire entrer était de taper son chemin à la
   main dans le formulaire. L'adoption qui existait déjà (`majAdoption`) ne
   comble pas ce trou : elle ne voit que les dossiers où une conversation est
   DÉJÀ ouverte, c'est-à-dire ceux dont on se souvenait.

   POURQUOI ÇA NE VIT QUE FILTRE ÉTEINT. « avec conversation » allumé, l'écran
   assume de ne montrer qu'une partie des projets : y poser « en manque-t-il ? »
   serait poser une question à laquelle l'écran vient lui-même de répondre non.
   Éteint, l'écran prétend montrer TOUT — c'est le seul état où l'exhaustivité
   est une promesse, donc le seul où elle peut être prise en défaut.

   TROIS GESTES, ET PAS UN DE MOINS.
     1. autoriser  — un panneau dit ce qui sera lu, la recherche attend ;
     2. choisir    — une ligne de la liste ouvre le formulaire pré-rempli ;
     3. valider    — c'est le formulaire d'adoption ordinaire qui écrit.
   Aucun de ces gestes n'est mémorisé d'un chargement à l'autre. */

const dcSection = $("#decouv"), dcBouton = $("#dc-go"), dcMot = $("#dc-mot"),
      dcJauge = $("#dc-jauge"), dcDegrade = $("#dc-degrade"), dcListe = $("#dc-liste");
const dlgAutor = $("#autorisation");

/* L'AUTORISATION EST UNE VARIABLE DE MODULE, ET C'EST TOUT L'ENJEU. Ni
   localStorage, ni sessionStorage, ni cookie, ni /api/layout : recharger la page
   la remet à faux, et c'est voulu. La doctrine d'`avecConv` refuse déjà de
   mémoriser une préférence d'AFFICHAGE ; une autorisation de LECTURE DU DISQUE
   qui se réveillerait toute seule serait la même faute d'un cran plus haut.
   Une fois par chargement de page, c'est un clic — pas zéro. */
let dcAutorise = false;
let dcEnCours = false;        // verrou : le bouton est désactivé, le drapeau double
let dcReleve = null;          // dernier relevé : {candidats, scannes, duree_ms, degrade}
let dcEchec = "";             // phrase d'échec en cours, vide sinon
let dcRendreFocus = null;     // à qui rendre le focus quand le panneau se ferme

/* Le nombre de dossiers parcourus se compte en milliers : sans séparateur il se
   lit mal, et `4213` a l'air d'un identifiant. La durée reste en secondes à une
   décimale — « 2900 ms » ne dit rien à personne. */
function dcNombre(n) {
  return Number.isFinite(n) ? Number(n).toLocaleString("fr-FR") : "?";
}
function dcDuree(ms) {
  return Number.isFinite(ms) ? (ms / 1000).toFixed(1).replace(".", ",") + " s" : "?";
}
function dcReleveMot(r) {
  return dcNombre(r.scannes) + " dossiers parcourus en " + dcDuree(r.duree_ms);
}
// Un horodatage de commit ne sert ici qu'à trier l'utile du dormant : la date
// suffit, l'heure serait du bruit. Absent ou aberrant, on n'écrit rien plutôt
// que d'inventer un « jamais » que le serveur n'a pas dit.
function dcDate(epoch) {
  if (!Number.isFinite(epoch) || epoch <= 0) return "";
  const d = new Date(epoch * 1000);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("fr-FR", { day:"numeric", month:"short", year:"numeric" });
}

/* ── QUI DÉCIDE DE LA VISIBILITÉ DE LA BANDE ──────────────────────────────────
   Un seul endroit, appelé par les trois choses qui peuvent la changer : la
   bascule du filtre, le changement d'onglet, et le chargement. La bande est un
   frère de `#board` dans le flux, pas un enfant : sans cette fonction elle
   resterait affichée au-dessus du panneau Pull Requests. */
function majDecouv() {
  const montrer = ongletActif === "board" && !avecConv;
  dcSection.hidden = !montrer;
  if (montrer) rendDecouv();
  else majHauteurDecouv();
}

/* ── L'AVIS NE DOIT PAS RECOUVRIR LA LISTE QU'IL ANNONCE ──────────────────────
   `#avis` est un toast `position:fixed; bottom:56px` — 56 px, la hauteur du pied
   de page, seule chose qui vivait sous lui. La bande s'installe entre les deux :
   sans cette mesure, « Recherche terminée — 4 projets trouvés » se posait
   exactement sur les quatre lignes trouvées, et pour six secondes. On publie la
   hauteur OCCUPÉE (boîte + marge basse de 12 px), que board.css ajoute au
   décalage du toast ; zéro quand la bande est masquée, donc rien ne change sur
   les autres onglets ni filtre allumé.

   `getBoundingClientRect` force un calcul de mise en page, et c'est justement ce
   qu'on veut : la valeur doit être celle d'APRÈS le rendu de la liste, pas celle
   d'avant. Il n'est appelé qu'aux quatre moments où la bande change de taille —
   bascule du filtre, changement d'onglet, relevé, adoption — jamais au rythme du
   flux. */
function majHauteurDecouv() {
  const h = dcSection.hidden || !dcSection.getBoundingClientRect ? 0
          : Math.round(dcSection.getBoundingClientRect().height) + 12;
  document.documentElement.style.setProperty("--dc-h", h + "px");
}

function rendDecouv() { peindreDecouv(); majHauteurDecouv(); }

function peindreDecouv() {
  attr(dcSection, "aria-busy", dcEnCours ? "true" : null);
  dcJauge.hidden = !dcEnCours;
  dcBouton.disabled = dcEnCours;

  if (dcEnCours) {
    texte(dcBouton, "recherche en cours…");
    texte(dcMot, "parcours du répertoire personnel — quelques secondes.");
    dcDegrade.hidden = true;
    return;
  }
  // Le libellé dit ce que le clic FERA, et il change quand ce n'est plus la
  // même chose : le premier clic ouvre le panneau d'autorisation, les suivants
  // rebalaient directement — l'accord donné vaut pour toute la page.
  texte(dcBouton, dcReleve || dcEchec ? "chercher à nouveau" : "chercher les projets du poste");
  attr(dcBouton, "title", dcAutorise
    ? "Reparcourir le répertoire personnel à la recherche de dépôts git non déclarés. "
      + "L'autorisation déjà donnée vaut pour cette page."
    : "Parcourir le répertoire personnel à la recherche de dépôts git non déclarés. "
      + "Un panneau dira d'abord ce qui sera lu ; rien n'est parcouru sans ton accord.");

  if (dcEchec) {
    texte(dcMot, dcEchec);
    classe(dcMot, "mauvais", true);
    dcDegrade.hidden = true;
    dcListe.textContent = "";
    return;
  }
  classe(dcMot, "mauvais", false);

  if (!dcReleve) {
    texte(dcMot, "Le board ne connaît que les projets déclarés. "
               + "S'il en manque, cette recherche les repère sur le poste.");
    dcDegrade.hidden = true;
    dcListe.textContent = "";
    return;
  }

  /* `degrade` NE SE TAIT PAS. Le serveur l'envoie quand des dossiers n'ont pas
     pu être lus : la liste est alors une liste PARTIELLE, et une liste partielle
     présentée comme complète ferait conclure « il ne manque rien » à qui il
     manque quelque chose. C'est le même interdit que l'instantané aveugle du
     filtre : on n'affirme pas sur une ignorance. */
  const deg = typeof dcReleve.degrade === "string" ? dcReleve.degrade.trim() : "";
  dcDegrade.hidden = !deg;
  if (deg) {
    /* Le ⚠ est un DOUBLON VISUEL des mots qui le suivent, comme le ⚠ de l'aveu
       du chrome : « Liste incomplète » est déjà écrit juste à côté, en toutes
       lettres. On le sort donc de l'arbre d'accessibilité plutôt que de faire
       annoncer « symbole attention, liste incomplète ». La phrase du serveur
       arrive en textContent, jamais en innerHTML — c'est du texte qu'on relaie,
       pas du balisage qu'on exécute. */
    dcDegrade.textContent = "";
    const g = el("b", "dc-alerte", "⚠");
    attr(g, "aria-hidden", "true");
    dcDegrade.append(g, document.createTextNode(" Liste incomplète — " + deg));
  }

  const cands = Array.isArray(dcReleve.candidats) ? dcReleve.candidats : [];
  if (!cands.length) {
    /* LE VIDE EST UNE BONNE NOUVELLE, et il faut le dire, sinon il se lit comme
       une panne. Zéro candidat ne veut pas dire « rien trouvé » : ça veut dire
       que tout ce que le poste porte comme dépôt git est déjà sur le board. */
    texte(dcMot, deg
      ? "Aucun projet à ajouter parmi ce qui a pu être lu — " + dcReleveMot(dcReleve)
        + ". Ce qui manque à la liste ci-dessus n'a pas été examiné."
      : "Rien à ajouter, et c'est la bonne réponse : " + dcReleveMot(dcReleve)
        + ", et pas un dépôt git qui ne soit déjà déclaré. Le board est complet.");
    dcListe.textContent = "";
    return;
  }

  texte(dcMot, pluriel(cands.length, "projet trouvé", "projets trouvés")
             + " que le board ne connaît pas · " + dcReleveMot(dcReleve)
             + ". Chaque ligne ouvre le formulaire — rien n'est ajouté sans ta validation.");

  /* Rendu complet de la liste, et pas de réconciliation par clé : elle ne change
     qu'à un relevé ou à une adoption, jamais au rythme du flux. Le seul focus
     qui vive ici est celui d'une ligne, et c'est `dcRetirer` qui le déplace —
     ce rendu-là n'est jamais déclenché sous les doigts de l'utilisateur. */
  dcListe.textContent = "";
  for (const c of cands) {
    const nom = String(c.name || ""), racine = String(c.root || "");
    const court = String(c.root_court || racine);
    const b = el("button", "dc-l");
    b.type = "button";
    b.dataset.root = racine;
    const bas = [];
    if (c.depot) bas.push(String(c.depot));
    const d = dcDate(c.dernier_commit_at);
    if (d) bas.push("dernier commit " + d);
    b.append(el("b", null, "+ " + nom), el("span", null, court));
    if (bas.length) b.append(el("em", null, bas.join(" · ")));
    attr(b, "title", `Adopter « ${court} » comme projet : une colonne à lui, sa `
      + `couleur, et un lanceur claude-${nom.toLowerCase()}. Le formulaire s'ouvre `
      + `pré-rempli — tu peux corriger le nom avant de valider.`);
    b.onclick = () => ouvrirFormProjet(nom, racine, () => dcRetirer(racine));
    dcListe.append(b);
  }
}

/* Une ligne adoptée quitte la liste — sinon elle proposerait d'adopter deux fois
   le même dossier, et le second essai échouerait sur un doublon côté serveur.
   Le board, lui, se met à jour tout seul : le prochain instantané SSE porte la
   nouvelle colonne. On ne rebalaie pas le disque pour ça.

   LE FOCUS NE TOMBE PAS. Le bouton qu'on retire est celui qui avait ouvert le
   formulaire, donc celui à qui `close()` vient de rendre la main : le supprimer
   sans rien faire renverrait le focus sur <body> et perdrait la place au clavier
   (WCAG 2.4.3). On le donne à la ligne suivante, sinon à la précédente, sinon au
   bouton de recherche — qui, lui, ne disparaît jamais. */
function dcRetirer(racine) {
  if (!dcReleve || !Array.isArray(dcReleve.candidats)) return;
  const lignes = [...dcListe.children];
  const i = lignes.findIndex(n => n.dataset && n.dataset.root === racine);
  dcReleve.candidats = dcReleve.candidats.filter(c => String(c.root || "") !== racine);
  const suivant = i < 0 ? null : (lignes[i + 1] || lignes[i - 1] || null);
  const cible = suivant && suivant.dataset ? suivant.dataset.root : null;
  rendDecouv();
  const rendu = cible
    ? [...dcListe.children].find(n => n.dataset && n.dataset.root === cible)
    : null;
  (rendu || dcBouton).focus();
}

/* ── LE BALAYAGE ──────────────────────────────────────────────────────────────
   `autorise=1` n'est pas décoratif : sans lui le serveur ne parcourt rien et
   répond un refus. Le paramètre est donc la TRACE du consentement dans la
   requête elle-même, et il n'est jamais posé ailleurs qu'ici, derrière le
   drapeau que seul le panneau d'autorisation lève.

   L'ÉCHEC EST TRAITÉ AVANT LE SUCCÈS, et il a plusieurs formes. La route peut
   ne pas exister du tout — c'est le cas sur un serveur plus ancien, qui répond
   404 avec du JSON : l'écran doit le dire, pas se figer sur « recherche en
   cours ». Le serveur peut refuser. Le réseau peut tomber. Et la réponse peut
   être bien formée mais sans `candidats` : on ne devine pas, on avoue. */
async function dcChercher() {
  if (dcEnCours || !dcAutorise) return;
  dcEnCours = true; dcEchec = "";
  rendDecouv();
  avis("<b>Recherche en cours</b> — parcours du répertoire personnel à la "
       + "recherche de dépôts git. Quelques secondes.");

  // Un balayage qui n'aboutit jamais laisserait le bouton désactivé pour de bon,
  // sans aucun moyen de réessayer : la minute est large pour un relevé mesuré à
  // trois secondes, et elle rend toujours la main.
  const stop = new AbortController();
  const minuteur = setTimeout(() => stop.abort(), 60000);
  let d = null;
  try {
    const rep = await fetch("/api/decouverte?autorise=1", { signal: stop.signal });
    if (!rep.ok) {
      dcEchec = rep.status === 404
        ? "Cette version du serveur ne sait pas chercher les projets du poste. "
          + "Rien n'a été parcouru."
        : "Le serveur a refusé la recherche (code " + rep.status + "). "
          + "Rien n'a été parcouru.";
    } else {
      d = await rep.json();
    }
  } catch (e) {
    dcEchec = e && e.name === "AbortError"
      ? "La recherche n'a pas répondu en une minute — elle a été interrompue. "
        + "Rien n'a été ajouté."
      : "Serveur injoignable — la recherche n'a pas eu lieu.";
  }
  clearTimeout(minuteur);

  if (d && !Array.isArray(d.candidats)) {
    // Réponse bien formée mais sans liste : c'est un refus, et le serveur en
    // donne parfois la raison. On la relaie telle quelle — en texte, jamais en
    // HTML : cette phrase vient du serveur et n'a rien à faire dans un innerHTML.
    const raison = typeof d.erreur === "string" ? d.erreur
                 : typeof d.message === "string" ? d.message : "";
    dcEchec = "La recherche n'a pas eu lieu" + (raison ? " — " + raison : ".") ;
    d = null;
  }
  if (d) { dcReleve = d; dcEchec = ""; }

  dcEnCours = false;
  rendDecouv();

  /* L'ARRIVÉE DE LA LISTE S'ANNONCE, et par `#avis` — la région live permanente
     de l'écran, dont la correction d'arbre d'accessibilité est mesurée au-dessus
     de `avis()`. En ouvrir une seconde ici ferait deux régions concurrentes pour
     un même écran, et rien ne garantirait laquelle parle en premier.
     Aucun nom de projet, aucune phrase du serveur n'entre dans `avis()`, qui
     écrit en innerHTML : les noms vivent dans la liste, en textContent. */
  if (dcEchec) {
    avis("<b>Recherche impossible</b> — rien n'a été parcouru. "
         + "Le détail est écrit sous les colonnes.");
  } else {
    const n = dcReleve.candidats.length;
    const deg = typeof dcReleve.degrade === "string" && dcReleve.degrade.trim();
    avis("<b>Recherche terminée</b> — "
         + (n ? pluriel(n, "projet trouvé", "projets trouvés")
                + " que le board ne connaît pas, sous les colonnes."
              : "aucun projet à ajouter : tous les dépôts git du poste sont déjà déclarés.")
         + (deg ? " La liste est incomplète : des dossiers n'ont pas pu être lus."
                : ""));
  }
}

/* ── LE PANNEAU D'AUTORISATION ────────────────────────────────────────────────
   `showModal()` porte le rôle, le piège à focus et l'inertie de la page. Ce qui
   reste à écrire tient en deux gestes que le natif ne fait pas à notre place :
   poser le focus D'ENTRÉE sur le dialogue (et non sur un bouton, qu'Entrée
   armerait), et le rendre à l'appelant à la sortie. La restitution de focus par
   `close()` existe dans les navigateurs récents, mais elle vise « l'élément
   précédemment focalisé », pas « le bouton qui a ouvert » — sur un panneau
   ouvert au clavier depuis une ligne qui n'existe plus, ce n'est pas la même
   chose. On la fait donc explicitement, et une seule fois. */
function ouvrirAutorisation() {
  dcRendreFocus = document.activeElement;
  dlgAutor.showModal();
  dlgAutor.focus();
}
function fermerAutorisation() {
  if (dlgAutor.open) dlgAutor.close();
}
// `close` est le SEUL point de sortie : il couvre le bouton Annuler, Échap
// (l'événement `cancel` natif ferme), le clic sur le fond et la validation.
dlgAutor.addEventListener("close", () => {
  const cible = dcRendreFocus;
  dcRendreFocus = null;
  if (cible && cible.isConnected && typeof cible.focus === "function") cible.focus();
});
dlgAutor.addEventListener("click", ev => { if (ev.target === dlgAutor) dlgAutor.close(); });
$("#au-annuler").onclick = () => fermerAutorisation();
$("#au-ok").onclick = () => {
  dcAutorise = true;
  fermerAutorisation();
  dcChercher();
};

dcBouton.onclick = () => {
  if (dcEnCours) return;
  if (!dcAutorise) { ouvrirAutorisation(); return; }
  dcChercher();
};

/* La grille de candidats se recompose en changeant de largeur (auto-fill), donc
   la bande change de hauteur sans qu'aucun de nos rendus soit passé. Même idiome
   que la plaque des onglets, plus bas dans ce fichier. */
if (window.ResizeObserver) new ResizeObserver(majHauteurDecouv).observe(dcSection);

majDecouv();


/* ═══════════════════ fiche d'une conversation ════════════════════════════
   Le clic sur une session ouvre la fiche au lieu de sauter directement au pane :
   on veut savoir ce qui se passe la-dedans AVANT de se deplacer. Le saut reste
   disponible, en bouton. Tout est en lecture seule. */
const dlgFiche = $("#fiche");
let ficheSid = null, ficheTimer = null;

// Meme table que EDGE, pour la fiche : blocked et error partagent le rouge,
// review prend l'ambre, --sky ne parait plus. Voir le commentaire d'EDGE.
const ETAT_COULEUR = { blocked:"var(--red)", error:"var(--red)",
                       silent:"var(--grey)", review:"var(--amber)", working:"var(--green)" };

// LES QUATRE STATUTS D'UN SOUS-AGENT. Table de module, comme ETAT_COULEUR et
// PR_ETAT : la fiche se réécrit toutes les 3 s, et rien de constant n'a de
// raison d'être reconstruit à chaque tour.
//
// Les libellés et les durées viennent du serveur (`statut`, `depuis_s`,
// `duree_s`) ; le board n'en déduit aucun. « depuis » et « en » ne sont pas
// interchangeables : l'un compte un travail qui court, l'autre un travail
// achevé — c'est pour ça que le serveur envoie deux champs et non un.
const AGENT = {
  en_cours: { cl:"vif",    txt:"EN COURS",      quand:s => "depuis " + dur(s) },
  perdu:    { cl:"perdu",  txt:"SANS NOUVELLE", quand:s => dur(s) },
  echoue:   { cl:"echoue", txt:"ÉCHEC",         quand:s => "en " + dur(s) },
  fini:     { cl:"fait",   txt:"fini",          quand:s => "en " + dur(s) },
};

function dur(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.floor(s));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m" + String(s % 60).padStart(2, "0");
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h" + String(m % 60).padStart(2, "0");
  return Math.floor(h / 24) + "j" + String(h % 24).padStart(2, "0");
}

function el(balise, cl, txt) {
  const n = document.createElement(balise);
  if (cl) n.className = cl;
  if (txt != null) n.textContent = txt;
  return n;
}

function fait(nom, valeur, titre) {
  const d = el("div");
  d.append(el("b", null, nom), el("span", null, valeur == null ? "—" : String(valeur)));
  if (titre) d.title = titre;
  return d;
}

async function ouvrirFiche(sid) {
  ficheSid = sid;
  if (!dlgFiche.open) dlgFiche.showModal();
  await rafraichirFiche();
  clearTimeout(ficheTimer);
  ficheTimer = setTimeout(boucleFiche, 3000);
}

async function boucleFiche() {
  if (!dlgFiche.open || !ficheSid) return;
  await rafraichirFiche();
  ficheTimer = setTimeout(boucleFiche, 3000);
}

async function rafraichirFiche() {
  let d;
  try { d = await (await fetch("/api/conv?sid=" + encodeURIComponent(ficheSid))).json(); }
  catch { d = { erreur: "serveur injoignable" }; }
  rendFiche(d);
}

function rendFiche(d) {
  const e = d.session || {};
  const tete = $("#fi-tete");
  tete.textContent = "";
  // Le glyphe rejoint la PASTILLE, pas l'identifiant : une couleur de statut ne
  // voyage jamais seule, elle est toujours accompagnée d'un glyphe et d'un
  // libellé. fiche.css s'appuie sur .fi-id et sur .etat pour les composer.
  tete.append(el("span", "fi-id", e.ident || d.sid || ""));
  if (e.libelle) {
    const b = el("span", "etat", (e.glyphe ? e.glyphe + " " : "") + e.libelle);
    b.style.color = ETAT_COULEUR[e.state] || "var(--t3)";
    tete.append(b);
  }
  if (e.project) tete.append(el("span", null, "· " + e.project));
  tete.append(el("span", "chrono", e.since || ""));

  // ── les faits
  // ── l'identité : un chemin, puis un nom.
  //
  // Plus d'étiquettes « dépôt / branche / titre » : trois mots qui n'apprenaient
  // rien. Un nom de dépôt, un nom de branche et un titre se reconnaissent à leur
  // FORME — chasse fixe et séparateur pour les deux premiers, prose pour le
  // troisième. Supprimer les étiquettes rend le bloc plus lisible ET plus
  // robuste : il n'y a plus de grille à deux colonnes qui puisse s'effondrer.
  const idz = $("#fi-ident");
  idz.textContent = "";
  if (e.repo || e.branch) {
    const chemin = el("div", "fi-chemin");
    if (e.repo) chemin.append(el("b", "fi-depot", e.repo));
    // › et non ▸ : U+25B8 est absent d'Arial (mesuré sur ce poste) et retombait
    // sur une police inconnue, alors que U+203A y est présent (avance 0,333 em).
    // Même correction que les chevrons de l'accordéon Chantier, même raison.
    if (e.repo && e.branch) chemin.append(el("i", "fi-sep", "\u203A"));
    if (e.branch) chemin.append(el("b", "fi-branche", e.branch));
    idz.append(chemin);
  }
  if (d.titre) idz.append(el("div", "fi-titre", d.titre));
  if (!idz.childElementCount) idz.append(el("p", "fi-rien", "identité inconnue"));

  const f = $("#fi-faits");
  f.textContent = "";
  f.append(fait("modèle", e.model));
  f.append(fait("messages", d.messages));
  f.append(fait("conversation", dur(d.duree_s)));
  f.append(fait("transcript", d.taille_ko ? d.taille_ko + " Ko" : null, d.transcript || ""));
  const g = e.git;
  if (g) {
    const bouts = [];
    if (g.fichiers) bouts.push(g.fichiers + " modifié" + (g.fichiers > 1 ? "s" : ""));
    if (g.ahead) bouts.push("↑" + g.ahead + " à pousser");
    if (g.behind) bouts.push("↓" + g.behind + " à récupérer");
    f.append(fait("travail local", bouts.length ? bouts.join(" · ") : "arbre propre"));
  }
  if (e.pr) {
    f.append(fait("pull request", "#" + e.pr.id + " · " +
                  (PR_ETAT[e.pr.etat]?.txt || e.pr.etat).toLowerCase()));
  }
  if (e.ctx_pct != null) {
    const c = el("div");
    c.append(el("b", null, "contexte"), el("span", null, Math.round(e.ctx_pct) + "%"));
    // Le niveau passe en CLASSE et non en fond inline : un style inline gagne
    // toujours contre une règle de classe, même sans !important — la piste
    // teintée « un pas plus clair de la même rampe » serait restée grise.
    const j = el("div", "fi-jauge " + (e.ctx_level || "ok")), i = el("i");
    i.style.width = Math.max(0, Math.min(100, e.ctx_pct)) + "%";
    j.append(i); c.append(j); f.append(c);
  }

  // ── ce qu'il fait maintenant
  const m = $("#fi-maintenant");
  m.textContent = "";
  if (d.erreur) {
    m.append(el("h3", null, "indisponible"), el("p", "fi-rien", d.erreur));
  } else {
    m.append(el("h3", null, "en ce moment"));
    if (d.outil) {
      const o = el("div", "fi-outil");
      o.append(el("b", null, d.outil.nom),
               el("span", null, d.outil.desc || ""),
               el("em", null, "depuis " + dur(d.outil.depuis_s)));
      m.append(o);
    } else if (e.state === "working") {
      m.append(el("p", "fi-rien", "au travail, aucun outil en vol à cet instant"));
    } else {
      // `attente` et non `meta` : la ligne technique de la carte porte
      // désormais le modèle et le coût, qui ne répondent pas à « que se
      // passe-t-il en ce moment ». La fiche affichait « Opus 5 » ici.
      m.append(el("p", "fi-rien", e.attente || "rien en cours"));
    }
  }

  // ── sous-agents
  //
  // QUATRE STATUTS, PAS DEUX. « en cours / fini » ne suffisait pas : un agent
  // tué par une limite de quota se présentait comme un agent qui a réussi, et
  // un agent d'une conversation éteinte comme un agent au travail. Les
  // libellés viennent du serveur (statut), le board n'en déduit aucun.
  //
  // Le TYPE d'agent est affiché à côté de sa description, et c'est le point de
  // cet écran : « difai-core:difai-dev » et « general-purpose » ne se
  // surveillent pas de la même façon, alors que leurs descriptions se
  // ressemblent toutes.
  const a = $("#fi-agents");
  a.textContent = "";
  if ((d.agents || []).length) {
    const total = d.agents_total != null ? d.agents_total : d.agents.length;
    // Le titre répond d'abord à « est-ce que ça travaille pour moi, là ? ».
    a.append(el("h3", null, d.agents_en_cours
      ? "sous-agents — " + d.agents_en_cours + " au travail sur " + total
      : "sous-agents — " + total + ", aucun au travail"));
    for (const g of d.agents) {
      const m = AGENT[g.statut] || AGENT.fini;
      const r = el("div", "fi-agent " + m.cl);
      r.append(el("b", null, m.txt), el("span", null, g.desc));
      // Le type n'est pas toujours transmis : l'appel peut l'omettre, et le
      // serveur ne le devine pas. Pas de ligne fantôme dans ce cas.
      if (g.type) r.append(el("i", "fi-type", g.type));
      r.append(el("em", null, m.quand(g.depuis_s != null ? g.depuis_s : g.duree_s)));
      a.append(r);
    }
    if (total > d.agents.length) {
      a.append(el("p", "fi-rien", "et " + (total - d.agents.length)
                                  + " de plus, déjà terminés"));
    }
  }

  // ── pieds
  //
  // Un bouton éteint doit dire POURQUOI, et pas seulement en infobulle : une
  // infobulle ne se lit qu'à celui qui soupçonne déjà qu'il y a quelque chose à
  // lire. C'est l'aveu que D6 demandait, rendu à l'endroit qu'il concerne.
  const bp = $("#fi-pane");
  const sansPane = e.pane == null;
  bp.disabled = sansPane;
  bp.title = sansPane
    ? "aucun pane suivi pour cette conversation"
    : "focaliser le pane " + e.pane;
  texte($("#fi-note"), sansPane ? "pane non suivi" : "pane " + e.pane);
  const mot = $("#fi-pane-mot");
  mot.hidden = !sansPane;
  if (sansPane) {
    texte(mot, "Bouton éteint : aucun pane n'est suivi pour cette conversation, "
             + "le board ne sait donc pas où elle vit dans le terminal. "
             + "Tout le reste de cette fiche reste juste.");
  }
}

$("#fi-fermer").onclick = () => { dlgFiche.close(); clearTimeout(ficheTimer); };
dlgFiche.addEventListener("close", () => { clearTimeout(ficheTimer); ficheSid = null; });
$("#fi-pane").onclick = () => { if (ficheSid) allerAuPane(ficheSid); };
$("#fi-poubelle").onclick = () => {
  if (!ficheSid) return;
  poste("/api/archiver", { sid: ficheSid });
  const c = $(`.carte[data-sid="${CSS.escape(ficheSid)}"]`);
  if (c) c.remove();
  dlgFiche.close();
};
dlgFiche.addEventListener("click", ev => { if (ev.target === dlgFiche) dlgFiche.close(); });

/* ─────────────────────── avis fugitif ────────────────────────────────────
   Deux usages, et deux seulement :
     · « je n'ai pas fait ce que tu attendais, et voici pourquoi » ;
     · « voici le texte que tu dois lire ou coller » — un chemin, une commande.
   Jamais pour féliciter. Le second usage est né avec les gestes de l'onglet
   Chantier, qui ne font qu'AMENER : ils remettent un chemin ou une commande à
   l'utilisateur au lieu d'agir eux-mêmes. Une confirmation vide (« copié ! »)
   resterait interdite ; ce qui est affiché ici, c'est la charge utile.

   ── POURQUOI `hidden` NE SERT PLUS À CACHER CETTE BOÎTE ──────────────────────
   `#avis` porte `role="status" aria-live="polite"` et le code affirmait ailleurs
   que c'était « le seul role=status PERMANENT de l'écran », donc annoncé de
   façon fiable. C'ÉTAIT FAUX, et c'est mesuré. `hidden` vaut `display:none`, et
   `avis()` posait le contenu PUIS levait `hidden` — la région et son texte
   entraient dans l'arbre d'accessibilité au même instant, exactement le cas
   qu'une région live permanente est censée éviter (WCAG 4.1.3). Dump de l'arbre,
   en Chromium instrumenté, sur les trois techniques :

     display:none (`hidden`)         -> ignored=true, reason "notRendered"
     visibility:hidden               -> ignored=true, reason "notVisible"
     opacity:0 + pointer-events:none -> role=status, ignored=false   ← retenu

   La région est donc posée dans l'arbre UNE FOIS, au chargement, et n'en sort
   plus jamais : seul son TEXTE change ensuite, ce qui est précisément la
   mutation qu'une région live sait annoncer. Les styles de neutralisation sont
   posés en ligne depuis ici plutôt que dans board.css parce qu'ils sont
   indissociables de ce mécanisme-là : c'est du comportement, pas de l'apparence,
   et les séparer permettrait à l'un de partir sans l'autre. La boîte garde
   toutes ses règles de board.css, elle est simplement peinte à alpha zéro.

   Le texte est VIDÉ à l'extinction, et pas seulement rendu transparent : une
   région live qui garde son dernier message laisse un avis d'il y a une heure
   sous le curseur virtuel d'un lecteur d'écran. `aria-relevant` vaut par défaut
   « additions text » ; les lecteurs d'écran courants n'annoncent pas les
   suppressions, et le silence est de toute façon le bon résultat ici. */
let avisTimer = null;
const zoneAvis = $("#avis");
zoneAvis.hidden = false;
zoneAvis.style.opacity = "0";
zoneAvis.style.pointerEvents = "none";

function avis(html) {
  zoneAvis.innerHTML = html;
  zoneAvis.style.opacity = "1";
  zoneAvis.style.pointerEvents = "";
  clearTimeout(avisTimer);
  avisTimer = setTimeout(() => {
    zoneAvis.style.opacity = "0";
    zoneAvis.style.pointerEvents = "none";
    zoneAvis.textContent = "";
  }, 6000);
}

/* ─────────────────────────── aide ────────────────────────────────────────── */
const dlgAide = $("#aide");
$("#btn-aide").onclick = () => dlgAide.showModal();
$("#aide-fermer").onclick = () => dlgAide.close();
// Clic sur le fond = fermeture. `showModal` gère déjà Échap nativement.
for (const d of [dlgAide, $("#nouveau")]) {
  d.addEventListener("click", ev => { if (ev.target === d) d.close(); });
}

// Le filtre rejoue le rendu sur les données déjà en mémoire : aucun appel
// serveur, donc réponse instantanée à la frappe.
$("#h-filtre").addEventListener("input", () => { if (histoData) rendHistorique(histoData); });

/* Les boutons d'aide par onglet mènent tous au MÊME dialogue, sur leur ancre :
   un seul endroit où chercher de l'aide, une seule rédaction à maintenir. */
$("#h-aide").onclick = () => {
  dlgAide.showModal();
  requestAnimationFrame(() =>
    $("#aide-historique")?.scrollIntoView({ block: "start" }));
};

// Le câblage découle de la table : ajouter un onglet ne demande plus de penser
// à trois endroits.
for (const [nom, o] of Object.entries(PANNEAUX)) $(o.onglet).onclick = () => ongler(nom);

// La plaque se pose après le premier rendu, puis à chaque fois que les polices
// arrivent (elles changent la largeur des onglets) et que la piste change de
// taille. Les trois appels sont « secs » : aucun voyage, juste une remesure.
requestAnimationFrame(() => placerCurseur(false));
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => placerCurseur(false)).catch(() => {});
}
if (window.ResizeObserver && $(".onglets")) {
  new ResizeObserver(() => placerCurseur(false)).observe($(".onglets"));
}
setInterval(rendFlux, 1000);       // seule chose qui tourne en local : la fraîcheur
brancherFlux();

/* La pastille « PR à traiter » doit être juste même sans ouvrir l'onglet.
   Une interrogation toutes les 3 minutes suffit : le serveur sert son cache,
   ça ne déclenche aucun appel réseau supplémentaire vers Azure DevOps. */
/* La pastille doit être juste même sans ouvrir l'onglet. Deux pièges corrigés :
   · au premier chargement le cache est vide, le serveur répond a_traiter=0 avec
     rafraichit=true — la pastille affichait donc 0 pendant tout le scan ;
   · 180 s entre deux sondages laissait un chiffre périmé trois minutes durant.
   On suit donc le rafraîchissement quand il tourne, et on sonde plus souvent.
   Le serveur sert son cache : ça ne déclenche aucun appel réseau de plus. */
/* Même exigence pour la pastille Chantier : elle doit être juste sans avoir
   ouvert l'onglet. Le relevé est local (54 ms pour 29 arbres) et le serveur le
   met en cache 30 s, donc un sondage toutes les trois minutes ne coûte
   quasiment rien — et il ne touche à aucun dépôt. */
let sondeChTimer = null;
async function sondeChantier() {
  // Un seul sondage en vol à la fois : le réveil par `appliquer()` peut tomber
  // pendant qu'un rappel de secours court encore, et deux relevés simultanés
  // ne diraient rien de plus.
  clearTimeout(sondeChTimer);
  // On attend le PREMIER instantané. Sans ça, la sonde interrogeait le serveur
  // avant que le flux ait produit quoi que ce soit : le relevé partait sans
  // savoir quels arbres portaient une conversation. Le serveur refuse
  // maintenant de mettre un tel relevé en cache, mais le demander pour rien
  // reste 25 appels git jetés à chaque ouverture de page.
  if (!dernier) {
    // Filet : `appliquer()` nous réveille normalement dès le premier
    // instantané. Ce rappel court ne coûte rien — il ne touche pas au serveur,
    // il relit une variable.
    sondeChTimer = setTimeout(sondeChantier, 200);
    return;
  }
  try {
    const d = await (await fetch("/api/chantier")).json();
    majBadgeChantier(d.a_traiter || 0);
  } catch {}
  sondeChTimer = setTimeout(sondeChantier, 180000);
}
sondeChantier();

/* La pastille Projets a disparu avec son onglet le 02/09 : ce qu'elle comptait
   — les choses qui t'attendent — est maintenant DANS le bandeau, nommé, au lieu
   d'être un nombre sur un onglet qu'il fallait ouvrir pour le comprendre. Voir
   server/attente.py. La sonde du chantier, elle, reste : elle réchauffe le
   cache que le bandeau lit à chaque instantané. */

let sondeTimer = null;
async function sondePR() {
  let d;
  try { d = await (await fetch("/api/pr")).json(); } catch { return; }
  majBadgePR(d.a_traiter || 0, d);
  clearTimeout(sondeTimer);
  sondeTimer = setTimeout(sondePR, d.rafraichit ? 3000 : 60000);
}
sondePR();
