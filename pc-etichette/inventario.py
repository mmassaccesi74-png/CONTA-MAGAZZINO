# -*- coding: utf-8 -*-
"""
inventario.py - Inventario con il telefono.

1) esporta_anagrafica(): crea il file che l'app "Conteggio inventario" sul telefono usa per
   riconoscere i codici a barre (articoli, codici a barre per UM, magazzini).
2) leggi_conteggio() + prepara_conteggio(): legge il file esportato dall'app, converte le
   quantita' nell'UM di magazzino, somma per magazzino/articolo e confronta con le giacenze SAP.
3) crea_conteggio_sap(): crea in SAP B1 il documento "Conteggio inventario" tramite la DI API.
   Le giacenze NON cambiano: l'inventario si registra poi in SAP (Registrazione inventario).

4) avvia_ricezione(): finche' il programma e' aperto, i telefoni sul Wi-Fi aziendale scaricano
   l'anagrafica e inviano il conteggio direttamente (porta 8766, codice di accesso).

Le letture usano la stessa connessione di sola lettura del resto del programma.
La scrittura passa solo dalla DI API di SAP (mai scritture dirette nel database).
"""

import json
import os
import re
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from decimal import Decimal, ROUND_HALF_UP

FORMATO_ANAGRAFICA = "anagrafica-inventario-1"
FORMATO_CONTEGGIO = "conteggio-inventario-1"
PORTA = 8766
NOME_ANAGRAFICA = "anagrafica_inventario.json"
CARTELLA_RICEVUTI = "conteggi_ricevuti"
NOME_ARCHIVIO = "usati"


# ---------------------------------------------------------------------------
# Lettura da SAP
# ---------------------------------------------------------------------------
SQL_MAGAZZINI = "SELECT WhsCode, WhsName FROM OWHS WHERE Inactive = 'N' ORDER BY WhsCode"
SQL_MAGAZZINI_VECCHIO = "SELECT WhsCode, WhsName FROM OWHS ORDER BY WhsCode"

SQL_ARTICOLI_INV = """
-- articoli senza gruppo UM (UgpEntry = -1, "Manuale"): si usa l'unita' scritta in anagrafica
SELECT T0.ItemCode, T0.ItemName, CASE WHEN T0.UgpEntry = -1 OR U.UomCode IS NULL THEN ISNULL(T0.InvntryUom, '') ELSE U.UomCode END
FROM OITM T0
LEFT JOIN OUOM U ON U.UomEntry = T0.IUoMEntry
WHERE T0.InvntItem = 'Y' AND T0.frozenFor = 'N'
"""

SQL_BARCODE_INV = """
SELECT B.BcdCode, B.ItemCode, CASE WHEN T0.UgpEntry = -1 THEN '' ELSE ISNULL(U.UomCode, '') END
FROM OBCD B
JOIN OITM T0 ON T0.ItemCode = B.ItemCode
LEFT JOIN OUOM U ON U.UomEntry = B.UomEntry
WHERE T0.InvntItem = 'Y' AND T0.frozenFor = 'N'
ORDER BY B.ItemCode, B.BcdEntry
"""

SQL_CODEBARS_INV = """
SELECT T0.CodeBars, T0.ItemCode
FROM OITM T0
WHERE T0.InvntItem = 'Y' AND T0.frozenFor = 'N' AND ISNULL(T0.CodeBars, '') <> ''
"""

# Fattore di conversione di ogni UM verso l'UM di magazzino dell'articolo
SQL_FATTORI = """
SELECT T0.ItemCode, U.UomCode, G.BaseQty / NULLIF(G.AltQty, 0),
       (SELECT G2.BaseQty / NULLIF(G2.AltQty, 0) FROM UGP1 G2
         WHERE G2.UgpEntry = T0.UgpEntry AND G2.UomEntry = T0.IUoMEntry)
FROM OITM T0
JOIN UGP1 G ON G.UgpEntry = T0.UgpEntry
JOIN OUOM U ON U.UomEntry = G.UomEntry
WHERE T0.ItemCode IN ({segnaposto})
"""

