"""
=============================================================================
 HYMPYR ÉNERGIES — Radar Territorial
 Extraction géolocalisée des contacts de collectivités (mairies / EPCI)
 et recensement des comités des fêtes dans un rayon défini.

 Sources 100 % officielles :
   - BAN (api-adresse.data.gouv.fr)          → géocodage de l'adresse centre
   - API Découpage administratif (geo.api.gouv.fr) → communes + centroïdes
   - API Annuaire de l'administration (DILA / Service-public.gouv.fr)
                                             → mairies, EPCI : mail + tél
   - API Recherche d'entreprises (DINUM)     → associations (RNA / Sirene)

 AVERTISSEMENT MÉTIER :
   L'open data français ne publie AUCUN email ni téléphone pour les
   associations loi 1901. Les comités des fêtes sont donc restitués sans
   contact direct, rattachés à la mairie de leur commune (canal réel).

 Auteur : Q. Debrie — Responsable Digital
 Fichier unique déployable sur Streamlit Community Cloud.
=============================================================================
"""

from __future__ import annotations

import io
import json
import math
import time
import unicodedata
from datetime import datetime
from typing import Any, Iterable

import pandas as pd
import requests
import streamlit as st

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

APP_NAME = "Scrapping données mairies - Deldossi Assainissement"
APP_VERSION = "1.0"
CONTACT_TECHNIQUE = "digital@hympyr.fr"  # ← à adapter : utilisé dans le User-Agent

VERT_HYMPYR = "#1A9E68"
VERT_FONCE = "#073D27"

# Domaines de l'API Annuaire (bascule automatique si le premier échoue)
ANNUAIRE_HOSTS = [
    "https://api-lannuaire.service-public.gouv.fr",
    "https://api-lannuaire.service-public.fr",
]
ANNUAIRE_DATASET = "api-lannuaire-administration"

URL_BAN = "https://api-adresse.data.gouv.fr/search/"
URL_GEO_COMMUNES = "https://geo.api.gouv.fr/departements/{dep}/communes"
URL_RECHERCHE_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"

# Départements couverts par Hympyr (présélection)
DEPTS_HYMPYR = ["31", "81", "82", "11", "65"]

DEPTS_FRANCE = (
    [f"{i:02d}" for i in range(1, 20)]
    + ["2A", "2B"]
    + [f"{i:02d}" for i in range(21, 96)]
    + ["971", "972", "973", "974", "976"]
)

# Valeurs du champ "pivot" de l'Annuaire (type de service local).
# Ajustables depuis l'interface si la DILA fait évoluer sa nomenclature.
PIVOTS_DEFAUT = {
    "Mairies": ["mairie"],
    "EPCI / Communautés de communes": ["epci"],
}

# Mots-clés de détection des comités des fêtes dans les raisons sociales
MOTS_CLES_COMITES = [
    "comite des fetes",
    "comite de fetes",
    "comite d animation",
    "comite fetes",
    "fetes et animations",
    "animation et fetes",
    "festivites",
    "comite des festivites",
]

TIMEOUT = 20
PAUSE_RECHERCHE_ENTREPRISES = 0.18  # ~5,5 req/s — marge sous la limite de l'API
MAX_COMMUNES_ASSOS = 400  # garde-fou anti-abus


# =============================================================================
# 2. UTILITAIRES BAS NIVEAU
# =============================================================================


