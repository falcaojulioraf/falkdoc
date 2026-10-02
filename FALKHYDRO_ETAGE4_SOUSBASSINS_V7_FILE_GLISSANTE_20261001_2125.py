#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FALKHYDRO+ — ÉTAGE 4 (SWAT+) : LIVRAISON 1 — sous-bassins, réseau hydrographique, météo PRÉPARÉE (V1)
=====================================================================================================
Module AUTONOME (testable seul, à déposer ensuite dans extensions/ où il devient un onglet).
Périmètre de CETTE livraison (et seulement) : (a) délimitation des sous-bassins et du réseau à partir du MNT SRTM 30 m DÉJÀ présent dans le cache de l'étage 2 et du contour du bassin de l'étage 2 ;
(b) statistiques par sous-bassin, dont A de l'étage 2 en LECTURE SEULE ; (c) mécanisme de téléchargement météo (CHIRPS + NASA POWER) construit mais JAMAIS déclenché sans clic confirmé.
PAS ici : PET, modèle SWAT+, bilan hydrique, recharge, débits, HEC-HMS.

RÈGLES
  - Étages 1, 2 et 3 en LECTURE SEULE : aucun fichier de Etages/01_envi, 02_rusle, 03_fokontany n'est écrit, renommé ni supprimé. Amont modifié ⇒ message dans le journal (« résultat périmé »),
    recalcul seulement sur clic conscient. Sorties dans <racine>/Etages/04_swat/<bassin>/ (noms stables ; fichier verrouillé ⇒ nom horodaté).
  - API QGIS sur le thread principal seulement ; calcul en thread (GDAL / OGR / NumPy ; TauDEM par sous-processus). Aucune dépendance à GRASS, aucun module à installer.
  - Toute valeur non sourcée est marquée « HYPOTHÈSE — à valider par l'utilisateur » (seuil du réseau, traitement du lac, exutoire principal, seuil des petits sous-bassins, estimations météo).

VOIES (voir « Inspecter QSWAT+ / TauDEM ») : A = TauDEM (pitremove, d8flowdir, aread8) par sous-processus puis réseau et sous-bassins ici ; C = NumPy (comblement des cuvettes, D8) ; B (piloter QSWAT+) non construite :
l'API de QSWAT+ n'a pas été inspectée. Les noms du module sont préfixés FH4_ / fh4_ / _fh4_.
"""

import os
import json
import time
import math
import glob
import csv
import re
import shutil
import traceback

from qgis.core import (QgsProject, QgsVectorLayer, QgsSettings,
                       QgsCoordinateReferenceSystem, QgsRectangle,
                       QgsPrintLayout, QgsLayoutItemMap, QgsLayoutItemLabel, QgsLayoutItemPicture,
                       QgsLayoutItemScaleBar, QgsLayoutPoint, QgsLayoutSize, QgsUnitTypes,
                       QgsLayoutExporter, QgsLayoutItemPage)
from qgis.PyQt.QtGui import QFont, QColor
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal

FH4_VERSION = "V3.3"
FH4_EXTENSION = {"nom": "Sous-bassins", "etage": 4, "description": "Sous-bassins, réseau hydrographique et statistiques (MNT SRTM + A de l'étage 2 en lecture seule) ; météo préparée"}

# ============================================================
# ⚙ RÉGLAGES (toute valeur non sourcée est une HYPOTHÈSE à valider)
# ============================================================
FH4_CRS = 32738                                   # UTM 38S
FH4_STEP_M = 30.0                                 # pas de la grille de calcul = résolution du SRTM
FH4_MARGE_M = 1000.0                              # marge autour du bassin pour recadrer le MNT
FH4_SEUILS = (("Fin (beaucoup de sous-bassins)", "fin", 0.5), ("Intermédiaire", "moyen", 1.5), ("Grossier (peu de sous-bassins)", "grossier", 5.0))   # HYPOTHÈSE : % de la surface du bassin
FH4_SEUILS = tuple((c, l, p) for l, c, p in FH4_SEUILS)
FH4_DEFAUT = 1                                    # seuil intermédiaire par défaut (HYPOTHÈSE)
FH4_PETIT_SB_HA = 50.0                            # HYPOTHÈSE : « très petit sous-bassin » si < 50 ha
FH4_RETRY_PAUSE = 0.3
FH4_OUTLET_TOL_M = 150.0                          # HYPOTHÈSE : écart d'exutoire jugé notable au-delà de 150 m (5 pixels)
FH4_PHRASE_A = "A = perte de sol potentielle brute, non validée, pas un apport de sédiments au lac."
FH4_PIED_CARTE = ("A = perte de sol POTENTIELLE BRUTE sur versant, non validée, pas un apport de sédiments au lac. Sous-bassins issus du MNT SRTM 30 m (erreurs de quelques mètres) et d'un seuil de réseau "
                  "choisi (HYPOTHÈSE) ; lac traité par un choix à valider. Aucun résultat hydrologique (débit, bilan) n'est produit ici.")
FH4_LIMITES = [
    "Le nombre et la forme des sous-bassins dépendent du MNT (SRTM 30 m, erreurs verticales de quelques mètres) et du seuil de définition du réseau (HYPOTHÈSE, à valider par l'utilisateur).",
    "Le lac Itasy est plat dans le SRTM : il est traité par le seul gradient imposé au comblement des cuvettes (HYPOTHÈSE, non forcé ; à valider ; QSWAT+ / SWAT+ le traiteront comme plan d'eau selon sa propre logique, non inspectée).",
    "L'exutoire principal est la sortie à plus forte aire drainée du contour du bassin (HYPOTHÈSE) ; il n'est déplacé qu'avec confirmation.",
    "A reste une perte potentielle brute non validée, reprise de l'étage 2 en lecture seule (aucun recalcul) ; ce n'est pas un apport de sédiments au lac.",
    "Voie A (TauDEM) non testée avec les vrais exécutables dans cette livraison ; voie C (NumPy) moins éprouvée.",
    "Aucun résultat hydrologique (débit, bilan hydrique, recharge) n'est produit ici ; la météo n'est que préparée (rien n'est converti au format SWAT+).",
]
FH4_LAYOUT_FORMATS = {"A4": (210, 297), "A3": (297, 420), "A0": (841, 1189)}
_FH4_SHEET = {"n": 0}
_FH4_NOM_SC = {"P_Itasy": "P Itasy", "P_ref": "P = 1"}
FH4_CLASSES_A = [("Faible", 0.0, 5.0, "#66bd63"), ("Moyenne", 5.0, 15.0, "#fee08b"), ("Forte", 15.0, 50.0, "#f46d43"), ("Très forte", 50.0, None, "#a50026")]
FH4_QUANTILE_COLORS = ("#ffffb2", "#fed976", "#feb24c", "#fd8d3c", "#e31a1c", "#800026")


class Fh4Error(Exception):
    """Erreur claire destinée à l'utilisateur (rien n'est inventé)."""


class Fh4Cancelled(Exception):
    pass


def fh4_dirs(root, basin):
    j = os.path.join
    return {"e1": j(root, "Etages", "01_envi", basin), "e2": j(root, "Etages", "02_rusle", basin), "e4": j(root, "Etages", "04_swat", basin)}


def fh4_cache_root(e4_dir):
    """Cache commun de l'étage 4 ; conserve le dossier V4 s'il existe afin de reprendre ses .part."""
    legacy = os.path.join(e4_dir, "meteo_cache")
    return legacy if os.path.isdir(legacy) else os.path.join(e4_dir, "cache")


# ============================================================
# 1. OUTILS : formats, écriture sûre (noms stables ; verrouillé → nom horodaté)
# ============================================================
def fh4_fr(x, nd=1, milliers=True):
    """Nombre au format français (virgule décimale, espace pour les milliers) ; NaN / None → « — »."""
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "—"
        s = "{:,.{}f}".format(float(x), nd)
    except Exception:
        return "—"
    if milliers:
        return s.replace(",", " ").replace(".", ",")
    return s.replace(",", "").replace(".", ",")


def fh4_csvnum(x, nd=4):
    """Nombre pour CSV (séparateur « ; », virgule décimale, pas de milliers) ; vide si inconnu."""
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return ""
        return "{:.{}f}".format(float(x), nd).replace(".", ",")
    except Exception:
        return ""


def fh4_iso(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def fh4_tmp_path(path):
    root, ext = os.path.splitext(path)
    return root + ".tmp" + ext


def fh4_stamped(path):
    root, ext = os.path.splitext(path)
    return "{}_{}{}".format(root, time.strftime("%Y%m%d_%H%M%S"), ext)


def fh4_commit(tmp, final, log=None):
    """Remplace « final » par « tmp » (os.replace, atomique). Fichier verrouillé (3 essais) : le nouveau contenu va sous un nom HORODATÉ
    (…_AAAAMMJJ_HHMMSS) et on le dit ; le fichier verrouillé n'est jamais touché. Renvoie le chemin réellement écrit."""
    last = None
    for i in range(3):
        try:
            os.replace(tmp, final)
            return final
        except OSError as ex:
            last = ex
            if not os.path.exists(tmp):
                raise
            if i < 2:
                time.sleep(FH4_RETRY_PAUSE * (i + 1))
    alt = fh4_stamped(final)
    n = 1
    while os.path.exists(alt):
        n += 1
        root, ext = os.path.splitext(fh4_stamped(final))
        alt = "{}_{}{}".format(root, n, ext)
    os.replace(tmp, alt)
    if log:
        log("   ⚠ {} est verrouillé ({}) : jamais réécrit. Nouveau contenu écrit sous un nom horodaté : {}.".format(os.path.basename(final), str(last)[:80], os.path.basename(alt)))
    return alt


def fh4_write_text(path, text, log=None, encoding="utf-8"):
    tmp = fh4_tmp_path(path)
    with open(tmp, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    return fh4_commit(tmp, path, log)


def fh4_write_csv(path, header, rows, meta=None, log=None):
    """CSV « ; » en UTF-8 avec BOM ; première ligne « # … » = métadonnées (date, base)."""
    tmp = fh4_tmp_path(path)
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        if meta:
            fh.write("# " + meta + "\n")
        w = csv.writer(fh, delimiter=";")
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return fh4_commit(tmp, path, log)


def fh4_read_raster(path):
    """(tableau float64 avec NaN pour NoData, géotransformée, projection, (nx, ny)). Lecture seule."""
    from osgeo import gdal
    import numpy as np
    ds = gdal.Open(path)
    if ds is None:
        raise Fh4Error("Fichier raster illisible : {}".format(path))
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    a[a < -9998] = np.nan
    gt, pj, nx, ny = ds.GetGeoTransform(), ds.GetProjection(), ds.RasterXSize, ds.RasterYSize
    ds = None
    return a, gt, pj, (nx, ny)


def _fh4_ogr():
    from osgeo import ogr, osr, gdal
    ogr.UseExceptions()
    osr.UseExceptions()
    gdal.UseExceptions()
    return ogr, osr, gdal


def _fh4_polys_only(g):
    """Garde les polygones d'une géométrie réparée (MakeValid peut renvoyer une collection avec lignes ou points)."""
    ogr, osr, gdal = _fh4_ogr()
    if g is None or g.IsEmpty():
        return None
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        return ogr.ForceToMultiPolygon(g.Clone())
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    for i in range(g.GetGeometryCount()):
        p = _fh4_polys_only(g.GetGeometryRef(i))
        if p is not None:
            for j in range(p.GetGeometryCount()):
                out.AddGeometry(p.GetGeometryRef(j))
    return out if out.GetGeometryCount() else None


def fh4_release_paths(paths):
    """THREAD PRINCIPAL, AVANT l'écriture : retire du projet QGIS les couches qui référencent ces fichiers (verrou Windows)."""
    try:
        pr = QgsProject.instance()
        norm = set(os.path.normcase(os.path.abspath(p)) for p in paths)
        for l_ in list(pr.mapLayers().values()):
            src_ = str(l_.source()).split("|")[0]
            if os.path.normcase(os.path.abspath(src_)) in norm:
                pr.removeMapLayer(l_.id())
    except Exception:
        pass

# ============================================================
# 2. ENTRÉES (lecture seule)
# ============================================================
def fh4_list_basins(root):
    """Bassins traités par l'étage 1 (manifeste.json sous Etages/01_envi/<bassin>/). Lecture seule."""
    out, seen = [], set()
    if not root or not os.path.isdir(root):
        return out
    cands = glob.glob(os.path.join(root, "Etages", "01_envi", "*", "manifeste.json"))
    cands += glob.glob(os.path.join(root, "**", "manifeste.json"), recursive=True)
    marker = os.path.join("Etages", "01_envi")
    for mp in cands:
        mp = os.path.normpath(mp)
        if marker not in mp or mp in seen:
            continue
        seen.add(mp)
        try:
            man = json.load(open(mp, encoding="utf-8"))
        except Exception:
            continue
        d = os.path.dirname(mp)
        out.append({"basin": man.get("bassin") or os.path.basename(d), "dir": d, "manifeste": man})
    return out


def fh4_classes_csv(d):
    """{code: nom} depuis classes.csv de l'étage 1 (lecture seule)."""
    p = os.path.join(d, "classes.csv")
    rows = {}
    if not os.path.isfile(p):
        return rows
    for ln in open(p, encoding="utf-8-sig").read().splitlines()[1:]:
        if ";" not in ln:
            continue
        parts = ln.split(";")
        try:
            rows[int(parts[0])] = parts[1]
        except Exception:
            pass
    return rows


def fh4_find_stage1(e1_dir):
    """Étage 1 : classification, classes.csv, manifeste (lecture seule). Erreur claire si absent."""
    if not os.path.isdir(e1_dir):
        raise Fh4Error("Étage 1 introuvable : le dossier {} n'existe pas (l'étage 4 ne relance jamais l'étage 1).".format(e1_dir))
    man, mp = {}, os.path.join(e1_dir, "manifeste.json")
    if os.path.isfile(mp):
        try:
            man = json.load(open(mp, encoding="utf-8"))
        except Exception:
            man = {}
    cls = None
    for nm in ("occupation_sol_ENVI.tif", "occupation_sol_secours.tif"):
        if os.path.isfile(os.path.join(e1_dir, nm)):
            cls = os.path.join(e1_dir, nm)
            break
    if cls is None:
        raise Fh4Error("Classification de l'étage 1 introuvable dans {} (occupation_sol_ENVI.tif absent) : l'étage 4 ne la recalcule pas.".format(e1_dir))
    classes = fh4_classes_csv(e1_dir)
    if not classes:
        raise Fh4Error("classes.csv de l'étage 1 introuvable ou vide dans {}.".format(e1_dir))
    return {"dir": e1_dir, "classification": cls, "classes": classes, "classes_csv": os.path.join(e1_dir, "classes.csv"), "manifeste": man, "manifeste_path": mp if os.path.isfile(mp) else None}


_FH4_EXCLUS_A = re.compile(r"\.tmp\.|_NOUVEAU|\d{8}_\d{6}|GloREDa|mensuel|_H\d\b", re.I)


def fh4_find_stage2(e2_dir, log=None):
    """Étage 2 (lecture seule) : manifeste lu d'abord, puis fichiers réels de A. NE PRÉSUME PAS les noms : liste le dossier, journalise les noms retenus ;
    ambigu ⇒ Fh4Error avec la liste trouvée (aucun choix silencieux). Renvoie {"A": {"P_Itasy": chemin|None, "P_ref": chemin|None}, "source_A": {...}, ...}."""
    say = log or (lambda m: None)
    if not os.path.isdir(e2_dir):
        raise Fh4Error("Étage 2 introuvable : le dossier {} n'existe pas (lance d'abord l'étage 2 ; l'étage 4 n'invente aucun calcul).".format(e2_dir))
    res = {"dir": e2_dir, "manifeste": {}, "manifeste_path": None, "A": {"P_Itasy": None, "P_ref": None}, "source_A": {}, "notes": [], "liste_A": []}
    mp = os.path.join(e2_dir, "manifeste.json")
    if os.path.isfile(mp):
        try:
            res["manifeste"] = json.load(open(mp, encoding="utf-8"))
            res["manifeste_path"] = mp
        except Exception as ex:
            res["notes"].append("manifeste.json de l'étage 2 illisible ({}) : repli sur les noms de fichiers".format(str(ex)[:80]))
    allf = sorted(os.listdir(e2_dir))
    cand = [f for f in allf if re.match(r"^erosion_A.*\.tif$", f, re.I)]
    res["liste_A"] = cand
    say("📂 Étage 2 : {} fichier(s) dans le dossier ; fichiers de A trouvés : {}.".format(len(allf), ", ".join(cand) if cand else "aucun"))
    ignores = [f for f in cand if _FH4_EXCLUS_A.search(f)]
    cand_ok = [f for f in cand if f not in ignores]
    if ignores:
        res["notes"].append("fichiers de A ignorés (copies, mensuel, variantes) : {}".format(", ".join(ignores)))
    man = res["manifeste"]
    p_choisi = ((man.get("scenario_choisi") or {}).get("P")) or ((man.get("scenario_P") or {}).get("cle"))
    if p_choisi not in ("P_Itasy", "P_ref"):
        p_choisi = None
    spec = {"P_Itasy": "erosion_A_P_Itasy.tif", "P_ref": "erosion_A_P_ref.tif"}
    for k, nm in spec.items():
        if nm in cand_ok:
            res["A"][k] = os.path.join(e2_dir, nm)
            res["source_A"][k] = "nom de fichier explicite ({})".format(nm)
    if "erosion_A.tif" in cand_ok:
        pth = os.path.join(e2_dir, "erosion_A.tif")
        if p_choisi:
            if res["A"][p_choisi] is None:
                res["A"][p_choisi] = pth
                res["source_A"][p_choisi] = "erosion_A.tif = scénario choisi à l'étage 2 d'après son manifeste ({})".format(p_choisi)
            else:
                res["notes"].append("erosion_A.tif (scénario choisi : {}) non utilisé : le fichier explicite {} existe".format(p_choisi, os.path.basename(res["A"][p_choisi])))
        else:
            if all(res["A"][k] is not None for k in res["A"]):
                res["notes"].append("erosion_A.tif non utilisé : les deux scénarios ont un fichier explicite")
            elif res["A"]["P_Itasy"] is not None:
                res["notes"].append("erosion_A.tif NON utilisé : son scénario P n'est indiqué ni par le manifeste de l'étage 2 ni par son nom (ambigu) ; le scénario P = 1 n'est donc pas calculé")
            else:
                raise Fh4Error("Fichier erosion_A.tif ambigu : le manifeste de l'étage 2 n'indique pas son scénario P (Itasy ou P = 1) et il n'existe pas de fichier "
                               "erosion_A_P_Itasy.tif / erosion_A_P_ref.tif pour lever le doute. Fichiers de A trouvés dans {} : {}. "
                               "Je ne choisis pas à ta place : relance l'étage 2 (le manifeste dira le scénario) ou produis la série de l'autre borne.".format(e2_dir, ", ".join(cand) or "aucun"))
    if res["A"]["P_Itasy"] is None:
        raise Fh4Error("Scénario PRINCIPAL (P Itasy) introuvable dans {} : fichiers de A trouvés : {}. Lance l'étage 2 avec P Itasy (ou la série de l'autre borne) : "
                       "l'étage 4 ne recalcule pas A.".format(e2_dir, ", ".join(cand) if cand else "aucun"))
    for k, p in res["A"].items():
        if p is not None:
            from osgeo import gdal
            ds = gdal.Open(p)
            if ds is None:
                raise Fh4Error("Fichier de A illisible : {}".format(p))
            ds = None
    if res["A"]["P_ref"] is None:
        res["notes"].append("scénario complément (P = 1) absent : seul P Itasy est calculé")
    for k in ("P_Itasy", "P_ref"):
        if res["A"][k]:
            say("   • A {} : {} — {}".format("P Itasy (principal)" if k == "P_Itasy" else "P = 1 (complément)", os.path.basename(res["A"][k]), res["source_A"][k]))
        else:
            say("   • A {} : non disponible".format("P Itasy (principal)" if k == "P_Itasy" else "P = 1 (complément)"))
    for n in res["notes"]:
        say("   ℹ " + n)
    return res


def fh4_sig(path, etage, role):
    st = os.stat(path)
    return {"etage": etage, "role": role, "fichier": os.path.basename(path), "chemin": path, "mtime": st.st_mtime, "date": fh4_iso(st.st_mtime), "taille": st.st_size}


def fh4_compare_amont(old, new):
    """Liste des différences (ancien/nouveau) entre deux signatures amont : par rôle ; date (à la seconde) ou taille différente, fichier ajouté ou retiré."""
    o = {s["role"]: s for s in old or []}
    n = {s["role"]: s for s in new or []}
    diffs = []
    for role in sorted(set(o) | set(n)):
        a, b = o.get(role), n.get(role)
        if a is None:
            diffs.append({"etage": b["etage"], "role": role, "ancien": "absent", "nouveau": "{} ({}, {} o)".format(b["fichier"], b["date"], b["taille"])})
        elif b is None:
            diffs.append({"etage": a["etage"], "role": role, "ancien": "{} ({}, {} o)".format(a["fichier"], a["date"], a["taille"]), "nouveau": "absent"})
        elif a["fichier"] != b["fichier"] or int(a["mtime"]) != int(b["mtime"]) or a["taille"] != b["taille"]:
            diffs.append({"etage": b["etage"], "role": role, "ancien": "{} ({}, {} o)".format(a["fichier"], a["date"], a["taille"]),
                          "nouveau": "{} ({}, {} o)".format(b["fichier"], b["date"], b["taille"])})
    return diffs


def fh4_water_codes(classes, e2_dir=None):
    """Codes de la classe Eau : nom commençant par « eau » (règle de l'étage 2) ou catégorie « eau » dans table_C_correspondance.csv de l'étage 2 (lue, jamais écrite)."""
    codes = set(c for c, n in classes.items() if c > 0 and str(n).strip().lower().startswith("eau"))
    if e2_dir:
        p = os.path.join(e2_dir, "table_C_correspondance.csv")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8-sig", newline="") as fh:
                    for r in csv.DictReader(fh, delimiter=";"):
                        if str(r.get("categorie_C_actuel", "")).strip().lower() == "eau":
                            codes.add(int(r["code"]))
            except Exception:
                pass
    return sorted(codes)


def fh4_read_basin_wkt(spec):
    """Contour OFFICIEL du bassin (fichier .gpkg/.shp/.geojson, « chemin|layername=… » accepté) → WKT en EPSG:32738 (réunion de ses entités). CRS absent ⇒ Fh4Error. OGR seulement."""
    ogr, osr, gdal = _fh4_ogr()
    parts = str(spec).split("|")
    path, layer = parts[0], None
    for q in parts[1:]:
        if q.startswith("layername="):
            layer = q[len("layername="):]
    ds = ogr.Open(path, 0)
    if ds is None:
        raise Fh4Error("Contour du bassin illisible : {}".format(path))
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayerByIndex(0)
    if lyr is None:
        raise Fh4Error("Couche du contour introuvable dans {}.".format(path))
    srs = lyr.GetSpatialRef()
    if srs is None:
        raise Fh4Error("CRS du contour du bassin absent dans {} : arrêt, je ne devine pas.".format(path))
    srs = srs.Clone()
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(FH4_CRS)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(srs, dst)
    uni = None
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone()
        g.Transform(tr)
        if not g.IsValid():
            g = g.Buffer(0)
        uni = g if uni is None else uni.Union(g)
    ds = None
    if uni is None or uni.IsEmpty():
        raise Fh4Error("Le contour du bassin est vide : {}".format(path))
    return uni.ExportToWkt()


def fh4_basin_from_stage2(info2, log=None):
    """Contour du bassin pour l'étage 4 SANS le demander : s'il est cité par le manifeste de l'étage 2 (champ masque) et existe encore, il est lu ; sinon None
    (le masque est alors l'emprise des pixels valides de A). Journalise la source retenue."""
    man = (info2 or {}).get("manifeste") or {}
    txt = json.dumps(man.get("masque") or {}, ensure_ascii=False)
    cands = re.findall(r"(?:[A-Za-z]:[\\\\/]|/)[^\"|;]*?\.(?:gpkg|shp|geojson)", txt.replace("\\\\", "\\"), flags=re.I)
    for c in cands:
        c = c.strip()
        if os.path.isfile(c):
            try:
                w = fh4_read_basin_wkt(c)
                if log:
                    log("🗺 Contour du bassin : fichier cité par le manifeste de l'étage 2 ({}).".format(os.path.basename(c)))
                return w
            except Exception:
                continue
    return None


def fh4_basin_footprint_wkt(a_path, max_side=1200):
    """Emprise du bassin (WKT EPSG:32738) déduite des pixels valides de A de l'étage 2 (lecture seule, sous-échantillonnée) : sert seulement à FILTRER les listes d'unités (celles qui touchent
    le bassin) et l'affichage, jamais aux calculs."""
    ogr, osr, gdal = _fh4_ogr()
    import numpy as np
    ds = gdal.Open(a_path)
    nx, ny, gt = ds.RasterXSize, ds.RasterYSize, ds.GetGeoTransform()
    k = max(1, int(math.ceil(max(nx, ny) / float(max_side))))
    bw, bh = max(1, nx // k), max(1, ny // k)
    b = ds.GetRasterBand(1)
    arr = b.ReadAsArray(buf_xsize=bw, buf_ysize=bh).astype("float64")
    nd = b.GetNoDataValue()
    valid = np.isfinite(arr) & (arr > -9998)
    if nd is not None:
        valid &= arr != nd
    ds = None
    if not valid.any():
        raise Fh4Error("La carte A de l'étage 2 ne contient aucun pixel valide.")
    sx, sy = gt[1] * nx / float(bw), gt[5] * ny / float(bh)
    mem = gdal.GetDriverByName("MEM").Create("", bw, bh, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((gt[0], sx, 0.0, gt[3], 0.0, sy))
    mem.GetRasterBand(1).WriteArray(valid.astype("uint8"))
    dsm = ogr.GetDriverByName("Memory").CreateDataSource("fp")
    lyr = dsm.CreateLayer("fp", None, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("dn", ogr.OFTInteger))
    gdal.Polygonize(mem.GetRasterBand(1), mem.GetRasterBand(1), lyr, 0, [])
    col = ogr.Geometry(ogr.wkbGeometryCollection)
    for f in lyr:
        if f.GetField("dn") == 1:
            col.AddGeometry(f.GetGeometryRef().Clone())
    try:
        u = col.UnionCascaded()
    except Exception:
        u = col
    u = u.Buffer(abs(sx)).Simplify(abs(sx) / 2.0)
    return u.ExportToWkt()


def fh4_rasterize_wkt(wkt, ref_gt, nx, ny, proj_wkt):
    """Masque booléen d'un polygone (UTM 38S) sur la grille de A."""
    ogr, osr, gdal = _fh4_ogr()
    srs = osr.SpatialReference()
    srs.ImportFromWkt(proj_wkt)
    ds = ogr.GetDriverByName("Memory").CreateDataSource("b")
    lyr = ds.CreateLayer("b", srs, ogr.wkbMultiPolygon)
    ft = ogr.Feature(lyr.GetLayerDefn())
    ft.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
    lyr.CreateFeature(ft)
    mem = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(ref_gt)
    mem.SetProjection(proj_wkt)
    mem.GetRasterBand(1).Fill(0)
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    arr = mem.GetRasterBand(1).ReadAsArray().astype(bool)
    mem = None
    ds = None
    return arr


def fh4_classification_on_grid(cls_path, ref_gt, nx, ny, proj_wkt, log=None):
    """Classification de l'étage 1 sur la grille de A. Même grille : lecture directe ; sinon rééchantillonnage au PLUS PROCHE VOISIN dans la mémoire (fichiers amont intacts)."""
    ogr, osr, gdal = _fh4_ogr()
    ds = gdal.Open(cls_path)
    gt = ds.GetGeoTransform()
    same = (ds.RasterXSize == nx and ds.RasterYSize == ny and all(abs(a - b) < 1e-6 for a, b in zip(gt, ref_gt)))
    if same:
        arr = ds.GetRasterBand(1).ReadAsArray().astype("int32")
        ds = None
        return arr, False
    xmin, ymax = ref_gt[0], ref_gt[3]
    xmax, ymin = xmin + ref_gt[1] * nx, ymax + ref_gt[5] * ny
    out = gdal.Warp("", ds, format="MEM", outputBounds=(xmin, ymin, xmax, ymax), width=nx, height=ny, dstSRS=proj_wkt, resampleAlg="near", outputType=gdal.GDT_Int32, dstNodata=0)
    arr = out.GetRasterBand(1).ReadAsArray().astype("int32")
    out = None
    ds = None
    if log:
        log("   ℹ Grille de l'ENVI différente de celle de A : classification rééchantillonnée au plus proche voisin (en mémoire, fichiers amont intacts).")
    return arr, True

# ============================================================
# 3. HYDROLOGIE : MNT → cuvettes comblées → D8 → aire drainée → réseau → sous-bassins (NumPy / GDAL ; TauDEM facultatif par sous-processus)
# ============================================================
FH4_D8 = ((0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1))     # codes 1..8 = E, NE, N, NO, O, SO, S, SE (convention TauDEM)
FH4_EPS = 1e-3                                                                      # m : gradient imposé aux cuvettes et plans d'eau (Barnes et al. 2014)


def fh4_load_dem(dem_path, basin_wkt, cls_path, water_codes, step, marge, log):
    """MNT SRTM du cache de l'étage 2 → grille EPSG:32738 de `step` m recadrée sur le bassin + marge, valeurs d'altitude reprises par interpolation bilinéaire (aucune autre modification).
    Renvoie {"z" (NaN hors bassin), "inside", "lake", "gt", "proj", "step", "n_voids"}. Lecture seule du MNT et de la classification."""
    ogr, osr, gdal = _fh4_ogr()
    import numpy as np
    geom = ogr.CreateGeometryFromWkt(basin_wkt)
    x0, x1, y0, y1 = geom.GetEnvelope()
    x0, y0 = math.floor((x0 - marge) / step) * step, math.floor((y0 - marge) / step) * step
    x1, y1 = math.ceil((x1 + marge) / step) * step, math.ceil((y1 + marge) / step) * step
    nx, ny = int(round((x1 - x0) / step)), int(round((y1 - y0) / step))
    ds = gdal.Open(dem_path)
    if ds is None:
        raise Fh4Error("MNT illisible : {}".format(dem_path))
    nd = ds.GetRasterBand(1).GetNoDataValue()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(FH4_CRS)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    proj = srs.ExportToWkt()
    out = gdal.Warp("", ds, format="MEM", outputBounds=(x0, y0, x1, y1), width=nx, height=ny, dstSRS="EPSG:{}".format(FH4_CRS), resampleAlg="bilinear",
                    srcNodata=nd if nd is not None else -32768, dstNodata=-9999, outputType=gdal.GDT_Float32)
    z = out.GetRasterBand(1).ReadAsArray().astype("float64")
    out = None
    ds = None
    gt = (x0, step, 0.0, y1, 0.0, -step)
    inside = fh4_rasterize_wkt(basin_wkt, gt, nx, ny, proj)
    bad = (z < -9000) | ~np.isfinite(z)
    n_voids = int((bad & inside).sum())
    if n_voids:
        if n_voids > 0.02 * max(1, int(inside.sum())):
            raise Fh4Error("Le MNT a {} pixels sans valeur dans le bassin (> 2 %) : couverture insuffisante du cache de l'étage 2 ({}).".format(n_voids, dem_path))
        zz = np.where(bad, -9999.0, z)
        mem = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Float32)
        mem.SetGeoTransform(gt)
        mem.SetProjection(proj)
        b = mem.GetRasterBand(1)
        b.SetNoDataValue(-9999)
        b.WriteArray(zz.astype("float32"))
        gdal.FillNodata(b, None, 50, 0)
        z = b.ReadAsArray().astype("float64")
        mem = None
        log("⚠ MNT : {} pixel(s) sans valeur dans le bassin comblés par interpolation (gdal.FillNodata, 50 px max).".format(n_voids))
    z = np.where(inside & (z > -9000), z, np.nan)
    lake = np.zeros(z.shape, bool)
    if cls_path and water_codes:
        cls, _re = fh4_classification_on_grid(cls_path, gt, nx, ny, proj, None)
        lake = np.isin(cls, list(water_codes)) & inside
    return {"z": z, "inside": inside & np.isfinite(z), "lake": lake, "gt": gt, "proj": proj, "step": float(step), "n_voids": n_voids}


def fh4_priority_flood(z, inside, eps=FH4_EPS, cancel=None):
    """Comble les cuvettes (priority-flood avec epsilon, Barnes et al. 2014) : chaque pixel du bassin finit avec un voisin STRICTEMENT plus bas, sauf au contour (l'eau sort).
    Les plans d'eau plats reçoivent le même gradient (écoulement vers leur point de sortie). Renvoie (z comblé, nombre de pixels relevés de > 5 mm)."""
    import heapq
    import numpy as np
    ny, nx = z.shape
    W = nx + 2
    zp = np.full((ny + 2, W), np.inf)
    ip = np.zeros((ny + 2, W), bool)
    zp[1:-1, 1:-1] = np.where(inside, z, np.inf)
    ip[1:-1, 1:-1] = inside
    zl, il = zp.ravel().tolist(), ip.ravel().tolist()
    offs = [dr * W + dc for dr, dc in FH4_D8]
    closed = [False] * len(zl)
    heap = []
    for k in np.flatnonzero(ip.ravel()).tolist():
        if not all(il[k + o] for o in offs):                         # pixel du contour (ou voisin hors bassin) : point de sortie possible
            heap.append((zl[k], k))
            closed[k] = True
    heapq.heapify(heap)
    out = list(zl)
    n = 0
    while heap:
        zc, k = heapq.heappop(heap)
        n += 1
        if cancel is not None and (n & 0x3FFFF) == 0 and cancel.is_set():
            raise Fh4Cancelled()
        for o in offs:
            j = k + o
            if il[j] and not closed[j]:
                closed[j] = True
                v = zl[j]
                if v <= zc + eps:
                    v = zc + eps
                out[j] = v
                heapq.heappush(heap, (v, j))
    zf = np.asarray(out).reshape(ny + 2, W)[1:-1, 1:-1]
    zf = np.where(inside, zf, np.nan)
    return zf, int(np.nansum((zf - z) > 0.005))


def fh4_d8(zf, inside, step):
    """Direction D8 (codes 1..8, 0 = pas de voisin plus bas dans le bassin = SORTIE) : plus forte pente, diagonales pondérées par √2."""
    import numpy as np
    ny, nx = zf.shape
    zp = np.full((ny + 2, nx + 2), np.inf)
    zp[1:-1, 1:-1] = np.where(inside, zf, np.inf)
    best = np.zeros((ny, nx))
    code = np.zeros((ny, nx), "uint8")
    c = zp[1:-1, 1:-1]
    for k, (dr, dc) in enumerate(FH4_D8):
        nb = zp[1 + dr:1 + dr + ny, 1 + dc:1 + dc + nx]
        dist = step * (math.sqrt(2.0) if (dr and dc) else 1.0)
        with np.errstate(invalid="ignore"):
            slope = (c - nb) / dist
        slope = np.where(np.isfinite(nb) & np.isfinite(c), slope, 0.0)
        better = slope > best + 1e-12
        best = np.where(better, slope, best)
        code = np.where(better, k + 1, code).astype("uint8")
    return np.where(inside, code, 0).astype("uint8")


def fh4_topo_order(dirs, inside):
    """Ordre TOPOLOGIQUE amont → aval (Kahn) des pixels du bassin d'après les directions D8 ; même résultat quel que soit le moteur (NumPy ou TauDEM), robuste aux plans plats et aux plans d'eau forcés.
    Renvoie (ordre [indices plats rembourrés], offsets, largeur rembourrée, direction aplatie). Une boucle de directions lève Fh4Error."""
    import numpy as np
    ny, nx = dirs.shape
    W = nx + 2
    offs = np.array([0] + [dr * W + dc for dr, dc in FH4_D8])
    d = np.zeros((ny + 2, W), "uint8")
    d[1:-1, 1:-1] = np.where(inside, dirs, 0)
    flat = d.ravel()
    ins = np.zeros((ny + 2, W), bool)
    ins[1:-1, 1:-1] = inside
    idx = np.flatnonzero(ins.ravel())
    has = flat[idx] > 0
    tgt = idx[has] + offs[flat[idx[has]]]
    indeg = np.bincount(tgt, minlength=flat.size).tolist()
    nxt = (np.arange(flat.size) + offs[flat]).tolist()
    dl = flat.tolist()
    stack = [k for k in idx.tolist() if indeg[k] == 0]
    order = []
    while stack:
        k = stack.pop()
        order.append(k)
        if dl[k]:
            j = nxt[k]
            indeg[j] -= 1
            if indeg[j] == 0:
                stack.append(j)
    if len(order) != len(idx):
        raise Fh4Error("Boucle dans les directions d'écoulement ({} pixels hors ordre) : le MNT ou le moteur D8 est en cause.".format(len(idx) - len(order)))
    return order, offs.tolist(), W, dl


def fh4_accumulate(order, dl, offs, W, size, cancel=None):
    """Aire drainée en NOMBRE DE PIXELS (chaque pixel compte pour 1), cumulée dans l'ordre topologique."""
    acc = [0] * size
    for k in order:
        acc[k] = 1
    for i, k in enumerate(order):
        if cancel is not None and (i & 0x3FFFF) == 0 and cancel.is_set():
            raise Fh4Cancelled()
        d = dl[k]
        if d:
            acc[k + offs[d]] += acc[k]
    return acc


def fh4_make_hyd(zf, dirs, inside, voie, fill_n=None, acc_override=None, cancel=None):
    import numpy as np
    order, offs, W, dl = fh4_topo_order(dirs, inside)
    ny, nx = dirs.shape
    acc = acc_override if acc_override is not None else fh4_accumulate(order, dl, offs, W, (ny + 2) * W, cancel)
    return {"zf": zf, "dirs": dirs, "acc": acc, "offs": offs, "W": W, "order": order, "fill_n": fill_n, "voie": voie}


def fh4_hydro_numpy(dem, log, cancel=None):
    """Voie C : cuvettes comblées + D8 + aire drainée, NumPy seul. Renvoie l'état hydrologique {"zf","dirs","acc" (liste plate), "offs","W","fill_n",…}."""
    zf, nfill = fh4_priority_flood(dem["z"], dem["inside"], cancel=cancel)
    log("🕳 Cuvettes comblées (priority-flood, epsilon {} m) : {} pixel(s) relevés de plus de 5 mm.".format(FH4_EPS, nfill))
    dirs = fh4_d8(zf, dem["inside"], dem["step"])
    return fh4_make_hyd(zf, dirs, dem["inside"], "C", nfill, None, cancel)


def fh4_hydro_taudem(dem, exes, workdir, log, cancel=None, procs=4):
    """Voie A : pitremove, d8flowdir, aread8 de TauDEM par SOUS-PROCESSUS (mêmes algorithmes que QSWAT+) puis relecture de fel / p / ad8. Les étapes suivantes (réseau, sous-bassins) sont les mêmes
    qu'en voie C. NON TESTÉ avec les vrais exécutables dans cette livraison : toute erreur est rapportée telle quelle, sans repli silencieux."""
    import subprocess
    import numpy as np
    ogr, osr, gdal = _fh4_ogr()
    os.makedirs(workdir, exist_ok=True)
    ny, nx = dem["z"].shape
    p_in = os.path.join(workdir, "mnt_bassin.tif")
    o = gdal.GetDriverByName("GTiff").Create(p_in, nx, ny, 1, gdal.GDT_Float32)
    o.SetGeoTransform(dem["gt"])
    o.SetProjection(dem["proj"])
    b = o.GetRasterBand(1)
    b.SetNoDataValue(-9999)
    b.WriteArray(np.where(dem["inside"], dem["z"], -9999).astype("float32"))
    o.FlushCache()
    o = None
    fel, pp, ad8 = (os.path.join(workdir, n) for n in ("fel.tif", "p.tif", "ad8.tif"))
    steps = (("pitremove", ["-z", p_in, "-fel", fel], fel), ("d8flowdir", ["-fel", fel, "-p", pp, "-sd8", os.path.join(workdir, "sd8.tif")], pp), ("aread8", ["-p", pp, "-ad8", ad8, "-nc"], ad8))
    mpi_cmd = [exes["mpiexec"], "-n", str(procs)] if exes.get("mpiexec") else []

    def run_step(nm, args, out, with_mpi):
        exe_dir = os.path.dirname(exes[nm]) or None
        env = dict(os.environ)
        if exe_dir:
            env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")            # les DLL de TauDEM sont à côté des exécutables
        cmd = (mpi_cmd if with_mpi else []) + [exes[nm]] + args
        log("🛠 TauDEM : {}".format(" ".join('"{}"'.format(c) if " " in c else c for c in cmd)))
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=exe_dir, env=env)
        if r.returncode != 0 or not os.path.isfile(out):
            hint = " (DLL manquante : TauDEM ne démarre pas sur cette machine)" if r.returncode in (3221225781, -1073741515) else ""
            raise Fh4Error("TauDEM « {} » a échoué (code {}{}) : {} — choisis la voie C (NumPy) ou corrige l'installation TauDEM.".format(
                nm, r.returncode, hint, (r.stderr or r.stdout or "")[-300:].strip()))
    use_mpi = bool(mpi_cmd)
    for nm, args, out in steps:
        if cancel is not None and cancel.is_set():
            raise Fh4Cancelled()
        try:
            run_step(nm, args, out, use_mpi)
        except Fh4Error as ex:
            if use_mpi and nm == "pitremove":                                      # MPI absent ou défaillant : on réessaie SANS mpiexec
                log("⚠ {} ; nouvel essai sans MPI.".format(str(ex).split(" — ")[0]))
                use_mpi = False
                run_step(nm, args, out, use_mpi)
            else:
                raise
    zf = np.where(dem["inside"], fh4_read_raster(fel)[0], np.nan)
    p_arr = fh4_read_raster(pp)[0]
    ac = fh4_read_raster(ad8)[0]
    dirs = np.where(dem["inside"] & np.isfinite(p_arr) & (p_arr >= 1) & (p_arr <= 8), np.nan_to_num(p_arr), 0).astype("uint8")
    # TauDEM : un pixel dont la direction pointe hors du bassin (ou vers un voisin non valide) = sortie
    W = nx + 2
    offs = np.array([0] + [dr * W + dc for dr, dc in FH4_D8])
    ins = np.zeros((ny + 2, W), bool)
    ins[1:-1, 1:-1] = dem["inside"]
    il = ins.ravel()
    dl = np.zeros((ny + 2, W), "uint8")
    dl[1:-1, 1:-1] = dirs
    dls = dl.ravel()
    tgt = np.arange(dls.size) + np.take(offs, dls)
    dls = np.where(il & ~il[tgt], 0, dls)
    dirs = dls.reshape(ny + 2, W)[1:-1, 1:-1].astype("uint8")
    ap = np.zeros((ny + 2, W))
    ap[1:-1, 1:-1] = np.nan_to_num(ac)
    return fh4_make_hyd(zf, dirs, dem["inside"], "A", None, ap.ravel().astype("int64").tolist(), cancel)


def fh4_network(dem, hyd, thr_cells, extra_cells=(), cancel=None):
    """Réseau (aire drainée ≥ seuil) découpé en TRONÇONS (un tronçon commence à une source, à une confluence ou à un exutoire supplémentaire), ordre de Strahler, lien amont → aval, puis
    SOUS-BASSINS (un par tronçon : chaque pixel du bassin va au tronçon que son chemin d'écoulement atteint). Renvoie le dictionnaire du réseau."""
    import numpy as np
    ny, nx = dem["z"].shape
    W, offs, acc = hyd["W"], hyd["offs"], hyd["acc"]
    step = dem["step"]
    ins = np.zeros((ny + 2, W), bool)
    ins[1:-1, 1:-1] = dem["inside"]
    il = ins.ravel().tolist()
    dp = np.zeros((ny + 2, W), "uint8")
    dp[1:-1, 1:-1] = hyd["dirs"]
    dl = dp.ravel().tolist()
    nxt = [k + offs[d] if d else -1 for k, d in enumerate(dl)]
    stream = [il[k] and acc[k] >= thr_cells for k in range(len(acc))]
    order_t = hyd["order"]                                         # amont → aval (topologique)
    scells = [k for k in order_t if stream[k]]
    ups = {}
    for k in scells:
        d = nxt[k]
        if d >= 0 and stream[d]:
            ups.setdefault(d, []).append(k)
    cuts = set(extra_cells)
    link_of, links = {}, []
    for k in scells:
        u = ups.get(k, [])
        if len(u) == 1 and k not in cuts:
            lid = link_of[u[0]]
            links[lid]["cells"].append(k)
        else:
            lid = len(links)
            if not u:
                order = 1
            else:
                ords = [links[link_of[x]]["order"] for x in u]
                m = max(ords)
                order = m + 1 if ords.count(m) >= 2 else m
            links.append({"cells": [k], "order": order, "ups": [link_of[x] for x in u], "down": None})
        link_of[k] = lid
    for L in links:
        d = nxt[L["cells"][-1]]
        L["exit"] = not (d >= 0 and stream[d])
        L["down"] = link_of[d] if (d >= 0 and stream[d]) else None
        ln = 0.0
        cs = L["cells"] + ([d] if L["down"] is not None else [])
        for a, b in zip(cs[:-1], cs[1:]):
            dd = b - a
            ln += step * (math.sqrt(2.0) if abs(dd) in (W - 1, W + 1) else 1.0)
        L["length_m"] = ln
        L["acc_end"] = acc[L["cells"][-1]]
    # sous-bassins : pixel de réseau → son tronçon ; autre pixel → celui de son aval (du plus bas au plus haut)
    asc = order_t[::-1]
    sb = [-1] * len(acc)
    for k in asc:
        if stream[k]:
            sb[k] = link_of[k]
        else:
            d = nxt[k]
            sb[k] = sb[d] if (d >= 0 and il[d]) else -1
    # plus long chemin d'écoulement jusqu'à chaque pixel (m), du haut vers le bas, dans le même sous-bassin
    flen = [0.0] * len(acc)
    for k in reversed(asc):
        d = nxt[k]
        if d >= 0 and il[d] and sb[d] == sb[k] and sb[k] >= 0:
            v = flen[k] + step * (math.sqrt(2.0) if abs(d - k) in (W - 1, W + 1) else 1.0)
            if v > flen[d]:
                flen[d] = v
    sbr = np.array(sb, dtype="int32").reshape(ny + 2, W)[1:-1, 1:-1]
    flr = np.array(flen).reshape(ny + 2, W)[1:-1, 1:-1]
    return {"links": links, "link_of": link_of, "sb": sbr, "flen": flr, "stream_n": len(scells), "W": W, "ny": ny, "nx": nx, "thr_cells": thr_cells, "nxt": nxt}


def fh4_cell_xy(dem, k, W):
    r, c = divmod(k, W)
    gt = dem["gt"]
    return gt[0] + (c - 1 + 0.5) * gt[1], gt[3] + (r - 1 + 0.5) * gt[5]


def fh4_snap_outlets(dem, hyd, net, pts_xy, radius_m, log):
    """Pour chaque point (x, y) UTM : pixel de réseau de plus forte aire drainée dans `radius_m`. Renvoie [(pixel plat, distance m, acc)] (aucun déplacement silencieux : tout est journalisé)."""
    out = []
    W, acc = hyd["W"], hyd["acc"]
    gt, step = dem["gt"], dem["step"]
    nr = int(math.ceil(radius_m / step))
    for x, y in pts_xy:
        c0, r0 = int((x - gt[0]) // step) + 1, int((gt[3] - y) // step) + 1
        best = None
        for r in range(r0 - nr, r0 + nr + 1):
            for c in range(c0 - nr, c0 + nr + 1):
                if 0 < r <= net["ny"] and 0 < c <= net["nx"]:
                    k = r * W + c
                    if k in net["link_of"]:
                        xx, yy = fh4_cell_xy(dem, k, W)
                        dist = math.hypot(xx - x, yy - y)
                        if dist <= radius_m and (best is None or acc[k] > best[2]):
                            best = (k, dist, acc[k])
        if best is None:
            log("⚠ Exutoire supplémentaire ({:.0f}, {:.0f}) : aucun pixel du réseau à moins de {:.0f} m — ignoré.".format(x, y, radius_m))
        else:
            out.append(best)
    return out


def fh4_threshold_options(area_ha, pcts=None):
    """Trois seuils proposés (HYPOTHÈSE) : part de la surface du bassin. Renvoie [{"cle","libelle","pct","km2"}]."""
    return [{"cle": c, "libelle": lib, "pct": p, "km2": area_ha * p / 100.0 / 100.0} for c, lib, p in (pcts or FH4_SEUILS)]


def fh4_preview(dem, hyd, area_ha, log, cancel=None):
    """Aperçu des 3 seuils : nombre de sous-bassins et longueur de réseau pour chacun (aucun fichier écrit)."""
    out = []
    px_ha = dem["step"] ** 2 / 1e4
    for o in fh4_threshold_options(area_ha):
        thr = max(2, int(round(o["km2"] * 100.0 / px_ha)))
        net = fh4_network(dem, hyd, thr, (), cancel)
        n = len(net["links"])
        length = sum(L["length_m"] for L in net["links"]) / 1000.0
        out.append(dict(o, seuil_px=thr, n_sb=n, reseau_km=length))
        log("📐 Seuil « {} » ({} % du bassin = {} km², HYPOTHÈSE) : {} sous-bassins, {} km de réseau.".format(o["libelle"], fh4_fr(o["pct"], 1), fh4_fr(o["km2"], 1), n, fh4_fr(length, 0)))
    return out


def fh4_lake_bodies(lake, min_cells=4):
    """Plans d'eau = composantes connexes (8 voisins) du masque d'eau de l'étage 1. Renvoie [liste de (r, c)]."""
    ny, nx = lake.shape
    seen = lake.copy()
    out = []
    rr, cc = (x.tolist() for x in lake.nonzero())
    for r0, c0 in zip(rr, cc):
        if not seen[r0, c0]:
            continue
        comp, stack = [], [(r0, c0)]
        seen[r0, c0] = False
        while stack:
            r, c = stack.pop()
            comp.append((r, c))
            for dr, dc in FH4_D8:
                r1, c1 = r + dr, c + dc
                if 0 <= r1 < ny and 0 <= c1 < nx and seen[r1, c1]:
                    seen[r1, c1] = False
                    stack.append((r1, c1))
        if len(comp) >= min_cells:
            out.append(comp)
    return out


def fh4_lake_force(dem, hyd, log, cancel=None):
    """Option « forcer le plan d'eau plat » : chaque plan d'eau n'a qu'UN pixel de sortie (le pixel d'eau par où l'eau sort déjà, à plus forte aire drainée) ; les autres pixels d'eau s'y écoulent par un arbre
    de plus court chemin à l'intérieur du plan d'eau (plus de lignes droites imposées par le gradient du comblement). Les aires drainées sont recalculées. HYPOTHÈSE, à valider."""
    import numpy as np
    ny, nx = dem["lake"].shape
    W, offs, acc = hyd["W"], hyd["offs"], hyd["acc"]
    dirs = hyd["dirs"].copy()
    bodies = fh4_lake_bodies(dem["lake"] & dem["inside"])
    n_cells = 0
    for comp in bodies:
        cs = set(comp)
        best, bo = None, None
        for (r, c) in comp:
            d = int(dirs[r, c])
            out = True
            if d:
                dr, dc = FH4_D8[d - 1]
                out = (r + dr, c + dc) not in cs
            if out:
                a = acc[(r + 1) * W + (c + 1)]
                if best is None or a > best:
                    best, bo = a, (r, c)
        if bo is None:
            continue
        dist = {bo: 0}
        queue = [bo]
        for (r, c) in queue:
            for dr, dc in FH4_D8:
                nb = (r + dr, c + dc)
                if nb in cs and nb not in dist:
                    dist[nb] = dist[(r, c)] + 1
                    queue.append(nb)
        for (r, c) in comp:
            if (r, c) == bo or (r, c) not in dist:
                continue
            for k, (dr, dc) in enumerate(FH4_D8):
                nb = (r + dr, c + dc)
                if nb in dist and dist[nb] == dist[(r, c)] - 1:
                    dirs[r, c] = k + 1
                    break
        n_cells += len(comp)
    log("💧 Plans d'eau forcés (option ii, HYPOTHÈSE) : {} plan(s), {} pixels ; un seul pixel de sortie par plan d'eau ; aires drainées recalculées.".format(len(bodies), n_cells))
    return fh4_make_hyd(hyd["zf"], dirs, dem["inside"], hyd["voie"], hyd.get("fill_n"), None, cancel)


def fh4_straight_segments(links, dem, W, min_cells=20):
    """Tronçons de rivière RECTILIGNES suspects : ≥ `min_cells` pixels consécutifs dans la même direction. Classe : « plan d'eau » (≥ 50 % sur l'eau de l'étage 1), « bord du contour » (≥ 50 % contre l'extérieur du
    bassin) ou « ligne droite ». Renvoie [{"lien", "n", "longueur_m", "classe", "x", "y"}]."""
    ny, nx = dem["inside"].shape
    ins = dem["inside"]
    out = []

    def edge(k):
        r, c = divmod(k, W)
        r, c = r - 1, c - 1
        for dr, dc in FH4_D8:
            r1, c1 = r + dr, c + dc
            if not (0 <= r1 < ny and 0 <= c1 < nx and ins[r1, c1]):
                return True
        return False
    for lid, L in enumerate(links):
        cs = L["cells"]
        run = [cs[0]] if cs else []
        prev_d = None
        runs = []
        for a, b in zip(cs[:-1], cs[1:]):
            d = b - a
            if d == prev_d:
                run.append(b)
            else:
                if len(run) >= min_cells:
                    runs.append(run)
                run, prev_d = [a, b], d
        if len(run) >= min_cells:
            runs.append(run)
        for run in runs:
            nl = sum(1 for k in run if dem["lake"][divmod(k, W)[0] - 1, divmod(k, W)[1] - 1])
            ne = sum(1 for k in run if edge(k))
            cl = "plan d'eau" if nl >= 0.5 * len(run) else ("bord du contour" if ne >= 0.5 * len(run) else "ligne droite")
            x, y = fh4_cell_xy(dem, run[len(run) // 2], W)
            out.append({"lien": lid, "n": len(run), "longueur_m": len(run) * dem["step"], "classe": cl, "x": x, "y": y})
    return out


def fh4_compare_nets(netA, netC, inside, step):
    """Comparaison voie A / voie C : nombre de sous-bassins, longueur de réseau, surfaces et pourcentage de pixels dont le sous-bassin diffère après appariement (recouvrement maximal)."""
    import numpy as np
    a, c = netA["sb"], netC["sb"]
    m = inside & (a >= 0) & (c >= 0)
    nA, nC = len(netA["links"]), len(netC["links"])
    pairs = np.bincount(a[m].astype("int64") * (nC + 1) + c[m], minlength=(nA + 1) * (nC + 1)).reshape(nA + 1, nC + 1) if m.any() else np.zeros((nA + 1, nC + 1), int)
    best = pairs.argmax(axis=1)
    diff = int((best[a[m]] != c[m]).sum()) if m.any() else 0
    ha = step * step / 1e4
    return {"n_A": nA, "n_C": nC, "reseau_A_km": sum(L["length_m"] for L in netA["links"]) / 1000.0, "reseau_C_km": sum(L["length_m"] for L in netC["links"]) / 1000.0,
            "surf_moy_A_ha": float(m.sum() * ha / max(1, nA)), "surf_moy_C_ha": float(m.sum() * ha / max(1, nC)), "pixels_differents_pct": 100.0 * diff / max(1, int(m.sum()))}


# ============================================================
# 4. CALCUL COMPLET : sous-bassins, réseau, statistiques (A de l'étage 2 en lecture seule), contrôles, fichiers
# ============================================================
def fh4_read_points(path, log=None):
    """Points (x, y) en EPSG:32738 d'une couche .gpkg / .shp (exutoires supplémentaires). Lecture seule ; CRS absent ⇒ Fh4Error."""
    ogr, osr, gdal = _fh4_ogr()
    ds = ogr.Open(path, 0)
    if ds is None:
        raise Fh4Error("Couche d'exutoires illisible : {}".format(path))
    lyr = ds.GetLayerByIndex(0)
    srs = lyr.GetSpatialRef()
    if srs is None:
        raise Fh4Error("CRS absent dans {} : arrêt, je ne devine pas.".format(path))
    srs = srs.Clone()
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(FH4_CRS)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(srs, dst)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone()
        g.Transform(tr)
        c = g.Centroid() if ogr.GT_Flatten(g.GetGeometryType()) != ogr.wkbPoint else g
        out.append((c.GetX(), c.GetY()))
    ds = None
    return out


def fh4_write_raster(path, arr, gt, proj, dtype, nodata, log=None):
    from osgeo import gdal
    import numpy as np
    tmp = fh4_tmp_path(path)
    o = gdal.GetDriverByName("GTiff").Create(tmp, arr.shape[1], arr.shape[0], 1, dtype, options=["COMPRESS=DEFLATE", "TILED=YES"])
    o.SetGeoTransform(gt)
    o.SetProjection(proj)
    o.SetMetadataItem("CALCUL_DATE", fh4_iso(time.time()))
    o.SetMetadataItem("ETAGE", "4 (FALKHYDRO+ sous-bassins)")
    b = o.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isfinite(arr), arr, nodata))
    o.FlushCache()
    o = None
    return fh4_commit(tmp, path, log)


def fh4_sb_on_grid(sb_arr, gt, proj, ref_gt, nx, ny):
    """Raster des sous-bassins (Int32) rééchantillonné au PLUS PROCHE VOISIN sur la grille de A (en mémoire)."""
    ogr, osr, gdal = _fh4_ogr()
    import numpy as np
    mem = gdal.GetDriverByName("MEM").Create("", sb_arr.shape[1], sb_arr.shape[0], 1, gdal.GDT_Int32)
    mem.SetGeoTransform(gt)
    mem.SetProjection(proj)
    mem.GetRasterBand(1).WriteArray(sb_arr.astype("int32"))
    mem.GetRasterBand(1).SetNoDataValue(0)
    xmin, ymax = ref_gt[0], ref_gt[3]
    out = gdal.Warp("", mem, format="MEM", outputBounds=(xmin, ymax + ref_gt[5] * ny, xmin + ref_gt[1] * nx, ymax), width=nx, height=ny, dstSRS=proj, resampleAlg="near",
                    outputType=gdal.GDT_Int32, srcNodata=0, dstNodata=0)
    a = out.GetRasterBand(1).ReadAsArray().astype("int32")
    out = None
    mem = None
    return a


def fh4_zonal_A(info1, info2, sb_no, gt, proj, water, nmax, log):
    """A de l'étage 2 (LECTURE SEULE) par sous-bassin, avec le MÊME masque que l'étage 3 (A valide ∩ classe > 0 ∩ pas eau). Aucun recalcul de A.
    Renvoie ({scénario: {"mean": [par n°], "loss": [...]}}, n_unit [...], n_land [...], notes)."""
    import numpy as np
    out, n_unit, n_land = {}, None, None
    for k, label in (("P_Itasy", "P Itasy (principal)"), ("P_ref", "P = 1 (complément)")):
        p = info2["A"].get(k)
        if not p:
            continue
        A, agt, apj, (nx, ny) = fh4_read_raster(p)
        sbA = fh4_sb_on_grid(sb_no, gt, proj, agt, nx, ny)
        cls, _re = fh4_classification_on_grid(info1["classification"], agt, nx, ny, apj, None)
        land = np.isfinite(A) & (cls > 0) & ~np.isin(cls, list(water))
        px_ha = abs(agt[1] * agt[5]) / 1e4
        ids = sbA.ravel()
        cnt_unit = np.bincount(ids, minlength=nmax + 1)
        m = land.ravel()
        cnt_land = np.bincount(ids[m], minlength=nmax + 1)
        sm = np.bincount(ids[m], weights=A.ravel()[m], minlength=nmax + 1)
        mean = np.where(cnt_land > 0, sm / np.maximum(cnt_land, 1), np.nan)
        out[k] = {"mean": mean.tolist(), "loss": (sm * px_ha).tolist(), "label": label}
        if n_unit is None:
            n_unit, n_land = (cnt_unit * px_ha).tolist(), (cnt_land * px_ha).tolist()
            out["_px_ha"] = px_ha
        log("📈 A {} : lu en lecture seule dans {} ({} pixels de terre classée dans les sous-bassins).".format(label, os.path.basename(p), int(cnt_land[1:].sum())))
    return out, n_unit, n_land


def fh4_prepare(p, log, progress=None, cancel=None):
    """Partie commune à l'aperçu des seuils et au calcul : entrées (lecture seule), MNT, comblement des cuvettes, D8, aire drainée. Renvoie le contexte {dirs, info1, info2, dem, hyd, wkt, basin_ha, water, ...}."""
    prog = progress or (lambda v: None)
    dirs = p.get("dirs") or fh4_dirs(p["root"], p["basin"])
    log("▶ Étage 4 {} — sous-bassins du bassin {} — lecture SEULE des étages 1 à 3.".format(FH4_VERSION, p["basin"]))
    info1 = fh4_find_stage1(dirs["e1"])
    log("📂 Étage 1 : classification {} ; {} classes.".format(os.path.basename(info1["classification"]), len(info1["classes"])))
    info2 = fh4_find_stage2(dirs["e2"], log)
    dem_path = os.path.join(dirs["e2"], "travail", "MNT_srtm.tif")
    if not os.path.isfile(dem_path):
        raise Fh4Error("MNT SRTM du cache de l'étage 2 introuvable ({}) : l'étage 4 ne télécharge aucun MNT ; relance l'étage 2 pour recréer son cache.".format(dem_path))
    log("🏔 MNT : {} (cache de l'étage 2, modifié le {}).".format(dem_path, fh4_iso(os.stat(dem_path).st_mtime)))
    wkt = fh4_basin_from_stage2(info2, log)
    if wkt is None:
        wkt = fh4_basin_footprint_wkt(info2["A"]["P_Itasy"], 1200)
        log("⚠ Contour officiel du bassin non retrouvé dans le manifeste de l'étage 2 : emprise des pixels valides de A utilisée (HYPOTHÈSE).")
    ogr, osr, gdal = _fh4_ogr()
    basin_ha = ogr.CreateGeometryFromWkt(wkt).GetArea() / 1e4
    water = fh4_water_codes(info1["classes"], dirs["e2"])
    prog(5)
    dem = fh4_load_dem(dem_path, wkt, info1["classification"], water, FH4_STEP_M, FH4_MARGE_M, log)
    log("🧮 Grille de calcul : {} × {} px de {} m (EPSG:{}) ; {} pixels dans le bassin ({} ha).".format(dem["z"].shape[1], dem["z"].shape[0], int(dem["step"]), FH4_CRS, int(dem["inside"].sum()), fh4_fr(basin_ha, 0)))
    lake_ha = float(dem["lake"].sum()) * dem["step"] ** 2 / 1e4
    if lake_ha > 0:
        log("💧 Lac / eau (classe eau de l'étage 1, lecture seule) : {} ha dans le bassin. HYPOTHÈSE : plan d'eau NON forcé ; le comblement des cuvettes (gradient epsilon) fait sortir l'eau "
            "par son point bas ; à valider (voir limites).".format(fh4_fr(lake_ha, 0)))
    voie = p.get("voie") or "auto"
    exes = p.get("exes") or {}
    have = all(exes.get(n) for n in ("pitremove", "d8flowdir", "aread8"))
    use_a = voie == "A" or (voie == "auto" and have)
    if voie == "A" and not have:
        raise Fh4Error("Voie A demandée mais TauDEM (pitremove, d8flowdir, aread8) est introuvable : lance « Inspecter QSWAT+ / TauDEM » ou choisis la voie C (NumPy).")
    hyd = None
    if use_a:
        log("🛣 Voie A : TauDEM par sous-processus (mêmes algorithmes que QSWAT+).")
        try:
            hyd = fh4_hydro_taudem(dem, exes, os.path.join(dirs["e4"], "travail_taudem"), log, cancel, p.get("procs") or 4)
        except Fh4Error as ex:
            if voie == "A":
                raise
            log("⚠ TauDEM inutilisable ({}) : BASCULE AUTOMATIQUE sur la voie C (NumPy, moins éprouvée).".format(str(ex).split(" — ")[0][:200]))
    if hyd is None:
        log("🛣 Voie C : D8 et comblement des cuvettes par NumPy (algorithme documenté, moins éprouvé que TauDEM).")
        hyd = fh4_hydro_numpy(dem, log, cancel)
    lake_mode = p.get("lake_mode") or "asis"
    if lake_ha > 0:
        if lake_mode == "flat":
            hyd = fh4_lake_force(dem, hyd, log, cancel)
        else:
            log("💧 Option lac : (i) laisser comme est (V1) — choix par défaut, NON confirmé par l'utilisateur ; options (ii) plan d'eau forcé et (iii) masse d'eau SWAT+ dans l'interface.")
    prog(40)
    return {"lake_mode": lake_mode, "dirs": dirs, "info1": info1, "info2": info2, "dem_path": dem_path, "wkt": wkt, "basin_ha": basin_ha, "water": water, "dem": dem, "hyd": hyd, "lake_ha": lake_ha}


def fh4_run(p, log, progress=None, cancel=None):
    """Étage 4, livraison 1. p = {"root","basin","thr_idx" (0..2) | "thr_pct","voie" ('auto'|'A'|'C'),"exes","extra_points" [(x,y)]|"extra_file","petit_ha","ctx" (contexte de fh4_prepare, facultatif)}.
    Écrit sous Etages/04_swat/<bassin>/ ; ne touche JAMAIS aux étages 1–3."""
    import numpy as np
    prog = progress or (lambda v: None)
    t0 = time.time()
    ctx = p.get("ctx") or fh4_prepare(p, log, prog, cancel)
    dirs, info1, info2, dem_path, wkt, basin_ha, water, dem, hyd, lake_ha = (ctx[k] for k in ("dirs", "info1", "info2", "dem_path", "wkt", "basin_ha", "water", "dem", "hyd", "lake_ha"))
    os.makedirs(dirs["e4"], exist_ok=True)
    px_ha = dem["step"] ** 2 / 1e4
    opts = fh4_threshold_options(basin_ha)
    if p.get("thr_pct") is not None:
        pct = float(p["thr_pct"])
        thr_label = "personnalisé"
    else:
        o = opts[int(p.get("thr_idx", FH4_DEFAUT))]
        pct, thr_label = o["pct"], o["libelle"]
    thr_cells = max(2, int(round(basin_ha * pct / 100.0 / px_ha)))
    log("📐 Seuil de définition du réseau : {} % du bassin = {} km² ({} pixels) — « {} » — HYPOTHÈSE, à valider par l'utilisateur.".format(
        fh4_fr(pct, 1), fh4_fr(thr_cells * px_ha / 100.0, 2), thr_cells, thr_label))
    extra = list(p.get("extra_points") or [])
    if p.get("extra_file"):
        extra += fh4_read_points(p["extra_file"])
    net0 = fh4_network(dem, hyd, thr_cells, (), cancel)
    calage = None
    if p.get("outlet_xy"):                              # exutoire CALÉ sur un point choisi par l'utilisateur (confirmé dans l'interface) : le bassin est limité à son amont
        sn = fh4_snap_outlets(dem, hyd, net0, [tuple(p["outlet_xy"])], 300.0, log)
        if not sn:
            raise Fh4Error("Calage impossible : aucun pixel du réseau à moins de 300 m du point indiqué.")
        k_o = sn[0][0]
        reach = bytearray(len(hyd["acc"]))
        reach[k_o] = 1
        nxt0 = net0["nxt"]
        for k in reversed(hyd["order"]):
            if k != k_o:
                d = nxt0[k]
                if d >= 0 and reach[d]:
                    reach[k] = 1
        ny_, nx_ = dem["inside"].shape
        W_ = net0["W"]
        rin = np.frombuffer(bytes(reach), dtype="uint8").reshape(ny_ + 2, W_)[1:-1, 1:-1].astype(bool)
        d2 = hyd["dirs"].copy()
        d2[~rin] = 0
        d2[(k_o // W_) - 1, (k_o % W_) - 1] = 0
        dem = dict(dem, inside=rin, z=np.where(rin, dem["z"], np.nan), lake=dem["lake"] & rin)
        hyd = dict(hyd, dirs=d2, order=[k for k in hyd["order"] if reach[k]])
        basin_ha = float(rin.sum()) * px_ha
        net0 = fh4_network(dem, hyd, thr_cells, (), cancel)
        calage = {"point": tuple(p["outlet_xy"]), "distance_m": sn[0][1], "surface_ha": basin_ha}
        log("📍 Exutoire CALÉ sur le point indiqué (à {} m du pixel de réseau) : bassin limité à son amont, {} ha.".format(int(sn[0][1]), fh4_fr(basin_ha, 0)))
    snapped = fh4_snap_outlets(dem, hyd, net0, extra, 300.0, log) if extra else []
    for k_, dist_, a_ in snapped:
        log("📍 Exutoire supplémentaire accroché au pixel de réseau le plus drainé à {} m.".format(int(dist_)))
    net = fh4_network(dem, hyd, thr_cells, [s[0] for s in snapped], cancel) if snapped else net0
    links = net["links"]
    straight = fh4_straight_segments(links, dem, net["W"])
    for sg_ in straight:
        log("⚠ Tronçon rectiligne suspect : {} pixels ({} m) en X = {}, Y = {} — classe « {} ».".format(sg_["n"], int(sg_["longueur_m"]), fh4_fr(sg_["x"], 0), fh4_fr(sg_["y"], 0), sg_["classe"]))
    if not straight:
        log("✔ Aucun tronçon de rivière rectiligne suspect (≥ 20 pixels dans la même direction).")
    compare = None
    if hyd.get("voie") == "A" and p.get("compare", True):
        hydC = fh4_hydro_numpy(dem, log, cancel)
        netC = fh4_network(dem, hydC, thr_cells, [s_[0] for s_ in snapped], cancel)
        compare = fh4_compare_nets(net, netC, dem["inside"], dem["step"])
        log("🔁 Comparaison voie A / voie C : {} contre {} sous-bassins ; réseau {} km contre {} km ; {} % des pixels changent de sous-bassin.".format(
            compare["n_A"], compare["n_C"], fh4_fr(compare["reseau_A_km"], 1), fh4_fr(compare["reseau_C_km"], 1), fh4_fr(compare["pixels_differents_pct"], 1)))
    prog(60)
    # numérotation stable : par aire drainée décroissante à l'exutoire du tronçon (SB_001 = le plus grand)
    order = sorted(range(len(links)), key=lambda i: (-links[i]["acc_end"], i))
    no_of = {lid: n + 1 for n, lid in enumerate(order)}
    lut = np.zeros(len(links) + 1, "int32")
    for lid, n in no_of.items():
        lut[lid + 1] = n
    sb_no = lut[np.where(net["sb"] >= 0, net["sb"] + 1, 0)]
    nmax = len(links)
    ids = sb_no.ravel()
    cnt = np.bincount(ids, minlength=nmax + 1)
    z = dem["z"]
    zr = np.where(np.isfinite(z), z, 0.0).ravel()
    zsum = np.bincount(ids, weights=zr, minlength=nmax + 1)
    srt = np.argsort(ids, kind="stable")
    ids_s, z_s = ids[srt], np.where(np.isfinite(z), z, np.nan).ravel()[srt]
    first = np.searchsorted(ids_s, np.arange(nmax + 2))
    zmin, zmax = [np.nan] * (nmax + 1), [np.nan] * (nmax + 1)
    for n in range(1, nmax + 1):
        seg = z_s[first[n]:first[n + 1]]
        if seg.size and np.isfinite(seg).any():
            zmin[n], zmax[n] = float(np.nanmin(seg)), float(np.nanmax(seg))
    gy, gx = np.gradient(np.where(np.isfinite(z), z, np.nan), dem["step"])
    slope = np.hypot(gx, gy) * 100.0
    ssum = np.bincount(ids, weights=np.nan_to_num(slope).ravel(), minlength=nmax + 1)
    scnt = np.bincount(ids, weights=np.isfinite(slope).ravel().astype(float), minlength=nmax + 1)
    fl = np.zeros(nmax + 1)
    np.maximum.at(fl, ids, net["flen"].ravel())
    lake_cnt = np.bincount(ids, weights=dem["lake"].ravel().astype(float), minlength=nmax + 1)
    Astat, a_unit_ha, a_land_ha = fh4_zonal_A(info1, info2, sb_no, dem["gt"], dem["proj"], water, nmax, log)
    scen = [k for k in ("P_Itasy", "P_ref") if k in Astat]
    prog(75)
    # sortie principale = tronçon de sortie à plus forte aire drainée
    exits = [i for i, L in enumerate(links) if L["exit"]]
    main = max(exits, key=lambda i: links[i]["acc_end"]) if exits else None
    rows = []
    for lid in order:
        L = links[lid]
        n = no_of[lid]
        down = no_of[L["down"]] if L["down"] is not None else None
        r = {"id": "SB_{:03d}".format(n), "no": n, "aval": ("SB_{:03d}".format(down) if down else None), "aval_no": down, "exutoire": lid == main,
             "sortie_secondaire": bool(L["exit"] and lid != main), "surf_ha": float(cnt[n] * px_ha), "z_min": zmin[n], "z_moy": float(zsum[n] / cnt[n]) if cnt[n] else float("nan"), "z_max": zmax[n],
             "pente_pct": float(ssum[n] / scnt[n]) if scnt[n] else float("nan"), "long_bassin_m": float(fl[n]), "long_cours_m": float(L["length_m"]), "ordre": L["order"],
             "n_amont": len(L["ups"]), "lac_ha": float(lake_cnt[n] * px_ha), "pct_terre": (a_land_ha[n] / a_unit_ha[n] * 100.0) if a_unit_ha and a_unit_ha[n] else float("nan"),
             "terre_ha": a_land_ha[n] if a_land_ha else float("nan"), "petit": bool(cnt[n] * px_ha < p.get("petit_ha", FH4_PETIT_SB_HA)), "s": {}}
        for k in scen:
            r["s"][k] = {"mean": Astat[k]["mean"][n], "loss": Astat[k]["loss"][n]}
        rows.append(r)
    tot = {"surf_ha": float(sum(r["surf_ha"] for r in rows)), "terre_ha": float(np.nansum([r["terre_ha"] for r in rows])), "s": {}}
    for k in scen:
        ls = float(np.nansum([r["s"][k]["loss"] for r in rows]))
        tot["s"][k] = {"loss": ls, "mean": (ls / tot["terre_ha"]) if tot["terre_ha"] else float("nan")}
    # contrôles
    ctl = []
    unatt = float((sb_no[dem["inside"]] == 0).sum() * px_ha)
    ecart = (tot["surf_ha"] - basin_ha) / basin_ha * 100.0 if basin_ha else float("nan")
    ctl.append(("Somme des surfaces des sous-bassins = surface du bassin", "{} ha contre {} ha : écart {} % ; non attribué (l'eau quitte le bassin sans passer par le réseau) : {} ha ({} %)".format(
        fh4_fr(tot["surf_ha"], 1), fh4_fr(basin_ha, 1), fh4_fr(ecart, 2), fh4_fr(unatt, 1), fh4_fr(unatt / basin_ha * 100.0 if basin_ha else float("nan"), 2)), abs(ecart) <= 1.0))
    sans = [r["id"] for r in rows if r["aval"] is None and not r["exutoire"]]
    ctl.append(("Sous-bassins sans aval (hors exutoire principal)", "{}{}".format(len(sans), " : " + ", ".join(sans[:12]) + (" …" if len(sans) > 12 else "") if sans else " : aucun"), not sans))
    boucle = False
    for L in links:
        j, steps = L["down"], 0
        while j is not None and steps <= len(links):
            j, steps = links[j]["down"], steps + 1
        boucle = boucle or steps > len(links)
    ctl.append(("Boucles dans les liens amont → aval", "aucune" if not boucle else "BOUCLE DÉTECTÉE", not boucle))
    petits = [r["id"] for r in rows if r["petit"]]
    ctl.append(("Très petits sous-bassins (< {} ha, seuil d'affichage HYPOTHÈSE)".format(fh4_fr(p.get("petit_ha", FH4_PETIT_SB_HA), 0)), "{}{}".format(len(petits), " : " + ", ".join(petits[:12]) + (" …" if len(petits) > 12 else "") if petits else " : aucun"), not petits))
    outlet = None
    if main is not None:
        xo, yo = fh4_cell_xy(dem, links[main]["cells"][-1], net["W"])
        outlet = {"xy": (xo, yo), "acc_km2": links[main]["acc_end"] * px_ha / 100.0, "stage2": None, "dist_m": None}
        s2 = p.get("outlet_stage2")
        if s2:
            outlet["stage2"], outlet["dist_m"] = tuple(s2), math.hypot(xo - s2[0], yo - s2[1])
            notable = outlet["dist_m"] > FH4_OUTLET_TOL_M
            ctl.append(("Exutoire principal / point « Exutoire » de l'étage 2", "pixel trouvé X = {}, Y = {} ({} km² drainés) ; point de l'étage 2 X = {}, Y = {} ; distance {} m{}".format(
                fh4_fr(xo, 0), fh4_fr(yo, 0), fh4_fr(outlet["acc_km2"], 1), fh4_fr(s2[0], 0), fh4_fr(s2[1], 0), fh4_fr(outlet["dist_m"], 0),
                " — ÉCART NOTABLE (> {} m, HYPOTHÈSE) : caler l'exutoire sur ce point est proposé, jamais appliqué sans confirmation".format(int(FH4_OUTLET_TOL_M)) if notable else ""), not notable))
        else:
            ctl.append(("Exutoire principal", "pixel de sortie à plus forte aire drainée ({} km²) en X = {}, Y = {} ; point « Exutoire » de l'étage 2 non fourni : distance non calculée (HYPOTHÈSE : exutoire non déplacé)".format(
                fh4_fr(outlet["acc_km2"], 1), fh4_fr(xo, 0), fh4_fr(yo, 0)), True))
    if len(exits) > 1:
        ctl.append(("Sorties secondaires du bassin", "{} tronçon(s) quittent le bassin ailleurs (petits cours en bordure du contour)".format(len(exits) - 1), True))
    for t_, v_, ok_ in ctl:
        log("{} {} : {}".format("✔" if ok_ else "⚠", t_, v_))
    log("🧭 {} sous-bassins ; réseau de {} km ; ordre de Strahler maximal {}.".format(len(rows), fh4_fr(sum(L["length_m"] for L in links) / 1000.0, 1), max([L["order"] for L in links] or [0])))
    # fichiers
    res = {"basin": p["basin"], "dirs": dirs, "rows": rows, "total": tot, "scen": scen, "controles": ctl, "seuil": {"pct": pct, "libelle": thr_label, "pixels": thr_cells, "km2": thr_cells * px_ha / 100.0, "statut": "HYPOTHÈSE — à valider par l'utilisateur"},
           "voie": hyd.get("voie"), "basin_ha": basin_ha, "lake_ha": lake_ha, "info1": info1, "info2": info2, "dem_path": dem_path, "wkt": wkt, "step": dem["step"], "fill_n": hyd.get("fill_n"),
           "n_exits": len(exits), "extra_outlets": len(snapped), "unattributed_ha": unatt, "outlet": outlet, "calage": calage, "straight": straight, "comparaison": compare, "lake_mode": ctx.get("lake_mode", "asis"),
           "thr_apercu": p.get("thr_apercu"), "named_rivers": p.get("rivers_named")}
    prog(85)
    if p.get("rivers_named"):
        try:
            res["river_names"] = fh4_name_rivers(p["rivers_named"], dem, net, links, log)
        except Exception as ex:
            log("⚠ Noms de rivières non joints : {}".format(ex))
    res["lake_by_sb"] = [(r["id"], r["lac_ha"]) for r in rows if r["lac_ha"] > 0]
    if res["lake_by_sb"]:
        log("💧 Sous-bassins traversés par le lac : {}.".format(" ; ".join("{} ({} ha)".format(a, fh4_fr(b, 0)) for a, b in res["lake_by_sb"])))
    res["files"] = fh4_write_outputs(res, dem, hyd, net, links, no_of, sb_no, main, log)
    res["duree_s"] = time.time() - t0
    res["rapport"] = fh4_write_text(os.path.join(dirs["e4"], "rapport_etage4.txt"), fh4_report_text(res), log)
    res["manifeste"] = fh4_write_manifest(res, p, log)
    prog(100)
    log("✅ Étage 4 terminé en {} s : {} sous-bassins ; résultats dans {}.".format(fh4_fr(res["duree_s"], 1), len(rows), dirs["e4"]))
    log("ℹ " + FH4_PHRASE_A)
    return res


def fh4_write_outputs(res, dem, hyd, net, links, no_of, sb_no, main, log):
    """GeoPackage (sous_bassins, rivieres, jonctions, exutoires, lac), rasters (MNT comblé, directions, aires drainées, sous-bassins) et CSV. Noms stables ; verrouillé ⇒ nom horodaté."""
    ogr, osr, gdal = _fh4_ogr()
    import numpy as np
    d4, W = res["dirs"]["e4"], net["W"]
    files = {}
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(FH4_CRS)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    gp = os.path.join(d4, "sous_bassins_utm38s.gpkg")
    tmp = fh4_tmp_path(gp)
    if os.path.exists(tmp):
        os.remove(tmp)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(tmp)
    # polygones par raster → union par identifiant
    mem = gdal.GetDriverByName("MEM").Create("", sb_no.shape[1], sb_no.shape[0], 1, gdal.GDT_Int32)
    mem.SetGeoTransform(dem["gt"])
    mem.SetProjection(dem["proj"])
    mem.GetRasterBand(1).WriteArray(sb_no.astype("int32"))
    tl = ogr.GetDriverByName("Memory").CreateDataSource("t")
    tlyr = tl.CreateLayer("t", srs, ogr.wkbPolygon)
    tlyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(mem.GetRasterBand(1), None, tlyr, 0, [])
    parts = {}
    for f in tlyr:
        v = f.GetField("v")
        if v > 0:
            parts.setdefault(v, []).append(f.GetGeometryRef().Clone())
    mem = None
    by_no = {r["no"]: r for r in res["rows"]}
    lyr = ds.CreateLayer("sous_bassins", srs, ogr.wkbMultiPolygon)
    flds = [("id_sb", ogr.OFTString), ("aval", ogr.OFTString), ("surface_ha", ogr.OFTReal), ("z_min", ogr.OFTReal), ("z_moy", ogr.OFTReal), ("z_max", ogr.OFTReal), ("pente_pct", ogr.OFTReal),
            ("long_bassin_m", ogr.OFTReal), ("long_cours_m", ogr.OFTReal), ("ordre", ogr.OFTInteger), ("A_moy_It", ogr.OFTReal), ("perte_It_t_an", ogr.OFTReal), ("A_moy_P1", ogr.OFTReal),
            ("perte_P1_t_an", ogr.OFTReal), ("pct_terre", ogr.OFTReal), ("lac_ha", ogr.OFTReal), ("no", ogr.OFTInteger)]
    for n_, t_ in flds:
        lyr.CreateField(ogr.FieldDefn(n_, t_))
    for no in sorted(parts):
        col = ogr.Geometry(ogr.wkbGeometryCollection)
        for g in parts[no]:
            col.AddGeometry(g)
        try:
            u = col.UnionCascaded()
        except Exception:
            u = col
        u = _fh4_polys_only(u)
        r = by_no[no]
        ft = ogr.Feature(lyr.GetLayerDefn())
        vals = {"id_sb": r["id"], "aval": r["aval"] or "", "surface_ha": r["surf_ha"], "z_min": r["z_min"], "z_moy": r["z_moy"], "z_max": r["z_max"], "pente_pct": r["pente_pct"], "long_bassin_m": r["long_bassin_m"],
                "long_cours_m": r["long_cours_m"], "ordre": r["ordre"], "pct_terre": r["pct_terre"], "lac_ha": r["lac_ha"], "no": no}
        for k_, pf in (("P_Itasy", "It"), ("P_ref", "P1")):
            if k_ in r["s"]:
                vals["A_moy_" + pf], vals["perte_{}_t_an".format(pf)] = r["s"][k_]["mean"], r["s"][k_]["loss"]
        for k_, v_ in vals.items():
            if v_ is not None and not (isinstance(v_, float) and math.isnan(v_)):
                ft.SetField(k_, v_)
        ft.SetGeometry(u)
        lyr.CreateFeature(ft)
    # rivières
    rl = ds.CreateLayer("rivieres", srs, ogr.wkbLineString)
    sus = {}
    for sg_ in res.get("straight") or []:
        sus.setdefault(sg_["lien"], sg_["classe"])
    names = res.get("river_names") or {}
    for n_, t_ in (("id_riv", ogr.OFTString), ("id_sb", ogr.OFTString), ("strahler", ogr.OFTInteger), ("aval_sb", ogr.OFTString), ("longueur_m", ogr.OFTReal), ("acc_km2", ogr.OFTReal), ("suspect", ogr.OFTString), ("nom", ogr.OFTString)):
        rl.CreateField(ogr.FieldDefn(n_, t_))
    jl = ds.CreateLayer("jonctions", srs, ogr.wkbPoint)
    for n_, t_ in (("id_sb", ogr.OFTString), ("type", ogr.OFTString), ("acc_km2", ogr.OFTReal)):
        jl.CreateField(ogr.FieldDefn(n_, t_))
    px_km2 = dem["step"] ** 2 / 1e6
    for lid, L in enumerate(links):
        cs = L["cells"][:]
        d = net["nxt"][cs[-1]]
        if L["down"] is not None:
            cs.append(d)
        if L["exit"]:                                    # le tracé rejoint le bord du bassin (premier voisin hors bassin)
            ny_, nx_ = dem["inside"].shape
            for o_ in hyd["offs"][1:]:
                j_ = cs[-1] + o_
                r_, c_ = divmod(j_, W)
                if not (1 <= r_ <= ny_ and 1 <= c_ <= nx_ and dem["inside"][r_ - 1, c_ - 1]):
                    cs.append(j_)
                    break
        pts = [fh4_cell_xy(dem, k, W) for k in cs]
        if len(pts) >= 2:
            g = ogr.Geometry(ogr.wkbLineString)
            for x_, y_ in pts:
                g.AddPoint_2D(x_, y_)
            ft = ogr.Feature(rl.GetLayerDefn())
            r = by_no[no_of[lid]]
            ft.SetField("id_riv", "RV_{:03d}".format(no_of[lid]))
            ft.SetField("id_sb", r["id"])
            ft.SetField("strahler", L["order"])
            ft.SetField("aval_sb", r["aval"] or "")
            ft.SetField("longueur_m", L["length_m"])
            ft.SetField("acc_km2", L["acc_end"] * px_km2)
            ft.SetField("suspect", sus.get(lid, ""))
            ft.SetField("nom", names.get(lid, ""))
            ft.SetGeometry(g)
            rl.CreateFeature(ft)
        x_, y_ = fh4_cell_xy(dem, L["cells"][-1], W)
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(x_, y_)
        ft = ogr.Feature(jl.GetLayerDefn())
        ft.SetField("id_sb", by_no[no_of[lid]]["id"])
        ft.SetField("type", "exutoire du bassin" if lid == main else ("sortie secondaire" if L["exit"] else "confluence"))
        ft.SetField("acc_km2", L["acc_end"] * px_km2)
        ft.SetGeometry(pt)
        jl.CreateFeature(ft)
    # lac (eau de l'étage 1) : polygones
    ll = ds.CreateLayer("lac", srs, ogr.wkbPolygon)
    ll.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    if dem["lake"].any():
        m2 = gdal.GetDriverByName("MEM").Create("", sb_no.shape[1], sb_no.shape[0], 1, gdal.GDT_Byte)
        m2.SetGeoTransform(dem["gt"])
        m2.SetProjection(dem["proj"])
        m2.GetRasterBand(1).WriteArray(dem["lake"].astype("uint8"))
        t2 = ogr.GetDriverByName("Memory").CreateDataSource("t2")
        l2 = t2.CreateLayer("t2", srs, ogr.wkbPolygon)
        l2.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
        gdal.Polygonize(m2.GetRasterBand(1), None, l2, 0, [])
        for f in l2:
            if f.GetField("v") == 1:
                ft = ogr.Feature(ll.GetLayerDefn())
                ft.SetField("v", 1)
                ft.SetGeometry(f.GetGeometryRef().Clone())
                ll.CreateFeature(ft)
        m2 = None
    ds = None
    files["gpkg"] = fh4_commit(tmp, gp, log)
    files["sb_id_tif"] = fh4_write_raster(os.path.join(d4, "sous_bassins_id.tif"), sb_no.astype("float64"), dem["gt"], dem["proj"], gdal.GDT_Int32, 0, log)
    files["mnt_comble_tif"] = fh4_write_raster(os.path.join(d4, "mnt_comble_utm38s.tif"), hyd["zf"], dem["gt"], dem["proj"], gdal.GDT_Float32, -9999, log)
    files["direction_tif"] = fh4_write_raster(os.path.join(d4, "direction_d8.tif"), np.where(dem["inside"], hyd["dirs"], 0).astype("float64"), dem["gt"], dem["proj"], gdal.GDT_Byte, 0, log)
    ny, nx = dem["z"].shape
    aa = np.array(hyd["acc"], dtype="float64").reshape(ny + 2, W)[1:-1, 1:-1] * (dem["step"] ** 2 / 1e6)
    files["aire_drainee_tif"] = fh4_write_raster(os.path.join(d4, "aire_drainee_km2.tif"), np.where(dem["inside"], aa, np.nan), dem["gt"], dem["proj"], gdal.GDT_Float32, -9999, log)
    files["mnt_tif"] = fh4_write_raster(os.path.join(d4, "mnt_utm38s.tif"), np.where(dem["inside"], dem["z"], np.nan), dem["gt"], dem["proj"], gdal.GDT_Float32, -9999, log)
    gy, gx = np.gradient(np.where(dem["inside"], dem["z"], np.nan), dem["step"])
    files["pente_tif"] = fh4_write_raster(os.path.join(d4, "pente_pct.tif"), np.where(dem["inside"], np.hypot(gx, gy) * 100.0, np.nan), dem["gt"], dem["proj"], gdal.GDT_Float32, -9999, log)
    cols = ["id_sb", "aval", "surface_ha", "z_min", "z_moy", "z_max", "pente_moy_pct", "long_bassin_m", "long_cours_m", "ordre_strahler", "terre_classee_ha", "pct_terre_classee", "lac_ha"]
    for k in res["scen"]:
        cols += ["A_moy_t_ha_an_" + k, "perte_t_an_" + k]
    lines = []
    for r in res["rows"]:
        line = [r["id"], r["aval"] or "", fh4_csvnum(r["surf_ha"], 2), fh4_csvnum(r["z_min"], 1), fh4_csvnum(r["z_moy"], 1), fh4_csvnum(r["z_max"], 1), fh4_csvnum(r["pente_pct"], 2),
                fh4_csvnum(r["long_bassin_m"], 0), fh4_csvnum(r["long_cours_m"], 0), r["ordre"], fh4_csvnum(r["terre_ha"], 2), fh4_csvnum(r["pct_terre"], 1), fh4_csvnum(r["lac_ha"], 2)]
        for k in res["scen"]:
            line += [fh4_csvnum(r["s"][k]["mean"], 2), fh4_csvnum(r["s"][k]["loss"], 0)]
        lines.append(line)
    tl_ = ["TOTAL BASSIN", "", fh4_csvnum(res["total"]["surf_ha"], 2), "", "", "", "", "", "", "", fh4_csvnum(res["total"]["terre_ha"], 2), "", ""]
    for k in res["scen"]:
        tl_ += [fh4_csvnum(res["total"]["s"][k]["mean"], 2), fh4_csvnum(res["total"]["s"][k]["loss"], 0)]
    lines.append(tl_)
    files["stats_csv"] = fh4_write_csv(os.path.join(d4, "stats_sous_bassins.csv"), cols, lines, "A = perte potentielle brute, non validée, pas un apport de sédiments au lac ; écrit le " + fh4_iso(time.time()), log)
    files["liens_csv"] = fh4_write_csv(os.path.join(d4, "table_sb_aval.csv"), ["id_sb", "sb_aval", "ordre_strahler"], [[r["id"], r["aval"] or "", r["ordre"]] for r in res["rows"]], None, log)
    return files


def fh4_report_text(res):
    s = res["seuil"]
    L = ["FALKHYDRO+ — ÉTAGE 4 {} — sous-bassins et réseau hydrographique (livraison 1)".format(FH4_VERSION), "Bassin : {} — calcul du {}".format(res["basin"], fh4_iso(time.time())), "",
         "ENTRÉES (lecture seule)", "  MNT : {} (cache de l'étage 2, modifié le {})".format(res["dem_path"], fh4_iso(os.stat(res["dem_path"]).st_mtime)),
         "  A étage 2 : " + " ; ".join("{} = {}".format(k, os.path.basename(v)) for k, v in res["info2"]["A"].items() if v), "  Classification étage 1 : {}".format(os.path.basename(res["info1"]["classification"])), "",
         "MÉTHODE", "  Voie {} ({}).".format(res["voie"], "TauDEM par sous-processus, non testée avec les vrais exécutables dans cette livraison" if res["voie"] == "A" else "NumPy : comblement des cuvettes (priority-flood, epsilon), D8, aire drainée ; moins éprouvée que TauDEM"),
         "  Grille de {} m en EPSG:{} (MNT SRTM 30 m interpolé en bilinéaire, altitudes non modifiées autrement).".format(int(res["step"]), FH4_CRS),
         "  Seuil de définition du réseau : {} % du bassin = {} km² ({} pixels) — « {} » — {}.".format(fh4_fr(s["pct"], 1), fh4_fr(s["km2"], 2), s["pixels"], s["libelle"], s["statut"]),
         "  Un sous-bassin par tronçon de rivière (sous-bassin = tronçon + versants qui s'y écoulent) ; ordre de Strahler par tronçon.",
         "  Lac / eau (étage 1) : {} ha ; HYPOTHÈSE : plan d'eau non forcé, écoulement obtenu par le gradient du comblement ; à valider.".format(fh4_fr(res["lake_ha"], 0)), "",
         "RÉSULTATS : {} sous-bassins, surface {} ha (bassin {} ha).".format(len(res["rows"]), fh4_fr(res["total"]["surf_ha"], 0), fh4_fr(res["basin_ha"], 0))]
    for k in res["scen"]:
        t = res["total"]["s"][k]
        L.append("  {} : A moyen {} t/ha/an ; perte totale {} Mt/an (lecture seule de l'étage 2).".format(_FH4_NOM_SC[k], fh4_fr(t["mean"], 2), fh4_fr(t["loss"] / 1e6, 2)))
    if res.get("calage"):
        L += ["", "EXUTOIRE CALÉ (choix de l'utilisateur) : point X = {}, Y = {} ; bassin limité à son amont ({} ha).".format(fh4_fr(res["calage"]["point"][0], 0), fh4_fr(res["calage"]["point"][1], 0), fh4_fr(res["calage"]["surface_ha"], 0))]
    L += ["", "LAC : option {} ; sous-bassins traversés : {}".format({"asis": "(i) laissé comme est (V1)", "flat": "(ii) plan d'eau forcé, un pixel de sortie (HYPOTHÈSE)"}.get(res.get("lake_mode"), "?"),
                                                                    " ; ".join("{} ({} ha)".format(a, fh4_fr(b, 0)) for a, b in res.get("lake_by_sb") or []) or "aucun")]
    L += ["", "TRONÇONS RECTILIGNES SUSPECTS : " + (str(len(res.get("straight") or [])) + " — " + " ; ".join("{} px, classe {}, X {} Y {}".format(g["n"], g["classe"], fh4_fr(g["x"], 0), fh4_fr(g["y"], 0)) for g in res["straight"]) if res.get("straight") else "aucun")]
    if res.get("comparaison"):
        c = res["comparaison"]
        L += ["", "COMPARAISON VOIE A (TauDEM) / VOIE C (NumPy)", "  sous-bassins : {} contre {} ; réseau : {} km contre {} km ; surface moyenne d'un sous-bassin : {} ha contre {} ha ; pixels qui changent de sous-bassin : {} %.".format(
            c["n_A"], c["n_C"], fh4_fr(c["reseau_A_km"], 1), fh4_fr(c["reseau_C_km"], 1), fh4_fr(c["surf_moy_A_ha"], 1), fh4_fr(c["surf_moy_C_ha"], 1), fh4_fr(c["pixels_differents_pct"], 1))]
    elif res.get("voie") == "C":
        L += ["", "VOIE C (NumPy) : moins éprouvée ; TauDEM absent ou non choisi : aucune comparaison A / C."]
    L += ["", "CONTRÔLES"] + ["  {} {} : {}".format("✔" if ok else "⚠", a, b) for a, b, ok in res["controles"]]
    L += ["", "LIMITES"] + ["  - " + x for x in FH4_LIMITES]
    return "\n".join(L) + "\n"


def fh4_amont_signature(info1, info2, dem_path):
    sig = []
    for k, label in (("P_Itasy", "A P Itasy"), ("P_ref", "A P = 1")):
        if info2["A"].get(k):
            sig.append(fh4_sig(info2["A"][k], 2, label))
    if info2.get("manifeste_path"):
        sig.append(fh4_sig(info2["manifeste_path"], 2, "manifeste étage 2"))
    if dem_path and os.path.isfile(dem_path):
        sig.append(fh4_sig(dem_path, 2, "MNT SRTM (cache étage 2)"))
    sig.append(fh4_sig(info1["classification"], 1, "classification ENVI"))
    return sig


def fh4_write_manifest(res, p, log):
    d4 = res["dirs"]["e4"]
    data = {"etage": 4, "nom": "Sous-bassins", "version": FH4_VERSION, "livraison": 1, "date": fh4_iso(time.time()), "bassin": res["basin"], "voie": res["voie"], "seuil": res["seuil"],
            "grille": {"crs": "EPSG:{}".format(FH4_CRS), "pas_m": res["step"]}, "amont": fh4_amont_signature(res["info1"], res["info2"], res["dem_path"]),
            "controles": [{"controle": a, "resultat": b, "ok": bool(ok)} for a, b, ok in res["controles"]], "n_sous_bassins": len(res["rows"]), "surface_bassin_ha": res["basin_ha"],
            "lac_ha": res["lake_ha"], "exutoires_supplementaires": res["extra_outlets"], "fichiers": res["files"], "limites": FH4_LIMITES,
            "totaux": {k: res["total"]["s"][k] for k in res["scen"]}, "hypotheses": ["seuil de définition du réseau", "traitement du lac (non forcé)", "exutoire principal = sortie à plus forte aire drainée",
                                                                                      "seuil d'affichage des très petits sous-bassins"]}
    return fh4_write_text(os.path.join(d4, "manifeste_etage4.json"), json.dumps(data, ensure_ascii=False, indent=1), log)


def fh4_check_upstream(e4_dir, info1, info2, dem_path):
    """Compare l'amont actuel au manifeste de l'étage 4. Ne calcule ni n'écrit rien. Renvoie (statut ∈ premier_calcul|inchange|modifie, message, différences)."""
    new = fh4_amont_signature(info1, info2, dem_path)
    mp = os.path.join(e4_dir, "manifeste_etage4.json")
    if not os.path.isfile(mp):
        return "premier_calcul", "Aucun calcul de l'étage 4 enregistré pour ce bassin (premier calcul).", []
    try:
        old = (json.load(open(mp, encoding="utf-8")) or {}).get("amont") or []
    except Exception:
        return "premier_calcul", "Manifeste de l'étage 4 illisible : traité comme premier calcul.", []
    diffs = fh4_compare_amont(old, new)
    if not diffs:
        return "inchange", "Amont inchangé depuis le dernier calcul de l'étage 4.", []
    qui = "étage 2" if any(d["etage"] == 2 for d in diffs) else "étage 1 (lu en lecture seule)"
    det = " ; ".join("{} : {} → {}".format(d["role"], d["ancien"], d["nouveau"]) for d in diffs)
    return "modifie", "⚠ {} modifié depuis le dernier calcul de l'étage 4 — résultat périmé ({}). Relancer le calcul actualisera les sous-bassins ; rien n'est recalculé sans clic.".format(qui, det), diffs


def fh4_load_results(e4_dir):
    """Relit le dernier résultat (manifeste + stats CSV + GeoPackage) SANS recalcul. None si absent."""
    mp = os.path.join(e4_dir, "manifeste_etage4.json")
    if not os.path.isfile(mp):
        return None
    try:
        man = json.load(open(mp, encoding="utf-8"))
        cp = (man.get("fichiers") or {}).get("stats_csv") or os.path.join(e4_dir, "stats_sous_bassins.csv")
        if not os.path.isfile(cp):
            cp = os.path.join(e4_dir, "stats_sous_bassins.csv")
        rows, tot = [], None
        with open(cp, encoding="utf-8-sig", newline="") as fh:
            rd = csv.reader((l for l in fh if not l.startswith("#")), delimiter=";")
            head = next(rd)
            for line in rd:
                d = dict(zip(head, line))
                num = lambda k: float(d[k].replace(",", ".")) if d.get(k, "") not in ("", None) else float("nan")
                if d["id_sb"] == "TOTAL BASSIN":
                    tot = {"surf_ha": num("surface_ha"), "terre_ha": num("terre_classee_ha"), "s": {}}
                    for k in ("P_Itasy", "P_ref"):
                        if "A_moy_t_ha_an_" + k in d:
                            tot["s"][k] = {"mean": num("A_moy_t_ha_an_" + k), "loss": num("perte_t_an_" + k)}
                    continue
                r = {"id": d["id_sb"], "no": int(d["id_sb"].split("_")[1]), "aval": d["aval"] or None, "surf_ha": num("surface_ha"), "z_min": num("z_min"), "z_moy": num("z_moy"), "z_max": num("z_max"),
                     "pente_pct": num("pente_moy_pct"), "long_bassin_m": num("long_bassin_m"), "long_cours_m": num("long_cours_m"), "ordre": int(d["ordre_strahler"] or 0), "terre_ha": num("terre_classee_ha"),
                     "pct_terre": num("pct_terre_classee"), "lac_ha": num("lac_ha"), "exutoire": False, "petit": False, "s": {}}
                for k in ("P_Itasy", "P_ref"):
                    if "A_moy_t_ha_an_" + k in d:
                        r["s"][k] = {"mean": num("A_moy_t_ha_an_" + k), "loss": num("perte_t_an_" + k)}
                rows.append(r)
        scen = [k for k in ("P_Itasy", "P_ref") if tot and k in tot["s"]]
        for r in rows:
            r["aval_no"] = int(r["aval"].split("_")[1]) if r["aval"] else None
        return {"basin": man.get("bassin"), "rows": rows, "total": tot, "scen": scen, "controles": [(c["controle"], c["resultat"], c["ok"]) for c in man.get("controles", [])], "seuil": man.get("seuil") or {},
                "voie": man.get("voie"), "basin_ha": man.get("surface_bassin_ha"), "lake_ha": man.get("lac_ha"), "files": man.get("fichiers") or {}, "date": man.get("date"), "relu": True,
                "dirs": {"e4": e4_dir}, "step": (man.get("grille") or {}).get("pas_m")}
    except Exception:
        return None


# ---- cartes : outils
def fh4_nice(x):
    """Arrondi lisible à 3 chiffres significatifs (bornes de légende)."""
    if x == 0 or not math.isfinite(x):
        return x
    e = math.floor(math.log10(abs(x))) - 2
    return round(x, -e) if e < 0 else float(round(x / 10 ** e) * 10 ** e)


def fh4_legend_classes(values, force_quantiles=False, unit="t/ha/an"):
    """Classes de la légende. Étage 2 (Faible < 5 ≤ Moyenne < 15 ≤ Forte < 50 ≤ Très forte) si ≥ 3 classes sont occupées ; sinon (et pour la perte totale) 6 classes par
    QUANTILES, signalées sur la carte. Renvoie {"mode", "bornes", "rows": [(couleur, libellé)] du plus fort au plus faible, "note"}."""
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return {"mode": "vide", "bornes": [], "rows": [], "note": "aucune valeur"}
    if not force_quantiles:
        edges = [c[1] for c in FH4_CLASSES_A[1:]]
        occ = set()
        for v in vals:
            occ.add(sum(1 for b in edges if v >= b))
        if len(occ) >= 3:
            rows = [(c[3], "{} ({})".format(c[0], "> {}".format(fh4_fr(c[1], 0, False)) if c[2] is None else ("< {}".format(fh4_fr(c[2], 0, False)) if c[1] == 0 else "{} – {}".format(fh4_fr(c[1], 0, False), fh4_fr(c[2], 0, False)))))
                    for c in reversed(FH4_CLASSES_A)]
            return {"mode": "etage2", "bornes": edges, "rows": rows, "note": "classes de l'étage 2 (seuils 5, 15, 50 t/ha/an)"}
    n = 6
    qs = []
    for i in range(1, n):
        q = vals[min(len(vals) - 1, int(round(i * (len(vals) - 1) / float(n))))]
        q = fh4_nice(q)
        if not qs or q > qs[-1]:
            qs.append(q)
    cols = FH4_QUANTILE_COLORS
    k = len(qs) + 1
    cols = [cols[int(round(i * (len(cols) - 1) / float(max(1, k - 1))))] for i in range(k)]
    rows = []
    for i in range(k - 1, -1, -1):
        if i == k - 1:
            lab = "> {}".format(fh4_fr(qs[-1], 0 if abs(qs[-1]) >= 100 else 1, False)) if qs else "tous"
        elif i == 0:
            lab = "≤ {}".format(fh4_fr(qs[0], 0 if abs(qs[0]) >= 100 else 1, False))
        else:
            lab = "{} – {}".format(fh4_fr(qs[i - 1], 0 if abs(qs[i - 1]) >= 100 else 1, False), fh4_fr(qs[i], 0 if abs(qs[i]) >= 100 else 1, False))
        rows.append((cols[i], lab))
    note = ("classes par QUANTILES (6 classes de même effectif) : les seuils de l'étage 2 (5, 15, 50 t/ha/an) ne séparent pas les fokontany" if not force_quantiles
            else "classes par quantiles (même effectif par classe)")
    return {"mode": "quantiles", "bornes": qs, "rows": rows, "note": note, "colors": cols}


def fh4_class_index(v, leg):
    """Indice de classe (0 = la plus faible) d'une valeur selon la légende."""
    if v is None or not math.isfinite(v):
        return None
    return sum(1 for b in leg["bornes"] if v >= b)


def fh4_class_color(i, leg):
    rows = leg["rows"]
    return rows[len(rows) - 1 - i][0]


def fh4_footer_fit(text, width_mm=281.0, base_h=24.0, max_h=34.0):
    best = None
    for size in (7, 6.5, 6, 5.5, 5, 4.5):
        cpl = max(20, int((width_mm - 4.0) / (size * 0.3528 * 0.55)))
        n = sum(max(1, int(math.ceil(len(seg) / float(cpl)))) for seg in str(text).split("\n"))
        need = n * size * 0.3528 * 1.3 + 3.0
        best = (size, max(base_h, need))
        if need <= max_h:
            return size, max(base_h, need)
    return best[0], min(best[1], max_h + 6.0)


def _fh4_label(lay, text, x, y, w, h, size=8, bold=False, halign=None, valign=None, frame=False):
    lb = QgsLayoutItemLabel(lay)
    lb.setText(text)
    fnt = QFont("Arial", int(size), QFont.Bold if bold else QFont.Normal)
    try:
        fnt.setPointSizeF(float(size))
    except Exception:
        pass
    lb.setFont(fnt)
    try:
        lb.setHAlign(halign if halign is not None else Qt.AlignLeft)
        lb.setVAlign(valign if valign is not None else Qt.AlignVCenter)
    except Exception:
        pass
    lb.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
    lb.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
    if frame:
        try:
            lb.setFrameEnabled(True)
            lb.setBackgroundEnabled(True)
            lb.setBackgroundColor(QColor(250, 250, 250, 245))
            lb.setMarginX(2.0)
            lb.setMarginY(1.2)
        except Exception:
            pass
    lay.addLayoutItem(lb)
    return lb


def _fh4_rect(lay, x, y, w, h, fill, outline="60,60,60,255", width="0.15"):
    from qgis.core import QgsLayoutItemShape, QgsFillSymbol
    r = QgsLayoutItemShape(lay)
    r.setShapeType(QgsLayoutItemShape.Rectangle)
    r.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutMillimeters))
    r.attemptResize(QgsLayoutSize(w, h, QgsUnitTypes.LayoutMillimeters))
    r.setSymbol(QgsFillSymbol.createSimple({"color": fill, "outline_color": outline, "outline_width": width}))
    lay.addLayoutItem(r)
    return r


def _fh4_grid(map_item, extent):
    """Carroyage UTM 38S sobre (cadre zébré + annotations), comme aux étages 1 et 2."""
    try:
        from qgis.core import QgsLayoutItemMapGrid
        grid = QgsLayoutItemMapGrid("Grille UTM", map_item)
        map_item.grids().addGrid(grid)
        grid.setEnabled(True)
        target = max(float(extent.width()) / 6.0, 1.0)
        step = min([250, 500, 1000, 2000, 5000, 10000, 20000, 25000, 50000, 100000], key=lambda v: abs(v - target))
        grid.setIntervalX(step)
        grid.setIntervalY(step)
        try:
            grid.setStyle(QgsLayoutItemMapGrid.FrameAnnotationsOnly)
            grid.setFrameStyle(QgsLayoutItemMapGrid.Zebra)
            grid.setFrameWidth(1.2)
            grid.setFramePenSize(0.25)
            grid.setFrameFillColor1(QColor("white"))
            grid.setFrameFillColor2(QColor("#5f6b73"))
            grid.setAnnotationEnabled(True)
            grid.setAnnotationPrecision(0)
            grid.setAnnotationFont(QFont("Arial", 7))
            grid.setAnnotationFrameDistance(1.2)
            grid.setAnnotationDirection(QgsLayoutItemMapGrid.Vertical, QgsLayoutItemMapGrid.Left)
            grid.setAnnotationDirection(QgsLayoutItemMapGrid.Vertical, QgsLayoutItemMapGrid.Right)
        except Exception:
            pass
        return grid
    except Exception:
        return None


def fh4_export_layout(lay, base_dir, base_name, fmt_paper="A4", log=None):
    """Exporte PNG + PDF (nom stable ; fichier verrouillé ⇒ nom horodaté). A3/A0 : mise en page redimensionnée avant l'export."""
    if fmt_paper != "A4":
        fh4_rescale_layout(lay, fmt_paper)
    os.makedirs(base_dir, exist_ok=True)
    stem = base_name if fmt_paper == "A4" else "{}_{}".format(base_name, fmt_paper)
    base = os.path.join(base_dir, "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem))
    ex = QgsLayoutExporter(lay)
    fh4_release_paths([base + ".png", base + ".pdf"])
    imgs = QgsLayoutExporter.ImageExportSettings()
    try:
        imgs.dpi = 200 if fmt_paper == "A4" else (150 if fmt_paper == "A3" else 72)
    except Exception:
        pass
    out = []
    for ext_, fn in ((".png", lambda t_: ex.exportToImage(t_, imgs)), (".pdf", lambda t_: ex.exportToPdf(t_, QgsLayoutExporter.PdfExportSettings()))):
        tmp = fh4_tmp_path(base + ext_)
        fn(tmp)
        if os.path.isfile(tmp):
            out.append(fh4_commit(tmp, base + ext_, log))
    if fmt_paper != "A4":   # remet la mise en page à l'échelle A4 (réexport possible)
        fh4_rescale_layout(lay, "A4")
    return out


def fh4_rescale_layout(layout, paper_fmt):
    mm = QgsUnitTypes.LayoutMillimeters
    items = [it for it in layout.items() if hasattr(it, "attemptMove")]
    orig = getattr(layout, "_fh4_orig_geom", None)
    if orig is None:
        orig = {}
        for it in items:
            try:
                fs = it.font().pointSizeF() if hasattr(it, "font") else None
            except Exception:
                fs = None
            orig[id(it)] = (it.positionWithUnits(), it.sizeWithUnits(), fs)
        layout._fh4_orig_geom = orig
        layout._fh4_orig_items = {id(it): it for it in items}
    long_side = FH4_LAYOUT_FORMATS.get(paper_fmt, FH4_LAYOUT_FORMATS["A4"])[1]
    factor = long_side / 297.0
    for iid, (pos, size, fsize) in orig.items():
        it = layout._fh4_orig_items.get(iid)
        if it is None:
            continue
        try:
            it.attemptMove(QgsLayoutPoint(pos.x() * factor, pos.y() * factor, mm))
            it.attemptResize(QgsLayoutSize(size.width() * factor, size.height() * factor, mm))
        except Exception:
            pass
        if fsize:
            try:
                f = it.font()
                f.setPointSizeF(max(4.0, fsize * factor))
                it.setFont(f)
            except Exception:
                pass
    w, h = FH4_LAYOUT_FORMATS.get(paper_fmt, FH4_LAYOUT_FORMATS["A4"])
    try:
        pc = layout.pageCollection()
        for i_ in range(max(1, pc.pageCount())):
            pc.page(i_).setPageSize(QgsLayoutSize(h, w, mm))
    except Exception:
        pass


def fh4_to_jpeg(png_path):
    """Conversion PNG → JPEG (Pillow) à côté du PNG ; renvoie le chemin ou None."""
    try:
        from PIL import Image
        dst = os.path.splitext(png_path)[0] + ".jpg"
        Image.open(png_path).convert("RGB").save(dst, "JPEG", quality=95)
        return dst
    except Exception:
        return None


def fh4_dialog_changement(parent, message):
    """Fenêtre « Étage 2 modifié » : « Recalculer (écraser) » ou « Garder tel quel » (DÉFAUT). Renvoie "recalculer" ou "garder"."""
    from qgis.PyQt.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Étage 2 modifié")
    box.setText(message)
    b_rec = box.addButton("Recalculer (écraser)", QMessageBox.AcceptRole)
    b_keep = box.addButton("Garder tel quel", QMessageBox.RejectRole)
    box.setDefaultButton(b_keep)
    box.setEscapeButton(b_keep)
    box.exec_()
    return "recalculer" if box.clickedButton() is b_rec else "garder"

# ============================================================
# 5. INSPECTION QSWAT+ / TauDEM (lecture seule, rien n'est lancé)
# ============================================================
FH4_TAUDEM_EXES = ("pitremove", "d8flowdir", "aread8", "threshold", "streamnet", "gagewatershed", "d8flowpathextremeup", "mpiexec")


def fh4_find_exe(name, roots, depth=5, cap=40000):
    """Cherche name[.exe] dans le PATH puis sous les dossiers `roots` (profondeur limitée). Renvoie le chemin ou None."""
    cands = [name + ".exe", name] if os.name == "nt" else [name, name + ".exe"]
    w = shutil.which(name)
    if w:
        return w
    seen = 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dp, dn, fn in os.walk(root):
            seen += 1
            if seen > cap or dp.count(os.sep) - base_depth >= depth:
                dn[:] = []
            for c in cands:
                if c in fn:
                    return os.path.join(dp, c)
    return None


# ============================================================
# 6. MÉTÉO — mécanisme PRÉPARÉ : estimation de taille (aucun téléchargement), téléchargement reprenable, simulateur injectable
#    JAMAIS déclenché sans clic. Les URL / paramètres ci-dessous n'ont PAS pu être vérifiés dans la documentation en ligne depuis la session de développement
#    (réseau bloqué) : « Estimer la taille » interroge le serveur (requêtes HEAD, sans téléchargement) et le journal le rappelle.
# ============================================================
FH4_CHIRPS_URL = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05/{Y}/chirps-v2.0.{Y}.{M:02d}.{D:02d}.tif.gz"
FH4_POWER_URL = ("https://power.larc.nasa.gov/api/temporal/daily/point?parameters={params}&community=AG&longitude={lon}&latitude={lat}&start={d0}&end={d1}&format=JSON")
FH4_POWER_PARAMS = ("T2M", "T2M_MAX", "T2M_MIN", "RH2M", "WS2M", "ALLSKY_SFC_SW_DWN")          # NON VÉRIFIÉS dans la documentation actuelle
FH4_POWER_GRILLE = (0.5, 0.625)                                                                 # HYPOTHÈSE : maille MERRA-2 (lat, lon) en degrés — NON VÉRIFIÉE
FH4_POWER_OCTETS_PAR_VALEUR = 14                                                               # HYPOTHÈSE d'estimation (ordre de grandeur du JSON)
FH4_METEO_WORKERS = 4                                                                          # HYPOTHÈSE prudente : fichiers météo en parallèle
FH4_POWER_WORKERS = 2                                                                          # limite plus basse pour l'API NASA POWER
FH4_METEO_NOTE = ("URL, noms de paramètres, résolutions et limites de requêtes NON VÉRIFIÉS dans la documentation en ligne lors du développement (réseau bloqué) : relire la documentation "
                  "de CHIRPS (CHC, UCSB) et de NASA POWER avant tout téléchargement lourd ; la résolution de NASA POWER est GROSSIÈRE (quelques cellules seulement sur le bassin).")
FH4_METEO_SOURCES = {
    "CHIRPS": {"source": "CHIRPS v2.0 quotidien, Afrique, 0,05° (≈ 5,5 km) — Climate Hazards Center, UCSB", "url_base": "https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05/",
               "resolution": "0,05° (≈ 5,5 km) — à vérifier", "licence": "à vérifier sur le site du CHC", "citation": "Funk et al. 2015, Scientific Data 2:150066 (à vérifier)"},
    "POWER": {"source": "NASA POWER, API quotidienne (point), communauté AG", "url_base": "https://power.larc.nasa.gov/api/temporal/daily/point", "resolution": "quelques dixièmes de degré à 1° selon le paramètre — à vérifier",
              "licence": "à vérifier (politique de données ouvertes de la NASA)", "citation": "NASA Langley Research Center POWER Project (à vérifier)"},
}


def fh4_days(d0, d1):
    import datetime
    a, b = datetime.date.fromisoformat(str(d0)[:10]), datetime.date.fromisoformat(str(d1)[:10])
    if b < a:
        raise Fh4Error("Période invalide : la fin ({}) précède le début ({}).".format(b, a))
    return [a + datetime.timedelta(days=i) for i in range((b - a).days + 1)]


def fh4_meteo_period_default(info2):
    """Période CHIRPS lue dans le manifeste de l'étage 2 (années complètes de R) ; None si absente."""
    man = (info2 or {}).get("manifeste") or {}
    yrs = ((man.get("formule_R") or {}).get("annees_completes")) or []
    if yrs:
        return "{}-01-01".format(min(yrs)), "{}-12-31".format(max(yrs))
    return None


def fh4_chirps_jobs(d0, d1, cache):
    out = []
    for d in fh4_days(d0, d1):
        out.append({"kind": "CHIRPS", "url": FH4_CHIRPS_URL.format(Y=d.year, M=d.month, D=d.day), "dest": os.path.join(cache, "chirps", str(d.year), "chirps-v2.0.{}.{:02d}.{:02d}.tif.gz".format(d.year, d.month, d.day)), "expected": None})
    return out


def fh4_power_points(bbox_wgs, marge=0.1):
    """Nœuds de la maille POWER (HYPOTHÈSE : 0,5° × 0,625°) couvrant l'emprise du bassin + marge. bbox = (ouest, sud, est, nord)."""
    w, s, e, n = bbox_wgs
    dl, dn = FH4_POWER_GRILLE[0], FH4_POWER_GRILLE[1]
    lat0, lat1 = math.floor((s - marge) / dl), math.ceil((n + marge) / dl)
    lon0, lon1 = math.floor((w - marge) / dn), math.ceil((e + marge) / dn)
    return [(round(la * dl, 4), round(lo * dn, 4)) for la in range(lat0, lat1 + 1) for lo in range(lon0, lon1 + 1)]


def fh4_power_jobs(bbox_wgs, d0, d1, cache):
    """Une requête par nœud ET par année (réponses petites, reprise simple)."""
    import datetime
    a, b = datetime.date.fromisoformat(str(d0)[:10]), datetime.date.fromisoformat(str(d1)[:10])
    out = []
    for la, lo in fh4_power_points(bbox_wgs):
        for y in range(a.year, b.year + 1):
            s_, e_ = max(a, datetime.date(y, 1, 1)), min(b, datetime.date(y, 12, 31))
            out.append({"kind": "POWER", "url": FH4_POWER_URL.format(params=",".join(FH4_POWER_PARAMS), lon=lo, lat=la, d0=s_.strftime("%Y%m%d"), d1=e_.strftime("%Y%m%d")),
                        "dest": os.path.join(cache, "power", "power_{}_{}_{}.json".format(la, lo, y)), "expected": (e_ - s_).days * len(FH4_POWER_PARAMS) * FH4_POWER_OCTETS_PAR_VALEUR + 800, "estime": True, "jours": (e_ - s_).days + 1})
    return out


class Fh4Http:
    """Accès HTTP minimal (urllib, aucune dépendance). Compte ses requêtes : n_head (sans téléchargement) et n_get (téléchargement). Remplaçable par un simulateur en test."""
    def __init__(self, timeout=60):
        import threading
        self.n_head = 0
        self.n_get = 0
        self.n_post = 0
        self.timeout = timeout
        self._counter_lock = threading.Lock()

    def post(self, url, data, on_chunk=None, cancel=None, chunk=65536):
        """POST Overpass : lecture par blocs et un seul nouvel essai après surcharge temporaire."""
        import urllib.request
        import urllib.parse
        import urllib.error
        from email.utils import parsedate_to_datetime
        for attempt in range(2):
            with self._counter_lock:
                self.n_post += 1
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": data}).encode("utf-8"),
                                        headers={"User-Agent": "FALKHYDRO+ (QGIS hydrology; OpenStreetMap data)"})
            try:
                with urllib.request.urlopen(req, timeout=max(self.timeout, 120)) as r:
                    cl = r.headers.get("Content-Length")
                    total = int(cl) if cl and cl.isdigit() else None
                    received, t0, parts = 0, time.time(), []
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Fh4Cancelled()
                        block = r.read(chunk)
                        if not block:
                            break
                        parts.append(block)
                        received += len(block)
                        if on_chunk:
                            on_chunk({"fichier": "osm_brut.json", "type": "OpenStreetMap / Overpass", "extension": ".json",
                                      "octets_reseau": received, "total_connu": total,
                                      "pct": 100.0 * received / total if total else None,
                                      "vitesse_globale": received / max(1e-6, time.time() - t0),
                                      "fichiers_faits": 0, "fichiers_total": 1, "total_estime": False})
                    return b"".join(parts)
            except urllib.error.HTTPError as ex:
                if attempt or ex.code not in (429, 502, 503, 504):
                    raise
                retry_after = ex.headers.get("Retry-After") if ex.headers else None
                delay = 5.0
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except Exception:
                            pass
                ex.close()
                # Respecte un délai serveur raisonnable ; n'attend pas de longues fenêtres
                # ni ne bascule vers des miroirs pour répéter une requête lourde.
                if delay > 30:
                    raise
                if cancel is not None and cancel.is_set():
                    raise Fh4Cancelled()
                time.sleep(max(1.0, delay))

    def head(self, url):
        import urllib.request
        with self._counter_lock:
            self.n_head += 1
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "FALKHYDRO+"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else None

    def open(self, url, start=0):
        """Renvoie (flux avec read(n), statut HTTP, taille totale du fichier ou None)."""
        import urllib.request
        import urllib.error
        from email.utils import parsedate_to_datetime
        h = {"User-Agent": "FALKHYDRO+"}
        if start:
            h["Range"] = "bytes={}-".format(start)
        for attempt in range(2):
            with self._counter_lock:
                self.n_get += 1
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=self.timeout)
                cl = r.headers.get("Content-Length")
                st = r.status if hasattr(r, "status") else 200
                total = (int(cl) + (start if st == 206 else 0)) if cl and cl.isdigit() else None
                return r, st, total
            except urllib.error.HTTPError as ex:
                if attempt or ex.code not in (429, 502, 503, 504):
                    raise
                retry_after = ex.headers.get("Retry-After") if ex.headers else None
                delay = 5.0
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except Exception:
                            pass
                ex.close()
                if delay > 30:
                    raise
                time.sleep(max(1.0, delay))


def fh4_meteo_estimate(http, d0, d1, bbox_wgs, cache, want_chirps=True, want_power=True):
    """ESTIMATION de la taille SANS télécharger : une ou deux requêtes HEAD sur des fichiers CHIRPS d'échantillon × le nombre de jours ; NASA POWER par formule (HYPOTHÈSE). Taille inconnue = None (jamais inventée)."""
    out = {"chirps": None, "power": None, "total": None, "total_connu": True, "lignes": []}
    tot = 0
    if want_chirps:
        jobs = fh4_chirps_jobs(d0, d1, cache)
        sizes = []
        for j in (jobs[0], jobs[len(jobs) // 2]) if len(jobs) > 1 else jobs[:1]:
            try:
                sz = http.head(j["url"])
            except Exception as ex:
                sz = None
                out["lignes"].append("CHIRPS : requête d'échantillon impossible ({}) : taille inconnue.".format(str(ex)[:80]))
            if sz:
                sizes.append(sz)
        if sizes:
            unit = sum(sizes) / float(len(sizes))
            out["chirps"] = {"fichiers": len(jobs), "octets": int(unit * len(jobs)), "unite": unit, "connu": True}
            tot += out["chirps"]["octets"]
        else:
            out["chirps"] = {"fichiers": len(jobs), "octets": None, "connu": False}
            out["total_connu"] = False
            out["lignes"].append("CHIRPS : taille totale inconnue (le serveur n'indique pas la taille d'un fichier).")
    if want_power:
        jobs = fh4_power_jobs(bbox_wgs, d0, d1, cache)
        o = sum(j["expected"] for j in jobs)
        out["power"] = {"requetes": len(jobs), "octets": o, "connu": True, "estime": True}
        tot += o
        out["lignes"].append("NASA POWER : {} requête(s) ; taille ESTIMÉE par formule (HYPOTHÈSE, {} octets par valeur).".format(len(jobs), FH4_POWER_OCTETS_PAR_VALEUR))
    out["total"] = tot if out["total_connu"] else None
    return out


def fh4_download_file(http, job, on_prog, cancel, chunk=65536, now=time.time):
    """Télécharge UN fichier vers dest (.part puis renommage) ; REPREND après coupure (Range) ; n'écrit rien si le fichier complet est déjà en cache. Renvoie (octets reçus ce coup-ci, depuis_cache)."""
    dest = job["dest"]
    if os.path.isfile(dest):
        return 0, True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    start = os.path.getsize(part) if os.path.isfile(part) else 0
    try:
        r, st, total = http.open(job["url"], start)
    except Exception as ex:
        if not start or getattr(ex, "code", None) != 416:
            raise
        # Le serveur refuse l'offset du .part (souvent parce qu'il dépasse la taille distante) :
        # ce seul fragment incomplet est jeté, puis le fichier est repris depuis zéro.
        try:
            os.remove(part)
        except OSError:
            with open(part, "wb"):
                pass
        start = 0
        r, st, total = http.open(job["url"], 0)
    if start and st == 416:
        try:
            r.close()
        except Exception:
            pass
        with open(part, "wb"):
            pass
        start = 0
        r, st, total = http.open(job["url"], 0)
    if start and st != 206:                 # le serveur ignore la reprise : on repart de zéro
        start = 0
    got = 0
    t0 = now()
    try:
        with open(part, "ab" if start else "wb") as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Fh4Cancelled()
                b = r.read(chunk)
                if not b:
                    break
                fh.write(b)
                got += len(b)
                on_prog({"fichier": os.path.basename(dest), "recu": start + got, "total": total,
                         "vitesse": got / max(1e-6, now() - t0), "delta_reseau": len(b), "octets_fichier_reseau": got})
        if total is not None and start + got < total:
            raise Fh4Error("Coupure : {} reçu à {} / {} octets ; relance « Télécharger » pour reprendre (le fichier partiel est conservé).".format(os.path.basename(dest), start + got, total))
        os.replace(part, dest)
        return got, False
    finally:
        try:
            r.close()
        except Exception:
            pass


def fh4_download_batch(http, jobs, on_prog, cancel, retries=2, now=time.time, workers=FH4_METEO_WORKERS):
    """Téléchargement reprenable avec une fenêtre glissante (4 CHIRPS, 2 POWER) ; arrête d'en lancer après la première erreur.
    La progression compte aussi les fichiers déjà en cache et les .part existants ; octets réseau = octets réellement reçus cette session."""
    import concurrent.futures
    import threading
    on_prog = on_prog or (lambda d: None)
    t0 = now()
    done_bytes, network_bytes, n_done, skipped = 0, 0, 0, 0
    exp = [j.get("expected") for j in jobs]
    total_known = sum(exp) if jobs and all(e is not None for e in exp) else None
    total_estimated = any(j.get("estime") for j in jobs)
    try:
        workers = max(1, int(workers))
    except Exception:
        workers = FH4_METEO_WORKERS
    lock = threading.RLock()
    progress_by_job, completed_paths = {}, set()
    last_ui_update = [0.0]
    for j in jobs:
        dest = j["dest"]
        if os.path.isfile(dest):
            progress_by_job[dest] = os.path.getsize(dest)
        else:
            part = dest + ".part"
            progress_by_job[dest] = os.path.getsize(part) if os.path.isfile(part) else 0
    progress_bytes = sum(progress_by_job.values())

    def extension(j):
        dest = str(j.get("dest", ""))
        return ".tif.gz" if dest.lower().endswith(".tif.gz") else os.path.splitext(dest)[1].lower()

    def make_report(j, d):
        nonlocal network_bytes, progress_bytes
        dest = j["dest"]
        with lock:
            network_bytes += int(d.get("delta_reseau") or 0)
            new_size = int(d.get("recu") or 0)
            progress_bytes += new_size - progress_by_job.get(dest, 0)
            progress_by_job[dest] = new_size
            stamp = now()
            elapsed = max(1e-6, stamp - t0)
            speed = network_bytes / elapsed
            pct = (100.0 * progress_bytes / total_known) if total_known else (100.0 * n_done / max(1, len(jobs)))
            pct = max(0.0, min(100.0, pct))
            remaining = (max(0, total_known - progress_bytes) / speed) if (total_known and speed > 0) else None
            if stamp - last_ui_update[0] >= 0.15 or (d.get("total") and new_size >= d["total"]):
                last_ui_update[0] = stamp
                on_prog(dict(d, type=j.get("kind") or "Fichier", extension=extension(j),
                             fichiers_faits=n_done, fichiers_total=len(jobs), octets=done_bytes + new_size,
                             octets_reseau=network_bytes, total_connu=total_known, total_estime=total_estimated,
                             pct=pct, restant_s=remaining, vitesse_globale=speed))

    def download_one(j):
        for attempt in range(retries + 1):
            try:
                return fh4_download_file(http, j, lambda d: make_report(j, d), cancel, now=now)
            except Fh4Cancelled:
                raise
            except Exception as ex:
                status = getattr(ex, "code", None)
                retryable = status is None or status in (408, 425, 429, 500, 502, 503, 504)
                if attempt >= retries or not retryable:
                    raise Fh4Error("Téléchargement interrompu sur {} : {} — relance « Télécharger » : les fichiers terminés et fragments valides sont conservés.".format(os.path.basename(j["dest"]), str(ex)[:120]))
                time.sleep(min(1.5 * (2 ** attempt), 6.0))

    segments = []
    for j in jobs:
        kind = str(j.get("kind") or "Fichier").upper()
        if not segments or segments[-1][0] != kind:
            segments.append((kind, [j]))
        else:
            segments[-1][1].append(j)
    first_error = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="FH4-download") as pool:
        for kind, segment in segments:
            limit = min(workers, FH4_POWER_WORKERS) if "POWER" in kind else workers
            next_index = 0
            pending = {}
            while next_index < len(segment) and len(pending) < limit:
                j = segment[next_index]
                pending[pool.submit(download_one, j)] = j
                next_index += 1
            while pending:
                if cancel is not None and cancel.is_set() and first_error is None:
                    first_error = Fh4Cancelled()
                finished, _ = concurrent.futures.wait(
                    tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    j = pending.pop(future)
                    try:
                        got, cached = future.result()
                    except Exception as ex:
                        if first_error is None:
                            first_error = ex
                    else:
                        dest = j["dest"]
                        with lock:
                            if dest not in completed_paths:
                                done_bytes += got
                                n_done += 1
                                skipped += 1 if cached else 0
                                completed_paths.add(dest)
                            actual = os.path.getsize(dest) if os.path.isfile(dest) else progress_by_job.get(dest, 0)
                            progress_bytes += actual - progress_by_job.get(dest, 0)
                            progress_by_job[dest] = actual
                            elapsed = max(1e-6, now() - t0)
                            speed = network_bytes / elapsed
                            pct = (100.0 * progress_bytes / total_known) if total_known else (100.0 * n_done / max(1, len(jobs)))
                            pct = max(0.0, min(100.0, pct))
                            remaining = (max(0, total_known - progress_bytes) / speed) if (total_known and speed > 0) else None
                            on_prog({"fichier": os.path.basename(dest), "type": j.get("kind") or "Fichier",
                                     "extension": extension(j), "recu": actual, "total": j.get("expected"),
                                     "vitesse": 0, "delta_reseau": 0, "octets_fichier_reseau": got,
                                     "fichiers_faits": n_done, "fichiers_total": len(jobs),
                                     "octets": done_bytes, "octets_reseau": network_bytes, "total_connu": total_known,
                                     "total_estime": total_estimated, "pct": pct, "restant_s": remaining,
                                     "vitesse_globale": speed})
                # Remplit immédiatement chaque place libérée ; ne reste pas bloqué
                # à attendre le fichier le plus lent d'un groupe fixe de quatre.
                while first_error is None and not (cancel is not None and cancel.is_set()) and next_index < len(segment) and len(pending) < limit:
                    j = segment[next_index]
                    pending[pool.submit(download_one, j)] = j
                    next_index += 1
            if first_error:
                break
    if first_error:
        if isinstance(first_error, Fh4Cancelled):
            raise first_error
        raise first_error
    on_prog({"fichier": "", "type": "Terminé", "extension": "", "recu": 0, "total": None, "vitesse": 0,
             "fichiers_faits": n_done, "fichiers_total": len(jobs), "octets": done_bytes, "octets_reseau": network_bytes,
             "total_connu": total_known, "total_estime": total_estimated, "pct": 100.0, "restant_s": 0.0,
             "vitesse_globale": network_bytes / max(1e-6, now() - t0), "termine": True})
    return {"fichiers": n_done, "octets": done_bytes, "octets_reseau": network_bytes, "en_cache": skipped}


def fh4_meteo_sources_json(cache, d0, d1, bbox_wgs, kinds, estimate=None):
    """meteo_sources.json : source, URL de base, date de téléchargement, période, résolution, licence / citation de chaque jeu."""
    data = {"date_ecriture": fh4_iso(time.time()), "periode": [str(d0)[:10], str(d1)[:10]], "emprise_wgs84": list(bbox_wgs), "note": FH4_METEO_NOTE, "jeux": {}}
    for k in kinds:
        data["jeux"][k] = dict(FH4_METEO_SOURCES[k], date_telechargement=fh4_iso(time.time()), parametres=list(FH4_POWER_PARAMS) if k == "POWER" else ["precipitation"])
    if estimate:
        data["estimation"] = {k: v for k, v in estimate.items() if k in ("chirps", "power", "total")}
    return fh4_write_text(os.path.join(cache, "meteo_sources.json"), json.dumps(data, ensure_ascii=False, indent=1))


def fh4_fmt_bytes(n):
    if n is None:
        return "taille inconnue"
    for u, f in (("Go", 1e9), ("Mo", 1e6), ("ko", 1e3)):
        if n >= f:
            return "{} {}".format(fh4_fr(n / f, 1), u)
    return "{} o".format(int(n))


def fh4_fmt_duration(s):
    if s is None:
        return "durée inconnue"
    s = int(s)
    return "{} h {:02d} min".format(s // 3600, (s % 3600) // 60) if s >= 3600 else ("{} min {:02d} s".format(s // 60, s % 60) if s >= 60 else "{} s".format(s))


# ============================================================
# 7. ISOLEMENT D'UN SOUS-BASSIN ET CARTES (mise en page identique aux étages 1–3 ; API QGIS sur le thread principal seulement)
# ============================================================
def fh4_isolate(p, log, progress=None):
    """Isole UN sous-bassin : masque du sous-bassin ∩ masque de l'étage 2 (A valide, terre classée, eau exclue), découpe de A (GeoTIFF taillés) sans AUCUN recalcul. Thread de calcul (GDAL / NumPy).
    p = {"root","basin","no"}. Renvoie {"files","extent","gt","legend_values","row","udir",…}."""
    import numpy as np
    ogr, osr, gdal = _fh4_ogr()
    prog = progress or (lambda v: None)
    dirs = p.get("dirs") or fh4_dirs(p["root"], p["basin"])
    res = fh4_load_results(dirs["e4"])
    if res is None:
        raise Fh4Error("Aucun résultat de l'étage 4 à isoler : lance « Calculer » (ou « Charger les derniers résultats »).")
    row = next((r for r in res["rows"] if r["no"] == int(p["no"])), None)
    if row is None:
        raise Fh4Error("Sous-bassin n° {} introuvable dans les résultats.".format(p["no"]))
    info1, info2 = fh4_find_stage1(dirs["e1"]), fh4_find_stage2(dirs["e2"], log)
    water = fh4_water_codes(info1["classes"], dirs["e2"])
    sbp = res["files"].get("sb_id_tif") or os.path.join(dirs["e4"], "sous_bassins_id.tif")
    sb, sgt, spj, _sz = fh4_read_raster(sbp)
    sb = np.nan_to_num(sb).astype("int32")
    udir = os.path.join(dirs["e4"], "Isoles", row["id"])
    os.makedirs(udir, exist_ok=True)
    files, legend, ext = {}, {}, None
    prog(20)
    for k in ("P_Itasy", "P_ref"):
        ap = info2["A"].get(k)
        if not ap:
            continue
        A, agt, apj, (nx, ny) = fh4_read_raster(ap)
        sbA = fh4_sb_on_grid(sb, sgt, spj, agt, nx, ny)
        cls, _re = fh4_classification_on_grid(info1["classification"], agt, nx, ny, apj, None)
        m = (sbA == int(p["no"]))
        if not m.any():
            raise Fh4Error("Ce sous-bassin ne recouvre aucun pixel de la grille de A de l'étage 2.")
        land = m & np.isfinite(A) & (cls > 0) & ~np.isin(cls, list(water))
        rr, cc = np.where(m)
        r0, r1, c0, c1 = rr.min(), rr.max() + 1, cc.min(), cc.max() + 1
        win = np.where(land[r0:r1, c0:c1], A[r0:r1, c0:c1], np.nan)
        wgt = (agt[0] + c0 * agt[1], agt[1], 0.0, agt[3] + r0 * agt[5], 0.0, agt[5])
        files["A_" + k] = fh4_write_raster(os.path.join(udir, "A_{}_{}.tif".format(k, row["id"])), win, wgt, apj, gdal.GDT_Float32, -9999, log)
        legend[k] = [float(v) for v in win[np.isfinite(win)][::max(1, int(np.isfinite(win).sum() // 20000))]]
        ext = (wgt[0], wgt[3] + wgt[5] * win.shape[0], wgt[0] + wgt[1] * win.shape[1], wgt[3])
        log("✂ {} ({}) : fenêtre {} × {} px, {} pixels de terre classée.".format(row["id"], _FH4_NOM_SC[k], win.shape[1], win.shape[0], int(land.sum())))
    prog(80)
    # contour du sous-bassin (WKT) depuis le GeoPackage
    ds = ogr.Open(res["files"].get("gpkg") or os.path.join(dirs["e4"], "sous_bassins_utm38s.gpkg"), 0)
    lyr = ds.GetLayerByName("sous_bassins")
    lyr.SetAttributeFilter("id_sb = '{}'".format(row["id"]))
    wkt = None
    for f in lyr:
        wkt = f.GetGeometryRef().ExportToWkt()
    ds = None
    lines = [["id_sb", row["id"]], ["aval", row["aval"] or "—"], ["surface_ha", fh4_csvnum(row["surf_ha"], 2)], ["ordre_strahler", row["ordre"]]]
    for k in res["scen"]:
        lines += [["A_moy_" + k, fh4_csvnum(row["s"][k]["mean"], 2)], ["perte_t_an_" + k, fh4_csvnum(row["s"][k]["loss"], 0)]]
    csvp = fh4_write_csv(os.path.join(udir, "valeurs_{}.csv".format(row["id"])), ["grandeur", "valeur"], lines, FH4_PHRASE_A, log)
    prog(100)
    log("✅ {} isolé dans {} (A non recalculé).".format(row["id"], udir))
    return {"row": row, "files": files, "csv": csvp, "udir": udir, "extent": ext, "legend_values": legend, "wkt": wkt, "scen": res["scen"], "basin": p["basin"], "res": res}


def fh4_page_frame(lay, title, subtitle, proj, numero_page):
    """Cadre commun : titre, sous-titre, pied de page « Informations projet » + n° de feuille automatique + limites. Renvoie (x, y, w, h) de la zone utile (mm)."""
    try:
        lay.pageCollection().page(0).setPageSize("A4", QgsLayoutItemPage.Landscape)
    except Exception:
        pass
    _fh4_label(lay, (proj.get("titre") or "").strip() or title, 8, 3, 281, 9, size=14, bold=True, halign=Qt.AlignHCenter)
    _fh4_label(lay, subtitle, 8, 11.5, 281, 5, size=8, halign=Qt.AlignHCenter)
    _FH4_SHEET["n"] += 1
    bits = ["Feuille SB {:03d}".format(_FH4_SHEET["n"]), numero_page, "Étage 4 — Sous-bassins {}".format(FH4_VERSION), "UTM 38S (EPSG:32738)"]
    for key, lab in (("auteur", "Auteur"), ("organisme", "Organisme"), ("bassin", "Bassin"), ("version", "Version"), ("date", "Date"), ("methode", "Méthode")):
        if proj.get(key):
            bits.append("{} : {}".format(lab, proj[key]))
    foot = "  •  ".join(bits) + "\n" + FH4_PIED_CARTE
    fsize, FOOT_H = fh4_footer_fit(foot, 281.0)
    _fh4_label(lay, foot, 8, 204 - FOOT_H, 281, FOOT_H, size=fsize, valign=Qt.AlignTop, frame=True)
    return 8.0, 20.0, 281.0, 204.0 - FOOT_H - 24.0


def fh4_res_layers(res):
    gp = (res.get("files") or {}).get("gpkg") or os.path.join(res["dirs"]["e4"], "sous_bassins_utm38s.gpkg")
    out = {}
    for nm in ("sous_bassins", "rivieres", "jonctions", "lac"):
        ly = QgsVectorLayer("{}|layername={}".format(gp, nm), nm, "ogr")
        out[nm] = ly if ly.isValid() else None
    return out


def fh4_sb_color(no, n):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(((no * 0.618034) % 1.0), 0.35 + 0.2 * (no % 3) / 2.0, 0.95)
    return QColor(int(r * 255), int(g * 255), int(b * 255))


def fh4_style_sb(layer, labels=True):
    """Sous-bassins : une couleur par sous-bassin, contour net, étiquette = numéro."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol, QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat, QgsTextBufferSettings
    cats = []
    n = layer.featureCount()
    for f in layer.getFeatures():
        no = int(f["no"])
        sym = QgsFillSymbol.createSimple({"color": "{},{},{},170".format(*fh4_sb_color(no, n).getRgb()[:3]), "outline_color": "50,50,50,255", "outline_width": "0.4"})
        cats.append(QgsRendererCategory(no, sym, str(f["id_sb"])))
    layer.setRenderer(QgsCategorizedSymbolRenderer("no", cats))
    if labels:
        s_ = QgsPalLayerSettings()
        s_.fieldName = "to_string(\"no\")"
        s_.isExpression = True
        tf = QgsTextFormat()
        tf.setFont(QFont("Arial", 8, QFont.Bold))
        tf.setSize(8)
        bf = QgsTextBufferSettings()
        bf.setEnabled(True)
        bf.setSize(0.6)
        bf.setColor(QColor("white"))
        tf.setBuffer(bf)
        s_.setFormat(tf)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(s_))
        layer.setLabelsEnabled(True)


def fh4_style_rivers(layer):
    """Rivières : épaisseur selon l'ordre de Strahler (bleu) ; tronçons RECTILIGNES suspects en pointillés rouges ; nom du tronçon en étiquette quand il existe."""
    from qgis.core import QgsLineSymbol, QgsProperty, QgsSymbolLayer, QgsRuleBasedRenderer
    root = QgsRuleBasedRenderer.Rule(None)
    for expr, col, style, lab in (("\"suspect\" IS NOT NULL AND \"suspect\" <> ''", "220,40,40,255", "dash", "tronçon rectiligne suspect"), ("ELSE", "30,90,200,255", "solid", "réseau (épaisseur = ordre de Strahler)")):
        sym = QgsLineSymbol.createSimple({"color": col, "width": "0.5", "line_style": style})
        try:
            sym.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.PropertyStrokeWidth, QgsProperty.fromExpression("0.25 + 0.3 * \"strahler\""))
        except Exception:
            pass
        root.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, expr, lab))
    layer.setRenderer(QgsRuleBasedRenderer(root))
    try:
        fh4_label_layer(layer, "nom", 7, "#1e3c8c", line=True)
    except Exception:
        pass


def fh4_label_layer(layer, field, size, color, line=False):
    """Étiquettes qui ne s'affichent que si elles tiennent (le moteur d'étiquettes évite les superpositions) ; halo blanc."""
    from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling, QgsTextFormat, QgsTextBufferSettings
    s_ = QgsPalLayerSettings()
    s_.fieldName = field
    tf = QgsTextFormat()
    tf.setFont(QFont("Arial", int(size)))
    tf.setSize(size)
    tf.setColor(QColor(color))
    bf = QgsTextBufferSettings()
    bf.setEnabled(True)
    bf.setSize(0.6)
    bf.setColor(QColor("white"))
    tf.setBuffer(bf)
    s_.setFormat(tf)
    try:
        s_.displayAll = False
        if line:
            s_.placement = QgsPalLayerSettings.Line
    except Exception:
        pass
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s_))
    layer.setLabelsEnabled(True)


def fh4_topo_layers(topo):
    """Couches de toponymes (points) et de rivières nommées (lignes) choisies par l'utilisateur : points discrets + étiquettes, lignes grises + étiquettes. Liste vide si rien n'est choisi."""
    from qgis.core import QgsMarkerSymbol, QgsLineSymbol, QgsSingleSymbolRenderer
    out = []
    for key, genre in (("lines", "ligne"), ("points", "point")):
        t = (topo or {}).get(key)
        if not t:
            continue
        ly = QgsVectorLayer(t["spec"], "topo_" + key, "ogr")
        if not ly.isValid():
            continue
        if genre == "point":
            ly.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({"name": "circle", "color": "40,40,40,255", "size": "1.4"})))
        else:
            ly.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({"color": "120,120,160,255", "width": "0.2"})))
        fh4_label_layer(ly, t.get("champ") or "name", 7, "#222222", line=(genre == "ligne"))
        out.append(ly)
    return out


def fh4_style_simple(layer, fill, outline, width="0.3"):
    from qgis.core import QgsFillSymbol, QgsSingleSymbolRenderer
    layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": fill, "outline_color": outline, "outline_width": width})))


def fh4_style_outlets(layer):
    from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({"name": "circle", "color": "220,30,30,255", "outline_color": "255,255,255,255", "size": "2.6"})))


def fh4_make_layout(kind, res, proj, iso=None, scen="P_Itasy", topo=None):
    """Carte A4 paysage : kind ∈ 'sb' (sous-bassins numérotés), 'reseau' (réseau, ordre de Strahler), 'isole' (A d'un sous-bassin isolé, `iso` = fh4_isolate). Cartouche, légende, flèche du nord,
    échelle, grille UTM 38S, pied de page. Renvoie (layout, nom de base)."""
    from qgis.core import QgsRasterLayer, QgsSingleBandPseudoColorRenderer
    pr = QgsProject.instance()
    layout_id = iso["row"]["id"] if kind == "isole" and iso else ("HRU" if kind == "hru" else "bassin")
    name = "SB4_{}_{}_{}".format(res["basin"], kind, layout_id)
    for lo in list(pr.layoutManager().layouts()):
        if lo.name() == name:
            pr.layoutManager().removeLayout(lo)
    L = fh4_res_layers(res)
    if L["sous_bassins"] is None:
        raise Fh4Error("GeoPackage des sous-bassins illisible : relance « Calculer » ou « Charger les derniers résultats ».")
    layers, legend_rows, titre, sub = [], [], "", ""
    if kind == "sb":
        fh4_style_sb(L["sous_bassins"])
        if L["lac"] is not None:
            fh4_style_simple(L["lac"], "170,210,240,255", "120,160,200,255", "0.2")
        if L["rivieres"] is not None:
            fh4_style_rivers(L["rivieres"])
        if L["jonctions"] is not None:
            fh4_style_outlets(L["jonctions"])
        layers = [x for x in (L["jonctions"], L["rivieres"], L["lac"], L["sous_bassins"]) if x is not None]
        titre = "Sous-bassins numérotés — {}".format(res["basin"])
        sub = "{} sous-bassins ; seuil {} % du bassin (HYPOTHÈSE) ; numéro = SB_xxx ; correspondance complète sur les pages annexes".format(len(res["rows"]), fh4_fr(res["seuil"].get("pct"), 1))
        legend_rows = [("#dddddd", "Sous-bassin (couleur = identifiant)"), ("#1e5ac8", "Réseau hydrographique"), ("#aad2f0", "Lac / eau (étage 1)"), ("#dc1e1e", "Exutoire de sous-bassin")]
    elif kind == "reseau":
        fh4_style_simple(L["sous_bassins"], "245,245,240,255", "150,150,150,255", "0.2")
        if L["lac"] is not None:
            fh4_style_simple(L["lac"], "170,210,240,255", "120,160,200,255", "0.2")
        fh4_style_rivers(L["rivieres"])
        fh4_style_outlets(L["jonctions"])
        layers = [x for x in (L["jonctions"], L["rivieres"], L["lac"], L["sous_bassins"]) if x is not None]
        omax = max([r["ordre"] for r in res["rows"]] or [1])
        titre = "Réseau hydrographique (ordre de Strahler) — {}".format(res["basin"])
        sub = "Épaisseur du trait selon l'ordre de Strahler (1 à {}) ; seuil {} % du bassin (HYPOTHÈSE)".format(omax, fh4_fr(res["seuil"].get("pct"), 1))
        legend_rows = [("#1e5ac8", "Ordre {} (trait d'autant plus épais)".format(o)) for o in range(1, omax + 1)] + [("#aad2f0", "Lac / eau (étage 1)"), ("#dc1e1e", "Exutoire de sous-bassin")]
    elif kind == "hru":
        hl = QgsVectorLayer("{}|layername=hru".format(iso["files"]["hru_gpkg"]), "hru", "ogr")
        if not hl.isValid():
            raise Fh4Error("GeoPackage des HRU illisible : relance « Construire les HRU ».")
        from qgis.core import QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsFillSymbol
        cats, seen = [], {}
        for r in iso["rows"]:
            seen.setdefault(r["lu"], r["lu_nom"])
        for code, nom in sorted(seen.items()):
            col = fh4_sb_color(code * 7 + 3, 1)
            cats.append(QgsRendererCategory(nom, QgsFillSymbol.createSimple({"color": "{},{},{},255".format(*col.getRgb()[:3]), "outline_color": "90,90,90,255", "outline_width": "0.1"}), nom))
            legend_rows.append((col.name(), nom))
        hl.setRenderer(QgsCategorizedSymbolRenderer("occupation", cats))
        fh4_style_simple(L["sous_bassins"], "0,0,0,0", "20,20,20,255", "0.4")
        layers = [L["sous_bassins"], hl]
        titre = "HRU — occupation du sol dans les sous-bassins — {}".format(res["basin"])
        sub = "{} HRU (occupation × sol × pente) ; classes de pente {} (HYPOTHÈSE) ; seuils d'élimination = HYPOTHÈSES ; entrées de SWAT+, aucun résultat hydrologique".format(len(iso["rows"]), " / ".join(fh4_slope_label(i, iso["bornes"]) for i in range(1, len(iso["bornes"]) + 2)))
    else:
        rl = QgsRasterLayer(iso["files"]["A_" + scen], "A")
        if not rl.isValid():
            raise Fh4Error("Raster isolé illisible : {}".format(iso["files"]["A_" + scen]))
        leg = fh4_legend_classes(iso["legend_values"][scen])
        from qgis.core import QgsColorRampShader, QgsRasterShader
        items, n = [], len(leg["rows"])
        for i in range(n):
            upper = leg["bornes"][i] if i < len(leg["bornes"]) else float("inf")
            items.append(QgsColorRampShader.ColorRampItem(upper, QColor(fh4_class_color(i, leg)), leg["rows"][n - 1 - i][1]))
        sh = QgsColorRampShader()
        sh.setColorRampType(QgsColorRampShader.Discrete)
        sh.setColorRampItemList(items)
        rs = QgsRasterShader()
        rs.setRasterShaderFunction(sh)
        rl.setRenderer(QgsSingleBandPseudoColorRenderer(rl.dataProvider(), 1, rs))
        fh4_style_simple(L["sous_bassins"], "0,0,0,0", "20,20,20,255", "0.5")
        L["sous_bassins"].setSubsetString("\"id_sb\" = '{}'".format(iso["row"]["id"]))
        if L["rivieres"] is not None:
            fh4_style_rivers(L["rivieres"])
            L["rivieres"].setSubsetString("\"id_sb\" = '{}'".format(iso["row"]["id"]))
        pr.addMapLayer(rl, False)
        layers = [x for x in (L["sous_bassins"], L["rivieres"], rl) if x is not None]
        titre = "A moyen — {} isolé ({})".format(iso["row"]["id"], res["basin"])
        sub = "{} — masque de l'étage 2 appliqué (terre classée) — A en t/ha/an, perte potentielle brute non validée".format(_FH4_NOM_SC.get(scen, scen))
        legend_rows = list(leg["rows"]) + [("#ffffff", "Hors masque (NoData)")]
    tl_ = fh4_topo_layers(topo) if kind in ("sb", "reseau", "isole") else []
    if tl_:
        layers = tl_ + layers
        legend_rows = legend_rows + [("#282828", "Toponymes / rivières nommées (couches de l'utilisateur)")]
    for ly in layers:
        pr.addMapLayer(ly, False)
    lay = QgsPrintLayout(pr)
    lay.initializeDefaults()
    lay.setName(name)
    pr.layoutManager().addLayout(lay)
    mm = QgsUnitTypes.LayoutMillimeters
    x, y, w, h = fh4_page_frame(lay, titre, sub, proj, "Page 1/1")
    if kind == "isole":
        e0 = iso["extent"]
        mg = 0.06 * max(e0[2] - e0[0], e0[3] - e0[1])
        ext = QgsRectangle(e0[0] - mg, e0[1] - mg, e0[2] + mg, e0[3] + mg)
    else:
        ext = L["sous_bassins"].extent()
        ext.scale(1.06)
    BOX_W, BOX_H = 196.0, min(150.0, h - 4)
    aspect = max(ext.width(), 1.0) / max(ext.height(), 1.0)
    MW, MH = BOX_W, BOX_W / aspect
    if MH > BOX_H:
        MH, MW = BOX_H, BOX_H * aspect
    m = QgsLayoutItemMap(lay)
    m.setLayers(layers)
    m.setCrs(QgsCoordinateReferenceSystem("EPSG:{}".format(FH4_CRS)))
    m.attemptMove(QgsLayoutPoint(22, y + 2, mm))
    m.attemptResize(QgsLayoutSize(round(MW, 1), round(MH, 1), mm))
    m.zoomToExtent(ext)
    m.setFrameEnabled(True)
    lay.addLayoutItem(m)
    _fh4_grid(m, m.extent())
    PX = min(22 + MW + 10, 289.0 - 60.0)
    _fh4_rect(lay, PX, y + 2, 60, 12 + 6.5 * len(legend_rows) + 6, "255,255,255,255", "40,40,40,255", "0.25")
    _fh4_label(lay, "Légende", PX + 2, y + 3, 56, 7, size=8, bold=True)
    yy = y + 11
    for color, label in legend_rows:
        _fh4_rect(lay, PX + 3, yy + 0.6, 9, 5.3, color)
        _fh4_label(lay, label, PX + 14, yy, 44, 6.5, size=7)
        yy += 6.5
    na = QgsLayoutItemPicture(lay)
    na.setPicturePath(":/images/north_arrows/layout_default_north_arrow.svg")
    na.attemptMove(QgsLayoutPoint(PX + 23, yy + 8, mm))
    na.attemptResize(QgsLayoutSize(14, 18, mm))
    lay.addLayoutItem(na)
    sb_ = QgsLayoutItemScaleBar(lay)
    sb_.setStyle("Single Box")
    sb_.setLinkedMap(m)
    try:
        sb_.setUnits(QgsUnitTypes.DistanceKilometers)
        sb_.setUnitLabel("km")
        sb_.setFont(QFont("Arial", 7))
        m_per_mm = ext.width() / MW
        seg = max([v for v in [100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000] if v <= 50.0 * m_per_mm / 2.0] or [100])
        sb_.setUnitsPerSegment(seg / 1000.0)
        sb_.setNumberOfSegments(2)
        sb_.setNumberOfSegmentsLeft(0)
    except Exception:
        pass
    sb_.attemptMove(QgsLayoutPoint(PX, yy + 30, mm))
    lay.addLayoutItem(sb_)
    base = {"sb": "{}_sous_bassins_numerotes", "reseau": "{}_reseau_strahler", "isole": "{}_carte_A_" + scen, "hru": "{}_HRU"}[kind].format(res["basin"] if kind != "isole" else iso["row"]["id"])
    return lay, base


def fh4_annex_pages(res, per_page=40):
    """Table de correspondance COMPLÈTE (jamais tronquée) : pages de `per_page` lignes. Renvoie [lignes de texte par page]."""
    rows = res["rows"]
    k0 = res["scen"][0] if res["scen"] else None
    head = "{:<8} {:<8} {:>11} {:>7} {:>10} {:>12}".format("SB", "aval", "surf. (ha)", "ordre", "alt. moy.", "A moy It" if k0 else "")
    out = []
    for i in range(0, max(1, len(rows)), per_page):
        chunk = rows[i:i + per_page]
        lines = [head]
        for r in chunk:
            lines.append("{:<8} {:<8} {:>11} {:>7} {:>10} {:>12}".format(r["id"], r["aval"] or "—", fh4_fr(r["surf_ha"], 1), r["ordre"], fh4_fr(r["z_moy"], 0), fh4_fr(r["s"][k0]["mean"], 1) if k0 else ""))
        out.append(lines)
    return out


def fh4_make_annex_layouts(res, proj):
    pr = QgsProject.instance()
    pages = fh4_annex_pages(res)
    out = []
    for i, lines in enumerate(pages):
        name = "SB4_{}_annexe_{}".format(res["basin"], i + 1)
        for lo in list(pr.layoutManager().layouts()):
            if lo.name() == name:
                pr.layoutManager().removeLayout(lo)
        lay = QgsPrintLayout(pr)
        lay.initializeDefaults()
        lay.setName(name)
        pr.layoutManager().addLayout(lay)
        x, y, w, h = fh4_page_frame(lay, "Correspondance des sous-bassins — {}".format(res["basin"]), "Annexe : liste complète des sous-bassins ; A en t/ha/an (P Itasy)", proj, "Page {}/{}".format(i + 1, len(pages)))
        lb = _fh4_label(lay, "\n".join(lines), x, y + 2, w, h - 4, size=8, valign=Qt.AlignTop)
        try:
            lb.setFont(QFont("Courier New", 8))
        except Exception:
            pass
        out.append((lay, "{}_annexe_sous_bassins_p{}".format(res["basin"], i + 1)))
    return out


# ============================================================
# 9. V2 — TauDEM : recherche élargie ; HRU ; préparation SWAT+ ; toponymes ; onglet Hydrologie préparé
# ============================================================
def fh4_taudem_roots(manual=(), plugin=None):
    """Emplacements cherchés (dans l'ordre) : dossier saisi à la main, plugin QSWAT+ et ses sous-dossiers, C:\\SWAT, Program Files*, LOCALAPPDATA, APPDATA, dossiers cités dans les fichiers du plugin, variable TAUDEM, PATH."""
    roots = [m for m in manual if m]
    if plugin:
        roots.append(plugin)
    roots += ["C:\\SWAT", "C:\\Program Files\\TauDEM", "C:\\Program Files (x86)\\TauDEM", "C:\\Program Files\\Microsoft MPI", "C:\\Program Files (x86)\\Microsoft SDKs\\MPI"]
    roots += [os.environ.get(k, "") for k in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "TAUDEM", "TAUDEM_DIR")]
    roots += [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    seen, out = set(), []
    for r in roots:
        k = os.path.normcase(os.path.abspath(r)) if r else ""
        if r and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def fh4_plugin_cited_dirs(plugin_dir, cap=300):
    """Chemins contenant « TauDEM » cités dans les fichiers texte du plugin (configuration de QSWAT+, sans présumer son format). Lecture seule."""
    out = []
    n = 0
    for dp, dn, fn in os.walk(plugin_dir):
        for f in fn:
            if f.lower().endswith((".py", ".ini", ".cfg", ".json", ".txt", ".bat")):
                n += 1
                if n > cap:
                    return out
                try:
                    txt = open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read(400000)
                except OSError:
                    continue
                for m in re.findall(r"(?:[A-Za-z]:[\\/]|/)[^\"'\n\r<>|?*]*?TauDEM[^\"'\n\r<>|?*]*", txt, flags=re.I):
                    d = m.replace("/", os.sep).rstrip("\\/ ")
                    if d not in out:
                        out.append(d)
    return out


def fh4_inspect_tools(extra_roots=(), plugin_dirs=(), manual=(), settings_keys=None):
    """Inspection de la machine (SANS rien présumer, rien n'est lancé) : plugin QSWAT+ (chemin, version), TauDEM (exécutables), MPI. Journalise les chemins CHERCHÉS et TROUVÉS.
    Renvoie {"qswat", "exes", "ok_a", "voie", "lignes", "cherches", "trouves", "mpiexec"}."""
    pdirs = list(plugin_dirs)
    try:
        from qgis.core import QgsApplication
        pdirs += [os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins"), os.path.join(QgsApplication.pkgDataPath(), "python", "plugins")]
    except Exception:
        pass
    ap = os.environ.get("APPDATA")
    if ap:
        pdirs.append(os.path.join(ap, "QGIS", "QGIS3", "profiles", "default", "python", "plugins"))
    pdirs.append(os.path.join(os.path.expanduser("~"), ".local", "share", "QGIS", "QGIS3", "profiles", "default", "python", "plugins"))
    qs = None
    for pd in pdirs:
        for d in sorted(glob.glob(os.path.join(pd, "QSWAT*"))):
            if os.path.isdir(d):
                ver = None
                try:
                    for ln in open(os.path.join(d, "metadata.txt"), encoding="utf-8", errors="ignore"):
                        if ln.lower().startswith("version="):
                            ver = ln.split("=", 1)[1].strip()
                            break
                except OSError:
                    pass
                qs = {"chemin": d, "version": ver}
                break
        if qs:
            break
    cited = fh4_plugin_cited_dirs(qs["chemin"]) if qs else []
    cfg = []
    for v in (settings_keys or []):
        if isinstance(v, str) and ("\\" in v or "/" in v):
            cfg.append(v)
    roots = fh4_taudem_roots(list(manual) + list(extra_roots) + cfg + cited, qs["chemin"] if qs else None)
    exes, cherches = {}, []
    for n in FH4_TAUDEM_EXES:
        exes[n] = fh4_find_exe(n, roots, depth=7)
    cherches = [r for r in roots if r and os.path.isdir(r)]
    need = ("pitremove", "d8flowdir", "aread8")
    ok_a = all(exes[n] for n in need)
    trouves = {n: p for n, p in exes.items() if p}
    mpi_txt = "MPI : {}".format(exes["mpiexec"] or "mpiexec introuvable (TauDEM tournera sans MPI)")
    lignes = ["1. QSWAT+ : {}.".format("installé — {} (version {})".format(qs["chemin"], qs["version"] or "non lue") if qs else "NON trouvé dans les dossiers de plugins inspectés ({})".format(" ; ".join(dict.fromkeys(pdirs)) or "aucun")),
              "   TauDEM : {}.".format("trouvé : " + " ; ".join("{} = {}".format(n, trouves[n]) for n in need if exes[n]) if any(exes[n] for n in need) else "INTROUVABLE (cherché dans {} dossier(s) existant(s), dont {} cité(s) par le plugin) ; indique le dossier avec « Parcourir… »".format(len(cherches), len(cited))),
              "   " + mpi_txt + ". Manquants : {}.".format(", ".join(n for n in FH4_TAUDEM_EXES if not exes[n]) or "aucun"),
              "   Dossiers cherchés : {}.".format(" ; ".join(cherches[:25]) + (" …" if len(cherches) > 25 else "")),
              "   Entrées de QSWAT+ (MNT, limite, exutoires, rivières, occupation du sol, sol) : voir « Inspecter SWAT+ » (onglet HRU / SWAT+).",
              "2. Pilotage de QSWAT+ par script : NON déterminé (aucune API présumée) ; la voie B n'est pas construite.",
              "   Voie retenue : {}.".format("A (TauDEM par sous-processus), comparée à la voie C dans le rapport" if ok_a else "C (NumPy, secours, moins éprouvée : TauDEM introuvable)")]
    return {"qswat": qs, "exes": exes, "ok_a": ok_a, "voie": "A" if ok_a else "C", "lignes": lignes, "mpiexec": exes.get("mpiexec"), "cherches": cherches, "trouves": trouves, "cites": cited}


# ---------------------------------------------------------------- SWAT+ / QSWAT+ : inspection (lecture seule) et PET
FH4_PET_OPTIONS = [
    ("Penman-Monteith", "température, rayonnement solaire, vitesse du vent, humidité relative"),
    ("Priestley-Taylor", "température, rayonnement solaire, humidité relative"),
    ("Hargreaves", "températures maximale et minimale (latitude)"),
    ("ETP mesurée", "série d'évapotranspiration potentielle fournie par l'utilisateur"),
]
FH4_PET_NOTE = ("Options usuelles de SWAT+ et variables qu'elles exigent, d'après la documentation de SWAT : À VÉRIFIER dans la documentation de SWAT+ de l'utilisateur (non vérifié en ligne lors du développement). "
                "L'étage 4 ne choisit PAS à la place de l'utilisateur et ne calcule AUCUNE PET dans cette livraison.")


def fh4_inspect_swat(extra_roots=(), plugin_dirs=()):
    """Inspection en lecture seule, rapportée en 8 lignes : SWAT+ (exécutable, Editor, version), base de données, QSWAT+ (version, entrées, création du projet), pilotage par script. Rien n'est présumé ni lancé."""
    pdirs = list(plugin_dirs)
    try:
        from qgis.core import QgsApplication
        pdirs.append(os.path.join(QgsApplication.qgisSettingsDirPath(), "python", "plugins"))
    except Exception:
        pass
    ap = os.environ.get("APPDATA")
    if ap:
        pdirs.append(os.path.join(ap, "QGIS", "QGIS3", "profiles", "default", "python", "plugins"))
    qs = None
    for pd in pdirs:
        for d in sorted(glob.glob(os.path.join(pd, "QSWAT*"))):
            if os.path.isdir(d):
                qs = d
                break
        if qs:
            break
    roots = list(extra_roots) + ["C:\\SWAT", "C:\\SWAT\\SWATPlus", "C:\\Program Files\\SWATPlus", "C:\\Program Files (x86)\\SWATPlus", os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramData", "")] + ([qs] if qs else [])
    roots = [r for r in roots if r and os.path.isdir(r)]
    exe = fh4_find_exe("swatplus", roots, depth=6) or fh4_find_exe("rev60.5.7_64rel", roots, depth=6)
    editor = None
    for r in roots:
        for g in glob.glob(os.path.join(r, "**", "*SWATPlusEditor*"), recursive=True)[:3] + glob.glob(os.path.join(r, "**", "*SWAT*Editor*"), recursive=True)[:3]:
            editor = g
            break
        if editor:
            break
    dbs = []
    for r in roots:
        for g in glob.glob(os.path.join(r, "**", "*.sqlite"), recursive=True)[:20]:
            dbs.append(g)
    ver = None
    files = []
    if qs:
        try:
            for ln in open(os.path.join(qs, "metadata.txt"), encoding="utf-8", errors="ignore"):
                if ln.lower().startswith("version="):
                    ver = ln.split("=", 1)[1].strip()
        except OSError:
            pass
        files = sorted(f for f in os.listdir(qs) if f.lower().endswith((".py", ".ui", ".txt", ".ini")))
    cand = [f for f in files if re.search(r"(batch|cmd|command|script|cli|run)", f, re.I)]
    kw = []
    if qs:
        for f in files:
            if f.endswith(".py"):
                try:
                    t = open(os.path.join(qs, f), encoding="utf-8", errors="ignore").read(200000)
                except OSError:
                    continue
                for k in ("landuse", "soil", "dem", "outlet", "stream", "lookup", "TxtInOut", "sqlite"):
                    if re.search(k, t, re.I) and k not in kw:
                        kw.append(k)
    lignes = ["1. SWAT+ (exécutable) : {}.".format(exe or "NON trouvé dans " + (" ; ".join(roots[:6]) or "aucun dossier existant")),
              "2. SWAT+ Editor : {}.".format(editor or "NON trouvé"),
              "3. Base de données SWAT+ (fichiers .sqlite trouvés : plantes / occupation, sols…) : {}.".format(" ; ".join(dbs[:5]) + (" …" if len(dbs) > 5 else "") if dbs else "AUCUNE trouvée (contenu non lu)"),
              "4. QSWAT+ : {}.".format("installé dans {} (version {})".format(qs, ver or "non lue") if qs else "NON trouvé"),
              "5. Entrées de QSWAT+ : le code n'est pas analysé ; mots rencontrés dans ses fichiers : {} (indicatif seulement, à confirmer dans son manuel).".format(", ".join(kw) or "aucun"),
              "6. Création du projet : (SQLite, dossier TxtInOut) NON déterminée par l'étage 4 ; à lire dans le manuel de QSWAT+.",
              "7. Pilotage par script : NON déterminé ; fichiers candidats (indicatif) : {}.".format(", ".join(cand[:8]) or "aucun"),
              "8. Voie proposée : B — l'étage 4 prépare MNT recadré, contour, exutoires, occupation du sol, sol et tables (dossier Preparation_QSWAT) et QSWAT+ construit le projet : QSWAT+ est l'outil de référence, "
              "son API n'est pas présumée, et l'utilisateur valide dans son interface. Voie A (écrire soi-même les entrées de SWAT+) écartée : risque d'erreur de format non vérifiable ici. SWAT+ n'est PAS lancé."]
    manque = ["météo (CHIRPS + NASA POWER à télécharger sur clic, puis conversion au format SWAT+ : livraison suivante)",
              "codes d'occupation du sol SWAT+ (colonne « code SWAT+ » de correspondance_occupation_SWAT.csv : à lire dans la base SWAT+ de l'utilisateur)",
              "paramètres hydrauliques des sols (sol_parametres_SWAT.csv : À REMPLIR/VALIDER)",
              "choix de la PET parmi les options de SWAT+ (décision de l'utilisateur)", "traitement du lac (option à confirmer)"]
    return {"lignes": lignes, "manque": manque, "qswat": qs, "exe": exe, "bases": dbs}


# ---------------------------------------------------------------- HRU : occupation du sol × sol × pente par sous-bassin
FH4_PENTE_BORNES = (5.0, 15.0, 30.0)               # % ; HYPOTHÈSE : classes 0–5, 5–15, 15–30, > 30 % (modifiables)
FH4_HRU_SEUILS = {"occupation_pct": 5.0, "sol_pct": 5.0, "pente_pct": 5.0, "min_ha": 5.0}   # HYPOTHÈSE (seuils d'élimination des petites HRU, % de la surface du sous-bassin ; surface minimale en ha)
FH4_STATUT_A_REMPLIR = "À REMPLIR/VALIDER"
FH4_TEXTURE_NOMS = {1: "argile", 2: "argile limoneuse", 3: "argile sableuse", 4: "limon argileux", 5: "limon argilo-limoneux", 6: "limon argilo-sableux", 7: "limon", 8: "limon fin", 9: "limon sableux",
                    10: "silt", 11: "sable limoneux", 12: "sable"}      # légende OpenLandMap USDA reprise de l'étage 2 (vérifiée par l'utilisateur)


def fh4_slope_classes(slope, bornes):
    """Classe de pente 1..len(bornes)+1 (borne basse incluse) ; NaN → 0."""
    import numpy as np
    out = np.zeros(slope.shape, "int32")
    ok = np.isfinite(slope)
    out[ok] = 1 + np.searchsorted(np.asarray(bornes, float), slope[ok], side="right")
    return out


def fh4_slope_label(i, bornes):
    b = list(bornes)
    if i == 1:
        return "0–{} %".format(fh4_fr(b[0], 0, False))
    if i == len(b) + 1:
        return "> {} %".format(fh4_fr(b[-1], 0, False))
    return "{}–{} %".format(fh4_fr(b[i - 2], 0, False), fh4_fr(b[i - 1], 0, False))


def fh4_hru_eliminate(sb, lu, soil, slope, land, th, log):
    """Élimination des petites HRU, sous-bassin par sous-bassin : (1) occupations < seuil % du sous-bassin, (2) sols < seuil %, (3) pentes < seuil %, chaque classe éliminée étant reportée sur la classe la plus
    étendue du sous-bassin (HYPOTHÈSE de méthode, documentée) ; (4) HRU (combinaison) < surface minimale fusionnées à la plus grande HRU du même sous-bassin ayant la même occupation, sinon la plus grande.
    Renvoie (lu, soil, slope, pourcentage de pixels reclassés, [message par étape])."""
    import numpy as np
    lu, soil, slope = lu.copy(), soil.copy(), slope.copy()
    orig = np.stack([lu, soil, slope])
    n_land = int(land.sum())
    stats = []
    for nm, arr, key in (("occupation", lu, "occupation_pct"), ("sol", soil, "sol_pct"), ("pente", slope, "pente_pct")):
        moved = 0
        for s in np.unique(sb[land]):
            m = land & (sb == s)
            vals, cnt = np.unique(arr[m], return_counts=True)
            tot = cnt.sum()
            dom = vals[cnt.argmax()]
            for v, c in zip(vals, cnt):
                if c / tot * 100.0 < th[key] and v != dom:
                    sel = m & (arr == v)
                    arr[sel] = dom
                    moved += int(sel.sum())
        stats.append("{} : {} pixels reclassés (seuil {} % du sous-bassin)".format(nm, moved, fh4_fr(th[key], 1)))
    return lu, soil, slope, stats, orig


def fh4_hru_merge_small(sb, lu, soil, slope, land, px_ha, min_ha):
    """Fusion des HRU (sb, lu, sol, pente) plus petites que min_ha ; renvoie le nombre de HRU fusionnées."""
    import numpy as np
    merged = 0
    for s in np.unique(sb[land]):
        m = land & (sb == s)
        key = lu[m].astype("int64") * 10000 + soil[m].astype("int64") * 100 + slope[m]
        vals, cnt = np.unique(key, return_counts=True)
        if len(vals) <= 1:
            continue
        big = vals[cnt.argmax()]
        for v, c in zip(vals, cnt):
            if c * px_ha < min_ha and v != big:
                lv = v // 10000
                same = [(cc, vv) for vv, cc in zip(vals, cnt) if vv // 10000 == lv and vv != v and cc * px_ha >= min_ha]
                tgt = max(same)[1] if same else big
                sel = m & ((lu.astype("int64") * 10000 + soil.astype("int64") * 100 + slope) == v)
                lu[sel], soil[sel], slope[sel] = tgt // 10000, (tgt // 100) % 100, tgt % 100
                merged += 1
    return merged


def fh4_hru_build(p, log, progress=None, cancel=None):
    """Construit les HRU (ENTRÉES de SWAT+ ; aucune infiltration ni recharge n'est calculée ici). p = {"root","basin","pente_bornes","seuils"}. Lecture seule des étages 1 et 2 ; sorties dans 04_swat/<bassin>/HRU/."""
    import numpy as np
    ogr, osr, gdal = _fh4_ogr()
    prog = progress or (lambda v: None)
    dirs = p.get("dirs") or fh4_dirs(p["root"], p["basin"])
    res = fh4_load_results(dirs["e4"])
    if res is None:
        raise Fh4Error("Aucun résultat de sous-bassins : lance « Calculer » d'abord (les HRU se construisent DANS les sous-bassins).")
    f = res["files"]
    sbp = f.get("sb_id_tif") or os.path.join(dirs["e4"], "sous_bassins_id.tif")
    mntp = f.get("mnt_tif") or os.path.join(dirs["e4"], "mnt_utm38s.tif")
    pentep = f.get("pente_tif") or os.path.join(dirs["e4"], "pente_pct.tif")
    for q in (sbp, mntp, pentep):
        if not os.path.isfile(q):
            raise Fh4Error("Fichier du calcul des sous-bassins absent ({}) : relance « Calculer » avec cette version.".format(os.path.basename(q)))
    info1 = fh4_find_stage1(dirs["e1"])
    water = fh4_water_codes(info1["classes"], dirs["e2"])
    sb, gt, pj, (nx, ny) = fh4_read_raster(sbp)
    sb = np.nan_to_num(sb).astype("int32")
    slope, _g, _p, _s = fh4_read_raster(pentep)
    bornes = tuple(p.get("pente_bornes") or FH4_PENTE_BORNES)
    th = dict(FH4_HRU_SEUILS, **(p.get("seuils") or {}))
    cls, _re = fh4_classification_on_grid(info1["classification"], gt, nx, ny, pj, log)
    texp = os.path.join(dirs["e2"], "travail", "K_classe_brut.tif")
    if os.path.isfile(texp):
        ds = gdal.Open(texp)
        out = gdal.Warp("", ds, format="MEM", outputBounds=(gt[0], gt[3] + gt[5] * ny, gt[0] + gt[1] * nx, gt[3]), width=nx, height=ny, dstSRS=pj, resampleAlg="near", outputType=gdal.GDT_Int32, srcNodata=-9999, dstNodata=0)
        soil = out.GetRasterBand(1).ReadAsArray().astype("int32")
        out = None
        ds = None
        soil_src = "texture OpenLandMap/USDA (K_classe_brut.tif de l'étage 2, lecture seule)"
    else:
        soil = np.zeros(sb.shape, "int32")
        soil_src = "ABSENTE (K_classe_brut.tif introuvable dans le cache de l'étage 2) : toutes les HRU seront « sans sol »"
        log("⚠ Carte de sol absente : {}.".format(soil_src))
    px_ha = abs(gt[1] * gt[5]) / 1e4
    inb = sb > 0
    is_water = np.isin(cls, list(water)) & inb
    unclass = (cls == 0) & inb
    land = inb & ~is_water & ~unclass
    sl = fh4_slope_classes(slope, bornes)
    sl[~np.isfinite(slope)] = 0
    prog(25)
    lu0, soil0, sl0 = cls.copy(), soil.copy(), sl.copy()
    lu, soil1, sl1, stats, orig = fh4_hru_eliminate(sb, lu0, soil0, sl0, land, th, log)
    merged = fh4_hru_merge_small(sb, lu, soil1, sl1, land, px_ha, th["min_ha"])
    changed = int((land & ((lu != orig[0]) | (soil1 != orig[1]) | (sl1 != orig[2]))).sum())
    pct_reclasse = changed / max(1, int(land.sum())) * 100.0
    for s_ in stats:
        log("🧩 Élimination — " + s_)
    log("🧩 HRU fusionnées (< {} ha) : {} ; pixels reclassés au total : {} %.".format(fh4_fr(th["min_ha"], 1), merged, fh4_fr(pct_reclasse, 1)))
    key = sb.astype("int64") * 10**9 + lu.astype("int64") * 10**6 + soil1.astype("int64") * 10**3 + sl1
    keys = np.unique(key[land])
    hid = np.zeros(sb.shape, "int32")
    idx = {k: i + 1 for i, k in enumerate(keys)}
    lut_k = np.array(sorted(idx))
    hid[land] = (np.searchsorted(lut_k, key[land]) + 1).astype("int32")
    prog(55)
    cnames = info1["classes"]
    sb_area = {int(s): float((sb == s).sum() * px_ha) for s in np.unique(sb[inb])}
    rows = []
    for k in lut_k:
        s_, l_, so_, sp_ = int(k // 10**9), int((k // 10**6) % 1000), int((k // 10**3) % 1000), int(k % 1000)
        a = float((hid == idx[k]).sum() * px_ha)
        rows.append({"hru": idx[k], "sb": "SB_{:03d}".format(s_), "sb_no": s_, "lu": l_, "lu_nom": cnames.get(l_, "?"), "sol": so_, "sol_nom": FH4_TEXTURE_NOMS.get(so_, "sans sol" if so_ == 0 else "?"),
                     "pente": sp_, "pente_nom": fh4_slope_label(sp_, bornes) if sp_ else "—", "surf_ha": a, "pct_sb": a / sb_area[s_] * 100.0 if sb_area.get(s_) else float("nan")})
    # contrôles : somme des HRU + eau + non classé = sous-bassin
    ctl = []
    worst = 0.0
    for s_, a_sb in sb_area.items():
        tot = sum(r["surf_ha"] for r in rows if r["sb_no"] == s_) + float(((sb == s_) & is_water).sum() * px_ha) + float(((sb == s_) & unclass).sum() * px_ha)
        worst = max(worst, abs(tot - a_sb) / a_sb * 100.0)
    ctl.append(("Somme des surfaces (HRU + eau + non classé) = surface du sous-bassin", "écart maximal {} %".format(fh4_fr(worst, 3)), worst < 0.01))
    ctl.append(("Eau (exclue des HRU, traitement à décider avec l'option lac)", "{} ha ; non classé (exclu) : {} ha".format(fh4_fr(float(is_water.sum() * px_ha), 1), fh4_fr(float(unclass.sum() * px_ha), 1)), True))
    ssol = [r["hru"] for r in rows if r["sol"] == 0]
    ctl.append(("HRU sans sol", "{}".format(len(ssol)) + (" (carte de sol absente ou pixels sans valeur)" if ssol else " : aucune"), not ssol))
    ctl.append(("HRU sans occupation du sol", "aucune (les pixels non classés sont exclus et comptés ci-dessus)", True))
    ctl.append(("Pixels reclassés par l'élimination des petites HRU", "{} % (surface conservée : {} % de la terre classée)".format(fh4_fr(pct_reclasse, 1), fh4_fr(100.0 - pct_reclasse, 1)), True))
    for a_, b_, ok_ in ctl:
        log("{} {} : {}".format("✔" if ok_ else "⚠", a_, b_))
    hd = os.path.join(dirs["e4"], "HRU")
    os.makedirs(hd, exist_ok=True)
    files = {"hru_tif": fh4_write_raster(os.path.join(hd, "hru_id.tif"), hid.astype("float64"), gt, pj, gdal.GDT_Int32, 0, log)}
    files["hru_csv"] = fh4_write_csv(os.path.join(hd, "hru_table.csv"), ["id_HRU", "id_SB", "code_ENVI", "occupation_du_sol", "code_sol", "sol", "classe_pente", "surface_ha", "pct_du_sous_bassin"],
                                    [[r["hru"], r["sb"], r["lu"], r["lu_nom"], r["sol"], r["sol_nom"], r["pente_nom"], fh4_csvnum(r["surf_ha"], 3), fh4_csvnum(r["pct_sb"], 2)] for r in rows],
                                    "HRU = entrées de SWAT+ (aucun résultat hydrologique) ; seuils et bornes de pente = HYPOTHÈSES à valider", log)
    # polygones des HRU
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(FH4_CRS)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    gp = os.path.join(hd, "hru_utm38s.gpkg")
    tmp = fh4_tmp_path(gp)
    if os.path.exists(tmp):
        os.remove(tmp)
    dso = ogr.GetDriverByName("GPKG").CreateDataSource(tmp)
    lyr = dso.CreateLayer("hru", srs, ogr.wkbMultiPolygon)
    for n_, t_ in (("id_hru", ogr.OFTInteger), ("id_sb", ogr.OFTString), ("code_envi", ogr.OFTInteger), ("occupation", ogr.OFTString), ("sol", ogr.OFTString), ("pente", ogr.OFTString), ("surface_ha", ogr.OFTReal), ("pct_sb", ogr.OFTReal)):
        lyr.CreateField(ogr.FieldDefn(n_, t_))
    mem = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Int32)
    mem.SetGeoTransform(gt)
    mem.SetProjection(pj)
    mem.GetRasterBand(1).WriteArray(hid)
    tl = ogr.GetDriverByName("Memory").CreateDataSource("t")
    tly = tl.CreateLayer("t", srs, ogr.wkbPolygon)
    tly.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(mem.GetRasterBand(1), None, tly, 0, [])
    parts = {}
    for ft_ in tly:
        v = ft_.GetField("v")
        if v > 0:
            parts.setdefault(v, []).append(ft_.GetGeometryRef().Clone())
    by = {r["hru"]: r for r in rows}
    for v, gl in parts.items():
        col = ogr.Geometry(ogr.wkbGeometryCollection)
        for g_ in gl:
            col.AddGeometry(g_)
        try:
            u = col.UnionCascaded()
        except Exception:
            u = col
        r = by[v]
        ft_ = ogr.Feature(lyr.GetLayerDefn())
        for k_, v_ in (("id_hru", v), ("id_sb", r["sb"]), ("code_envi", r["lu"]), ("occupation", r["lu_nom"]), ("sol", r["sol_nom"]), ("pente", r["pente_nom"]), ("surface_ha", r["surf_ha"]), ("pct_sb", r["pct_sb"])):
            ft_.SetField(k_, v_)
        ft_.SetGeometry(_fh4_polys_only(u))
        lyr.CreateFeature(ft_)
    dso = None
    mem = None
    files["hru_gpkg"] = fh4_commit(tmp, gp, log)
    prog(80)
    # tables à valider (jamais écrasées : l'utilisateur les complète)
    area_lu = {}
    for r in rows:
        area_lu[r["lu"]] = area_lu.get(r["lu"], 0.0) + r["surf_ha"]
    area_so = {}
    for r in rows:
        area_so[r["sol"]] = area_so.get(r["sol"], 0.0) + r["surf_ha"]
    auto = bool(p.get("auto_tables"))

    def lu_row(c, a):
        if not auto:
            return [c, cnames.get(c, "?"), fh4_csvnum(a, 2), "", FH4_STATUT_A_REMPLIR]
        code, why = fh4_auto_landuse(cnames.get(c, "?"))
        return [c, cnames.get(c, "?"), fh4_csvnum(a, 2), code, FH4_STATUT_AUTO + " ({})".format(why)]

    def so_row(c, a):
        nm = FH4_TEXTURE_NOMS.get(c, "sans sol" if c == 0 else "?")
        if not auto:
            return [c, nm, fh4_csvnum(a, 2), "", "", "", "", "", "", FH4_STATUT_A_REMPLIR]
        return fh4_auto_soil_row(c, nm, fh4_csvnum(a, 2))
    files["corr_occupation"] = fh4_ensure_table(os.path.join(dirs["e4"], "correspondance_occupation_SWAT.csv"), ["code_ENVI", "occupation_du_sol", "surface_ha_HRU", "code_SWAT_plus", "statut"],
                                               [lu_row(c, a) for c, a in sorted(area_lu.items())], 0,
                                               ("Codes d'occupation SWAT+ proposés AUTOMATIQUEMENT d'après le nom des classes, écrits de mémoire : HYPOTHÈSES à vérifier dans la base SWAT+" if auto else
                                                "Codes d'occupation SWAT+ à LIRE dans la base de données SWAT+ de l'utilisateur ; aucun code n'est écrit de mémoire"), log, replace=auto)
    files["sol_params"] = fh4_ensure_table(os.path.join(dirs["e4"], "sol_parametres_SWAT.csv"),
                                          ["code_sol", "sol", "surface_ha_HRU", "profondeur_mm", "densite_apparente", "eau_disponible", "conductivite_hydraulique_saturee", "carbone_organique", "groupe_hydrologique", "statut"],
                                          [so_row(c, a) for c, a in sorted(area_so.items())], 0,
                                          ("Paramètres du sol AUTOMATIQUES : Saxton & Rawls (2006) écrits de mémoire (non revérifiés à la source), texture représentative par classe, profondeur 1000 mm et matière organique 2 % : HYPOTHÈSES non calibrées, jamais un résultat"
                                           if auto else "Paramètres hydrauliques du sol exigés par SWAT+ : À REMPLIR/VALIDER ; aucune valeur proposée par l'étage 4"), log, replace=auto)
    rep_txt = ["FALKHYDRO+ — ÉTAGE 4 {} — HRU (entrées de SWAT+)".format(FH4_VERSION), "Bassin : {} — {}".format(p["basin"], fh4_iso(time.time())), "",
               "Occupation du sol : étage 1 (lecture seule) ; sol : {} ; pente : MNT SRTM (étage 2), classes {} (HYPOTHÈSE, modifiables).".format(soil_src, " / ".join(fh4_slope_label(i, bornes) for i in range(1, len(bornes) + 2))),
               "Seuils d'élimination (HYPOTHÈSES à valider) : occupation {} %, sol {} %, pente {} % du sous-bassin ; HRU minimale {} ha.".format(*(fh4_fr(th[k], 1) for k in ("occupation_pct", "sol_pct", "pente_pct", "min_ha"))),
               "{} HRU dans {} sous-bassins.".format(len(rows), len(sb_area)), "", "CONTRÔLES"] + ["  {} {} : {}".format("✔" if ok else "⚠", a, b) for a, b, ok in ctl] + \
              ["", "À REMPLIR/VALIDER : correspondance_occupation_SWAT.csv (codes SWAT+), sol_parametres_SWAT.csv. Aucun résultat hydrologique n'est produit ici."]
    files["rapport"] = fh4_write_text(os.path.join(hd, "rapport_HRU.txt"), "\n".join(rep_txt) + "\n", log)
    prog(100)
    log("✅ {} HRU construites dans {} sous-bassins (entrées de SWAT+ ; aucune infiltration ni recharge calculée).".format(len(rows), len(sb_area)))
    return {"rows": rows, "controles": ctl, "files": files, "pct_reclasse": pct_reclasse, "bornes": bornes, "seuils": th, "n_sb": len(sb_area), "basin": p["basin"], "dirs": dirs, "soil_src": soil_src}


def fh4_ensure_table(path, header, rows, key_col, meta, log, replace=False):
    """Table CSV MODIFIABLE : jamais écrasée. Si elle existe, les lignes de l'utilisateur sont conservées et seules les classes absentes sont ajoutées. Renvoie le chemin."""
    exist = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.reader((l for l in fh if not l.startswith("#")), delimiter=";")
            h0 = next(rd, None)
            for line in rd:
                if line:
                    exist[line[key_col]] = line
        if h0 and h0 != header:
            log("⚠ {} : en-têtes différents de ceux attendus ; le fichier n'est pas modifié.".format(os.path.basename(path)))
            return path
    out = []
    for r in rows:
        k = str(r[key_col])
        if k in exist:
            line = list(exist[k])
            untouched = (line[-1] if line else "") in (FH4_STATUT_A_REMPLIR, "") or str(line[-1]).startswith("AUTO")
            if replace and untouched:                         # ligne non validée par l'utilisateur : remplie automatiquement (HYPOTHÈSE)
                out.append(r)
                continue
            for i in range(len(header)):                      # on ne met à jour que les surfaces, jamais les valeurs saisies
                if "surface" in header[i] and i < len(r):
                    line[i] = r[i]
            out.append(line)
        else:
            out.append(r)
    for k, line in exist.items():
        if k not in {str(r[key_col]) for r in rows}:
            out.append(line)
    return fh4_write_csv(path, header, out, meta, log)


def fh4_swat_prepare(p, log, progress=None, cancel=None):
    """Voie B : prépare dans 04_swat/<bassin>/Preparation_QSWAT/ le MNT recadré, le contour, les exutoires, l'occupation du sol, le sol et les tables, pour que QSWAT+ construise le projet. SWAT+ n'est PAS lancé."""
    import numpy as np
    ogr, osr, gdal = _fh4_ogr()
    dirs = p.get("dirs") or fh4_dirs(p["root"], p["basin"])
    res = fh4_load_results(dirs["e4"])
    if res is None:
        raise Fh4Error("Aucun résultat de sous-bassins : lance « Calculer » d'abord.")
    d = os.path.join(dirs["e4"], "Preparation_QSWAT")
    os.makedirs(d, exist_ok=True)
    info1 = fh4_find_stage1(dirs["e1"])
    sbp = res["files"].get("sb_id_tif") or os.path.join(dirs["e4"], "sous_bassins_id.tif")
    mntp = res["files"].get("mnt_tif") or os.path.join(dirs["e4"], "mnt_utm38s.tif")
    sb, gt, pj, (nx, ny) = fh4_read_raster(sbp)
    z, _g, _p, _s = fh4_read_raster(mntp)
    files = {"mnt": fh4_write_raster(os.path.join(d, "mnt_recadre_utm38s.tif"), np.where(np.nan_to_num(sb) > 0, z, np.nan), gt, pj, gdal.GDT_Float32, -9999, log)}
    cls, _re = fh4_classification_on_grid(info1["classification"], gt, nx, ny, pj, log)
    files["occupation"] = fh4_write_raster(os.path.join(d, "occupation_sol_bassin.tif"), np.where(np.nan_to_num(sb) > 0, cls, 0).astype("float64"), gt, pj, gdal.GDT_Int32, 0, log)
    texp = os.path.join(dirs["e2"], "travail", "K_classe_brut.tif")
    manque = []
    if os.path.isfile(texp):
        ds = gdal.Open(texp)
        o = gdal.Warp("", ds, format="MEM", outputBounds=(gt[0], gt[3] + gt[5] * ny, gt[0] + gt[1] * nx, gt[3]), width=nx, height=ny, dstSRS=pj, resampleAlg="near", outputType=gdal.GDT_Int32, srcNodata=-9999, dstNodata=0)
        so = o.GetRasterBand(1).ReadAsArray().astype("float64")
        files["sol"] = fh4_write_raster(os.path.join(d, "sol_texture_bassin.tif"), np.where(np.nan_to_num(sb) > 0, so, 0), gt, pj, gdal.GDT_Int32, 0, log)
        o = None
        ds = None
    else:
        manque.append("carte de sol (K_classe_brut.tif) absente du cache de l'étage 2")
    for nm in ("correspondance_occupation_SWAT.csv", "sol_parametres_SWAT.csv"):
        if os.path.isfile(os.path.join(dirs["e4"], nm)):
            shutil.copy2(os.path.join(dirs["e4"], nm), os.path.join(d, nm))
            files[nm] = os.path.join(d, nm)
        else:
            manque.append("{} (construis les HRU d'abord)".format(nm))
    # contour + exutoires
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(FH4_CRS)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    gp = os.path.join(d, "contour_et_exutoires_utm38s.gpkg")
    tmp = fh4_tmp_path(gp)
    if os.path.exists(tmp):
        os.remove(tmp)
    dso = ogr.GetDriverByName("GPKG").CreateDataSource(tmp)
    src = ogr.Open(res["files"].get("gpkg") or os.path.join(dirs["e4"], "sous_bassins_utm38s.gpkg"), 0)
    lsb = src.GetLayerByName("sous_bassins")
    col = ogr.Geometry(ogr.wkbMultiPolygon)
    for f_ in lsb:
        g_ = f_.GetGeometryRef()
        for i_ in range(g_.GetGeometryCount()):
            col.AddGeometry(g_.GetGeometryRef(i_).Clone())
    uni = col.UnionCascaded() if col.GetGeometryCount() else col
    lc = dso.CreateLayer("contour", srs, ogr.wkbMultiPolygon)
    ft_ = ogr.Feature(lc.GetLayerDefn())
    ft_.SetGeometry(ogr.ForceToMultiPolygon(uni))
    lc.CreateFeature(ft_)
    lo = dso.CreateLayer("exutoires", srs, ogr.wkbPoint)
    lo.CreateField(ogr.FieldDefn("id_sb", ogr.OFTString))
    lo.CreateField(ogr.FieldDefn("type", ogr.OFTString))
    ljn = src.GetLayerByName("jonctions")
    for f_ in ljn:
        t_ = f_.GetField("type")
        if t_ in ("exutoire du bassin", "sortie secondaire"):
            ft_ = ogr.Feature(lo.GetLayerDefn())
            ft_.SetField("id_sb", f_.GetField("id_sb"))
            ft_.SetField("type", t_)
            ft_.SetGeometry(f_.GetGeometryRef().Clone())
            lo.CreateFeature(ft_)
    src = None
    dso = None
    files["contour_exutoires"] = fh4_commit(tmp, gp, log)
    pet = ["PET — options de SWAT+ (l'étage 4 ne choisit pas, ne calcule rien) :"] + ["  - {} : {}".format(a, b) for a, b in FH4_PET_OPTIONS] + ["  " + FH4_PET_NOTE]
    auto_ = os.path.isfile(os.path.join(dirs["e4"], "correspondance_occupation_SWAT.csv")) and "AUTO" in open(os.path.join(dirs["e4"], "correspondance_occupation_SWAT.csv"), encoding="utf-8-sig").read()
    lisez = ["PRÉPARATION POUR QSWAT+ (voie B) — {} — {}".format(p["basin"], fh4_iso(time.time())), "Tous les fichiers sont en EPSG:{} (UTM 38S). SWAT+ n'est PAS lancé ; QSWAT+ construit le projet.".format(FH4_CRS), "",
             "Fichiers : mnt_recadre_utm38s.tif, contour_et_exutoires_utm38s.gpkg (couches contour, exutoires), occupation_sol_bassin.tif (+ correspondance_occupation_SWAT.csv), sol_texture_bassin.tif (+ sol_parametres_SWAT.csv).",
             "", "CE QUI MANQUE POUR LANCER SWAT+ :"] + ["  - " + m for m in res_manque(manque, auto_)] + [""] + pet + ["", FH4_PHRASE_A]
    files["lisez_moi"] = fh4_write_text(os.path.join(d, "LISEZMOI.txt"), "\n".join(lisez) + "\n", log)
    for m in res_manque(manque, auto_):
        log("⚠ Manque pour lancer SWAT+ : " + m)
    log("✅ Entrées préparées pour QSWAT+ dans {} (SWAT+ non lancé).".format(d))
    return {"files": files, "manque": res_manque(manque, auto_), "dir": d, "pet": pet}


def res_manque(manque, auto=False):
    base = ["météo (CHIRPS + NASA POWER : conversion au format SWAT+ à venir)"]
    if auto:
        base += ["codes d'occupation et paramètres de sol : remplis AUTOMATIQUEMENT (HYPOTHÈSES écrites de mémoire, à vérifier dans la base SWAT+)"]
    else:
        base += ["codes d'occupation du sol SWAT+ (à lire dans la base SWAT+ de l'utilisateur)", "paramètres hydrauliques des sols (sol_parametres_SWAT.csv : À REMPLIR/VALIDER)"]
    base += ["choix de la PET (options présentées, rien n'est choisi ni calculé)", "traitement du lac (choix automatique (ii), HYPOTHÈSE)", "exécution de SWAT+ (non lancé par l'étage 4)"]
    return list(manque) + base


# ---------------------------------------------------------------- toponymes et rivières nommées (couches fournies, OSM seulement sur clic)
FH4_NOM_CHAMPS = ("name", "nom", "NAME", "NOM", "toponyme", "TOPONYME", "name:fr", "name_fr", "label", "libelle", "ADM4_EN", "ADM3_EN", "Name", "Nom")


def fh4_scan_layers(paths, kinds=("point", "ligne")):
    """Couches de POINTS (toponymes) et de LIGNES (rivières nommées) dans une liste de fichiers/dossiers fournis par l'utilisateur (.shp / .gpkg). Renvoie [{"spec","nom","genre","n","champ"}]. Lecture seule."""
    ogr, osr, gdal = _fh4_ogr()
    files = []
    for q in paths:
        if os.path.isdir(q):
            for dp, dn, fn in os.walk(q):
                files += [os.path.join(dp, f) for f in fn if f.lower().endswith((".shp", ".gpkg"))]
        elif os.path.isfile(q):
            files.append(q)
    out = []
    for fpath in files[:300]:
        try:
            ds = ogr.Open(fpath, 0)
        except Exception:
            continue
        if ds is None:
            continue
        for i in range(ds.GetLayerCount()):
            ly = ds.GetLayerByIndex(i)
            gt_ = ogr.GT_Flatten(ly.GetGeomType())
            genre = "point" if gt_ in (ogr.wkbPoint, ogr.wkbMultiPoint) else ("ligne" if gt_ in (ogr.wkbLineString, ogr.wkbMultiLineString) else None)
            if genre not in kinds:
                continue
            defn = ly.GetLayerDefn()
            names = [defn.GetFieldDefn(j).GetName() for j in range(defn.GetFieldCount())]
            champ = next((c for c in FH4_NOM_CHAMPS if c in names), None)
            if champ is None:
                continue
            spec = fpath if fpath.lower().endswith(".shp") else "{}|layername={}".format(fpath, ly.GetName())
            out.append({"spec": spec, "nom": "{} — {}".format(os.path.basename(fpath), ly.GetName()), "genre": genre, "n": ly.GetFeatureCount(), "champ": champ})
        ds = None
    return out


FH4_OSM_NAME_KEYS = ("name", "name:fr", "name:mg", "name:en", "official_name", "alt_name", "short_name", "int_name")
FH4_OSM_NAME_KEY_RE = "^(name|name:fr|name:mg|name:en|official_name|alt_name|short_name|int_name)$"
FH4_OSM_CACHE_VERSION = 3
FH4_OSM_URL = "https://overpass-api.de/api/interpreter"
FH4_OSM_OCTETS_PAR_ELEMENT = 600          # HYPOTHÈSE d'estimation


def fh4_osm_query(bbox, sortie=None):
    """Toponymes nodaux/zonaux nommés et cours d'eau nommés ; géométrie complète seulement pour les lignes."""
    w, s, e, n = bbox
    bounds = "({},{},{},{})".format(s, w, n, e)
    names = FH4_OSM_NAME_KEY_RE
    places = 'nwr["place"][~"{}"~"."]{};'.format(names, bounds)
    named_water = 'nwr["natural"~"^(water|peak|waterfall)$"][~"{}"~"."]{};'.format(names, bounds)
    reservoirs = 'nwr["landuse"="reservoir"][~"{}"~"."]{};'.format(names, bounds)
    admin = 'relation["boundary"="administrative"][~"{}"~"."]{};'.format(names, bounds)
    rivers = 'way["waterway"][~"{}"~"."]{};'.format(names, bounds)
    if sortie:
        return "[out:json][timeout:60];({}{}{}{}{});{}".format(places, named_water, reservoirs, admin, rivers, sortie)
    # "center" évite de transférer le contour complet des zones habitées ; les lignes
    # fluviales gardent leur géométrie pour être affichables et étiquetables dans QGIS.
    place_features = places + named_water + reservoirs + admin
    return "[out:json][timeout:60];({});out center;({});out geom;".format(place_features, rivers)


def fh4_osm_name(tags):
    """Nom affichable et clé OSM d'origine ; les variantes linguistiques restent exportées séparément."""
    for key in FH4_OSM_NAME_KEYS:
        value = str((tags or {}).get(key) or "").strip()
        if value:
            return value, key
    return "", ""


def fh4_osm_cache_info(dest_dir, bbox):
    """Cache OSM valide uniquement pour la même emprise et la même version de requête."""
    if not dest_dir:
        return None
    meta_path = os.path.join(dest_dir, "osm_cache_meta.json")
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        old_bbox = meta.get("bbox") or []
        if int(meta.get("version", 0)) != FH4_OSM_CACHE_VERSION or len(old_bbox) != 4:
            return None
        if any(abs(float(a) - float(b)) > 1e-7 for a, b in zip(old_bbox, bbox)):
            return None
        gpkg_name = os.path.basename(str(meta.get("gpkg") or "osm_toponymes.gpkg"))
        gpkg = os.path.join(dest_dir, gpkg_name)
        if not os.path.isfile(gpkg):
            return None
        return {"gpkg": gpkg, "lieux": int(meta.get("lieux", 0)), "cours": int(meta.get("cours", 0)),
                "sans_nom": int(meta.get("sans_nom", 0)), "elements": int(meta.get("elements", 0)),
                "date": meta.get("date"), "cached": True}
    except Exception:
        return None


def fh4_osm_estimate(http, bbox):
    """Comptage léger, avec noms alternatifs et objets place en points, zones ou relations."""
    try:
        raw = http.post(FH4_OSM_URL, fh4_osm_query(bbox, "out count;"))
        js = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        tags = (js.get("elements") or [{}])[0].get("tags") or {}
        n = int(tags.get("total", 0))
        return {"elements": n, "octets": n * FH4_OSM_OCTETS_PAR_ELEMENT, "connu": True}
    except Exception as ex:
        return {"elements": None, "octets": None, "connu": False, "erreur": str(ex)[:120]}


def fh4_osm_download(http, bbox, dest_dir, log, on_prog=None, cancel=None):
    """Télécharge une fois les noms OSM du bassin, les met en cache par emprise et produit un GeoPackage lisible dans QGIS."""
    ogr, osr, gdal = _fh4_ogr()
    os.makedirs(dest_dir, exist_ok=True)
    cached = fh4_osm_cache_info(dest_dir, bbox)
    if cached:
        log("ℹ OSM : cache local valide pour cette emprise ({} lieux, {} cours d'eau) ; aucune requête réseau.".format(cached["lieux"], cached["cours"]))
        if on_prog:
            on_prog({"fichier": os.path.basename(cached["gpkg"]) + " (cache)", "type": "OpenStreetMap / cache",
                     "extension": ".gpkg", "octets_reseau": 0, "total_connu": 0, "pct": 100.0,
                     "fichiers_faits": 1, "fichiers_total": 1, "total_estime": False, "termine": True})
        return cached
    net = {"octets_reseau": 0, "total_connu": None, "vitesse_globale": 0}

    def report(d):
        net.update(d)
        if on_prog:
            on_prog(dict(d, fichier="osm_brut.json", type="OpenStreetMap / Overpass", extension=".json",
                         fichiers_faits=0, fichiers_total=1, total_estime=False))

    try:
        raw = http.post(FH4_OSM_URL, fh4_osm_query(bbox), on_chunk=report, cancel=cancel)
    except TypeError as ex:
        if "unexpected keyword argument" not in str(ex):
            raise
        raw = http.post(FH4_OSM_URL, fh4_osm_query(bbox))
    txt = raw if isinstance(raw, str) else raw.decode("utf-8")
    raw_path = os.path.join(dest_dir, "osm_brut.json")
    fh4_write_text(raw_path, txt, log)
    js = json.loads(txt)
    s84 = osr.SpatialReference()
    s84.ImportFromEPSG(4326)
    s84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    s32 = osr.SpatialReference()
    s32.ImportFromEPSG(FH4_CRS)
    s32.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(s84, s32)
    gp = os.path.join(dest_dir, "osm_toponymes.gpkg")
    tmp = fh4_tmp_path(gp)
    if os.path.exists(tmp):
        os.remove(tmp)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(tmp)
    lp = ds.CreateLayer("lieux", s32, ogr.wkbPoint)
    lw = ds.CreateLayer("cours_d_eau", s32, ogr.wkbLineString)
    fields = ("name", "name_fr", "name_mg", "name_en", "alt_name", "official_name", "short_name", "int_name", "name_source", "type", "osm_type", "osm_id")
    for ly in (lp, lw):
        for field in fields:
            ly.CreateField(ogr.FieldDefn(field, ogr.OFTString))

    def set_osm_fields(feature, tags, element, kind):
        name, source = fh4_osm_name(tags)
        values = {"name": name, "name_fr": tags.get("name:fr", ""), "name_mg": tags.get("name:mg", ""),
                  "name_en": tags.get("name:en", ""), "alt_name": tags.get("alt_name", ""),
                  "official_name": tags.get("official_name", ""), "short_name": tags.get("short_name", ""),
                  "int_name": tags.get("int_name", ""), "name_source": source, "type": kind,
                  "osm_type": element.get("type", ""), "osm_id": element.get("id", "")}
        for field, value in values.items():
            feature.SetField(field, str(value or ""))

    npts = nlin = sans_nom = 0
    seen_places, seen_rivers = set(), set()
    for el in js.get("elements", []):
        tags = el.get("tags") or {}
        key = (el.get("type"), el.get("id"))
        has_point_name = (tags.get("place") or tags.get("natural") in ("water", "peak", "waterfall")
                          or tags.get("landuse") == "reservoir" or tags.get("boundary") == "administrative")
        if has_point_name and key not in seen_places:
            if el.get("type") == "node" and "lat" in el and "lon" in el:
                xy = (el["lon"], el["lat"])
            else:
                center = el.get("center") or {}
                xy = (center.get("lon"), center.get("lat")) if "lon" in center and "lat" in center else None
            if xy:
                g = ogr.Geometry(ogr.wkbPoint)
                g.AddPoint_2D(float(xy[0]), float(xy[1]))
                g.Transform(tr)
                ft = ogr.Feature(lp.GetLayerDefn())
                kind = (tags.get("place") or ("natural:" + tags.get("natural", "") if tags.get("natural")
                         else "landuse:" + tags.get("landuse", "") if tags.get("landuse")
                         else "boundary:" + tags.get("boundary", "")))
                set_osm_fields(ft, tags, el, kind)
                ft.SetGeometry(g)
                lp.CreateFeature(ft)
                seen_places.add(key)
                npts += 1
                if not fh4_osm_name(tags)[0]:
                    sans_nom += 1
        if el.get("type") == "way" and tags.get("waterway") and key not in seen_rivers:
            coords = el.get("geometry") or []
            if len(coords) >= 2:
                g = ogr.Geometry(ogr.wkbLineString)
                for q in coords:
                    g.AddPoint_2D(float(q["lon"]), float(q["lat"]))
                g.Transform(tr)
                ft = ogr.Feature(lw.GetLayerDefn())
                set_osm_fields(ft, tags, el, tags.get("waterway", ""))
                ft.SetGeometry(g)
                lw.CreateFeature(ft)
                seen_rivers.add(key)
                nlin += 1
                if not fh4_osm_name(tags)[0]:
                    sans_nom += 1
    ds = None
    out = fh4_commit(tmp, gp, log)
    result = {"gpkg": out, "lieux": npts, "cours": nlin, "sans_nom": sans_nom, "elements": npts + nlin}
    meta = {"version": FH4_OSM_CACHE_VERSION, "bbox": [float(x) for x in bbox], "gpkg": os.path.basename(out),
            "lieux": npts, "cours": nlin, "sans_nom": sans_nom, "elements": npts + nlin, "date": fh4_iso(time.time())}
    try:
        fh4_write_text(os.path.join(dest_dir, "osm_cache_meta.json"), json.dumps(meta, ensure_ascii=False, indent=1), log)
    except Exception as ex:
        log("⚠ Métadonnées du cache OSM non écrites : {}.".format(str(ex)[:120]))
    log("✅ OSM : {} lieux, {} cours d'eau ({} sans nom exploitable). Noms exportés : name + variantes fr/mg/en et alias ; vérifier leur exactitude. Attribution OpenStreetMap/ODbL requise.".format(npts, nlin, sans_nom))
    if on_prog:
        on_prog(dict(net, fichier="osm_brut.json → osm_toponymes.gpkg", type="OpenStreetMap / GeoPackage", extension=".gpkg",
                     fichiers_faits=1, fichiers_total=1, total_estime=False, pct=100.0, termine=True))
    return result


def fh4_name_rivers(spec, dem, net, links, log, tol_m=100.0):
    """Joint un NOM à chaque tronçon calculé : nom de la ligne nommée la plus proche (≤ tol_m) en majorité sur les pixels du tronçon. Renvoie {indice de tronçon: nom}."""
    ogr, osr, gdal = _fh4_ogr()
    parts = str(spec).split("|")
    ds = ogr.Open(parts[0], 0)
    ly = ds.GetLayerByName(parts[1].split("=")[1]) if len(parts) > 1 else ds.GetLayerByIndex(0)
    srs = ly.GetSpatialRef()
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(FH4_CRS)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = None
    if srs is not None:
        srs = srs.Clone()
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(srs, dst)
    defn = ly.GetLayerDefn()
    names = [defn.GetFieldDefn(j).GetName() for j in range(defn.GetFieldCount())]
    champ = next((c for c in FH4_NOM_CHAMPS if c in names), None)
    if champ is None:
        raise Fh4Error("Aucun champ de nom reconnu dans la couche de rivières nommées.")
    lines = []
    gt = dem["gt"]
    xmin, ymax = gt[0], gt[3]
    xmax, ymin = xmin + gt[1] * dem["z"].shape[1], ymax + gt[5] * dem["z"].shape[0]
    for f in ly:
        g = f.GetGeometryRef()
        nm = f.GetField(champ)
        if g is None or not nm:
            continue
        g = g.Clone()
        if tr is not None:
            g.Transform(tr)
        e = g.GetEnvelope()
        if e[1] < xmin - tol_m or e[0] > xmax + tol_m or e[3] < ymin - tol_m or e[2] > ymax + tol_m:
            continue
        lines.append((str(nm), g))
    out = {}
    W = net["W"]
    for lid, L in enumerate(links):
        votes = {}
        for k in L["cells"][::3]:
            x, y = fh4_cell_xy(dem, k, W)
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(x, y)
            best = None
            for nm, g in lines:
                dd = g.Distance(pt)
                if dd <= tol_m and (best is None or dd < best[0]):
                    best = (dd, nm)
            if best:
                votes[best[1]] = votes.get(best[1], 0) + 1
        if votes:
            out[lid] = max(votes.items(), key=lambda t: t[1])[0]
    log("🏷 Rivières nommées : {} tronçon(s) sur {} reçoivent un nom (≤ {} m d'une ligne nommée).".format(len(out), len(links), int(tol_m)))
    return out


# ---------------------------------------------------------------- onglet « Hydrologie » PRÉPARÉ : squelette, aucune valeur
FH4_HYDRO_LIGNES = ["Précipitation", "Évapotranspiration réelle", "Évapotranspiration potentielle (PET)", "Ruissellement de surface", "Écoulement latéral", "Percolation", "Recharge de la nappe",
                    "Débit de base", "Humidité / stock d'eau du sol", "Stock de la nappe", "Rendement en eau", "Débit à l'exutoire de chaque sous-bassin", "Érosion / sédiments (si SWAT+ les produit)"]
FH4_HYDRO_NOTE = ("Aucun chiffre ni carte factice : chaque ligne reste « EN ATTENTE DE SWAT+ » jusqu'à l'exécution du modèle. Les noms exacts des variables de sortie de SWAT+ ne sont PAS écrits de mémoire : "
                  "ils seront lus dans sa documentation et ses fichiers de sortie. À ma connaissance, les « surfaces mouillées / inondées » ne sont pas une sortie standard par sous-bassin de SWAT+ "
                  "(zones humides et réservoirs ont leurs sorties propres ; l'inondation 2D relève de HEC-RAS, étage 6) : à vérifier dans la documentation de SWAT+ avant de promettre cette carte. "
                  "Le contrôle de fermeture du bilan (P = ET + ruissellement + percolation + variation de stock…) est prévu pour la livraison où SWAT+ aura tourné.")


# ============================================================
# 10. V3 — AUTOMATISATION : tables SWAT+ remplies automatiquement (HYPOTHÈSES), période météo par défaut, bilan du lancement
# ============================================================
FH4_STATUT_AUTO = "AUTO — HYPOTHÈSE non validée"
# Mots-clés (sans accents, minuscules) des classes d'occupation de l'étage 1 → nom de code d'occupation SWAT+ ÉCRIT DE MÉMOIRE (HYPOTHÈSE à vérifier dans la base SWAT+)
FH4_LU_MOTS = (("riz", "rice"), ("rizi", "rice"), ("eau", "watr"), ("plan d'eau", "watr"), ("lac", "watr"), ("marais", "wetl"), ("humide", "wetl"), ("mangrove", "wetl"), ("foret", "frst"), ("forest", "frst"),
               ("arbre", "frst"), ("boise", "frst"), ("savane", "past"), ("prairie", "past"), ("herb", "past"), ("paturage", "past"), ("pature", "past"), ("steppe", "past"), ("culture", "agrl"),
               ("agric", "agrl"), ("champ", "agrl"), ("cultiv", "agrl"), ("bati", "urbn"), ("urbain", "urbn"), ("habitat", "urbn"), ("route", "urbn"), ("sol nu", "barr"), ("nu", "barr"), ("rocher", "barr"),
               ("buisson", "rnge"), ("arbust", "rnge"), ("brousse", "rnge"))
FH4_SOL_SC = {1: (20, 60), 2: (7, 47), 3: (52, 42), 4: (32, 34), 5: (10, 34), 6: (60, 27), 7: (41, 19), 8: (20, 12), 9: (65, 10), 10: (7, 6), 11: (82, 6), 12: (92, 4)}   # % sable, % argile représentatifs (HYPOTHÈSE)
FH4_SOL_HSG = {12: "A", 11: "A", 9: "A", 7: "B", 8: "B", 10: "B", 6: "C", 4: "D", 5: "D", 3: "D", 2: "D", 1: "D"}                                      # groupe hydrologique par classe de texture (HYPOTHÈSE)
FH4_SOL_PROFONDEUR_MM = 1000.0       # HYPOTHÈSE
FH4_SOL_OM_PCT = 2.0                 # HYPOTHÈSE : matière organique


def fh4_strip(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower()) if unicodedata.category(c) != "Mn")


def fh4_auto_landuse(nom):
    """Code d'occupation SWAT+ proposé pour une classe de l'étage 1 d'après son nom : (code, explication). Écrit de mémoire = HYPOTHÈSE à vérifier dans la base SWAT+."""
    n = fh4_strip(nom)
    for k, code in FH4_LU_MOTS:
        if k in n:
            return code, "mot « {} » dans le nom de la classe".format(k)
    return "past", "classe non reconnue : pâturage par défaut"


def fh4_saxton_rawls(sand_pct, clay_pct, om_pct):
    """Propriétés hydrauliques par fonction de pédotransfert de Saxton & Rawls (2006), équations écrites de mémoire (NON revérifiées à la source dans cette session) : HYPOTHÈSE non calibrée.
    Renvoie {"theta_1500", "theta_33", "theta_s", "densite", "eau_disponible", "ksat_mm_h"}."""
    S, C, OM = sand_pct / 100.0, clay_pct / 100.0, om_pct
    t1500t = -0.024 * S + 0.487 * C + 0.006 * OM + 0.005 * S * OM - 0.013 * C * OM + 0.068 * S * C + 0.031
    t1500 = t1500t + (0.14 * t1500t - 0.02)
    t33t = -0.251 * S + 0.195 * C + 0.011 * OM + 0.006 * S * OM - 0.027 * C * OM + 0.452 * S * C + 0.299
    t33 = t33t + (1.283 * t33t ** 2 - 0.374 * t33t - 0.015)
    ts33t = 0.278 * S + 0.034 * C + 0.022 * OM - 0.018 * S * OM - 0.027 * C * OM - 0.584 * S * C + 0.078
    ts33 = ts33t + (0.636 * ts33t - 0.107)
    ts = t33 + ts33 - 0.097 * S + 0.043
    B = (math.log(1500.0) - math.log(33.0)) / (math.log(t33) - math.log(t1500))
    lam = 1.0 / B
    ks = 1930.0 * max(ts - t33, 1e-6) ** (3.0 - lam)
    return {"theta_1500": t1500, "theta_33": t33, "theta_s": ts, "densite": (1.0 - ts) * 2.65, "eau_disponible": t33 - t1500, "ksat_mm_h": ks}


def fh4_auto_soil_row(code, nom, surf):
    if code not in FH4_SOL_SC:
        return [code, nom, surf, "", "", "", "", "", "", FH4_STATUT_A_REMPLIR]
    s_, c_ = FH4_SOL_SC[code]
    r = fh4_saxton_rawls(s_, c_, FH4_SOL_OM_PCT)
    return [code, nom, surf, fh4_csvnum(FH4_SOL_PROFONDEUR_MM, 0), fh4_csvnum(r["densite"], 2), fh4_csvnum(r["eau_disponible"], 3), fh4_csvnum(r["ksat_mm_h"], 1),
            fh4_csvnum(FH4_SOL_OM_PCT / 1.724, 2), FH4_SOL_HSG.get(code, ""), FH4_STATUT_AUTO]


def fh4_meteo_period_fallback(today=None):
    """Période par défaut quand l'étage 2 n'en donne pas : les 5 dernières années civiles complètes (HYPOTHÈSE)."""
    y = (today or time.localtime()).tm_year
    return "{}-01-01".format(y - 5), "{}-12-31".format(y - 1)


FH4_ETAPES = (("amont", "Vérification des étages 1 et 2"), ("outils", "Recherche de TauDEM / QSWAT+"), ("sb", "Sous-bassins, réseau, statistiques"), ("hru", "HRU et tables SWAT+ (automatiques)"),
              ("topo", "Toponymes et rivières nommées (en ligne)"), ("meteo", "Météo en ligne (CHIRPS + NASA POWER)"), ("swat", "Préparation des entrées QSWAT+"), ("cartes", "Cartes"), ("bilan", "Bilan"))


def fh4_bilan_text(statuts, decisions, res, basin):
    """Bilan du lancement : étapes réussies / ignorées, décisions AUTOMATIQUES (HYPOTHÈSES) et ce qu'il reste à faire hors de l'étage 4."""
    L = ["FALKHYDRO+ — ÉTAGE 4 {} — BILAN DU LANCEMENT AUTOMATIQUE".format(FH4_VERSION), "Bassin : {} — {}".format(basin, fh4_iso(time.time())), "", "ÉTAPES"]
    for k, lab in FH4_ETAPES:
        s = statuts.get(k)
        L.append("  {} {}{}".format({"ok": "✔", "echec": "⚠", "ignore": "–"}.get(s[0] if s else "", "?"), lab, " : " + s[1] if s and s[1] else ""))
    L += ["", "DÉCISIONS PRISES AUTOMATIQUEMENT (toutes des HYPOTHÈSES non validées, modifiables dans les réglages avancés)"] + ["  - " + d for d in decisions]
    if res:
        L += ["", "RÉSULTAT : {} sous-bassins, réseau et statistiques dans {}.".format(len(res["rows"]), res["dirs"]["e4"])]
    L += ["", "CE QUI RESTE (hors de cette livraison)",
          "  - Exécuter SWAT+ : l'étage 4 prépare les entrées (Preparation_QSWAT) mais ne lance pas SWAT+ (son installation et son API n'ont pas été vérifiées).",
          "  - Cartes d'infiltration, de stocks d'eau et de recharge : elles viendront des sorties de SWAT+ (onglet Hydrologie, en attente).",
          "  - Les codes SWAT+ et les paramètres de sol écrits automatiquement sont des HYPOTHÈSES (écrits de mémoire) à vérifier dans la base SWAT+ avant tout résultat.", "", FH4_PHRASE_A]
    return "\n".join(L) + "\n"


# ============================================================
# 8. THREAD DE CALCUL GÉNÉRIQUE, CARTE INTERACTIVE, INTERFACE (onglets)
# ============================================================
class Fh4Task(QThread):
    """Exécute fn(params, log, progress, cancel) dans un thread : GDAL / OGR / NumPy / réseau seulement, AUCUN objet QGIS. Les couches et cartes (API QGIS) se font ensuite sur le thread principal."""
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    info_signal = pyqtSignal(object)
    done_signal = pyqtSignal(object)
    error_signal = pyqtSignal(str)

    def __init__(self, fn, params):
        super().__init__()
        import threading
        self.fn, self.params = fn, params
        self.cancel = threading.Event()

    def run(self):
        try:
            p = dict(self.params)
            p["_info"] = self.info_signal.emit
            self.done_signal.emit(self.fn(p, self.log_signal.emit, self.progress_signal.emit, self.cancel))
        except Fh4Cancelled:
            self.error_signal.emit("⏹ Arrêt demandé : traitement interrompu (les fichiers déjà écrits restent tels quels ; un téléchargement partiel sera repris au prochain clic).")
        except Fh4Error as ex:
            self.error_signal.emit("❌ " + str(ex))
        except Exception:
            self.error_signal.emit("❌ Erreur inattendue :\n" + traceback.format_exc())


def fh4_preview_task(p, log, progress, cancel):
    """Aperçu des 3 seuils : prépare l'hydrologie (réutilisée ensuite par Calculer si rien ne change) et compte les sous-bassins."""
    ctx = fh4_prepare(p, log, progress, cancel)
    pv = fh4_preview(ctx["dem"], ctx["hyd"], ctx["basin_ha"], log, cancel)
    return {"preview": pv, "ctx": ctx, "key": p.get("key")}


def fh4_run_task(p, log, progress, cancel):
    return fh4_run(p, log, progress, cancel)


def fh4_isolate_task(p, log, progress, cancel):
    return fh4_isolate(p, log, progress)


def fh4_meteo_zone(root, basin, log):
    """Emprise WGS 84 (ouest, sud, est, nord) du bassin (contour de l'étage 2) et période par défaut du manifeste de l'étage 2. Lecture seule."""
    ogr, osr, gdal = _fh4_ogr()
    dirs = fh4_dirs(root, basin)
    info2 = fh4_find_stage2(dirs["e2"], None)
    wkt = fh4_basin_from_stage2(info2, log) or fh4_basin_footprint_wkt(info2["A"]["P_Itasy"], 600)
    g = ogr.CreateGeometryFromWkt(wkt)
    s32, s84 = osr.SpatialReference(), osr.SpatialReference()
    s32.ImportFromEPSG(FH4_CRS)
    s84.ImportFromEPSG(4326)
    for s_ in (s32, s84):
        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    g.Transform(osr.CoordinateTransformation(s32, s84))
    x0, x1, y0, y1 = g.GetEnvelope()
    return (x0, y0, x1, y1), fh4_meteo_period_default(info2)


def fh4_meteo_estimate_task(p, log, progress, cancel):
    http = p.get("http") or Fh4Http()
    bbox, _per = fh4_meteo_zone(p["root"], p["basin"], log) if not p.get("bbox") else (p["bbox"], None)
    log("ℹ " + FH4_METEO_NOTE)
    est = fh4_meteo_estimate(http, p["d0"], p["d1"], bbox, p["cache"], p.get("chirps", True), p.get("power", True))
    for ln in est["lignes"]:
        log("ℹ " + ln)
    est["bbox"] = bbox
    est["requetes"] = {"head": http.n_head, "get": http.n_get}
    return est


def fh4_meteo_jobs(p, bbox, est=None):
    jobs = []
    if p.get("chirps", True):
        jobs += fh4_chirps_jobs(p["d0"], p["d1"], p["cache"])
        if est and est.get("chirps") and est["chirps"].get("unite"):
            for j in jobs:
                j["expected"], j["estime"] = int(est["chirps"]["unite"]), True
    if p.get("power", True):
        jobs += fh4_power_jobs(bbox, p["d0"], p["d1"], p["cache"])
    return jobs


def fh4_meteo_download_task(p, log, progress, cancel):
    """Téléchargement (appelé SEULEMENT après un clic confirmé) : reprenable, avec progression détaillée ; écrit meteo_sources.json."""
    http = p.get("http") or Fh4Http()
    bbox = p.get("bbox") or fh4_meteo_zone(p["root"], p["basin"], log)[0]
    jobs = fh4_meteo_jobs(p, bbox, p.get("estimate"))
    kinds = [k for k, on in (("CHIRPS", p.get("chirps", True)), ("POWER", p.get("power", True))) if on]
    os.makedirs(p["cache"], exist_ok=True)
    fh4_meteo_sources_json(p["cache"], p["d0"], p["d1"], bbox, kinds, p.get("estimate"))
    log("⬇ Téléchargement lancé sur clic : {} fichier(s)/requête(s) ; cache {}.".format(len(jobs), p["cache"]))
    log("⚡ Météo : file glissante, jusqu'à {} CHIRPS et {} POWER simultanés ; chaque place libre est relancée sans attendre les autres.".format(FH4_METEO_WORKERS, FH4_POWER_WORKERS))
    r = fh4_download_batch(http, jobs, p["_info"] if p.get("_info") else (lambda d: None), cancel)
    log("✅ Météo : {} fichier(s) prêts ({} déjà en cache, {} reçus).".format(r["fichiers"], r["en_cache"], fh4_fmt_bytes(r["octets"])))
    fh4_meteo_sources_json(p["cache"], p["d0"], p["d1"], bbox, kinds, p.get("estimate"))
    r["requetes"] = {"head": http.n_head, "get": http.n_get}
    return r


def fh4_meteo_test_task(p, log, progress, cancel):
    """Test rapide (1 mois, 1 point NASA POWER + 1 fichier CHIRPS) dans cache/test."""
    import datetime
    http = p.get("http") or Fh4Http()
    bbox = p.get("bbox") or fh4_meteo_zone(p["root"], p["basin"], log)[0]
    d0 = datetime.date.fromisoformat(str(p["d0"])[:10])
    d1 = min(datetime.date.fromisoformat(str(p["d1"])[:10]), d0 + datetime.timedelta(days=29))
    pts = fh4_power_points(bbox)
    la, lo = pts[len(pts) // 2]
    tc = os.path.join(p["cache"], "test")
    jobs = []
    if p.get("power", True):
        jobs.append({"kind": "POWER", "url": FH4_POWER_URL.format(params=",".join(FH4_POWER_PARAMS), lon=lo, lat=la, d0=d0.strftime("%Y%m%d"), d1=d1.strftime("%Y%m%d")),
                     "dest": os.path.join(tc, "power_test_{}_{}.json".format(la, lo)), "expected": None})
    if p.get("chirps", True):
        jobs += fh4_chirps_jobs(d0, d0, tc)
    log("🧪 Test : {} requête(s) (point {}, {} ; {} → {}).".format(len(jobs), la, lo, d0, d1))
    r = fh4_download_batch(http, jobs, p["_info"] if p.get("_info") else (lambda d: None), cancel)
    r["requetes"] = {"head": http.n_head, "get": http.n_get}
    log("✅ Test terminé : {} fichier(s), {}.".format(r["fichiers"], fh4_fmt_bytes(r["octets"])))
    return r


def fh4_hru_task(p, log, progress, cancel):
    return fh4_hru_build(p, log, progress, cancel)


def fh4_swat_prepare_task(p, log, progress, cancel):
    return fh4_swat_prepare(p, log, progress, cancel)


def fh4_inspect_swat_task(p, log, progress, cancel):
    return fh4_inspect_swat(extra_roots=p.get("roots") or ())


def fh4_osm_estimate_task(p, log, progress, cancel):
    """Estimation OSM (requête de COMPTAGE, aucun téléchargement de données) ; calcule l'emprise du bassin."""
    http = p.get("http") or Fh4Http()
    bbox = p.get("bbox") or fh4_meteo_zone(p["root"], p["basin"], log)[0]
    cached = fh4_osm_cache_info(p.get("dest"), bbox)
    if cached:
        est = {"elements": cached["elements"], "octets": 0, "connu": True, "cached": True, "cache": cached}
    else:
        est = fh4_osm_estimate(http, bbox)
    est["bbox"] = bbox
    est["requetes"] = {"post": http.n_post, "get": http.n_get}
    return est


def fh4_osm_download_task(p, log, progress, cancel):
    http = p.get("http") or Fh4Http()
    return fh4_osm_download(http, p["bbox"], p["dest"], log, p.get("_info"), cancel)


def fh4_project_outlet():
    """Point « Exutoire » de l'étage 2 : première couche de POINTS du projet QGIS dont le nom contient « exutoire » (lecture seule) → (x, y, nom) en EPSG:32738, ou None. Thread principal."""
    try:
        from qgis.core import QgsWkbTypes, QgsCoordinateTransform
        for l in QgsProject.instance().mapLayers().values():
            if isinstance(l, QgsVectorLayer) and "xutoire" in l.name().lower() and l.geometryType() == QgsWkbTypes.PointGeometry:
                for f in l.getFeatures():
                    g = f.geometry()
                    if g is not None and not g.isNull():
                        pt = QgsCoordinateTransform(l.crs(), QgsCoordinateReferenceSystem("EPSG:{}".format(FH4_CRS)), QgsProject.instance()).transform(g.centroid().asPoint())
                        return (pt.x(), pt.y(), l.name())
    except Exception:
        return None
    return None


def fh4_project_layers_for_topo():
    """Couches du projet QGIS candidates (points / lignes avec un champ de nom) → même format que fh4_scan_layers."""
    out = []
    try:
        from qgis.core import QgsWkbTypes
        for l in QgsProject.instance().mapLayers().values():
            if isinstance(l, QgsVectorLayer) and l.geometryType() in (QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry):
                names = [f.name() for f in l.fields()]
                champ = next((c for c in FH4_NOM_CHAMPS if c in names), None)
                if champ and "xutoire" not in l.name().lower():
                    out.append({"spec": l.source().split("|")[0] if "|layername" not in l.source() else l.source(), "nom": "projet : " + l.name(), "genre": "point" if l.geometryType() == QgsWkbTypes.PointGeometry else "ligne", "n": l.featureCount(), "champ": champ})
    except Exception:
        pass
    return out


def fh4_sb_tooltip(row, scen):
    k = scen if scen in row["s"] else (next(iter(row["s"])) if row["s"] else None)
    t = "{} — {} ha — aval : {}".format(row["id"], fh4_fr(row["surf_ha"], 1), row["aval"] or "sortie du bassin")
    if k:
        t += " — A moyen {} t/ha/an ({})".format(fh4_fr(row["s"][k]["mean"], 1), _FH4_NOM_SC.get(k, k))
    return t


def fh4_sb_lines(row, res):
    lines = ["{}".format(row["id"]), "Surface : {} ha ; ordre de Strahler : {}".format(fh4_fr(row["surf_ha"], 1), row["ordre"]), "Sous-bassin aval : {}".format(row["aval"] or "— (sortie du bassin)")]
    for k in res["scen"]:
        s = row["s"][k]
        lines.append("{} : A moyen {} t/ha/an ; perte {} t/an".format(_FH4_NOM_SC[k], fh4_fr(s["mean"], 1), fh4_fr(s["loss"], 0)))
    lines.append(FH4_PHRASE_A)
    return lines


def fh4_dialog_sb(parent, lines):
    """Fenêtre d'un sous-bassin cliqué : id, surface, aval, A moyen ; « Isoler » ; DÉFAUT = Fermer (un clic ne lance aucun calcul)."""
    from qgis.PyQt.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("Sous-bassin")
    box.setText("\n".join(lines))
    b_ok = box.addButton("Isoler (masque de l'étage 2) et créer la carte", QMessageBox.AcceptRole)
    b_no = box.addButton("Fermer", QMessageBox.RejectRole)
    box.setDefaultButton(b_no)
    box.setEscapeButton(b_no)
    box.exec_()
    return box.clickedButton() is b_ok


def fh4_map_view_factory(parent):
    """Carte interactive (QgsMapCanvas) : sous-bassins coloriés, réseau, exutoires, lac ; clic → on_click(no) ; survol → on_hover(no) ; vide tant qu'aucun résultat n'est chargé."""
    from qgis.gui import QgsMapCanvas, QgsMapToolEmitPoint, QgsRubberBand
    from qgis.core import QgsSpatialIndex, QgsGeometry, QgsPointXY, QgsWkbTypes

    class View:
        def __init__(self):
            self.widget = QgsMapCanvas(parent)
            self.widget.setCanvasColor(QColor("white"))
            self.widget.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:{}".format(FH4_CRS)))
            self.layers, self.on_click, self.on_hover = [], None, None
            self.geoms, self.idx, self.rb, self.full = {}, None, None, None
            self.tool = QgsMapToolEmitPoint(self.widget)
            self.tool.canvasClicked.connect(self._click)
            self.widget.setMapTool(self.tool)
            self.widget.xyCoordinates.connect(self._move)

        def clear(self):
            self.widget.setLayers([])
            self.layers, self.geoms, self.idx = [], {}, None
            self.widget.refresh()

        def show(self, res):
            L = fh4_res_layers(res)
            if L["sous_bassins"] is None:
                raise Fh4Error("GeoPackage des sous-bassins illisible : relance « Calculer ».")
            fh4_style_sb(L["sous_bassins"])
            if L["lac"] is not None:
                fh4_style_simple(L["lac"], "170,210,240,255", "120,160,200,255", "0.2")
            if L["rivieres"] is not None:
                fh4_style_rivers(L["rivieres"])
            if L["jonctions"] is not None:
                fh4_style_outlets(L["jonctions"])
            self.layers = [x for x in (L["jonctions"], L["rivieres"], L["lac"], L["sous_bassins"]) if x is not None]
            self.geoms, self.idx = {}, QgsSpatialIndex()
            for f in L["sous_bassins"].getFeatures():
                self.geoms[f.id()] = (int(f["no"]), f.geometry())
                self.idx.addFeature(f)
            self.widget.setLayers(self.layers)
            self.full = L["sous_bassins"].extent()
            self.zoom_full()

        def zoom_full(self):
            if self.full is not None:
                e = QgsRectangle(self.full)
                e.scale(1.05)
                self.widget.setExtent(e)
                self.widget.refresh()
                self.highlight(None)

        def sb_at(self, x, y):
            if self.idx is None:
                return None
            pt = QgsPointXY(x, y)
            for fid in self.idx.intersects(QgsRectangle(x, y, x, y)):
                no, g = self.geoms[fid]
                if g.contains(QgsGeometry.fromPointXY(pt)):
                    return no
            return None

        def highlight(self, no):
            if self.rb is not None:
                self.rb.reset(QgsWkbTypes.PolygonGeometry)
            if no is None:
                return
            for fid, (n, g) in self.geoms.items():
                if n == no:
                    if self.rb is None:
                        self.rb = QgsRubberBand(self.widget, QgsWkbTypes.PolygonGeometry)
                        self.rb.setColor(QColor(255, 0, 0, 60))
                        self.rb.setStrokeColor(QColor(220, 0, 0))
                        self.rb.setWidth(2)
                    self.rb.setToGeometry(g, None)
                    e = g.boundingBox()
                    e.scale(1.4)
                    self.widget.setExtent(e)
                    self.widget.refresh()

        def _click(self, pt, btn):
            no = self.sb_at(pt.x(), pt.y())
            if no is not None and self.on_click:
                self.on_click(no)

        def _move(self, pt):
            if self.on_hover:
                self.on_hover(self.sb_at(pt.x(), pt.y()), self.widget)
    return View()


FH4_COLS = (("id", "Sous-bassin", "txt"), ("aval", "Aval", "txt"), ("surf_ha", "Surface (ha)", "num1"), ("z_min", "Alt. min (m)", "num0"), ("z_moy", "Alt. moy (m)", "num0"), ("z_max", "Alt. max (m)", "num0"),
            ("pente_pct", "Pente moy (%)", "num1"), ("long_bassin_m", "Long. bassin (m)", "num0"), ("long_cours_m", "Long. cours (m)", "num0"), ("ordre", "Ordre Strahler", "int"),
            ("pct_terre", "Terre classée (%)", "num1"), ("lac_ha", "Lac (ha)", "num1"))


def fh4_table_columns(res):
    cols = list(FH4_COLS)
    for k in res["scen"]:
        cols += [("A_" + k, "A moyen {} (t/ha/an)".format(_FH4_NOM_SC[k]), "num1"), ("L_" + k, "Perte {} (t/an)".format(_FH4_NOM_SC[k]), "num0")]
    return cols + [("remarque", "Remarque", "txt")]


def fh4_cell_value(r, key):
    if key.startswith("A_"):
        return (r["s"].get(key[2:]) or {}).get("mean")
    if key.startswith("L_"):
        return (r["s"].get(key[2:]) or {}).get("loss")
    if key == "remarque":
        bits = []
        if r.get("exutoire"):
            bits.append("exutoire du bassin")
        if r.get("sortie_secondaire"):
            bits.append("sortie secondaire")
        if r.get("petit"):
            bits.append("très petit")
        if (r.get("lac_ha") or 0) > 0:
            bits.append("contient du lac")
        return ", ".join(bits)
    return r.get(key)


def fh4_cell_text(v, kind):
    if kind == "txt":
        return "" if v is None else str(v)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return {"num0": fh4_fr(v, 0), "num1": fh4_fr(v, 1), "int": str(int(v))}[kind]


def fh4_table_csv(res, path, log=None):
    cols = fh4_table_columns(res)
    rows = [[fh4_csvnum(fh4_cell_value(r, k), 3) if kind != "txt" and kind != "int" else (fh4_cell_value(r, k) or "") for k, _t, kind in cols] for r in res["rows"]]
    t = res["total"]
    rows.append(["TOTAL BASSIN"] + [""] * (len(cols) - 1))
    for i, (k, _t, kind) in enumerate(cols):
        if k == "surf_ha":
            rows[-1][i] = fh4_csvnum(t["surf_ha"], 2)
        elif k.startswith("A_") and k[2:] in t["s"]:
            rows[-1][i] = fh4_csvnum(t["s"][k[2:]]["mean"], 2)
        elif k.startswith("L_") and k[2:] in t["s"]:
            rows[-1][i] = fh4_csvnum(t["s"][k[2:]]["loss"], 0)
    return fh4_write_csv(path, [t_ for _k, t_, _kind in cols], rows, FH4_PHRASE_A, log)


def fh4_extension_widget(parent=None, map_factory=None):
    """Onglets : ⚙ Sous-bassins · 📋 Résultats · 🌦 Météo · 📜 Journal. Aucun calcul tant que l'utilisateur n'a pas cliqué ; aucun téléchargement tant qu'il n'a pas confirmé."""
    from qgis.PyQt.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton, QComboBox, QFileDialog, QTextEdit, QProgressBar, QCheckBox, QMessageBox,
                                     QGroupBox, QScrollArea, QTableWidget, QTableWidgetItem, QAbstractItemView, QTabWidget, QSplitter, QSizePolicy, QHeaderView, QRadioButton, QDoubleSpinBox, QListWidget, QListWidgetItem,
                                     QGridLayout, QApplication)
    from qgis.PyQt.QtCore import QTimer
    S = QgsSettings()
    outer = QWidget(parent)
    ov = QVBoxLayout(outer)
    ov.setContentsMargins(2, 2, 2, 2)
    tabs = QTabWidget()
    ov.addWidget(tabs)
    st = {"res": None, "busy": None, "stale": False, "ctx": None, "key": None, "preview": None, "tools": None, "extra_file": None, "iso": None, "est": None, "outlet2": None, "calage": None,
          "network_active": False, "network_source": "—", "network_bytes": 0,
          "taudem_dir": str(S.value("FALKHYDRO/etage4/taudem_dir", "") or ""), "hru": None, "topo_found": []}
    logbox = QTextEdit()
    logbox.setReadOnly(True)

    def log(m):
        logbox.append("{}  {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), m))
        logbox.verticalScrollBar().setValue(logbox.verticalScrollBar().maximum())

    def network_begin(label):
        low = str(label or "").lower()
        if "estim" in low:
            st["network_active"] = False
            net_status.setText("{} — vérification de métadonnées seulement ; aucun fichier téléchargé.".format(label))
            net_detail.setText("Reçu : 0 o | débit : — | le compteur porte uniquement sur les transferts de fichiers.")
            net_bar.setRange(0, 100)
            net_bar.setValue(0)
            net_bar.setFormat("Aucun fichier en transfert")
            return
        if "télécharg" not in low and not ("test" in low and "météo" in low):
            return
        st["network_active"] = True
        st["network_source"] = "En attente du premier bloc"
        st["network_bytes"] = 0
        net_status.setText("{} — connexion en cours, attente des données…".format(label))
        net_detail.setText("Reçu : 0 o | débit moyen : 0 o/s | type et fichier à venir")
        net_bar.setRange(0, 0)
        net_bar.setFormat("Taille totale en attente")

    def network_update(d):
        if not isinstance(d, dict):
            return
        st["network_active"] = True
        source = str(d.get("type") or st.get("network_source") or "Téléchargement")
        if source.lower() not in ("terminé", "termine"):
            st["network_source"] = source
        filename = str(d.get("fichier") or "—")
        received = int(d.get("octets_reseau", d.get("octets", 0)) or 0)
        st["network_bytes"] = received
        speed = d.get("vitesse_globale", d.get("vitesse", 0)) or 0
        ext = str(d.get("extension") or "")
        if ext and ext not in filename:
            source += " ({})".format(ext)
        total = d.get("total_connu")
        if total is None:
            total_text = "taille totale inconnue"
        else:
            total_text = "{} : {}".format("total estimé" if d.get("total_estime") else "total annoncé", fh4_fmt_bytes(total))
        n_done, n_total = d.get("fichiers_faits"), d.get("fichiers_total")
        count_text = "fichiers {}/{}".format(n_done, n_total) if n_done is not None and n_total is not None else "fichier unique"
        net_detail.setText("Reçu sur le réseau : {} | débit moyen : {}/s | {} | {}".format(
            fh4_fmt_bytes(received), fh4_fmt_bytes(speed), count_text, total_text))
        if d.get("termine"):
            net_status.setText("Transfert terminé — {} | {}".format(st.get("network_source") or source, filename))
        else:
            net_status.setText("Type : {} | fichier : {}".format(source, filename))
        pct = d.get("pct")
        if pct is None:
            net_bar.setRange(0, 0)
            net_bar.setFormat("Taille totale inconnue — transfert en cours")
        else:
            precise_pct = max(0.0, min(100.0, float(pct)))
            net_bar.setRange(0, 1000)
            net_bar.setValue(int(precise_pct * 10.0))
            net_bar.setFormat("{:.1f} %".format(precise_pct))

    def network_end(success=True, message=None):
        if not st.get("network_active"):
            return
        st["network_active"] = False
        if success:
            net_status.setText("Transfert terminé — {} | {} reçus.".format(
                st.get("network_source") or "Données", fh4_fmt_bytes(st.get("network_bytes", 0))))
            if net_bar.maximum() == 0 or net_bar.value() >= net_bar.maximum():
                net_bar.setRange(0, 1000)
                net_bar.setValue(1000)
                net_bar.setFormat("Terminé")
        else:
            net_status.setText("Transfert interrompu / erreur — {}".format(str(message or "voir le journal").splitlines()[0][:180]))
            if net_bar.maximum() == 0:
                net_bar.setRange(0, 100)
                net_bar.setValue(0)
                net_bar.setFormat("Interrompu")

    # ---------------------------------------------------------------- ⚙ Sous-bassins : réglages compacts à gauche, carte à droite
    t_calc = QWidget()
    cl = QHBoxLayout(t_calc)
    cl.setContentsMargins(2, 2, 2, 2)
    calc_split = QSplitter(Qt.Horizontal)
    cl.addWidget(calc_split)
    left_w = QWidget()
    lv = QVBoxLayout(left_w)
    lv.setContentsMargins(4, 4, 4, 4)
    sa_left = QScrollArea()
    sa_left.setWidgetResizable(True)
    sa_left.setWidget(left_w)
    sa_left.setMinimumWidth(300)
    sa_left.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa_left.setFrameShape(QScrollArea.NoFrame)
    t_lab = QLabel("<b>Étage 4 — tout en un clic</b><br><i>Choisis le bassin puis « Lancer tout » : données des étages 1–2 et données EN LIGNE seulement ; tout le reste est automatique "
                   "(choix techniques = HYPOTHÈSES journalisées). Étages 1 à 3 en LECTURE SEULE. A = perte potentielle brute, non validée.</i>")
    t_lab.setWordWrap(True)
    lv.addWidget(t_lab)
    net_box = QGroupBox("Suivi réseau — téléchargements")
    net_v = QVBoxLayout(net_box)
    net_status = QLabel("Aucun téléchargement en cours.")
    net_status.setWordWrap(True)
    net_status.setStyleSheet("font-weight:bold;")
    net_detail = QLabel("Reçu : 0 o | débit moyen : — | le type de fichier apparaîtra au démarrage.")
    net_detail.setWordWrap(True)
    net_bar = QProgressBar()
    net_bar.setRange(0, 100)
    net_bar.setValue(0)
    net_bar.setFormat("En attente")
    net_v.addWidget(net_status)
    net_v.addWidget(net_detail)
    net_v.addWidget(net_bar)
    lv.addWidget(net_box)
    g = QGroupBox("Entrées")
    f = QFormLayout(g)
    f.setRowWrapPolicy(QFormLayout.WrapAllRows)
    root_e1 = str(S.value("FALKHYDRO/output_root", "") or "").strip()
    root_edit = QLineEdit(root_e1 if root_e1 and os.path.isdir(root_e1) else str(S.value("FALKHYDRO/dossier_racine", "") or ""))
    b_root = QPushButton("📂")
    b_root.setMaximumWidth(38)
    hr = QHBoxLayout()
    hr.addWidget(root_edit, 1)
    hr.addWidget(b_root)
    f.addRow("Dossier de sortie :", hr)
    basin_combo = QComboBox()
    f.addRow("Bassin :", basin_combo)
    voie_combo = QComboBox()
    voie_combo.addItem("Automatique (TauDEM si trouvé, sinon NumPy)", "auto")
    voie_combo.addItem("Voie A — TauDEM (sous-processus)", "A")
    voie_combo.addItem("Voie C — NumPy (secours, moins éprouvée)", "C")
    for cb_ in (voie_combo, basin_combo):
        cb_.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        cb_.setMinimumContentsLength(12)
    b_inspect = QPushButton("Inspecter les outils")
    b_inspect.setToolTip("Inspecte QSWAT+ et TauDEM sur cette machine (rien n'est lancé) et choisit la voie A ou C.")
    lv.addWidget(g)
    # ---- le bouton unique + la liste des étapes (tout le reste est dans « Réglages avancés »)
    b_all = QPushButton("🚀 Lancer tout")
    b_all.setStyleSheet("font-weight:bold; font-size:14px; padding:6px;")
    b_all.setToolTip("Un clic : vérifications, sous-bassins, HRU et tables, toponymes et météo en ligne, entrées QSWAT+, cartes et bilan. Les choix techniques sont automatiques (HYPOTHÈSES journalisées).")
    lv.addWidget(b_all)
    steps_list = QListWidget()
    steps_list.setSelectionMode(QAbstractItemView.NoSelection)
    steps_list.setFocusPolicy(Qt.NoFocus)
    for k_, lab_ in FH4_ETAPES:
        it_ = QListWidgetItem("○ " + lab_)
        it_.setData(Qt.UserRole, k_)
        steps_list.addItem(it_)
    steps_list.setFixedHeight(22 * len(FH4_ETAPES) + 8)
    steps_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    lv.addWidget(steps_list)
    b_copy_home = QPushButton("Copier le journal")
    b_copy_home.setToolTip("Copie tout le journal dans le presse-papiers (étapes, messages, erreurs).")
    lv.addWidget(b_copy_home)
    adv_in = QWidget()
    adv_lay = QVBoxLayout(adv_in)
    adv_lay.setContentsMargins(0, 0, 0, 0)
    gm = QGroupBox("Méthode")
    gmf = QFormLayout(gm)
    gmf.setRowWrapPolicy(QFormLayout.WrapAllRows)
    gmf.addRow("Calcul :", voie_combo)
    gmf.addRow("", b_inspect)
    adv_lay.addWidget(gm)
    gs = QGroupBox("Seuil du réseau")
    sv = QVBoxLayout(gs)
    thr_table = QTableWidget(len(FH4_SEUILS), 5)
    thr_table.setHorizontalHeaderLabels(["Seuil", "% bassin", "km²", "Sous-bassins", "Réseau (km)"])
    thr_table.setSelectionBehavior(QAbstractItemView.SelectRows)
    thr_table.setSelectionMode(QAbstractItemView.SingleSelection)
    thr_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    thr_table.verticalHeader().setVisible(False)
    thr_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    thr_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    thr_table.setWordWrap(True)
    thr_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    thr_table.setMinimumWidth(0)
    thr_table.setFixedHeight(32 + 30 * len(FH4_SEUILS) + 24)
    for i, (cle, lib, pct) in enumerate(FH4_SEUILS):
        for j, v in enumerate((lib.split(" (")[0], fh4_fr(pct, 1), "—", "—", "—")):
            it = QTableWidgetItem(v)
            it.setToolTip("{} — {} % de la surface du bassin — HYPOTHÈSE".format(lib, fh4_fr(pct, 1)))
            thr_table.setItem(i, j, it)
    thr_table.selectRow(FH4_DEFAUT)
    sv.addWidget(thr_table)
    thr_note = QLabel("Le choix et sa valeur sont des HYPOTHÈSES à valider. « Aperçu » remplit le tableau.")
    thr_note.setWordWrap(True)
    thr_note.setStyleSheet("color:#546e7a; font-size:11px;")
    sv.addWidget(thr_note)
    b_preview = QPushButton("Aperçu des 3 seuils")
    b_preview.setToolTip("Nombre de sous-bassins et longueur de réseau pour chacun des 3 seuils (aucun fichier écrit).")
    sv.addWidget(b_preview)
    adv_lay.addWidget(gs)
    ge = QGroupBox("Exutoire")
    ev = QVBoxLayout(ge)
    e_lab = QLabel("Exutoire : sortie à plus forte aire drainée (HYPOTHÈSE, non déplacé). Distance au point « Exutoire » de l'étage 2 affichée après le calcul.")
    e_lab.setWordWrap(True)
    ev.addWidget(e_lab)
    b_out2 = QPushButton("Point de l'étage 2…")
    b_out2.setToolTip("Point « Exutoire » de l'étage 2 (lecture seule) : détecté dans la couche « Exutoire » du projet QGIS, sinon à indiquer (fichier de points).")
    b_calage = QPushButton("Caler sur ce point…")
    b_calage.setToolTip("Propose de caler l'exutoire sur le point de l'étage 2 (confirmation demandée ; jamais appliqué sans clic).")
    b_calage.setEnabled(False)
    ev.addWidget(b_out2)
    ev.addWidget(b_calage)
    he = QHBoxLayout()
    b_extra = QPushButton("Exutoires en plus…")
    b_extra.setToolTip("Exutoires supplémentaires facultatifs (stations, confluences) : couche de points .gpkg / .shp.")
    b_extra_clear = QPushButton("Effacer")
    he.addWidget(b_extra, 1)
    he.addWidget(b_extra_clear)
    ev.addLayout(he)
    extra_lab = QLabel("Aucun exutoire supplémentaire.")
    extra_lab.setWordWrap(True)
    ev.addWidget(extra_lab)
    adv_lay.addWidget(ge)
    gl = QGroupBox("Lac Itasy (à choisir)")
    glv = QVBoxLayout(gl)
    lake_radios = []
    for txt, tip, on in (("(i) Laisser comme est (V1)", "Le comblement des cuvettes donne un gradient au plan d'eau (peut créer des lignes droites).", True),
                         ("(ii) Forcer : 1 sortie", "Chaque plan d'eau s'écoule par un seul pixel de sortie (HYPOTHÈSE à valider).", False),
                         ("(iii) Masse d'eau SWAT+", "Non disponible : dépend de l'inspection de SWAT+ / QSWAT+ (onglet HRU / SWAT+).", False)):
        rb_ = QRadioButton(txt)
        rb_.setChecked(on)
        rb_.setToolTip(tip)
        glv.addWidget(rb_)
        lake_radios.append(rb_)
    lake_radios[2].setEnabled(False)
    lake_lab = QLabel("Aucune option n'est appliquée en silence : le choix est écrit dans le journal et le rapport ; la carte montre le lac et les limites de sous-bassins qui le traversent.")
    lake_lab.setWordWrap(True)
    lake_lab.setStyleSheet("color:#546e7a; font-size:11px;")
    glv.addWidget(lake_lab)
    adv_lay.addWidget(gl)
    gt_ = QGroupBox("Outils (TauDEM)")
    gtv = QVBoxLayout(gt_)
    taudem_lab = QLabel("TauDEM : pas encore recherché (« Inspecter les outils »).")
    taudem_lab.setWordWrap(True)
    gtv.addWidget(taudem_lab)
    b_taudem = QPushButton("TauDEM : Parcourir…")
    b_taudem.setToolTip("Indique à la main le dossier qui contient pitremove, d8flowdir, aread8 (et mpiexec).")
    gtv.addWidget(b_taudem)
    adv_lay.addWidget(gt_)
    gp_ = QGroupBox("Toponymes et rivières nommées")
    gpv = QVBoxLayout(gp_)
    topo_pts, topo_lines = QComboBox(), QComboBox()
    for cb_ in (topo_pts, topo_lines):
        cb_.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        cb_.setMinimumContentsLength(12)
        cb_.addItem("Aucun", None)
    gpv.addWidget(QLabel("Toponymes (points) :"))
    gpv.addWidget(topo_pts)
    gpv.addWidget(QLabel("Rivières nommées (lignes) :"))
    gpv.addWidget(topo_lines)
    topo_lab = QLabel("Facultatif : les couches du projet QGIS sont proposées ; sinon indique un dossier. Rien n'est téléchargé sans clic.")
    topo_lab.setWordWrap(True)
    topo_lab.setStyleSheet("color:#546e7a; font-size:11px;")
    gpv.addWidget(topo_lab)
    hto = QHBoxLayout()
    b_topo = QPushButton("Chercher…")
    b_osm = QPushButton("OSM…")
    b_osm.setToolTip("Source en ligne possible (OpenStreetMap) : estimation d'abord (requête de comptage), téléchargement seulement sur confirmation.")
    hto.addWidget(b_topo, 1)
    hto.addWidget(b_osm, 1)
    gpv.addLayout(hto)
    adv_lay.addWidget(gp_)
    adv = QCheckBox("Réglages avancés")
    petit_spin = QDoubleSpinBox()
    petit_spin.setRange(0.0, 100000.0)
    petit_spin.setValue(FH4_PETIT_SB_HA)
    petit_spin.setSuffix(" ha")
    pl_ = QLabel("Très petit sous-bassin si < (HYPOTHÈSE) :")
    pl_.setWordWrap(True)
    adv_lay.addWidget(pl_)
    adv_lay.addWidget(petit_spin)
    adv_in.setVisible(False)
    adv.toggled.connect(adv_in.setVisible)
    status = QLabel("")
    status.setWordWrap(True)
    status.setMinimumWidth(0)
    lv.addWidget(status)
    hb = QHBoxLayout()
    b_calc = QPushButton("Calculer")
    b_cancel = QPushButton("Annuler")
    b_cancel.setVisible(False)
    b_loadres = QPushButton("Derniers résultats")
    b_loadres.setVisible(False)
    hb.addWidget(b_cancel, 1)
    lv.addLayout(hb)
    adv_lay.addWidget(b_calc)
    lv.addWidget(b_loadres)
    prog = QProgressBar()
    prog.setRange(0, 100)
    prog.setVisible(False)
    lv.addWidget(prog)
    maps_box = QWidget()
    mg = QGridLayout(maps_box)
    mg.setContentsMargins(0, 0, 0, 0)
    b_map_sb = QPushButton("Carte des sous-bassins")
    b_map_rv = QPushButton("Carte du réseau")
    paper_combo = QComboBox()
    for p_ in ("A4", "A3", "A0"):
        paper_combo.addItem(p_, p_)
    fmt_combo = QComboBox()
    for p_ in ("PNG", "PDF", "JPEG"):
        fmt_combo.addItem(p_, p_)
    sc_combo = QComboBox()
    sc_combo.addItem("P Itasy (principal)", "P_Itasy")
    sc_combo.addItem("P = 1 (complément)", "P_ref")
    b_voir_tab = QPushButton("Voir le tableau")
    mg.addWidget(b_map_sb, 0, 0, 1, 2)
    mg.addWidget(b_map_rv, 1, 0, 1, 2)
    mg.addWidget(QLabel("Papier :"), 2, 0)
    mg.addWidget(paper_combo, 2, 1)
    mg.addWidget(QLabel("Format :"), 3, 0)
    mg.addWidget(fmt_combo, 3, 1)
    mg.addWidget(QLabel("Scénario :"), 4, 0)
    mg.addWidget(sc_combo, 4, 1)
    mg.addWidget(b_voir_tab, 5, 0, 1, 2)
    lv.addWidget(maps_box)
    maps_box.setVisible(False)
    lv.addWidget(adv)
    lv.addWidget(adv_in)
    lv.addStretch(1)
    mapview = (map_factory or fh4_map_view_factory)(outer)
    map_box = QWidget()
    mv = QVBoxLayout(map_box)
    mv.setContentsMargins(2, 2, 2, 2)
    m_bar = QWidget()
    hm = QHBoxLayout(m_bar)
    hm.setContentsMargins(0, 0, 0, 0)
    b_full = QPushButton("Tout le bassin")
    hm.addWidget(b_full)
    hm.addStretch(1)
    mv.addWidget(m_bar)
    m_bar.setVisible(False)
    m_placeholder = QLabel("Lance « Calculer » : les sous-bassins apparaîtront sur la carte après le calcul.")
    m_placeholder.setAlignment(Qt.AlignCenter)
    m_placeholder.setStyleSheet("color:#546e7a; font-size:14px;")
    mv.addWidget(m_placeholder)
    mapview.widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    mapview.widget.setMinimumHeight(300)
    mv.addWidget(mapview.widget, 1)
    m_rappel = QLabel(FH4_PHRASE_A + "   Clic = fenêtre du sous-bassin (id, surface, aval, A moyen) ; survol = info-bulle.")
    m_rappel.setStyleSheet("font-weight:bold; color:#8a1c1c;")
    m_rappel.setWordWrap(True)
    mv.addWidget(m_rappel)
    calc_split.addWidget(sa_left)
    calc_split.addWidget(map_box)
    calc_split.setStretchFactor(0, 1)
    calc_split.setStretchFactor(1, 2)
    calc_split.setSizes([400, 900])
    tabs.addTab(t_calc, "⚙ Sous-bassins")

    # ---------------------------------------------------------------- 📋 Résultats : le grand tableau
    t_tab = QWidget()
    tv = QVBoxLayout(t_tab)
    tv.setContentsMargins(2, 2, 2, 2)
    ht = QHBoxLayout()
    t_search = QLineEdit()
    t_search.setPlaceholderText("Rechercher un sous-bassin…")
    b_csv = QPushButton("Exporter CSV…")
    ht.addWidget(t_search, 1)
    ht.addWidget(b_csv)
    tv.addLayout(ht)
    table = QTableWidget(0, 0)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSortingEnabled(True)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    tv.addWidget(table, 1)
    total_tab = QTableWidget(1, 0)
    total_tab.horizontalHeader().setVisible(False)
    total_tab.verticalHeader().setVisible(False)
    total_tab.setMaximumHeight(34)
    total_tab.setMinimumHeight(30)
    total_tab.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    total_tab.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    total_tab.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tv.addWidget(total_tab)
    t_rappel = QLabel(FH4_PHRASE_A + " Le total reprend la somme des sous-bassins ; contrôles dans le rapport et le journal.")
    t_rappel.setWordWrap(True)
    t_rappel.setStyleSheet("font-weight:bold; color:#8a1c1c;")
    tv.addWidget(t_rappel)
    tabs.addTab(t_tab, "📋 Résultats")

    class Item(QTableWidgetItem):
        def __lt__(self, o):
            a, b = self.data(Qt.UserRole), o.data(Qt.UserRole)
            try:
                return float(a) < float(b)
            except Exception:
                return str(self.text()) < str(o.text())

    def fill_table():
        res = st["res"]
        table.setSortingEnabled(False)
        table.clear()
        if res is None:
            table.setRowCount(0)
            table.setColumnCount(0)
            total_tab.setColumnCount(0)
            return
        cols = fh4_table_columns(res)
        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels([c[1] for c in cols])
        table.setRowCount(len(res["rows"]))
        for i, r in enumerate(res["rows"]):
            for j, (k, _t, kind) in enumerate(cols):
                v = fh4_cell_value(r, k)
                it = Item(fh4_cell_text(v, kind))
                it.setData(Qt.UserRole, (r["no"] if k == "id" else (float("nan") if v is None or v == "" else v)) if kind != "txt" or k == "id" else str(v))
                it.setData(Qt.UserRole + 1, r["no"])
                if kind != "txt":
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(i, j, it)
        table.resizeColumnsToContents()
        table.setSortingEnabled(True)
        total_tab.setColumnCount(len(cols))
        t = res["total"]
        for j, (k, _t, kind) in enumerate(cols):
            txt = "TOTAL BASSIN" if k == "id" else (fh4_fr(t["surf_ha"], 1) if k == "surf_ha" else (fh4_fr(t["s"][k[2:]]["mean"], 1) if k.startswith("A_") and k[2:] in t["s"] else
                                                      (fh4_fr(t["s"][k[2:]]["loss"], 0) if k.startswith("L_") and k[2:] in t["s"] else "")))
            it = QTableWidgetItem(txt)
            it.setFont(QFont("Arial", 9, QFont.Bold))
            if kind != "txt":
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            total_tab.setItem(0, j, it)
            total_tab.setColumnWidth(j, table.columnWidth(j))

    table.horizontalHeader().sectionResized.connect(lambda i, o, n: total_tab.setColumnWidth(i, n) if i < total_tab.columnCount() else None)
    table.horizontalScrollBar().valueChanged.connect(total_tab.horizontalScrollBar().setValue)

    def on_search(*_):
        q = t_search.text().strip().lower()
        for i in range(table.rowCount()):
            table.setRowHidden(i, bool(q) and not any(q in (table.item(i, j).text() or "").lower() for j in range(table.columnCount())))
    t_search.textChanged.connect(on_search)

    # ---------------------------------------------------------------- 🧩 HRU / SWAT+ : entrées de SWAT+ (rien n'est lancé)
    t_hru = QWidget()
    hv = QVBoxLayout(t_hru)
    hv.setContentsMargins(4, 4, 4, 4)
    hf = QHBoxLayout()
    pente_edit = QLineEdit(", ".join(fh4_fr(b_, 0, False) for b_ in FH4_PENTE_BORNES))
    pente_edit.setToolTip("Bornes des classes de pente en % (HYPOTHÈSE par défaut, modifiables) : 5, 15, 30 donne 0–5, 5–15, 15–30, > 30 %.")
    hf.addWidget(QLabel("Pente (bornes %) :"))
    hf.addWidget(pente_edit, 1)
    hv.addLayout(hf)
    hs = QHBoxLayout()
    sp = {}
    for key, lab in (("occupation_pct", "Occupation <"), ("sol_pct", "Sol <"), ("pente_pct", "Pente <"), ("min_ha", "HRU min.")):
        w_ = QDoubleSpinBox()
        w_.setRange(0.0, 100000.0)
        w_.setDecimals(1)
        w_.setValue(FH4_HRU_SEUILS[key])
        w_.setSuffix(" ha" if key == "min_ha" else " %")
        w_.setToolTip("Seuil d'élimination des petites HRU — HYPOTHÈSE, à valider")
        hs.addWidget(QLabel(lab))
        hs.addWidget(w_)
        sp[key] = w_
    hs.addStretch(1)
    hv.addLayout(hs)
    hb2 = QHBoxLayout()
    b_hru = QPushButton("Construire les HRU")
    b_hru_map = QPushButton("Carte des HRU")
    b_open_tables = QPushButton("Ouvrir le dossier des tables à valider")
    b_swat_insp = QPushButton("Inspecter SWAT+ / QSWAT+")
    b_swat_prep = QPushButton("Préparer les entrées QSWAT+")
    for w_ in (b_hru, b_hru_map, b_open_tables, b_swat_insp, b_swat_prep):
        hb2.addWidget(w_)
    hb2.addStretch(1)
    hv.addLayout(hb2)
    hru_split = QSplitter(Qt.Vertical)
    hru_table = QTableWidget(0, 9)
    hru_table.setHorizontalHeaderLabels(["HRU", "Sous-bassin", "Occupation du sol", "Sol", "Pente", "Surface (ha)", "% du sous-bassin", "Code SWAT+", "Statut"])
    hru_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    hru_table.setSortingEnabled(False)
    hru_split.addWidget(hru_table)
    hru_txt = QTextEdit()
    hru_txt.setReadOnly(True)
    hru_txt.setPlainText("Les HRU (occupation du sol × sol × pente par sous-bassin) sont les ENTRÉES de SWAT+ : l'étage 4 ne calcule ici ni infiltration, ni recharge, ni débit. Construis d'abord les sous-bassins.\n"
                         "Les codes d'occupation SWAT+ et les paramètres de sol restent « {} » : ils sont à lire dans la base de données SWAT+ de l'utilisateur, jamais écrits de mémoire.".format(FH4_STATUT_A_REMPLIR))
    hru_split.addWidget(hru_txt)
    hru_split.setStretchFactor(0, 3)
    hru_split.setStretchFactor(1, 1)
    hv.addWidget(hru_split, 1)
    tabs.addTab(t_hru, "🧩 HRU / SWAT+")

    # ---------------------------------------------------------------- 💧 Hydrologie : PRÉPARÉ (grisé), aucune valeur
    t_hyd = QWidget()
    yv = QVBoxLayout(t_hyd)
    hyd_table = QTableWidget(len(FH4_HYDRO_LIGNES), 3)
    hyd_table.setHorizontalHeaderLabels(["Carte prévue (par sous-bassin, et par HRU si utile)", "État", "Variable de sortie SWAT+"])
    hyd_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    hyd_table.setSelectionMode(QAbstractItemView.NoSelection)
    hyd_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    hyd_table.setWordWrap(True)
    for i, nm in enumerate(FH4_HYDRO_LIGNES):
        for j, v in enumerate((nm, "EN ATTENTE DE SWAT+", "à lire dans la documentation / les fichiers de sortie de SWAT+ (non écrit de mémoire)")):
            it = QTableWidgetItem(v)
            it.setFlags(Qt.NoItemFlags)
            it.setForeground(QColor("#9e9e9e"))
            hyd_table.setItem(i, j, it)
    hyd_table.setEnabled(False)
    yv.addWidget(hyd_table, 1)
    hyd_note = QLabel(FH4_HYDRO_NOTE)
    hyd_note.setWordWrap(True)
    hyd_note.setStyleSheet("color:#8a1c1c;")
    yv.addWidget(hyd_note)
    tabs.addTab(t_hyd, "💧 Hydrologie")

    # ---------------------------------------------------------------- 🌦 Météo : mécanisme PRÉPARÉ, jamais lancé sans clic
    t_met = QWidget()
    mtv = QVBoxLayout(t_met)
    mf = QFormLayout()
    d0_edit, d1_edit = QLineEdit(), QLineEdit()
    d0_edit.setPlaceholderText("AAAA-MM-JJ")
    d1_edit.setPlaceholderText("AAAA-MM-JJ")
    zone_lab = QLabel("Zone : emprise du bassin de l'étage 2 + marge (calculée au clic sur « Estimer la taille »).")
    zone_lab.setWordWrap(True)
    cache_edit = QLineEdit()
    b_cache = QPushButton("📂")
    b_cache.setMaximumWidth(38)
    hcache = QHBoxLayout()
    hcache.addWidget(cache_edit, 1)
    hcache.addWidget(b_cache)
    ck_chirps = QCheckBox("Pluie CHIRPS quotidienne (fichiers)")
    ck_chirps.setChecked(True)
    ck_power = QCheckBox("Température, humidité, vent, rayonnement : NASA POWER (API quotidienne)")
    ck_power.setChecked(True)
    mf.addRow("Début :", d0_edit)
    mf.addRow("Fin :", d1_edit)
    mf.addRow("Zone :", zone_lab)
    mf.addRow("Cache météo + OSM :", hcache)
    mf.addRow("", ck_chirps)
    mf.addRow("", ck_power)
    mtv.addLayout(mf)
    hmb = QHBoxLayout()
    b_est = QPushButton("Estimer la taille")
    b_dl = QPushButton("Télécharger")
    b_test = QPushButton("Test (1 mois, 1 point)")
    b_mcancel = QPushButton("Annuler")
    b_mcancel.setVisible(False)
    for w_ in (b_est, b_dl, b_test, b_mcancel):
        hmb.addWidget(w_)
    hmb.addStretch(1)
    mtv.addLayout(hmb)
    m_info = QLabel("Aucun téléchargement n'a lieu tant que tu n'as pas cliqué sur « Télécharger » puis confirmé.")
    m_info.setWordWrap(True)
    mtv.addWidget(m_info)
    m_prog = QProgressBar()
    m_prog.setRange(0, 100)
    m_prog.setVisible(False)
    mtv.addWidget(m_prog)
    m_note = QLabel(FH4_METEO_NOTE + " Rien n'est converti au format SWAT+ dans cette livraison (PET, .pcp / .tmp viendront ensuite).")
    m_note.setWordWrap(True)
    m_note.setStyleSheet("color:#8a1c1c;")
    mtv.addWidget(m_note)
    mtv.addStretch(1)
    tabs.addTab(t_met, "🌦 Météo")

    # ---------------------------------------------------------------- 📜 Journal
    t_log = QWidget()
    lgv = QVBoxLayout(t_log)
    hl = QHBoxLayout()
    b_copy, b_savelog, b_clearlog = QPushButton("Copier"), QPushButton("Enregistrer…"), QPushButton("Vider")
    for w_ in (b_copy, b_savelog, b_clearlog):
        hl.addWidget(w_)
    hl.addStretch(1)
    lgv.addLayout(hl)
    lgv.addWidget(logbox, 1)
    tabs.addTab(t_log, "📜 Journal")

    # ---------------------------------------------------------------- logique
    def flash(text, style="color:#1b5e20;", ms=8000):
        status.setText(text)
        status.setStyleSheet(style + " font-weight:bold;")
        QTimer.singleShot(ms, lambda: status.setText("") if status.text() == text else None)

    def busy(on, what=None):
        st["busy"] = what if on else None
        b_calc.setEnabled(not on)
        b_preview.setEnabled(not on)
        b_all.setEnabled(not on and not st.get("chain"))
        b_cancel.setVisible(on)
        prog.setVisible(on)
        if not on:
            prog.setValue(0)

    def start(fn, params, on_done, label, on_fail=None):
        if st["busy"]:
            flash("Un traitement est déjà en cours.", "color:#e65100;")
            return
        network_begin(label)
        w = Fh4Task(fn, params)
        st["worker"] = w
        w.log_signal.connect(log)
        w.progress_signal.connect(prog.setValue)
        w.info_signal.connect(network_update)

        def done(r):
            busy(False)
            network_end(True)
            try:
                on_done(r)
            except Fh4Error as ex:
                fail("❌ " + str(ex))
            except Exception:
                fail("❌ " + traceback.format_exc())

        def fail(m):
            busy(False)
            network_end(False, m)
            flash(m.splitlines()[0][:220], "color:#c62828;", 15000)
            log(m)
            if on_fail:
                on_fail(m)
        w.done_signal.connect(done)
        w.error_signal.connect(fail)
        busy(True, label)
        flash(label + "…", "color:#1565c0;", 600000)
        w.start()

    def dirs_now():
        d = basin_combo.currentData()
        return fh4_dirs(root_edit.text().strip(), d["basin"]) if d else None

    def download_cache_root():
        d = dirs_now()
        return cache_edit.text().strip() or (fh4_cache_root(d["e4"]) if d else "")

    def refresh_basins(*_):
        basin_combo.clear()
        for b in fh4_list_basins(root_edit.text().strip()):
            basin_combo.addItem(b["basin"], b)
        if basin_combo.count() == 0:
            log("ℹ Aucun bassin trouvé sous {} (Etages/01_envi/<bassin>/manifeste.json).".format(root_edit.text().strip() or "—"))

    def check_upstream(*_):
        d = basin_combo.currentData()
        b_loadres.setVisible(False)
        st["avail"], st["stale"] = None, False
        if not d:
            return
        dirs = dirs_now()
        try:
            info1, info2 = fh4_find_stage1(dirs["e1"]), fh4_find_stage2(dirs["e2"], None)
        except Fh4Error as ex:
            log("ℹ " + str(ex))
            return
        stt, msg, _df = fh4_check_upstream(dirs["e4"], info1, info2, os.path.join(dirs["e2"], "travail", "MNT_srtm.tif"))
        st["stale"] = stt == "modifie"
        log(("⚠ " if stt == "modifie" else "ℹ ") + msg)
        old = fh4_load_results(dirs["e4"])
        st["avail"] = old
        b_loadres.setVisible(bool(old))
        if old:
            b_loadres.setText("Charger les derniers\nrésultats ({})".format(str(old.get("date") or "?")[:10]))
            log("ℹ Résultats d'un calcul précédent disponibles ({}) : « Charger les derniers résultats » les affiche (jamais automatique){}.".format(old.get("date") or "?", " — PÉRIMÉ" if st["stale"] else ""))
        if stt == "modifie":
            flash("Étage 2 modifié depuis le dernier calcul de l'étage 4 — résultat périmé.", "color:#e65100;", 15000)
        # période météo par défaut = celle de l'étage 2
        per = fh4_meteo_period_default(info2)
        if per and not d0_edit.text():
            d0_edit.setText(per[0])
            d1_edit.setText(per[1])
        if not cache_edit.text():
            cache_edit.setText(fh4_cache_root(dirs["e4"]))

    def show_outlet_info(res):
        o = res.get("outlet")
        if not o:
            e_lab.setText("Exutoire : sortie à plus forte aire drainée (HYPOTHÈSE). Point de l'étage 2 non fourni.")
            return
        s2 = st.get("outlet2")
        txt = "Exutoire trouvé : X = {}, Y = {} ({} km² drainés).".format(fh4_fr(o["xy"][0], 0), fh4_fr(o["xy"][1], 0), fh4_fr(o["acc_km2"], 1))
        if s2:
            d_ = math.hypot(o["xy"][0] - s2[0], o["xy"][1] - s2[1])
            txt += " Point « Exutoire » de l'étage 2 ({}) : X = {}, Y = {} → distance {} m.".format(s2[2] if len(s2) > 2 else "indiqué", fh4_fr(s2[0], 0), fh4_fr(s2[1], 0), fh4_fr(d_, 0))
            b_calage.setEnabled(d_ > FH4_OUTLET_TOL_M)
            if d_ > FH4_OUTLET_TOL_M:
                txt += " ÉCART NOTABLE (> {} m, HYPOTHÈSE) : « Caler sur ce point… » le propose (confirmation demandée).".format(int(FH4_OUTLET_TOL_M))
        else:
            txt += " Point de l'étage 2 non trouvé dans le projet QGIS (couche « Exutoire ») : indique-le avec « Point de l'étage 2… »."
        e_lab.setText(txt)
        log("📍 " + txt)

    def show_results(res):
        st["res"] = res
        show_outlet_info(res)
        m_placeholder.setVisible(False)
        m_bar.setVisible(True)
        maps_box.setVisible(True)
        b_loadres.setVisible(False)
        if res.get("files", {}).get("gpkg") and os.path.isfile(res["files"]["gpkg"]):
            try:
                mapview.show(res)
            except Exception as ex:
                log("⚠ Carte interactive indisponible : {}".format(ex))
        fill_table()

    def on_inspect(*_):
        r = do_inspect_tools()
        d = dirs_now()
        if d:
            try:
                os.makedirs(d["e4"], exist_ok=True)
                fh4_write_text(os.path.join(d["e4"], "inspection_outils.json"), json.dumps(dict({k: v for k, v in r.items() if k != "qswat" or True}, date=fh4_iso(time.time())), ensure_ascii=False, indent=1))
            except Exception:
                pass
        flash("Inspection : voie retenue {}.".format(r["voie"]), "color:#1b5e20;")
        tabs.setCurrentWidget(t_log)

    def do_inspect_tools():
        keys = []
        try:
            S3 = QgsSettings()
            for k_ in S3.allKeys():
                if "swat" in k_.lower() or "taudem" in k_.lower():
                    keys.append(str(S3.value(k_, "") or ""))
        except Exception:
            pass
        r = fh4_inspect_tools(manual=[st["taudem_dir"]] if st["taudem_dir"] else (), settings_keys=keys)
        st["tools"] = r
        for ln in r["lignes"]:
            log(ln)
        taudem_lab.setText("TauDEM : {}".format(("trouvé — voie A par défaut" + (" (MPI : oui)" if r["mpiexec"] else " (sans MPI)")) if r["ok_a"] else "INTROUVABLE (voie C en secours, moins éprouvée). Indique son dossier avec « Parcourir… »."))
        return r

    def tool_exes():
        if st["tools"] is None:
            do_inspect_tools()
        return dict(st["tools"]["exes"])

    def pick_taudem(*_):
        p_ = QFileDialog.getExistingDirectory(outer, "Dossier de TauDEM (contient pitremove, d8flowdir, aread8…)", st["taudem_dir"] or root_edit.text() or "")
        if p_:
            st["taudem_dir"] = p_
            try:
                QgsSettings().setValue("FALKHYDRO/etage4/taudem_dir", p_)
            except Exception:
                pass
            log("📂 Dossier TauDEM indiqué à la main : {}".format(p_))
            do_inspect_tools()

    def params(extra=None):
        d = basin_combo.currentData()
        if not d:
            raise Fh4Error("Choisis un bassin (dossier de sortie → Etages/01_envi/<bassin>).")
        v = voie_combo.currentData()
        p = {"root": root_edit.text().strip(), "basin": d["basin"], "voie": v, "exes": tool_exes() if v in ("auto", "A") else {}, "petit_ha": petit_spin.value(),
             "thr_idx": max(0, thr_table.currentRow()) if thr_table.currentRow() >= 0 else FH4_DEFAUT, "extra_file": st.get("extra_file"),
             "lake_mode": "flat" if lake_radios[1].isChecked() else "asis", "outlet_stage2": tuple(st["outlet2"][:2]) if st.get("outlet2") else None,
             "outlet_xy": tuple(st["calage"]) if st.get("calage") else None, "rivers_named": (topo_lines.currentData() or {}).get("spec"), "compare": True}
        if st.get("preview"):
            p["thr_apercu"] = [dict(o) for o in st["preview"]]
        p.update(extra or {})
        return p

    def ctx_key(p):
        dirs = fh4_dirs(p["root"], p["basin"])
        dp = os.path.join(dirs["e2"], "travail", "MNT_srtm.tif")
        return (p["root"], p["basin"], p["voie"], p.get("lake_mode"), os.path.getmtime(dp) if os.path.isfile(dp) else None)

    def on_preview(*_):
        try:
            p = params()
            p["key"] = ctx_key(p)
        except Fh4Error as ex:
            flash(str(ex), "color:#c62828;", 12000)
            return

        def done(r):
            st["ctx"], st["key"], st["preview"] = r["ctx"], r["key"], r["preview"]
            log("📊 Aperçu des seuils (HYPOTHÈSES) : " + " ; ".join("{} % → {} sous-bassins, {} km".format(fh4_fr(o["pct"], 1), o["n_sb"], fh4_fr(o["reseau_km"], 0)) for o in r["preview"]))
            for i_, o in enumerate(r["preview"]):
                for j_, v_ in ((2, fh4_fr(o["km2"], 1)), (3, str(o["n_sb"])), (4, fh4_fr(o["reseau_km"], 0))):
                    thr_table.item(i_, j_).setText(v_)
            thr_table.resizeRowsToContents()
            flash("Aperçu prêt : choisis un seuil puis Calculer.", "color:#1b5e20;")
        start(fh4_preview_task, p, done, "Aperçu des seuils")

    def on_calc(*_):
        try:
            p = params()
        except Fh4Error as ex:
            flash(str(ex), "color:#c62828;", 12000)
            return
        if st["stale"] and (st["res"] is not None or st.get("avail")):
            if fh4_dialog_changement(outer, "Étage 2 modifié depuis le dernier calcul de l'étage 4 : le résultat actuel est PÉRIMÉ.\nRecalculer écrasera les sous-bassins existants.") != "recalculer":
                log("ℹ Recalcul non lancé : résultat conservé tel quel (périmé).")
                return
        if st.get("ctx") is not None and st.get("key") == ctx_key(p):
            p["ctx"] = st["ctx"]
            log("ℹ Hydrologie de l'aperçu réutilisée (aucune donnée n'a changé).")

        def done(res):
            st["ctx"], st["key"] = None, None
            st["stale"] = False
            show_results(res)
            flash("✅ {} sous-bassins calculés.".format(len(res["rows"])), "color:#1b5e20;")
        start(fh4_run_task, p, done, "Calcul des sous-bassins")

    def load_last(*_):
        old = st.get("avail")
        if not old:
            return
        log("📂 Derniers résultats chargés à la demande ({}).".format(old.get("date") or "?"))
        show_results(old)

    def on_cancel(*_):
        w = st.get("worker")
        if st.get("chain"):
            st["chain"]["stop"] = True
        if w is not None:
            w.cancel.set()
            log("⏹ Arrêt demandé…")

    def pick_extra(*_):
        p_, _f = QFileDialog.getOpenFileName(outer, "Exutoires supplémentaires (points)", root_edit.text() or "", "Points (*.gpkg *.shp)")
        if p_:
            st["extra_file"] = p_
            extra_lab.setText("Fichier : {}".format(os.path.basename(p_)))
            log("📍 Exutoires supplémentaires : {} (accrochés au réseau à 300 m maximum, au calcul).".format(p_))

    def clear_extra(*_):
        st["extra_file"] = None
        extra_lab.setText("Aucun exutoire supplémentaire.")

    def proj_info():
        S2 = QgsSettings()
        return {k: str(S2.value("FALKHYDRO/info/" + k, "") or "").strip() for k in ("titre", "auteur", "organisme", "bassin", "date", "version", "methode")}

    def export_map(kind):
        res = st["res"]
        if res is None:
            flash("Pas de résultat : lance « Calculer ».", "color:#c62828;")
            return
        try:
            res["dirs"] = res.get("dirs") or dirs_now()
            res["dirs"]["e4"] = dirs_now()["e4"]
            res["basin"] = res.get("basin") or basin_combo.currentData()["basin"]
            _FH4_SHEET["n"] = 0
            lay, base = fh4_make_layout(kind, res, proj_info(), topo=topo_now())
            items = [(lay, base)] + (fh4_make_annex_layouts(res, proj_info()) if kind == "sb" else [])
            out = []
            for lo, bs in items:
                out += fh4_export_layout(lo, os.path.join(res["dirs"]["e4"], "Cartes"), bs, paper_combo.currentData(), log)
            if fmt_combo.currentData() == "JPEG":
                out += [x for x in (fh4_to_jpeg(o) for o in out if o.lower().endswith(".png")) if x]
            log("🗺 Carte « {} » : {}".format(kind, ", ".join(os.path.basename(o) for o in out)))
            flash("Carte écrite dans Cartes/.", "color:#1b5e20;")
        except Exception as ex:
            flash("Carte impossible : {}".format(ex), "color:#c62828;", 15000)
            log("❌ Carte : " + traceback.format_exc())

    def on_sb_click(no):
        res = st["res"]
        row = next((r for r in res["rows"] if r["no"] == no), None) if res else None
        if row is None:
            return
        mapview.highlight(None)
        if not fh4_dialog_sb(outer, fh4_sb_lines(row, res)):
            return
        p = {"root": root_edit.text().strip(), "basin": basin_combo.currentData()["basin"], "no": no}

        def done(iso):
            st["iso"] = iso
            mapview.highlight(no)
            try:
                for k in iso["scen"]:
                    lay, base = fh4_make_layout("isole", dict(res, dirs=dirs_now(), basin=p["basin"]), proj_info(), iso=iso, scen=k, topo=topo_now())
                    fh4_export_layout(lay, os.path.join(iso["udir"], "Cartes"), base, paper_combo.currentData(), log)
                flash("{} isolé : rasters et cartes dans Isoles/.".format(row["id"]), "color:#1b5e20;")
            except Exception as ex:
                log("❌ Carte de l'isolé : " + traceback.format_exc())
                flash("Isolé calculé ; carte impossible : {}".format(ex), "color:#e65100;", 15000)
        start(fh4_isolate_task, p, done, "Isolement de " + row["id"])

    mapview.on_click = on_sb_click

    def on_hover(no, widget):
        res = st["res"]
        if res is None or no is None:
            return
        row = next((r for r in res["rows"] if r["no"] == no), None)
        if row is not None:
            try:
                from qgis.PyQt.QtWidgets import QToolTip
                from qgis.PyQt.QtGui import QCursor
                QToolTip.showText(QCursor.pos(), fh4_sb_tooltip(row, sc_combo.currentData()), widget)
            except Exception:
                pass
    mapview.on_hover = on_hover

    # --- météo (aucune requête sans clic ; la confirmation affiche la taille estimée)
    def meteo_params(extra=None):
        d = basin_combo.currentData()
        if not d:
            raise Fh4Error("Choisis un bassin.")
        if not d0_edit.text().strip() or not d1_edit.text().strip():
            raise Fh4Error("Indique la période (AAAA-MM-JJ) : l'étage 2 n'en donne pas pour ce bassin.")
        fh4_days(d0_edit.text(), d1_edit.text())
        if not (ck_chirps.isChecked() or ck_power.isChecked()):
            raise Fh4Error("Coche au moins une source (CHIRPS ou NASA POWER).")
        p = {"root": root_edit.text().strip(), "basin": d["basin"], "d0": d0_edit.text().strip(), "d1": d1_edit.text().strip(), "cache": download_cache_root(),
             "chirps": ck_chirps.isChecked(), "power": ck_power.isChecked(), "http": st.get("http"), "bbox": st.get("bbox")}
        p.update(extra or {})
        return p

    def met_busy(on):
        for b in (b_est, b_dl, b_test):
            b.setEnabled(not on)
        b_mcancel.setVisible(on)
        m_prog.setVisible(on)
        if not on:
            m_prog.setValue(0)

    def met_start(fn, p, on_done, label, on_fail=None):
        if st["busy"]:
            return
        network_begin(label)
        w = Fh4Task(fn, p)
        st["worker"] = w
        w.log_signal.connect(log)

        def info(d):
            network_update(d)
            tot = d.get("total_connu")
            m_prog.setValue(int(d.get("pct") or 0))
            m_info.setText("{} — {} / {} — {} /s — fichiers {}/{} — reste {} — total {}".format(
                d.get("fichier") or "…", fh4_fmt_bytes(d.get("recu")), fh4_fmt_bytes(d.get("total")) if d.get("total") else "taille totale inconnue",
                fh4_fmt_bytes(d.get("vitesse_globale") or d.get("vitesse")), d.get("fichiers_faits"), d.get("fichiers_total"), fh4_fmt_duration(d.get("restant_s")),
                fh4_fmt_bytes(tot) if tot else "taille totale inconnue"))
        w.info_signal.connect(info)

        def done(r):
            met_busy(False)
            st["busy"] = None
            network_end(True)
            try:
                on_done(r)
            except Exception:
                log("❌ " + traceback.format_exc())
                if on_fail:
                    on_fail("❌ erreur dans le traitement du résultat")

        def fail(m):
            met_busy(False)
            st["busy"] = None
            network_end(False, m)
            m_info.setText(m.splitlines()[0][:300])
            log(m)
            if on_fail:
                on_fail(m)
        w.done_signal.connect(done)
        w.error_signal.connect(fail)
        st["busy"] = label
        met_busy(True)
        m_info.setText(label + "…")
        w.start()

    def on_estimate(*_):
        try:
            p = meteo_params()
        except Fh4Error as ex:
            m_info.setText(str(ex))
            return

        def done(est):
            st["est"], st["bbox"] = est, est.get("bbox")
            zone_lab.setText("Ouest {:.3f}, Sud {:.3f}, Est {:.3f}, Nord {:.3f} (WGS 84) + marge".format(*est["bbox"]))
            parts = []
            if est.get("chirps"):
                parts.append("CHIRPS : {} fichiers, {}".format(est["chirps"]["fichiers"], fh4_fmt_bytes(est["chirps"]["octets"])))
            if est.get("power"):
                parts.append("NASA POWER : {} requêtes, ~{} (estimé)".format(est["power"]["requetes"], fh4_fmt_bytes(est["power"]["octets"])))
            m_info.setText("Estimation (aucun téléchargement effectué) — " + " ; ".join(parts) + " ; total : " + fh4_fmt_bytes(est["total"]))
            log("📏 " + m_info.text())
        met_start(fh4_meteo_estimate_task, p, done, "Estimation de la taille")

    def on_download(*_):
        try:
            p = meteo_params({"estimate": st.get("est")})
        except Fh4Error as ex:
            m_info.setText(str(ex))
            return
        est = st.get("est")
        taille = fh4_fmt_bytes(est["total"]) if est else "non estimée (clique d'abord sur « Estimer la taille »)"
        box = QMessageBox(outer)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Télécharger la météo ?")
        box.setText("Période {} → {}\nTaille estimée : {}\nDossier : {}\nLe téléchargement est reprenable (rien n'est retéléchargé si déjà en cache).".format(p["d0"], p["d1"], taille, p["cache"]))
        b_yes = box.addButton("Télécharger", QMessageBox.AcceptRole)
        b_no = box.addButton("Annuler", QMessageBox.RejectRole)
        box.setDefaultButton(b_no)
        box.setEscapeButton(b_no)
        box.exec_()
        if box.clickedButton() is not b_yes:
            log("ℹ Téléchargement non lancé (annulé).")
            return
        met_start(fh4_meteo_download_task, p, lambda r: m_info.setText("Terminé : {} fichier(s), {}.".format(r["fichiers"], fh4_fmt_bytes(r["octets"]))), "Téléchargement")

    def on_test(*_):
        try:
            p = meteo_params()
        except Fh4Error as ex:
            m_info.setText(str(ex))
            return
        met_start(fh4_meteo_test_task, p, lambda r: m_info.setText("Test terminé : {} fichier(s), {}.".format(r["fichiers"], fh4_fmt_bytes(r["octets"]))), "Test météo")

    def pick_out2(*_):
        p_, _f = QFileDialog.getOpenFileName(outer, "Point « Exutoire » de l'étage 2 (couche de points)", root_edit.text() or "", "Points (*.gpkg *.shp)")
        if p_:
            try:
                pts = fh4_read_points(p_)
            except Fh4Error as ex:
                flash(str(ex), "color:#c62828;", 12000)
                return
            if pts:
                st["outlet2"] = (pts[0][0], pts[0][1], os.path.basename(p_))
                log("📍 Point de l'étage 2 lu dans {} : X = {}, Y = {}.".format(p_, fh4_fr(pts[0][0], 0), fh4_fr(pts[0][1], 0)))
                if st["res"]:
                    show_outlet_info(st["res"])

    def do_calage(*_):
        s2 = st.get("outlet2")
        if not s2:
            return
        box = QMessageBox(outer)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Caler l'exutoire ?")
        box.setText("Caler l'exutoire sur le point de l'étage 2 (X = {}, Y = {}) ?\nLe bassin sera limité à son amont au prochain « Calculer » ; rien n'est appliqué avant.".format(fh4_fr(s2[0], 0), fh4_fr(s2[1], 0)))
        b_yes = box.addButton("Caler au prochain calcul", QMessageBox.AcceptRole)
        b_no = box.addButton("Annuler", QMessageBox.RejectRole)
        box.setDefaultButton(b_no)
        box.setEscapeButton(b_no)
        box.exec_()
        if box.clickedButton() is b_yes:
            st["calage"] = (s2[0], s2[1])
            log("📍 Calage de l'exutoire CONFIRMÉ par l'utilisateur : appliqué au prochain « Calculer ».")
            flash("Exutoire calé au prochain calcul : clique « Calculer ».", "color:#1565c0;")
        else:
            log("ℹ Calage de l'exutoire refusé : exutoire inchangé.")

    def fill_topo(found):
        for cb_, genre in ((topo_pts, "point"), (topo_lines, "ligne")):
            cur = cb_.currentData()
            cb_.clear()
            cb_.addItem("Aucun", None)
            for f_ in found:
                if f_["genre"] == genre:
                    cb_.addItem("{} ({} entités, champ {})".format(f_["nom"], f_["n"], f_["champ"]), f_)
        st["topo_found"] = found

    def scan_topo(*_):
        folder = QFileDialog.getExistingDirectory(outer, "Dossier de couches (toponymes, rivières) — Annuler pour ne chercher que dans le projet QGIS", root_edit.text() or "")
        found = fh4_project_layers_for_topo() + (fh4_scan_layers([folder]) if folder else [])
        fill_topo(found)
        if found:
            log("🏷 {} couche(s) de toponymes / rivières trouvée(s) (qualité des noms non présumée).".format(len(found)))
            topo_lab.setText("{} couche(s) trouvée(s) : choisis-les dans les listes (qualité des noms non présumée).".format(len(found)))
        else:
            msg = ("Aucune couche de toponymes ni de rivières nommées trouvée dans le projet QGIS ni dans le dossier choisi. Source en ligne possible : OpenStreetMap "
                   "(bouton « OSM… » : estimation d'abord, aucun téléchargement sans ton clic ; qualité des noms non présumée).")
            topo_lab.setText(msg)
            log("ℹ " + msg)

    def topo_now():
        t = {}
        for key, cb_ in (("points", topo_pts), ("lines", topo_lines)):
            if cb_.currentData():
                t[key] = cb_.currentData()
        return t

    def on_osm(*_):
        try:
            d = basin_combo.currentData()
            if not d:
                raise Fh4Error("Choisis un bassin.")
            p = {"root": root_edit.text().strip(), "basin": d["basin"], "http": st.get("http"), "bbox": st.get("bbox"),
                 "dest": os.path.join(download_cache_root(), "osm")}
        except Fh4Error as ex:
            flash(str(ex), "color:#c62828;")
            return

        def done(est):
            st["bbox"] = est.get("bbox")
            if not est.get("connu"):
                topo_lab.setText("Estimation OSM impossible ({}). Rien n'a été téléchargé.".format(est.get("erreur", "?")))
                log("ℹ " + topo_lab.text())
                return
            if not est.get("elements"):
                topo_lab.setText("Aucun lieu ni cours d'eau nommé trouvé dans cette emprise OSM.")
                log("ℹ " + topo_lab.text())
                return
            box = QMessageBox(outer)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Télécharger les toponymes OSM ?")
            if est.get("cached"):
                box.setText("Cache OSM déjà disponible pour ce bassin : {} éléments. Aucun appel réseau ne sera effectué.".format(est["elements"]))
            else:
                box.setText("OpenStreetMap (Overpass) : {} éléments (lieux et cours d'eau nommés), taille estimée {}. Les noms peuvent être incomplets : à vérifier.".format(
                    est["elements"], fh4_fmt_bytes(est["octets"])))
            b_yes = box.addButton("Charger le cache" if est.get("cached") else "Télécharger", QMessageBox.AcceptRole)
            b_no = box.addButton("Annuler", QMessageBox.RejectRole)
            box.setDefaultButton(b_no)
            box.setEscapeButton(b_no)
            box.exec_()
            if box.clickedButton() is not b_yes:
                log("ℹ Téléchargement OSM non lancé (annulé).")
                return
            start(fh4_osm_download_task, {"bbox": est["bbox"], "dest": os.path.join(download_cache_root(), "osm"), "http": st.get("http")}, lambda r: (fill_topo(st["topo_found"] + [
                {"spec": "{}|layername=lieux".format(r["gpkg"]), "nom": "OSM — lieux", "genre": "point", "n": r["lieux"], "champ": "name"},
                {"spec": "{}|layername=cours_d_eau".format(r["gpkg"]), "nom": "OSM — cours d'eau", "genre": "ligne", "n": r["cours"], "champ": "name"}]), flash("OSM téléchargé.", "color:#1b5e20;")), "Téléchargement OSM")
        start(fh4_osm_estimate_task, p, done, "Estimation OSM")

    def hru_bornes():
        try:
            b_ = sorted(float(x.replace(",", ".")) for x in pente_edit.text().replace(";", ",").split(",") if x.strip())
        except ValueError:
            raise Fh4Error("Bornes de pente invalides : écris des nombres séparés par des virgules (ex. 5, 15, 30).")
        if not b_:
            raise Fh4Error("Indique au moins une borne de pente.")
        return b_

    def on_hru(*_):
        try:
            p = {"root": root_edit.text().strip(), "basin": basin_combo.currentData()["basin"], "pente_bornes": hru_bornes(), "seuils": {k: w_.value() for k, w_ in sp.items()}}
        except (Fh4Error, TypeError) as ex:
            flash(str(ex) if isinstance(ex, Fh4Error) else "Choisis un bassin.", "color:#c62828;", 12000)
            return

        def done(r):
            st["hru"] = r
            hru_table.setRowCount(len(r["rows"]))
            for i, x in enumerate(r["rows"]):
                for j, v in enumerate((x["hru"], x["sb"], x["lu_nom"], x["sol_nom"], x["pente_nom"], fh4_fr(x["surf_ha"], 1), fh4_fr(x["pct_sb"], 1), "", FH4_STATUT_A_REMPLIR)):
                    it = QTableWidgetItem(str(v))
                    hru_table.setItem(i, j, it)
            hru_table.resizeColumnsToContents()
            hru_txt.setPlainText("\n".join(["{} {} : {}".format("✔" if ok else "⚠", a_, b_) for a_, b_, ok in r["controles"]] +
                                          ["", "Tables à valider : correspondance_occupation_SWAT.csv (code SWAT+ vide = « {} »), sol_parametres_SWAT.csv ; seuils et bornes = HYPOTHÈSES.".format(FH4_STATUT_A_REMPLIR)]))
            flash("{} HRU construites.".format(len(r["rows"])), "color:#1b5e20;")
        start(fh4_hru_task, p, done, "Construction des HRU")

    def on_hru_map(*_):
        r = st.get("hru")
        res = st["res"]
        if r is None or res is None:
            flash("Construis d'abord les HRU.", "color:#c62828;")
            return
        try:
            _FH4_SHEET["n"] = 0
            res["dirs"] = res.get("dirs") or dirs_now()
            res["basin"] = res.get("basin") or basin_combo.currentData()["basin"]
            lay, base = fh4_make_layout("hru", res, proj_info(), iso=r)
            out = fh4_export_layout(lay, os.path.join(dirs_now()["e4"], "HRU", "Cartes"), base, paper_combo.currentData(), log)
            log("🗺 Carte des HRU : " + ", ".join(os.path.basename(o) for o in out))
            flash("Carte des HRU écrite.", "color:#1b5e20;")
        except Exception as ex:
            flash("Carte des HRU impossible : {}".format(ex), "color:#c62828;", 15000)
            log("❌ " + traceback.format_exc())

    def on_open_tables(*_):
        d = dirs_now()
        if d:
            try:
                from qgis.PyQt.QtGui import QDesktopServices
                from qgis.PyQt.QtCore import QUrl
                QDesktopServices.openUrl(QUrl.fromLocalFile(d["e4"]))
            except Exception:
                pass
            log("📂 Tables à valider dans {}".format(d["e4"]))

    def on_swat_inspect(*_):
        def done(r):
            for ln in r["lignes"]:
                log(ln)
            for m in r["manque"]:
                log("⚠ Manque pour lancer SWAT+ : " + m)
            hru_txt.setPlainText("\n".join(r["lignes"] + ["", "Manque pour lancer SWAT+ :"] + ["  - " + m for m in r["manque"]] + ["", "PET : " + FH4_PET_NOTE] + ["  - {} : {}".format(a_, b_) for a_, b_ in FH4_PET_OPTIONS]))
            flash("Inspection de SWAT+ / QSWAT+ terminée.", "color:#1b5e20;")
        start(fh4_inspect_swat_task, {"roots": [st["taudem_dir"]] if st["taudem_dir"] else []}, done, "Inspection de SWAT+ / QSWAT+")

    def on_swat_prepare(*_):
        try:
            p = {"root": root_edit.text().strip(), "basin": basin_combo.currentData()["basin"]}
        except TypeError:
            flash("Choisis un bassin.", "color:#c62828;")
            return

        def done(r):
            hru_txt.setPlainText("Entrées préparées pour QSWAT+ dans {}\n(SWAT+ n'est PAS lancé).\n\nManque :\n".format(r["dir"]) + "\n".join("  - " + m for m in r["manque"]) + "\n\n" + "\n".join(r["pet"]))
            flash("Entrées QSWAT+ préparées.", "color:#1b5e20;")
        start(fh4_swat_prepare_task, p, done, "Préparation des entrées QSWAT+")

    # ================================================================ 🚀 LANCER TOUT : chaîne automatique, un clic, aucune saisie
    def set_step(key, state, msg=""):
        sym = {"run": "⏳", "ok": "✔", "echec": "⚠", "ignore": "–", "todo": "○"}[state]
        for i in range(steps_list.count()):
            it = steps_list.item(i)
            if it.data(Qt.UserRole) == key:
                lab = dict(FH4_ETAPES)[key]
                it.setText("{} {}{}".format(sym, lab, " — " + msg if msg else ""))
                it.setForeground(QColor({"ok": "#1b5e20", "echec": "#e65100", "run": "#1565c0", "ignore": "#757575", "todo": "#000000"}[state]))

    def steps_text():
        return "\n".join(steps_list.item(i).text() for i in range(steps_list.count()))

    def confirm_all():
        box = QMessageBox(outer)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Lancer tout ?")
        box.setText("Tout est automatique, sans autre question :\n• vérification des étages 1 et 2, sous-bassins et réseau, HRU et tables SWAT+ (valeurs par défaut = HYPOTHÈSES)\n"
                    "• toponymes (OpenStreetMap) et météo (CHIRPS + NASA POWER) téléchargés EN LIGNE : la taille estimée est journalisée avant le téléchargement ; reprenable, annulable\n"
                    "• entrées QSWAT+, cartes et bilan\nSWAT+ n'est pas lancé. Les étages 1 à 3 ne sont jamais modifiés.")
        b_yes = box.addButton("Lancer tout", QMessageBox.AcceptRole)
        b_no = box.addButton("Annuler", QMessageBox.RejectRole)
        box.setDefaultButton(b_yes)
        box.setEscapeButton(b_no)
        box.exec_()
        return box.clickedButton() is b_yes

    def run_all(*_):
        if st["busy"] or st.get("chain"):
            flash("Un traitement est déjà en cours.", "color:#e65100;")
            return
        if not basin_combo.currentData():
            flash("Choisis d'abord le dossier de sortie et le bassin.", "color:#c62828;", 12000)
            return
        if not confirm_all():
            log("ℹ Lancement automatique annulé.")
            return
        ch = {"i": 0, "stat": {}, "dec": [], "stop": False, "res": None}
        st["chain"] = ch
        b_all.setEnabled(False)
        for k_, _l in FH4_ETAPES:
            set_step(k_, "todo")
        log("🚀 Lancement automatique : {} étapes.".format(len(FH4_ETAPES)))
        ch["dec"] += ["méthode de calcul : automatique (TauDEM si trouvé, sinon NumPy)", "seuil du réseau : « {} » ({} % du bassin)".format(FH4_SEUILS[FH4_DEFAUT][1].split(" (")[0], fh4_fr(FH4_SEUILS[FH4_DEFAUT][2], 1)),
                      "lac : option (ii) plan d'eau à un seul pixel de sortie (évite les lignes droites imposées par le comblement)", "exutoire : sortie à plus forte aire drainée, jamais déplacé automatiquement",
                      "HRU : bornes de pente {} %, seuils d'élimination {} % / {} % / {} % et HRU minimale {} ha".format("/".join(fh4_fr(b_, 0, False) for b_ in FH4_PENTE_BORNES), *(fh4_fr(FH4_HRU_SEUILS[k_], 1) for k_ in ("occupation_pct", "sol_pct", "pente_pct", "min_ha"))),
                      "codes d'occupation SWAT+ (par nom de classe) et paramètres de sol (Saxton & Rawls 2006, texture représentative, profondeur 1000 mm, matière organique 2 %) : écrits de mémoire, à vérifier"]
        step_next()

    def step_finish(key, ok_, msg="", fatal=False):
        ch = st["chain"]
        ch["stat"][key] = ("ok" if ok_ else "echec", msg)
        set_step(key, "ok" if ok_ else "echec", msg)
        if not ok_ and fatal:
            log("⛔ Étape « {} » en échec : les étapes qui en dépendent sont ignorées.".format(dict(FH4_ETAPES)[key]))
            ch["stop"] = True
        QTimer.singleShot(0, step_next)

    def step_next():
        ch = st.get("chain")
        if ch is None:
            return
        if ch["i"] >= len(FH4_ETAPES) or ch["stop"] and FH4_ETAPES[ch["i"]][0] != "bilan":
            if ch["i"] < len(FH4_ETAPES) - 1:
                for k_, _l in FH4_ETAPES[ch["i"]:-1]:
                    ch["stat"].setdefault(k_, ("ignore", "ignorée"))
                    set_step(k_, "ignore", "ignorée")
                ch["i"] = len(FH4_ETAPES) - 1
            elif ch["i"] >= len(FH4_ETAPES):
                st["chain"] = None
                b_all.setEnabled(True)
                return
        key = FH4_ETAPES[ch["i"]][0]
        ch["i"] += 1
        set_step(key, "run")
        status.setText("Étape {}/{} : {}".format(ch["i"], len(FH4_ETAPES), dict(FH4_ETAPES)[key]))
        try:
            STEP_FN[key](ch)
        except Fh4Error as ex:
            log("❌ " + str(ex))
            step_finish(key, False, str(ex)[:160], fatal=key in ("amont", "sb", "hru"))
        except Exception:
            log("❌ " + traceback.format_exc())
            step_finish(key, False, "erreur inattendue (voir le journal)", fatal=key in ("amont", "sb", "hru"))

    def s_amont(ch):
        d = dirs_now()
        fh4_find_stage1(d["e1"])
        fh4_find_stage2(d["e2"], None)
        if not os.path.isfile(os.path.join(d["e2"], "travail", "MNT_srtm.tif")):
            raise Fh4Error("MNT SRTM du cache de l'étage 2 introuvable ({}) : relance l'étage 2 pour recréer son cache.".format(os.path.join(d["e2"], "travail", "MNT_srtm.tif")))
        check_upstream()
        step_finish("amont", True, "étages 1 et 2 présents")

    def s_outils(ch):
        r = do_inspect_tools()
        ch["dec"].append("TauDEM : {}".format("trouvé, voie A comparée à la voie C" if r["ok_a"] else "introuvable, voie C (NumPy, moins éprouvée)"))
        step_finish("outils", True, "voie " + r["voie"])

    def s_sb(ch):
        p = params({"lake_mode": "flat", "thr_idx": FH4_DEFAUT, "outlet_xy": None, "calage": None, "rivers_named": (topo_lines.currentData() or {}).get("spec"), "compare": True})
        p["outlet_xy"] = None

        def done(res):
            st["ctx"], st["key"] = None, None
            st["stale"] = False
            ch["res"] = res
            show_results(res)
            step_finish("sb", True, "{} sous-bassins".format(len(res["rows"])))
        start(fh4_run_task, p, done, "Calcul des sous-bassins", on_fail=lambda m: step_finish("sb", False, m.splitlines()[0][:160], fatal=True))

    def s_hru(ch):
        p = {"root": root_edit.text().strip(), "basin": basin_combo.currentData()["basin"], "pente_bornes": list(FH4_PENTE_BORNES), "seuils": dict(FH4_HRU_SEUILS), "auto_tables": True}

        def done(r):
            st["hru"] = r
            hru_table.setRowCount(len(r["rows"]))
            for i, x in enumerate(r["rows"]):
                for j, v in enumerate((x["hru"], x["sb"], x["lu_nom"], x["sol_nom"], x["pente_nom"], fh4_fr(x["surf_ha"], 1), fh4_fr(x["pct_sb"], 1), "auto", FH4_STATUT_AUTO)):
                    hru_table.setItem(i, j, QTableWidgetItem(str(v)))
            hru_table.resizeColumnsToContents()
            hru_txt.setPlainText("\n".join(["{} {} : {}".format("✔" if ok else "⚠", a_, b_) for a_, b_, ok in r["controles"]] + ["", "Codes SWAT+ et paramètres de sol AUTOMATIQUES : " + FH4_STATUT_AUTO + "."]))
            step_finish("hru", True, "{} HRU".format(len(r["rows"])))
        start(fh4_hru_task, p, done, "Construction des HRU", on_fail=lambda m: step_finish("hru", False, m.splitlines()[0][:160], fatal=True))

    def s_topo(ch):
        if topo_pts.currentData() or topo_lines.currentData():
            ch["dec"].append("toponymes : couches choisies par l'utilisateur")
            step_finish("topo", True, "couches déjà choisies")
            return
        found = fh4_project_layers_for_topo()
        if found:
            fill_topo(found)
            for cb_, genre in ((topo_pts, "point"), (topo_lines, "ligne")):
                for i in range(1, cb_.count()):
                    if cb_.itemData(i) and cb_.itemData(i)["genre"] == genre:
                        cb_.setCurrentIndex(i)
                        break
            ch["dec"].append("toponymes : couches du projet QGIS utilisées")
            step_finish("topo", True, "couches du projet QGIS")
            return
        p = {"root": root_edit.text().strip(), "basin": basin_combo.currentData()["basin"], "http": st.get("http"), "bbox": st.get("bbox"),
             "dest": os.path.join(download_cache_root(), "osm")}

        def got_est(est):
            st["bbox"] = est.get("bbox")
            if not est.get("connu") or not est.get("elements"):
                raise Fh4Error("OpenStreetMap indisponible ou sans données ({}) : cartes sans toponymes.".format(est.get("erreur", "aucun élément")))
            if est.get("cached"):
                log("ℹ OSM : {} éléments déjà dans le cache local ; aucun appel réseau.".format(est["elements"]))
            else:
                log("📏 OSM : {} éléments nommés, taille estimée {} : téléchargement.".format(est["elements"], fh4_fmt_bytes(est["octets"])))

            def got(r):
                fill_topo(st["topo_found"] + [{"spec": "{}|layername=lieux".format(r["gpkg"]), "nom": "OSM — lieux", "genre": "point", "n": r["lieux"], "champ": "name"},
                                              {"spec": "{}|layername=cours_d_eau".format(r["gpkg"]), "nom": "OSM — cours d'eau", "genre": "ligne", "n": r["cours"], "champ": "name"}])
                topo_pts.setCurrentIndex(topo_pts.count() - 1)
                topo_lines.setCurrentIndex(topo_lines.count() - 1)
                ch["dec"].append("toponymes : OpenStreetMap (noms non vérifiés)")
                step_finish("topo", True, "{} lieux, {} cours d'eau (OSM)".format(r["lieux"], r["cours"]))
            start(fh4_osm_download_task, {"bbox": est["bbox"], "dest": os.path.join(download_cache_root(), "osm"), "http": st.get("http")}, got, "Téléchargement OSM",
                  on_fail=lambda m: step_finish("topo", False, "téléchargement OSM impossible"))
        start(fh4_osm_estimate_task, p, got_est, "Estimation OSM", on_fail=lambda m: step_finish("topo", False, "OSM indisponible"))

    def s_meteo(ch):
        if not d0_edit.text().strip() or not d1_edit.text().strip():
            a_, b_ = fh4_meteo_period_fallback()
            d0_edit.setText(a_)
            d1_edit.setText(b_)
            ch["dec"].append("période météo : {} → {} (5 dernières années complètes, l'étage 2 n'en donne pas)".format(a_, b_))
        else:
            ch["dec"].append("période météo : {} → {} (celle de l'étage 2)".format(d0_edit.text(), d1_edit.text()))
        ck_chirps.setChecked(True)
        ck_power.setChecked(True)
        p = meteo_params()

        def got_est(est):
            st["est"], st["bbox"] = est, est.get("bbox")
            zone_lab.setText("Ouest {:.3f}, Sud {:.3f}, Est {:.3f}, Nord {:.3f} (WGS 84) + marge".format(*est["bbox"]))
            log("📏 Météo — taille estimée : {} (CHIRPS : {} ; NASA POWER : {} requêtes). Téléchargement en cours (reprenable).".format(
                fh4_fmt_bytes(est["total"]), "{} fichiers".format(est["chirps"]["fichiers"]) if est.get("chirps") else "—", est["power"]["requetes"] if est.get("power") else "—"))
            p2 = meteo_params({"estimate": est, "bbox": est["bbox"]})
            met_start(fh4_meteo_download_task, p2, lambda r: (m_info.setText("Terminé : {} fichier(s), {}.".format(r["fichiers"], fh4_fmt_bytes(r["octets"]))),
                                                            step_finish("meteo", True, "{} fichier(s), {}".format(r["fichiers"], fh4_fmt_bytes(r["octets"])))), "Téléchargement météo",
                      on_fail=lambda m: step_finish("meteo", False, "téléchargement interrompu (relance : reprise automatique)"))
        met_start(fh4_meteo_estimate_task, p, got_est, "Estimation de la taille", on_fail=lambda m: step_finish("meteo", False, "estimation impossible : " + m.splitlines()[0][:100]))

    def s_swat(ch):
        p = {"root": root_edit.text().strip(), "basin": basin_combo.currentData()["basin"]}
        start(fh4_swat_prepare_task, p, lambda r: step_finish("swat", True, "{} manque(s) listé(s)".format(len(r["manque"]))), "Préparation des entrées QSWAT+",
              on_fail=lambda m: step_finish("swat", False, m.splitlines()[0][:160]))

    def s_cartes(ch):
        n_ok = 0
        for kind in ("sb", "reseau"):
            try:
                export_map(kind)
                n_ok += 1
            except Exception:
                log("❌ " + traceback.format_exc())
        try:
            on_hru_map()
            n_ok += 1
        except Exception:
            log("❌ " + traceback.format_exc())
        step_finish("cartes", n_ok > 0, "{} carte(s) dans Cartes/".format(n_ok))

    def s_bilan(ch):
        d = dirs_now()
        ch["stat"]["bilan"] = ("ok", "bilan_lancement.txt")
        txt = fh4_bilan_text(ch["stat"], ch["dec"], ch["res"] or st.get("res"), basin_combo.currentData()["basin"])
        try:
            fh4_write_text(os.path.join(d["e4"], "bilan_lancement.txt"), txt, log)
        except Exception as ex:
            log("⚠ Bilan non écrit : {}".format(ex))
        for ln in txt.splitlines():
            log(ln)
        ok_n = sum(1 for v in ch["stat"].values() if v[0] == "ok")
        set_step("bilan", "ok", "bilan_lancement.txt")
        ch["stat"]["bilan"] = ("ok", "")
        flash("✅ Terminé : {}/{} étapes réussies. Bilan dans le journal et bilan_lancement.txt.".format(ok_n, len(FH4_ETAPES) - 1), "color:#1b5e20;", 60000)
        st["chain"] = None
        b_all.setEnabled(True)

    STEP_FN = {"amont": s_amont, "outils": s_outils, "sb": s_sb, "hru": s_hru, "topo": s_topo, "meteo": s_meteo, "swat": s_swat, "cartes": s_cartes, "bilan": s_bilan}

    def pick_root(*_):
        p_ = QFileDialog.getExistingDirectory(outer, "Dossier de sortie (racine)", root_edit.text() or "")
        if p_:
            root_edit.setText(p_)

    def pick_cache(*_):
        p_ = QFileDialog.getExistingDirectory(outer, "Dossier de cache météo + OSM", cache_edit.text() or "")
        if p_:
            cache_edit.setText(p_)

    def save_log(*_):
        d = dirs_now()
        p_, _f = QFileDialog.getSaveFileName(outer, "Enregistrer le journal", os.path.join(d["e4"] if d else "", "journal_etage4.txt"), "Texte (*.txt)")
        if p_:
            fh4_write_text(p_, logbox.toPlainText())
            log("💾 Journal enregistré : {}".format(p_))

    def to_table(*_):
        tabs.setCurrentWidget(t_tab)

    b_root.clicked.connect(pick_root)
    b_cache.clicked.connect(pick_cache)
    root_edit.editingFinished.connect(lambda: (refresh_basins(), check_upstream()))
    basin_combo.currentIndexChanged.connect(check_upstream)
    b_inspect.clicked.connect(on_inspect)
    b_taudem.clicked.connect(pick_taudem)
    b_out2.clicked.connect(pick_out2)
    b_calage.clicked.connect(do_calage)
    b_topo.clicked.connect(scan_topo)
    b_osm.clicked.connect(on_osm)
    b_hru.clicked.connect(on_hru)
    b_hru_map.clicked.connect(on_hru_map)
    b_open_tables.clicked.connect(on_open_tables)
    b_swat_insp.clicked.connect(on_swat_inspect)
    b_swat_prep.clicked.connect(on_swat_prepare)
    b_preview.clicked.connect(on_preview)
    b_calc.clicked.connect(on_calc)
    b_all.clicked.connect(run_all)
    b_cancel.clicked.connect(on_cancel)
    b_mcancel.clicked.connect(on_cancel)
    b_loadres.clicked.connect(load_last)
    b_extra.clicked.connect(pick_extra)
    b_extra_clear.clicked.connect(clear_extra)
    b_full.clicked.connect(lambda *_: mapview.zoom_full())
    b_map_sb.clicked.connect(lambda *_: export_map("sb"))
    b_map_rv.clicked.connect(lambda *_: export_map("reseau"))
    b_voir_tab.clicked.connect(to_table)
    b_csv.clicked.connect(lambda *_: (lambda p_: (fh4_table_csv(st["res"], p_, log), log("💾 CSV : " + p_)) if (p_ and st["res"]) else None)(QFileDialog.getSaveFileName(outer, "Exporter le tableau", os.path.join(dirs_now()["e4"] if dirs_now() else "", "sous_bassins.csv"), "CSV (*.csv)")[0]))
    b_est.clicked.connect(on_estimate)
    b_dl.clicked.connect(on_download)
    b_test.clicked.connect(on_test)
    b_copy.clicked.connect(lambda *_: QApplication.clipboard().setText(logbox.toPlainText()))
    b_copy_home.clicked.connect(lambda *_: (QApplication.clipboard().setText(steps_text() + "\n\n" + logbox.toPlainText()), flash("Journal copié.", "color:#1b5e20;", 4000)))
    b_savelog.clicked.connect(save_log)
    b_clearlog.clicked.connect(lambda *_: logbox.clear())
    refresh_basins()
    o2 = fh4_project_outlet()
    if o2:
        st["outlet2"] = o2
        log("📍 Point « Exutoire » de l'étage 2 trouvé dans le projet QGIS (couche « {} ») : X = {}, Y = {}.".format(o2[2], fh4_fr(o2[0], 0), fh4_fr(o2[1], 0)))
    fill_topo(fh4_project_layers_for_topo())
    check_upstream()
    outer._fh4 = {"tabs": tabs, "st": st, "log": log, "W": {"root": root_edit, "basin": basin_combo, "calc": b_calc, "preview": b_preview, "cancel": b_cancel, "table": table, "total": total_tab, "map": mapview,
                                                          "placeholder": m_placeholder, "maps_box": maps_box, "loadres": b_loadres, "logbox": logbox, "est": b_est, "dl": b_dl, "test": b_test,
                                                          "d0": d0_edit, "d1": d1_edit, "cache": cache_edit, "info": m_info, "calc_split": calc_split, "sa_left": sa_left, "status": status, "all": b_all, "steps": steps_list, "adv": adv, "adv_in": adv_in, "run_all": run_all, "thr_table": thr_table, "lake_radios": lake_radios, "taudem_lab": taudem_lab, "b_taudem": b_taudem, "b_out2": b_out2, "b_calage": b_calage, "e_lab": e_lab, "topo_pts": topo_pts, "topo_lines": topo_lines, "topo_lab": topo_lab, "b_osm": b_osm, "b_topo": b_topo, "hru": b_hru, "hru_table": hru_table, "hru_txt": hru_txt, "pente": pente_edit, "swat_insp": b_swat_insp, "swat_prep": b_swat_prep, "hyd_table": hyd_table, "hru_map": b_hru_map,
                                                          "voie": voie_combo, "inspect": b_inspect, "search": t_search, "chirps": ck_chirps, "power": ck_power, "prog": prog, "mprog": m_prog},
                  "show_results": show_results, "do_inspect_tools": do_inspect_tools, "scan_topo": scan_topo, "fill_topo": fill_topo, "on_osm": on_osm, "on_sb_click": on_sb_click, "check_upstream": check_upstream, "fill_table": fill_table, "params": params, "meteo_params": meteo_params}
    return outer


def fh4_fit_window(dlg):
    """Fenêtre à 85 % de l'écran disponible (taille minimale raisonnable)."""
    try:
        from qgis.PyQt.QtWidgets import QApplication
        g = QApplication.primaryScreen().availableGeometry()
        dlg.resize(int(g.width() * 0.85), int(g.height() * 0.85))
        dlg.setMinimumSize(min(900, int(g.width() * 0.85)), min(600, int(g.height() * 0.85)))
    except Exception:
        dlg.resize(1280, 800)


def fh4_extension_run(popup):
    """Depuis le grand pop-up FALKHYDRO+ : ouvre l'onglet Sous-bassins dans une fenêtre."""
    from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout
    dlg = QDialog(popup)
    dlg.setWindowTitle("FALKHYDRO+ — Étage 4 : Sous-bassins")
    QVBoxLayout(dlg).addWidget(fh4_extension_widget(dlg))
    fh4_fit_window(dlg)
    dlg.show()
    popup._sousbassins_dlg = dlg


# ---------------------------------------------------------------- lancement AUTONOME (sans le main)
# Dans QGIS : Console Python -> Éditeur -> ouvrir ce fichier -> ▶ : une fenêtre « Étage 4 » s'ouvre seule (test avant dépôt dans extensions/).
if "FH_MODULES_CHARGES" not in globals():
    import builtins as _b
    from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout
    _old = getattr(_b, "_FALKHYDRO_SB4_SEUL", None)
    if _old is not None:
        try:
            _old.close()
        except Exception:
            pass
    try:
        _par = iface.mainWindow()
    except Exception:
        _par = None
    _dlg = QDialog(_par)
    _dlg.setWindowTitle("FALKHYDRO+ — Étage 4 : Sous-bassins (module autonome)")
    QVBoxLayout(_dlg).addWidget(fh4_extension_widget(_dlg))
    fh4_fit_window(_dlg)
    _b._FALKHYDRO_SB4_SEUL = _dlg
    _dlg.show()
    _dlg.raise_()
    _dlg.activateWindow()
    print("✅ Module Sous-bassins (étage 4) lancé seul.")