SQL_DETTAGLI = """
SELECT T0.ItemCode, T0.ItemName, CASE WHEN T0.UgpEntry = -1 OR U.UomCode IS NULL THEN ISNULL(T0.InvntryUom, '') ELSE U.UomCode END,
       T0.ManBtchNum, T0.ManSerNum, T0.InvntItem, T0.UgpEntry
FROM OITM T0
LEFT JOIN OUOM U ON U.UomEntry = T0.IUoMEntry
WHERE T0.ItemCode IN ({segnaposto})
"""

SQL_GIACENZE = "SELECT ItemCode, WhsCode, OnHand FROM OITW WHERE ItemCode IN ({segnaposto})"

SQL_RISOLVI_BARCODE = """
SELECT B.BcdCode, B.ItemCode, ISNULL(U.UomCode, '')
FROM OBCD B LEFT JOIN OUOM U ON U.UomEntry = B.UomEntry
WHERE B.BcdCode IN ({segnaposto})
UNION ALL
SELECT T0.CodeBars, T0.ItemCode, ''
FROM OITM T0 WHERE T0.CodeBars IN ({segnaposto})
"""


def _righe(conn, sql, params=()):
    """Una query su un cursore nuovo, letta tutta subito.
    Con python-tds aprire un altro cursore chiude quello precedente: mai riusarne uno."""
    cur = conn.cursor()
    cur.execute(sql, list(params))
    return cur.fetchall()


def _a_blocchi(valori, n=500):
    valori = list(valori)
    for i in range(0, len(valori), n):
        yield valori[i:i + n]


def magazzini(conn):
    try:
        righe = _righe(conn, SQL_MAGAZZINI)
    except Exception:
        righe = _righe(conn, SQL_MAGAZZINI_VECCHIO)
    return [(c, n or "") for c, n in righe]


def esporta_anagrafica(conn, percorso, azienda=""):
    """Scrive il file per l'app del telefono. Ritorna (n. articoli, n. codici a barre)."""
    articoli = {code: [name or "", um or ""] for code, name, um in _righe(conn, SQL_ARTICOLI_INV)}

    barcode = {}
    for bcd, item, um in _righe(conn, SQL_BARCODE_INV):
        bcd = (bcd or "").strip()
        if bcd and bcd not in barcode:
            barcode[bcd] = [item, um or ""]
    for bcd, item in _righe(conn, SQL_CODEBARS_INV):
        bcd = (bcd or "").strip()
        if bcd and bcd not in barcode:
            barcode[bcd] = [item, ""]          # "" = UM di magazzino

    dati = {
        "formato": FORMATO_ANAGRAFICA,
        "creato": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "azienda": azienda,
        "magazzini": magazzini(conn),
        "articoli": articoli,
        "barcode": barcode,
    }
    os.makedirs(os.path.dirname(percorso) or ".", exist_ok=True)
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, separators=(",", ":"))
    return len(articoli), len(barcode)


# ---------------------------------------------------------------------------
# Conteggio dal telefono
# ---------------------------------------------------------------------------
class ErroreConteggio(Exception):
    pass


def leggi_conteggio(percorso):
    try:
        with open(percorso, encoding="utf-8-sig") as f:
            dati = json.load(f)
    except (OSError, ValueError) as e:
        raise ErroreConteggio(f"Il file non e' leggibile: {e}")
    return valida_conteggio(dati)


def valida_conteggio(dati):
    if not isinstance(dati, dict) or dati.get("formato") != FORMATO_CONTEGGIO:
        raise ErroreConteggio("Questo non e' un file di conteggio esportato dall'app Conteggio inventario.")
    righe = dati.get("righe")
    if not isinstance(righe, list):
        raise ErroreConteggio("Il file di conteggio non contiene righe.")
    pulite = []
    for r in righe:
        if not isinstance(r, dict):
            continue
        try:
            q = Decimal(str(r.get("quantita", 0)).replace(",", "."))
        except Exception:
            continue
        if q <= 0:
            continue
        pulite.append({
            "magazzino": str(r.get("magazzino", "")).strip(),
            "codice": str(r.get("codice", "") or "").strip(),
            "barcode": str(r.get("barcode", "") or "").strip(),
            "um": str(r.get("um", "") or "").strip(),
            "quantita": q,
        })
    return dati.get("sessione", ""), pulite


