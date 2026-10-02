#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FALKHYDRO+ ÉTAGE 4 V2 — PATCH COMPATIBLE
WeatherManager + SWATManager
Patch sécurisé sans remplacement du fichier existant.

Ce patch ajoute à ton fichier existant:
- Classe WeatherManager (gestion météo CHIRPS + ERA5-Land)
- Classe SWATManager (préparation et intégration SWAT+)
- Intégration légère à l'interface QGIS

CONSIGNES:
- Copier-coller après les imports existants (ligne ~60)
- Ne pas effacer l'architecture existante
- Garder tous les modules hydrologiques
- Compatible PyQt5 + QGIS + GDAL
"""

# ============================================================
# PATCH 1: IMPORTS SUPPLÉMENTAIRES (ajouter après les imports existants)
# ============================================================
# À coller après: from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
# Remplacer par:
# from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QObject
# (ajouter QObject)

# ============================================================
# PATCH 2: CLASSES MANAGER (copier-coller avant Fh4Task)
# ============================================================

class WeatherManager(QObject):
    """Manager météo sécurisé compatible FALKHYDRO.
    Reutilise les fonctions existantes.
    Aucune dépendance externe.
    """
    progressChanged = pyqtSignal(int, str)
    statusChanged = pyqtSignal(str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, root=None, basin=None, cache_dir=None, parent=None):
        super(WeatherManager, self).__init__(parent)
        self.root = root
        self.basin = basin
        self.cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), "FALKHYDRO", "meteo")
        self.http = Fh4Http()
        self.last_estimate = {}
        self.last_bbox = None

    def set_context(self, root=None, basin=None, cache_dir=None):
        if root is not None:
            self.root = root
        if basin is not None:
            self.basin = basin
        if cache_dir is not None:
            self.cache_dir = cache_dir
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except Exception:
            pass
        return self

    def _log(self, message):
        try:
            self.statusChanged.emit(str(message))
        except Exception:
            pass

    def validate_period(self, d0, d1):
        if not self.root or not self.basin:
            raise Fh4Error("Pas de contexte bassin pour la météo.")
        try:
            fh4_days(d0, d1)
        except Exception as ex:
            raise Fh4Error("Période météo invalide: {}".format(str(ex)[:100]))
        return True

    def estimate(self, d0, d1, chirps=True, power=True):
        try:
            if not self.root or not self.basin:
                raise Fh4Error("Contexte bassin manquant.")
            self.validate_period(d0, d1)
            bbox, _ = fh4_meteo_zone(self.root, self.basin, self._log)
            self.last_bbox = bbox
            est = fh4_meteo_estimate(self.http, d0, d1, bbox, self.cache_dir, chirps, power)
            est["bbox"] = bbox
            est["requetes"] = {"head": self.http.n_head, "get": self.http.n_get}
            self.last_estimate = est
            self.progressChanged.emit(100, "Estimation OK")
            return est
        except Fh4Error as ex:
            self.failed.emit(str(ex))
            raise
        except Exception as ex:
            self.failed.emit("Erreur estimation: {}".format(str(ex)[:100]))
            raise Fh4Error("Estimation météo échouée.")

    def download(self, d0, d1, chirps=True, power=True, estimate=None, on_prog=None):
        try:
            if not self.root or not self.basin:
                raise Fh4Error("Contexte bassin manquant.")
            self.validate_period(d0, d1)
            bbox = self.last_bbox or fh4_meteo_zone(self.root, self.basin, self._log)[0]
            self.last_bbox = bbox
            jobs = fh4_meteo_jobs({"d0": d0, "d1": d1, "cache": self.cache_dir, "chirps": chirps, "power": power}, bbox, estimate)
            kinds = [k for k, on in (("CHIRPS", chirps), ("POWER", power)) if on]
            os.makedirs(self.cache_dir, exist_ok=True)
            fh4_meteo_sources_json(self.cache_dir, d0, d1, bbox, kinds, estimate)
            self._log("Téléchargement: {} job(s)".format(len(jobs)))
            def on_prog_default(d):
                pct = float(d.get("pct") or 0)
                msg = str(d.get("fichier") or "Météo")
                self.progressChanged.emit(int(min(100, max(0, pct))), msg)
            prog_fn = on_prog or on_prog_default
            result = fh4_download_batch(self.http, jobs, prog_fn, None, workers=FH4_METEO_WORKERS)
            fh4_meteo_sources_json(self.cache_dir, d0, d1, bbox, kinds, estimate)
            result["bbox"] = bbox
            result["requetes"] = {"head": self.http.n_head, "get": self.http.n_get}
            self.progressChanged.emit(100, "Téléchargement OK")
            self.finished.emit(result)
            return result
        except Fh4Error as ex:
            self.failed.emit(str(ex))
            raise
        except Exception as ex:
            self.failed.emit("Erreur DL: {}".format(str(ex)[:100]))
            raise Fh4Error("Téléchargement météo échoué.")

    def test(self, d0, d1, chirps=True, power=True):
        try:
            import datetime
            if not self.root or not self.basin:
                raise Fh4Error("Contexte bassin manquant.")
            bbox, _ = fh4_meteo_zone(self.root, self.basin, self._log)
            self.last_bbox = bbox
            d0d = datetime.date.fromisoformat(str(d0)[:10])
            d1d = min(datetime.date.fromisoformat(str(d1)[:10]), d0d + datetime.timedelta(days=29))
            pts = fh4_power_points(bbox)
            la, lo = (pts[len(pts) // 2] if pts else (0, 0))
            tc = os.path.join(self.cache_dir, "test")
            jobs = []
            if power:
                jobs.append({
                    "kind": "POWER",
                    "url": FH4_POWER_URL.format(
                        params=",".join(FH4_POWER_PARAMS),
                        lon=lo, lat=la,
                        d0=d0d.strftime("%Y%m%d"),
                        d1=d1d.strftime("%Y%m%d")
                    ),
                    "dest": os.path.join(tc, "power_test_{}_{}.json".format(la, lo)),
                    "expected": None
                })
            if chirps:
                jobs += fh4_chirps_jobs(d0d, d0d, tc)
            self._log("Test: {} job(s)".format(len(jobs)))
            result = fh4_download_batch(self.http, jobs, lambda d: self.progressChanged.emit(
                int(d.get("pct") or 0), d.get("fichier") or "Test"
            ), None)
            result["requetes"] = {"head": self.http.n_head, "get": self.http.n_get}
            self.progressChanged.emit(100, "Test OK")
            self.finished.emit(result)
            return result
        except Exception as ex:
            self.failed.emit("Test échoué: {}".format(str(ex)[:100]))
            raise Fh4Error("Test météo échoué.")


class SWATManager(QObject):
    """Manager SWAT+ sécurisé sans simulateur factice."""
    progressChanged = pyqtSignal(int, str)
    statusChanged = pyqtSignal(str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, root=None, basin=None, parent=None):
        super(SWATManager, self).__init__(parent)
        self.root = root
        self.basin = basin

    def set_context(self, root=None, basin=None):
        if root is not None:
            self.root = root
        if basin is not None:
            self.basin = basin
        return self

    def _log(self, message):
        try:
            self.statusChanged.emit(str(message))
        except Exception:
            pass

    def prepare_project(self):
        try:
            if not self.root or not self.basin:
                raise Fh4Error("Contexte bassin manquant.")
            res = fh4_swat_prepare({"root": self.root, "basin": self.basin}, self._log, None, None)
            self.progressChanged.emit(100, "Préparation OK")
            self.statusChanged.emit("Préparation SWAT+ terminée.")
            self.finished.emit(res)
            return res
        except Fh4Error as ex:
            self.failed.emit(str(ex))
            raise
        except Exception as ex:
            self.failed.emit("Erreur: {}".format(str(ex)[:100]))
            raise Fh4Error("Préparation SWAT+ échouée.")

    def create_hrus(self, pente_bornes=None, seuils=None):
        try:
            if not self.root or not self.basin:
                raise Fh4Error("Contexte bassin manquant.")
            p = {
                "root": self.root,
                "basin": self.basin,
                "pente_bornes": tuple(pente_bornes or FH4_PENTE_BORNES),
                "seuils": dict(FH4_HRU_SEUILS, **(seuils or {}))
            }
            out = fh4_hru_build(p, self._log, None, None)
            self.progressChanged.emit(100, "HRU OK")
            self.finished.emit(out)
            return out
        except Exception as ex:
            self.failed.emit("Erreur HRU: {}".format(str(ex)[:100]))
            raise Fh4Error("Création HRU échouée.")

    def run_simulation(self):
        exe = fh4_find_exe("swatplus", [], depth=6)
        if exe is None:
            exe = fh4_find_exe("rev60.5.7_64rel", [], depth=6)
        if exe is None:
            raise Fh4Error("SWAT+ non trouvé. Plugin prépare les fichiers, ne lance pas de simulateur factice.")
        self.statusChanged.emit("SWAT+ prêt: {}".format(exe))
        return {"executable": exe, "status": "ready"}

    def read_results(self, outdir):
        if not os.path.isdir(outdir):
            raise Fh4Error("Dossier résultats introuvable: {}".format(outdir))
        files = {"csv": [], "tif": [], "txt": []}
        for root, _, names in os.walk(outdir):
            for name in names:
                full = os.path.join(root, name)
                if name.lower().endswith(".csv"):
                    files["csv"].append(full)
                elif name.lower().endswith((".tif", ".tiff")):
                    files["tif"].append(full)
                elif name.lower().endswith(".txt"):
                    files["txt"].append(full)
        return files

    def export_results_qgis(self, outdir):
        try:
            files = self.read_results(outdir)
            pr = QgsProject.instance()
            added = []
            for path in files.get("tif", []):
                layer = QgsRasterLayer(path, os.path.basename(path))
                if layer.isValid():
                    pr.addMapLayer(layer)
                    added.append(path)
            self.progressChanged.emit(100, "Export OK")
            return {"added": added, "files": files}
        except Exception as ex:
            raise Fh4Error("Export QGIS échoué: {}".format(str(ex)[:100]))


# ============================================================
# PATCH 3: INTÉGRATION LÉGÈRE DANS fh4_extension_widget
# ============================================================
# Dans la fonction fh4_extension_widget, trouver:
#
# st = {"res": None, "busy": None, ..., "topo_found": []}
#
# Ajouter avant la fermeture de st:
#
#     "weather_manager": WeatherManager(),
#     "swat_manager": SWATManager(),
#
# Exemple:
# st = {
#     "res": None,
#     "busy": None,
#     ...
#     "topo_found": [],
#     "weather_manager": WeatherManager(),
#     "swat_manager": SWATManager(),
# }


# ============================================================
# PATCH 4: MODIFIER on_estimate (remplacer la fonction existante)
# ============================================================

def on_estimate_patched(*_):
    """Version améliorée avec WeatherManager."""
    try:
        p = meteo_params()
    except Fh4Error as ex:
        m_info.setText(str(ex))
        return

    mgr = st.get("weather_manager")
    if mgr is None:
        mgr = WeatherManager()
        st["weather_manager"] = mgr
    
    mgr.set_context(root_edit.text().strip(), basin_combo.currentData()["basin"], download_cache_root())
    try:
        est = mgr.estimate(p["d0"], p["d1"], p.get("chirps", True), p.get("power", True))
        st["est"], st["bbox"] = est, est.get("bbox")
        zone_lab.setText("Ouest {:.3f}, Sud {:.3f}, Est {:.3f}, Nord {:.3f} (WGS 84)".format(*est["bbox"]))
        parts = []
        if est.get("chirps"):
            parts.append("CHIRPS: {} fichiers, {}".format(est["chirps"]["fichiers"], fh4_fmt_bytes(est["chirps"].get("octets", 0))))
        if est.get("power"):
            parts.append("POWER: {} req, ~{}".format(est["power"].get("requetes", 0), fh4_fmt_bytes(est["power"].get("octets", 0))))
        m_info.setText("Estimation OK - Total: {} - {}".format(fh4_fmt_bytes(est.get("total", 0)), " | ".join(parts)))
        log("📏 Estimation météo: OK")
    except Exception as ex:
        m_info.setText("Erreur estimation: {}".format(str(ex)[:150]))
        log("❌ Estimation: {}".format(traceback.format_exc()[:300]))

# À placer dans les connexions de boutons:
# b_est.clicked.connect(on_estimate_patched)


# ============================================================
# PATCH 5: MODIFIER on_download (remplacer la fonction existante)
# ============================================================

def on_download_patched(*_):
    """Version améliorée avec WeatherManager."""
    try:
        p = meteo_params({"estimate": st.get("est")})
    except Fh4Error as ex:
        m_info.setText(str(ex))
        return

    est = st.get("est")
    taille = fh4_fmt_bytes(est.get("total", 0)) if est else "inconnue"
    
    from qgis.PyQt.QtWidgets import QMessageBox
    box = QMessageBox(outer)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle("Télécharger la météo?")
    box.setText("Période {} → {}\nTaille: {}\nDossier: {}\nReprenable.".format(
        p["d0"], p["d1"], taille, p["cache"]))
    b_yes = box.addButton("Télécharger", QMessageBox.AcceptRole)
    b_no = box.addButton("Annuler", QMessageBox.RejectRole)
    box.setDefaultButton(b_no)
    box.exec_()
    if box.clickedButton() is not b_yes:
        log("ℹ Téléchargement annulé.")
        return

    mgr = st.get("weather_manager")
    if mgr is None:
        mgr = WeatherManager()
        st["weather_manager"] = mgr
    mgr.set_context(root_edit.text().strip(), basin_combo.currentData()["basin"], download_cache_root())
    
    try:
        result = mgr.download(p["d0"], p["d1"], p.get("chirps", True), p.get("power", True), p.get("estimate"))
        m_info.setText("✅ Terminé: {} fichier(s), {}".format(result.get("fichiers", 0), fh4_fmt_bytes(result.get("octets", 0))))
        log("✅ Météo: {} fichier(s) prêts.".format(result.get("fichiers", 0)))
    except Exception as ex:
        m_info.setText("❌ Erreur DL: {}".format(str(ex)[:150]))
        log("❌ DL: {}".format(traceback.format_exc()[:300]))

# À placer dans les connexions:
# b_dl.clicked.connect(on_download_patched)


# ============================================================
# PATCH 6: MODIFIER on_swat_prepare (remplacer la fonction existante)
# ============================================================

def on_swat_prepare_patched(*_):
    """Version avec SWATManager."""
    try:
        d = basin_combo.currentData()
        if not d:
            flash("Choisis un bassin.", "color:#c62828;")
            return
        p = {"root": root_edit.text().strip(), "basin": d["basin"]}
    except TypeError:
        flash("Choisis un bassin.", "color:#c62828;")
        return

    mgr = st.get("swat_manager")
    if mgr is None:
        mgr = SWATManager()
        st["swat_manager"] = mgr
    mgr.set_context(p["root"], p["basin"])
    
    try:
        res = mgr.prepare_project()
        msg = "Préparation QSWAT+ OK.\nManques:\n" + "\n".join(["  - " + m for m in res.get("manque", [])])
        hru_txt.setPlainText(msg)
        flash("Entrées QSWAT+ préparées.", "color:#1b5e20;")
        log("✅ QSWAT+: Préparation OK")
    except Exception as ex:
        flash("Erreur prep SWAT+: {}".format(str(ex)[:150]), "color:#c62828;", 15000)
        log("❌ SWAT+: {}".format(traceback.format_exc()[:300]))

# À placer dans les connexions:
# b_swat_prep.clicked.connect(on_swat_prepare_patched)


print("✅ PATCH FALKHYDRO V2 - WeatherManager + SWATManager prêt à intégrer.")
print("   Instructions: voir commentaires dans ce fichier.")