def session_http() -> requests.Session:
    """Session HTTP avec User-Agent explicite (les WAF publics rejettent
    fréquemment le User-Agent par défaut de requests → erreurs 403)."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": f"Hympyr-{APP_NAME}/{APP_VERSION} (+{CONTACT_TECHNIQUE})",
            "Accept": "application/json",
        }
    )
    return s


HTTP = session_http()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en kilomètres."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def sans_accents(txt: str) -> str:
    """Minuscules, sans accents, ponctuation remplacée par des espaces.

    La normalisation de la ponctuation est indispensable : sans elle,
    « Comité d'animation » ne correspond pas au motif « comite d animation ».
    """
    if not txt:
        return ""
    n = unicodedata.normalize("NFD", str(txt))
    base = "".join(c for c in n if unicodedata.category(c) != "Mn").lower()
    nettoye = "".join(c if (c.isalnum() or c.isspace()) else " " for c in base)
    return " ".join(nettoye.split())


def aplatir(valeur: Any, cles_preferees: Iterable[str] = ("valeur", "value", "email", "adresse")) -> str:
    """Normalise les structures hétérogènes de l'API Annuaire.

    Le champ `telephone` peut être une liste de dicts {valeur, description},
    `adresse_courriel` une chaîne, une liste de chaînes, ou une chaîne
    contenant du JSON. Cette fonction absorbe tous ces cas.
    """
    if valeur is None:
        return ""

    # Chaîne susceptible de contenir du JSON sérialisé
    if isinstance(valeur, str):
        v = valeur.strip()
        if v.startswith("[") or v.startswith("{"):
            try:
                return aplatir(json.loads(v), cles_preferees)
            except (json.JSONDecodeError, ValueError):
                return v
        return v

    if isinstance(valeur, dict):
        for cle in cles_preferees:
            if valeur.get(cle):
                return str(valeur[cle]).strip()
        # Dernier recours : première valeur scalaire non vide
        for v in valeur.values():
            if isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()
        return ""

    if isinstance(valeur, (list, tuple)):
        morceaux = [aplatir(v, cles_preferees) for v in valeur]
        return " | ".join(m for m in morceaux if m)

    return str(valeur).strip()


def premier_champ(rec: dict, *noms: str) -> Any:
    """Retourne la première clé existante et non vide parmi `noms`."""
    for n in noms:
        if n in rec and rec[n] not in (None, "", [], {}):
            return rec[n]
    return None


# =============================================================================
# 3. APPELS API (mis en cache)
# =============================================================================


@st.cache_data(ttl=3600, show_spinner=False)
def geocoder(adresse: str, limite: int = 5) -> list[dict]:
    """Géocodage via la Base Adresse Nationale."""
    try:
        r = HTTP.get(
            URL_BAN,
            params={"q": adresse, "limit": limite, "autocomplete": 0},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        feats = r.json().get("features", [])
    except (requests.RequestException, ValueError) as e:
        st.error(f"Géocodage indisponible : {e}")
        return []

    sorties = []
    for f in feats:
        p = f.get("properties", {})
        lon, lat = f.get("geometry", {}).get("coordinates", [None, None])
        if lat is None:
            continue
        code_insee = p.get("citycode", "")
        sorties.append(
            {
                "label": p.get("label", ""),
                "lat": lat,
                "lon": lon,
                "code_insee": code_insee,
                "departement": code_insee[:3] if code_insee.startswith("97") else code_insee[:2],
                "commune": p.get("city", ""),
            }
        )
    return sorties


@st.cache_data(ttl=86400, show_spinner=False)
def communes_departement(dep: str) -> list[dict]:
    """Liste des communes d'un département avec leur centroïde."""
    try:
        r = HTTP.get(
            URL_GEO_COMMUNES.format(dep=dep),
            params={
                "fields": "nom,code,centre,population,codesPostaux,codeDepartement",
                "format": "json",
                "geometry": "centre",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    out = []
    for c in data:
        centre = c.get("centre") or {}
        coords = centre.get("coordinates") or []
        if len(coords) != 2:
            continue
        cps = c.get("codesPostaux") or []
        out.append(
            {
                "code_insee": c.get("code", ""),
                "commune": c.get("nom", ""),
                "departement": c.get("codeDepartement", dep),
                "population": c.get("population") or 0,
                "code_postal": cps[0] if cps else "",
                "lat": coords[1],
                "lon": coords[0],
            }
        )
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def annuaire_lot(codes_insee: tuple[str, ...], pivot: str) -> tuple[list[dict], str | None]:
    """Interroge l'API Annuaire pour un lot de communes et un type de service.

    Retourne (enregistrements, message_erreur).
    Stratégie défensive : si la clause d'égalité ne renvoie rien, on retente
    avec un LIKE (le champ code_insee_commune peut être multivalué).
    """
    if not codes_insee:
        return [], None

    clauses_eq = " OR ".join(f'code_insee_commune="{c}"' for c in codes_insee)
    clauses_like = " OR ".join(f'code_insee_commune LIKE "{c}"' for c in codes_insee)

    derniere_erreur: str | None = None

    for clause in (clauses_eq, clauses_like):
        where = f'pivot LIKE "{pivot}" AND ({clause})'
        for host in ANNUAIRE_HOSTS:
            url = f"{host}/api/explore/v2.1/catalog/datasets/{ANNUAIRE_DATASET}/records"
            resultats: list[dict] = []
            offset = 0
            try:
                while True:
                    r = HTTP.get(
                        url,
                        params={"where": where, "limit": 100, "offset": offset},
                        timeout=TIMEOUT,
                    )
                    if r.status_code == 403:
                        derniere_erreur = (
                            f"403 sur {host} — accès automatisé refusé (WAF). "
                            "Vérifier le User-Agent ou contacter donnees-dila@dila.gouv.fr."
                        )
                        break
                    r.raise_for_status()
                    payload = r.json()
                    lot = payload.get("results", [])
                    resultats.extend(lot)
                    total = payload.get("total_count", len(resultats))
                    offset += 100
                    if len(lot) < 100 or offset >= min(total, 10000):
                        break
                if resultats:
                    return resultats, None
            except (requests.RequestException, ValueError) as e:
                derniere_erreur = f"{host} : {e}"
                continue
    return [], derniere_erreur


@st.cache_data(ttl=3600, show_spinner=False)
def associations_commune(code_insee: str, motif: str = "comité des fêtes") -> list[dict]:
    """Recherche les associations d'une commune via l'API Recherche d'entreprises."""
    try:
        r = HTTP.get(
            URL_RECHERCHE_ENTREPRISES,
            params={
                "q": motif,
                "code_commune": code_insee,
                "per_page": 25,
                "page": 1,
                "etat_administratif": "A",
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            time.sleep(1.5)
            r = HTTP.get(
                URL_RECHERCHE_ENTREPRISES,
                params={"q": motif, "code_commune": code_insee, "per_page": 25, "page": 1},
                timeout=TIMEOUT,
            )
        r.raise_for_status()
        return r.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []


# =============================================================================
# 4. TRANSFORMATIONS MÉTIER
# =============================================================================


def communes_dans_rayon(deps: list[str], lat: float, lon: float, rayon_km: float) -> pd.DataFrame:
    lignes: list[dict] = []
    for dep in deps:
        for c in communes_departement(dep):
            d = haversine_km(lat, lon, c["lat"], c["lon"])
            if d <= rayon_km:
                lignes.append({**c, "distance_km": round(d, 1)})
    if not lignes:
        return pd.DataFrame(
            columns=["code_insee", "commune", "departement", "population",
                     "code_postal", "lat", "lon", "distance_km"]
        )
    return pd.DataFrame(lignes).sort_values("distance_km").reset_index(drop=True)


def normaliser_collectivite(rec: dict, ref_communes: dict[str, dict]) -> dict:
    """Mappe un enregistrement brut de l'Annuaire vers une ligne exploitable."""
    code_insee = aplatir(premier_champ(rec, "code_insee_commune", "code_insee")).split(" | ")[0]
    ref = ref_communes.get(code_insee, {})

    adresse_brute = premier_champ(rec, "adresse", "adresse_postale")
    adresse = ""
    cp = ""
    ville = ""
    if isinstance(adresse_brute, str) and adresse_brute.strip().startswith("["):
        try:
            adresse_brute = json.loads(adresse_brute)
        except (json.JSONDecodeError, ValueError):
            pass
    if isinstance(adresse_brute, list) and adresse_brute:
        adresse_brute = adresse_brute[0]
    if isinstance(adresse_brute, dict):
        parts = [adresse_brute.get(k, "") for k in ("numero_voie", "complement1", "complement2")]
        adresse = " ".join(p for p in parts if p).strip()
        cp = str(adresse_brute.get("code_postal", "") or "")
        ville = adresse_brute.get("nom_commune", "") or ""
    else:
        adresse = aplatir(adresse_brute)

    lat = premier_champ(rec, "latitude", "lat")
    lon = premier_champ(rec, "longitude", "long", "lon")

    return {
        "type": aplatir(premier_champ(rec, "pivot")) or "",
        "nom": aplatir(premier_champ(rec, "nom", "nom_service", "libelle")),
        "commune": ville or ref.get("commune", ""),
        "code_insee": code_insee,
        "code_postal": cp or ref.get("code_postal", ""),
        "departement": ref.get("departement", ""),
        "email": aplatir(premier_champ(rec, "adresse_courriel", "courriel", "email")),
        "telephone": aplatir(premier_champ(rec, "telephone", "telephone_accessible")),
        "site_web": aplatir(premier_champ(rec, "site_internet", "url", "site_web"), ("valeur", "libelle")),
        "adresse": adresse,
        "population": ref.get("population", 0),
        "distance_km": ref.get("distance_km", None),
        "lat": float(lat) if lat not in (None, "") else ref.get("lat"),
        "lon": float(lon) if lon not in (None, "") else ref.get("lon"),
        "id_source": aplatir(premier_champ(rec, "id", "identifiant")),
    }


def est_comite_des_fetes(nom: str, mots_cles: list[str]) -> bool:
    n = sans_accents(nom)
    return any(mc in n for mc in mots_cles)


def normaliser_association(rec: dict, ref: dict, contact_mairie: dict) -> dict:
    siege = rec.get("siege") or {}
    dirigeants = rec.get("dirigeants") or []
    noms_dirigeants = ", ".join(
        f"{d.get('prenoms', '')} {d.get('nom', '')}".strip()
        for d in dirigeants
        if d.get("nom")
    )
    return {
        "nom_association": rec.get("nom_complet") or rec.get("nom_raison_sociale") or "",
        "commune": siege.get("libelle_commune") or ref.get("commune", ""),
        "code_insee": ref.get("code_insee", ""),
        "code_postal": siege.get("code_postal", "") or ref.get("code_postal", ""),
        "adresse_siege": siege.get("adresse", ""),
        "date_creation": rec.get("date_creation", ""),
        "dirigeants": noms_dirigeants,
        "siren": rec.get("siren", ""),
        "distance_km": ref.get("distance_km", None),
        # Canal de contact réel : la mairie de rattachement
        "mairie_rattachement": contact_mairie.get("nom", ""),
        "mairie_email": contact_mairie.get("email", ""),
        "mairie_telephone": contact_mairie.get("telephone", ""),
        "contact_direct_association": "NON DISPONIBLE (absent de l'open data)",
        "lat": siege.get("latitude") or ref.get("lat"),
        "lon": siege.get("longitude") or ref.get("lon"),
    }


# =============================================================================
# 5. EXPORT
# =============================================================================


def construire_xlsx(df_coll: pd.DataFrame, df_asso: pd.DataFrame, journal: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        (df_coll if not df_coll.empty else pd.DataFrame({"info": ["Aucun résultat"]})).to_excel(
            writer, sheet_name="Collectivites", index=False
        )
        (df_asso if not df_asso.empty else pd.DataFrame({"info": ["Aucun résultat"]})).to_excel(
            writer, sheet_name="Comites_des_fetes", index=False
        )
        journal.to_excel(writer, sheet_name="Journal_RGPD", index=False)
    return buffer.getvalue()


# =============================================================================
# 6. INTERFACE
# =============================================================================

st.set_page_config(page_title=f"Hympyr — {APP_NAME}", page_icon="📍", layout="wide")

st.markdown(
    f"""
    <style>
      .hym-header {{
        background: linear-gradient(90deg, {VERT_FONCE} 0%, {VERT_HYMPYR} 100%);
        padding: 1.1rem 1.4rem; border-radius: 10px; color: #fff; margin-bottom: 1rem;
      }}
      .hym-header h1 {{ margin: 0; font-size: 1.5rem; font-weight: 700; }}
      .hym-header p  {{ margin: .25rem 0 0; opacity: .88; font-size: .9rem; }}
      div[data-testid="stMetricValue"] {{ font-size: 1.6rem; }}
    </style>
    <div class="hym-header">
      <h1>{APP_NAME}</h1>
      <p>Contacts extraits de la base de données du Gouvernement via l'API Annuaire de l'admnistration des services publics.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if "journal" not in st.session_state:
    st.session_state.journal = []

# ---------------------------------------------------------------- Paramètres
with st.sidebar:
    st.subheader("Paramètres d'extraction")

    adresse_saisie = st.text_input(
        "Adresse centre du rayon",
        placeholder="ex : 12 avenue de Toulouse, 81100 Castres",
        help="Géocodage via la Base Adresse Nationale.",
    )
    rayon_km = st.slider("Rayon (km)", 5, 80, 25, step=5)

    st.markdown("**Cibles**")
    cible_mairies = st.checkbox("Mairies", value=True)
    cible_epci = st.checkbox("EPCI / Communautés de communes", value=True)
    cible_comites = st.checkbox("Comités des fêtes (sans contact direct)", value=True)

    st.divider()
    st.markdown("**Départements à scanner**")
    st.caption(
        "Le rayon reste la contrainte réelle. Les départements bornent seulement "
        "la zone de chargement : si le cercle franchit une limite, ajoutez le voisin."
    )
    deps_manuels = st.multiselect(
        "Départements", DEPTS_FRANCE, default=DEPTS_HYMPYR, label_visibility="collapsed"
    )

    st.divider()
    operateur = st.text_input("Opérateur (journal RGPD)", value="", placeholder="Prénom Nom")
    finalite = st.selectbox(
        "Finalité déclarée",
        ["Prospection commerciale B2B", "Étude de marché / cartographie", "Mise à jour base clients"],
    )

    with st.expander("⚙️ Réglages avancés"):
        pivots_mairie = st.text_input("Valeur pivot — mairies", value="mairie")
        pivots_epci = st.text_input("Valeur pivot — EPCI", value="epci")
        mots_cles_txt = st.text_area(
            "Mots-clés comités des fêtes",
            value="\n".join(MOTS_CLES_COMITES),
            height=140,
            help="Un motif par ligne, sans accent, en minuscules.",
        )
        taille_lot = st.number_input("Communes par requête Annuaire", 10, 80, 40, step=5)

    lancer = st.button("🔎 Lancer l'extraction", type="primary", use_container_width=True)

# ---------------------------------------------------------------- Exécution
if lancer:
    if not adresse_saisie.strip():
        st.error("Saisissez une adresse centre.")
        st.stop()
    if not deps_manuels:
        st.error("Sélectionnez au moins un département à scanner.")
        st.stop()
    if not operateur.strip():
        st.error("Renseignez l'opérateur : chaque extraction est journalisée (obligation RGPD).")
        st.stop()

    candidats = geocoder(adresse_saisie)
    if not candidats:
        st.error("Adresse introuvable dans la Base Adresse Nationale.")
        st.stop()

    centre = candidats[0]
    st.success(f"Centre retenu : **{centre['label']}** ({centre['lat']:.5f}, {centre['lon']:.5f})")

    deps = sorted(set(deps_manuels) | {centre["departement"]})

    with st.spinner("Découpage administratif…"):
        df_communes = communes_dans_rayon(deps, centre["lat"], centre["lon"], rayon_km)

    if df_communes.empty:
        st.warning("Aucune commune dans ce rayon. Élargissez le rayon ou ajoutez des départements.")
        st.stop()

    ref_communes = df_communes.set_index("code_insee").to_dict("index")
    for k, v in ref_communes.items():
        v["code_insee"] = k
    codes = list(df_communes["code_insee"])

    st.caption(f"{len(codes)} communes dans le rayon · départements scannés : {', '.join(deps)}")

    # ---- Collectivités
    lignes_coll: list[dict] = []
    erreurs: list[str] = []
    bruts: list[dict] = []

    pivots_actifs = []
    if cible_mairies:
        pivots_actifs.append(pivots_mairie.strip())
    if cible_epci:
        pivots_actifs.append(pivots_epci.strip())

    if pivots_actifs:
        lots = [
            tuple(codes[i : i + int(taille_lot)])
            for i in range(0, len(codes), int(taille_lot))
        ]
        total_appels = len(lots) * len(pivots_actifs)
        barre = st.progress(0.0, text="Interrogation de l'Annuaire de l'administration…")
        n = 0
        for pivot in pivots_actifs:
            for lot in lots:
                recs, err = annuaire_lot(lot, pivot)
                if err:
                    erreurs.append(err)
                bruts.extend(recs[:1])
                for rec in recs:
                    lignes_coll.append(normaliser_collectivite(rec, ref_communes))
                n += 1
                barre.progress(n / total_appels, text=f"Annuaire — lot {n}/{total_appels}")
        barre.empty()

    df_coll = pd.DataFrame(lignes_coll)
    if not df_coll.empty:
        # Dédoublonnage sur un tuple métier : un id_source vide ne doit jamais
        # écraser l'ensemble des lignes.
        df_coll = df_coll.drop_duplicates(subset=["id_source", "nom", "code_insee", "type"])
        df_coll = df_coll.sort_values("distance_km", na_position="last").reset_index(drop=True)

    # ---- Comités des fêtes
    df_asso = pd.DataFrame()
    if cible_comites:
        mots_cles = [sans_accents(m).strip() for m in mots_cles_txt.splitlines() if m.strip()]
        cibles = codes[:MAX_COMMUNES_ASSOS]
        if len(codes) > MAX_COMMUNES_ASSOS:
            st.warning(
                f"Recherche associative limitée aux {MAX_COMMUNES_ASSOS} communes les plus proches "
                f"({len(codes)} dans le rayon) pour préserver l'API publique."
            )

        contacts_mairies: dict[str, dict] = {}
        if not df_coll.empty:
            for _, r in df_coll.iterrows():
                if sans_accents(str(r.get("type", ""))).find("mairie") >= 0:
                    contacts_mairies.setdefault(r["code_insee"], r.to_dict())

        lignes_asso: list[dict] = []
        barre2 = st.progress(0.0, text="Recensement des comités des fêtes…")
        for i, code in enumerate(cibles, start=1):
            for rec in associations_commune(code):
                nom = rec.get("nom_complet") or rec.get("nom_raison_sociale") or ""
                if est_comite_des_fetes(nom, mots_cles):
                    lignes_asso.append(
                        normaliser_association(
                            rec, ref_communes.get(code, {}), contacts_mairies.get(code, {})
                        )
                    )
            time.sleep(PAUSE_RECHERCHE_ENTREPRISES)
            barre2.progress(i / len(cibles), text=f"Associations — {i}/{len(cibles)} communes")
        barre2.empty()

        df_asso = pd.DataFrame(lignes_asso)
        if not df_asso.empty:
            df_asso = (
                df_asso.drop_duplicates(subset=["siren", "nom_association"])
                .sort_values("distance_km", na_position="last")
                .reset_index(drop=True)
            )

    # ---- Journal RGPD
    st.session_state.journal.append(
        {
            "horodatage": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operateur": operateur.strip(),
            "finalite": finalite,
            "adresse_centre": centre["label"],
            "rayon_km": rayon_km,
            "departements": ", ".join(deps),
            "communes_scannees": len(codes),
            "collectivites_extraites": len(df_coll),
            "associations_extraites": len(df_asso),
        }
    )

    st.session_state.resultats = {
        "coll": df_coll,
        "asso": df_asso,
        "communes": df_communes,
        "centre": centre,
        "erreurs": erreurs,
        "bruts": bruts,
    }

# ---------------------------------------------------------------- Résultats
res = st.session_state.get("resultats")

if res:
    df_coll: pd.DataFrame = res["coll"]
    df_asso: pd.DataFrame = res["asso"]
    df_communes: pd.DataFrame = res["communes"]

    nb_mail = int((df_coll["email"].astype(str).str.contains("@")).sum()) if not df_coll.empty else 0
    nb_tel = int((df_coll["telephone"].astype(str).str.len() > 5).sum()) if not df_coll.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Communes dans le rayon", len(df_communes))
    c2.metric("Collectivités", len(df_coll))
    c3.metric("Emails exploitables", nb_mail)
    c4.metric("Comités des fêtes", len(df_asso))

    if res["erreurs"]:
        with st.expander("⚠️ Incidents API", expanded=True):
            for e in dict.fromkeys(res["erreurs"]):
                st.warning(e)

    onglets = st.tabs(
        ["🏛️ Collectivités", "🎪 Comités des fêtes", "🗺️ Carte", "⬇️ Export", "🔧 Diagnostic"]
    )

    with onglets[0]:
        if df_coll.empty:
            st.info("Aucune collectivité retournée. Vérifiez les valeurs `pivot` dans les réglages avancés.")
        else:
            f1, f2 = st.columns([1, 2])
            avec_mail = f1.checkbox("Uniquement avec email", value=False)
            recherche = f2.text_input("Filtrer (nom ou commune)", "")
            vue = df_coll.copy()
            if avec_mail:
                vue = vue[vue["email"].astype(str).str.contains("@")]
            if recherche:
                m = sans_accents(recherche)
                vue = vue[
                    vue["nom"].map(sans_accents).str.contains(m, regex=False)
                    | vue["commune"].map(sans_accents).str.contains(m, regex=False)
                ]
            st.dataframe(
                vue[["nom", "type", "commune", "code_postal", "email", "telephone",
                     "site_web", "population", "distance_km"]],
                use_container_width=True,
                hide_index=True,
            )

    with onglets[1]:
        if df_asso.empty:
            st.info("Aucun comité des fêtes identifié sur ce périmètre.")
        else:
            st.caption(
                "Rappel : aucun contact direct n'existe en open data. La colonne "
                "`mairie_email` est le point d'entrée opérationnel."
            )
            st.dataframe(
                df_asso[["nom_association", "commune", "code_postal", "adresse_siege",
                         "date_creation", "mairie_email", "mairie_telephone", "distance_km"]],
                use_container_width=True,
                hide_index=True,
            )

    with onglets[2]:
        pts = []
        if not df_coll.empty:
            pts.append(df_coll[["lat", "lon"]].dropna())
        if not df_asso.empty:
            pts.append(df_asso[["lat", "lon"]].dropna())
        if pts:
            st.map(pd.concat(pts).astype(float), zoom=8)
        else:
            st.info("Pas de coordonnées disponibles.")

    with onglets[3]:
        journal = pd.DataFrame(st.session_state.journal)
        xlsx = construire_xlsx(df_coll, df_asso, journal)
        horo = datetime.now().strftime("%Y%m%d_%H%M")
        st.download_button(
            "📊 Télécharger le classeur Excel (3 onglets)",
            data=xlsx,
            file_name=f"hympyr_radar_territorial_{horo}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        if not df_coll.empty:
            st.download_button(
                "📄 Collectivités (CSV)",
                data=df_coll.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name=f"collectivites_{horo}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        st.markdown("**Journal des extractions (session courante)**")
        st.dataframe(journal, use_container_width=True, hide_index=True)

    with onglets[4]:
        st.markdown("**Schéma brut du premier enregistrement Annuaire**")
        st.caption(
            "Sert à recaler les noms de champs si la DILA fait évoluer son modèle. "
            "Les fonctions `premier_champ()` et `normaliser_collectivite()` sont les "
            "seuls points à modifier."
        )
        if res["bruts"]:
            st.json(res["bruts"][0], expanded=False)
            st.code(", ".join(sorted(res["bruts"][0].keys())), language="text")
        else:
            st.info("Aucun enregistrement brut disponible.")

else:
    st.markdown(
        """
        ### Mode d'emploi
        1. Saisissez l'**adresse centre** (siège, dépôt, ou point de livraison prospecté).
        2. Réglez le **rayon** et cochez les cibles.
        3. Vérifiez que les **départements scannés** couvrent le cercle.
        4. Renseignez l'**opérateur** — chaque extraction est journalisée.
    )