def _fattori(conn, codici):
    """{(articolo, UM): fattore verso l'UM di magazzino}"""
    fatt = {}
    for blocco in _a_blocchi(sorted(codici)):
        for item, um, f_um, f_mag in _righe(conn, SQL_FATTORI.format(segnaposto=",".join("?" for _ in blocco)), blocco):
            if f_um is None:
                continue
            f = Decimal(str(f_um)) / (Decimal(str(f_mag)) if f_mag else Decimal(1))
            fatt[(item.upper(), um)] = f
    return fatt


def prepara_conteggio(conn, righe):
    """
    Ritorna (righe_ok, problemi).
    righe_ok: lista di dict {magazzino, codice, descrizione, um_mag, contato, giacenza, differenza}
    problemi: lista di testi (codici sconosciuti, articoli a lotti/seriali, magazzini sconosciuti...)
    """
    problemi = []

    # 1) codici a barre senza articolo: si cercano in SAP
    da_risolvere = sorted({r["barcode"] for r in righe if not r["codice"] and r["barcode"]})
    risolti = {}
    for blocco in _a_blocchi(da_risolvere, 400):
        seg = ",".join("?" for _ in blocco)
        for bcd, item, um in _righe(conn, SQL_RISOLVI_BARCODE.format(segnaposto=seg), blocco + blocco):
            risolti.setdefault(bcd.strip(), (item, um or ""))
    for r in righe:
        if not r["codice"] and r["barcode"] in risolti:
            r["codice"], um = risolti[r["barcode"]]
            r["um"] = r["um"] or um

    sconosciuti = {}
    for r in righe:
        if not r["codice"]:
            sconosciuti[r["barcode"] or "(vuoto)"] = sconosciuti.get(r["barcode"] or "(vuoto)", 0) + r["quantita"]
    for bcd, q in sorted(sconosciuti.items()):
        problemi.append(f"Codice a barre {bcd} non trovato in SAP (quantita' {q:g}): escluso.")
    righe = [r for r in righe if r["codice"]]

    # 2) anagrafica, fattori UM, giacenze
    codici = {r["codice"] for r in righe}
    dettagli = {}
    for blocco in _a_blocchi(sorted(codici)):
        for item, nome, um_mag, lotti, seriali, magaz, ugp in _righe(
                conn, SQL_DETTAGLI.format(segnaposto=",".join("?" for _ in blocco)), blocco):
            dettagli[item.upper()] = (item, nome or "", um_mag or "", lotti == "Y", seriali == "Y", magaz == "Y",
                                      ugp == -1)
    fattori = _fattori(conn, codici)
    giacenze = {}
    for blocco in _a_blocchi(sorted(codici)):
        for item, whs, onhand in _righe(conn, SQL_GIACENZE.format(segnaposto=",".join("?" for _ in blocco)), blocco):
            giacenze[(item.upper(), whs)] = Decimal(str(onhand or 0))
    mag_validi = {c for c, _ in magazzini(conn)}

    # 3) somma per magazzino/articolo nell'UM di magazzino
    totali, esclusi = {}, set()
    for r in righe:
        d = dettagli.get(r["codice"].upper())
        if not d:
            esclusi.add(f"Articolo {r['codice']} non trovato in SAP: escluso.")
            continue
        item, nome, um_mag, lotti, seriali, magaz, manuale = d
        if not magaz:
            esclusi.add(f"Articolo {item} non e' di magazzino: escluso.")
            continue
        if lotti or seriali:
            esclusi.add(f"Articolo {item} gestito a {'lotti' if lotti else 'numeri di serie'}: "
                        f"va contato direttamente in SAP, escluso.")
            continue
        if r["magazzino"] not in mag_validi:
            esclusi.add(f"Magazzino '{r['magazzino']}' non esiste in SAP: righe di {item} escluse.")
            continue
        um = r["um"]
        if manuale:                      # nessun gruppo UM: niente conversione
            f = Decimal(1)
        elif um and um != um_mag:
            f = fattori.get((item.upper(), um))
            if f is None:
                esclusi.add(f"Articolo {item}: unita' di misura {um} non trovata nel gruppo UM, escluso.")
                continue
        else:
            f = Decimal(1)
        chiave = (r["magazzino"], item)
        totali[chiave] = totali.get(chiave, Decimal(0)) + r["quantita"] * f
    problemi.extend(sorted(esclusi))

    righe_ok = []
    for (whs, item), contato in sorted(totali.items()):
        _, nome, um_mag, *_ = dettagli[item.upper()]
        contato = contato.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP).normalize()
        giac = giacenze.get((item.upper(), whs), Decimal(0)).normalize()
        righe_ok.append({
            "magazzino": whs, "codice": item, "descrizione": nome, "um_mag": um_mag,
            "contato": contato, "giacenza": giac, "differenza": (contato - giac).normalize(),
        })
    return righe_ok, problemi


# ---------------------------------------------------------------------------
# Creazione del documento in SAP tramite DI API
# ---------------------------------------------------------------------------
class ErroreSAP(Exception):
    pass


_TIPI_SQL = {13: "2016", 14: "2017", 15: "2019", 16: "2022"}


def _testo_errore_com(e):
    try:
        info = e.excepinfo
        if info and info[2]:
            return str(info[2])
    except AttributeError:
        pass
    return str(e)


@contextmanager
def sessione_di(sap_cfg, versione_sql, utente, password):
    """Collegamento a SAP tramite DI API: 'with sessione_di(...) as (comp, constants):'."""
    try:
        import pythoncom
        from win32com.client import gencache, constants
    except ImportError:
        raise ErroreSAP("La scrittura in SAP funziona solo su Windows con pywin32 installato.")

    pythoncom.CoInitialize()
    try:
        try:
            comp = gencache.EnsureDispatch("SAPbobsCOM.Company")
        except Exception as e:
            raise ErroreSAP("DI API di SAP Business One non trovata su questo computer.\n"
                            "Usa il programma sul server SAP (dove e' installato il client SAP).\n"
                            f"Dettaglio: {_testo_errore_com(e)}")
        anno_sql = (sap_cfg.get("tipo_server_sql", "") or _TIPI_SQL.get(versione_sql, "")).strip()
        try:
            tipo = getattr(constants, f"dst_MSSQL{anno_sql}")
        except AttributeError:
            raise ErroreSAP(f"Versione di SQL Server non riconosciuta ({versione_sql}). "
                            "Indica in config.ini, sezione [sap], la riga: tipo_server_sql = 2019 (o 2016, 2017, 2022).")
        server = sap_cfg.get("server", "")
        comp.Server = server
        comp.CompanyDB = sap_cfg.get("database", "")
        comp.DbServerType = tipo
        comp.UserName = utente
        comp.Password = password
        if str(sap_cfg.get("autenticazione_windows", "no")).lower() in ("si", "sì", "yes", "true", "1"):
            comp.UseTrusted = True
        else:
            comp.UseTrusted = False
            comp.DbUserName = sap_cfg.get("utente", "")
            comp.DbPassword = sap_cfg.get("password", "")
        nome_server = server.split("\\")[0]
        licenze = sap_cfg.get("license_server", "") or f"{nome_server}:30000"
        try:
            comp.LicenseServer = licenze
        except Exception:
            pass
        sld = sap_cfg.get("sld_server", "")
        if sld:
            try:
                comp.SLDServer = sld
            except Exception:
                pass
        try:
            comp.language = constants.ln_Italian
        except Exception:
            pass

        if comp.Connect() != 0:
            raise ErroreSAP(f"Accesso a SAP non riuscito: {comp.GetLastErrorDescription()}")
        try:
            yield comp, constants
        finally:
            try:
                comp.Disconnect()
            except Exception:
                pass
    finally:
        pythoncom.CoUninitialize()


def crea_conteggio_sap(sap_cfg, versione_sql, utente, password, righe, data_conteggio, note=""):
    """
    sap_cfg: sezione [sap] di config.ini (server, database, ...)
    versione_sql: numero di versione principale di SQL Server (es. 15 = 2019)
    righe: righe_ok di prepara_conteggio
    Ritorna (DocEntry, DocNum).
    """
    with sessione_di(sap_cfg, versione_sql, utente, password) as (comp, constants):
        svc = comp.GetCompanyService().GetBusinessService(constants.InventoryCountingsService)
        doc = svc.GetDataInterface(constants.icsInventoryCounting)
        doc.CountDate = data_conteggio
        if note:
            doc.Remarks = note[:250]
        for r in righe:
            riga = doc.InventoryCountingLines.Add()
            riga.ItemCode = r["codice"]
            riga.WarehouseCode = r["magazzino"]
            riga.CountedQuantity = float(r["contato"])
            riga.Counted = constants.tYES
        try:
            risultato = svc.Add(doc)
        except Exception as e:
            raise ErroreSAP(f"SAP ha rifiutato il documento: {_testo_errore_com(e)}")
        return risultato.DocumentEntry, risultato.DocumentNumber


def versione_sql(conn):
    try:
        r = _righe(conn, "SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)")
        return int(r[0][0]) if r and r[0][0] is not None else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Ricezione diretta dai telefoni (Wi-Fi aziendale)
# ---------------------------------------------------------------------------
def codice_accesso(cartella):
    """Codice a 6 cifre che i telefoni devono conoscere: creato la prima volta, poi sempre lo stesso."""
    percorso = os.path.join(cartella, "inventario_codice.txt")
    try:
        with open(percorso, encoding="utf-8") as f:
            c = f.read().strip()
        if c:
            return c
    except OSError:
        pass
    c = f"{secrets.randbelow(900000) + 100000}"
    os.makedirs(cartella, exist_ok=True)
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(c)
    return c


def indirizzo_locale():
    """L'indirizzo di questo computer sulla rete aziendale."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))   # non spedisce nulla: serve solo a sapere quale scheda di rete si usa
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "?"


def parti_anagrafica(dati, n_art=8000, n_bc=20000):
    """L'anagrafica divisa in pezzi piccoli, piu' leggeri da scaricare per il telefono.
    Il pezzo 0 contiene tutto tranne articoli e codici a barre, piu' il numero dei pezzi."""
    parti = [{k: v for k, v in dati.items() if k not in ("articoli", "barcode")}]
    for nome, n in (("articoli", n_art), ("barcode", n_bc)):
        voci = list(dati.get(nome, {}).items())
        for i in range(0, len(voci), n):
            parti.append({nome: dict(voci[i:i + n])})
    parti[0]["parti"] = len(parti)
    return parti


def salva_ricevuto(cartella, dati):
    """Salva un conteggio arrivato da un telefono. Un nuovo invio della stessa sessione dallo stesso
    telefono sostituisce il precedente. Ritorna (percorso, numero di righe)."""
    _, righe = valida_conteggio(dati)
    dati = {k: v for k, v in dati.items() if not str(k).startswith("_")}
    dati["ricevuto"] = datetime.now().isoformat(timespec="seconds")
    nome = re.sub(r"[^A-Za-z0-9_-]", "", str(dati.get("id", "")))[:60] or datetime.now().strftime("%Y%m%d%H%M%S")
    dest = os.path.join(cartella, CARTELLA_RICEVUTI)
    os.makedirs(dest, exist_ok=True)
    percorso = os.path.join(dest, f"conteggio_{nome}.json")
    temp = percorso + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1)
    os.replace(temp, percorso)
    return percorso, len(righe)


def elenco_ricevuti(cartella):
    """[(percorso, dati)] dei conteggi ricevuti e non ancora usati, dal piu' recente."""
    dest = os.path.join(cartella, CARTELLA_RICEVUTI)
    try:
        nomi = [os.path.join(dest, n) for n in os.listdir(dest) if n.lower().endswith(".json")]
    except OSError:
        return []
    out = []
    for p in sorted(nomi, key=os.path.getmtime, reverse=True):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            out.append((p, d))
    return out


def archivia(cartella, percorso):
    """Toglie dall'elenco un conteggio ricevuto, conservandolo nella sottocartella 'usati'."""
    dest = os.path.join(cartella, CARTELLA_RICEVUTI)
    if os.path.dirname(os.path.abspath(percorso)) != os.path.abspath(dest):
        return False          # file aperto a mano da un'altra cartella: non si tocca
    arch = os.path.join(dest, NOME_ARCHIVIO)
    try:
        os.makedirs(arch, exist_ok=True)
        os.replace(percorso, os.path.join(arch, datetime.now().strftime("%Y%m%d_%H%M%S_") + os.path.basename(percorso)))
        return True
    except OSError:
        return False


def avvia_ricezione(cartella, codice, avvisa, porta=PORTA):
    """Avvia in sottofondo il servizio che riceve dai telefoni.
    avvisa(percorso) viene chiamata (da un altro thread) a ogni conteggio ricevuto.
    Ritorna il server (server.shutdown() per fermarlo)."""
    cache = {"mtime": None, "parti": None}
    blocco = threading.Lock()
    sbagliati = {}     # indirizzo -> (numero di codici sbagliati, fino a quando e' sospeso)
    MAX_SBAGLIATI, SOSPENSIONE = 5, 60

    def anagrafica():
        percorso = os.path.join(cartella, NOME_ANAGRAFICA)
        with blocco:
            m = os.path.getmtime(percorso)     # OSError se non e' mai stata creata
            if cache["mtime"] != m:
                with open(percorso, encoding="utf-8") as f:
                    cache["parti"] = [json.dumps(p, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                                      for p in parti_anagrafica(json.load(f))]
                cache["mtime"] = m
            return cache["parti"]

    class Gestore(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30          # chiude i collegamenti rimasti aperti senza far nulla

        def log_message(self, *a):
            pass

        def _rispondi(self, stato, corpo):
            if not isinstance(corpo, bytes):
                corpo = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
            self.send_response(stato)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Codice")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)

        def _autorizzato(self):
            # dopo 5 codici sbagliati di fila l'indirizzo aspetta un minuto: provare tutti i codici diventa impraticabile
            ip, ora = self.client_address[0], time.monotonic()
            with blocco:
                n, fino = sbagliati.get(ip, (0, 0))
                if ora < fino:
                    sospeso = True
                else:
                    sospeso = False
                    if secrets.compare_digest((self.headers.get("X-Codice") or "").strip().encode(), codice.encode()):
                        sbagliati.pop(ip, None)
                        return True
                    n += 1
                    sbagliati[ip] = (0, ora + SOSPENSIONE) if n >= MAX_SBAGLIATI else (n, 0)
                    if len(sbagliati) > 5000:
                        sbagliati.clear()
            self.close_connection = True
            if sospeso:
                self._rispondi(429, {"errore": "Troppi codici sbagliati: aspetta un minuto e riprova."})
            else:
                self._rispondi(403, {"errore": "Codice di accesso sbagliato: e' scritto nella scheda Inventario del programma Etichette SAP."})
            return False

        def do_OPTIONS(self):
            self._rispondi(204, b"")

        def do_GET(self):
            if not self._autorizzato():
                return
            percorso = self.path.split("?")[0].rstrip("/")
            if percorso == "/ping":
                return self._rispondi(200, {"ok": True, "programma": "inventario-sap"})
            m = re.fullmatch(r"/anagrafica/(\d+)", percorso)
            if m:
                try:
                    parti = anagrafica()
                except (OSError, ValueError):
                    return self._rispondi(404, {"errore": "Sul server non c'e' ancora l'anagrafica: premi 'Crea file anagrafica' nella scheda Inventario del programma Etichette SAP."})
                i = int(m.group(1))
                if i < len(parti):
                    return self._rispondi(200, parti[i])
            self._rispondi(404, {"errore": "Richiesta non riconosciuta."})

        def do_POST(self):
            try:
                lung = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                lung = 0
            corpo = self.rfile.read(lung) if 0 < lung <= 20_000_000 else b""
            if lung > 20_000_000:
                self.close_connection = True
            if not self._autorizzato():
                return
            if self.path.split("?")[0].rstrip("/") != "/conteggio":
                return self._rispondi(404, {"errore": "Richiesta non riconosciuta."})
            try:
                dati = json.loads(corpo.decode("utf-8"))
                percorso, n = salva_ricevuto(cartella, dati)
            except ErroreConteggio as e:
                return self._rispondi(400, {"errore": str(e)})
            except (ValueError, OSError) as e:
                return self._rispondi(400, {"errore": f"Conteggio non valido: {e}"})
            avvisa(percorso)
            self._rispondi(200, {"ok": True, "righe": n})

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass   # collegamenti interrotti dal telefono: niente errori a video

    server = Server(("0.0.0.0", porta), Gestore)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
