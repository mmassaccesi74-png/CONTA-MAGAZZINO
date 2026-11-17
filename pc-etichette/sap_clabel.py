#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sap_clabel.py - Etichette prodotto da SAP Business One (SQL Server).

Stampa direttamente sulla stampante termica (Toshiba TEC B-SA4T o altra con
driver Windows), oppure esporta un Excel per Clabel trade (--excel).

Uso:
  python sap_clabel.py entrata 1234            # etichette da Entrata merci n. 1234
  python sap_clabel.py entrata 1234 --una-per-riga   # 1 etichetta per riga documento
  python sap_clabel.py fattura 1234            # etichette da Fattura fornitore n. 1234
  python sap_clabel.py fattura-cliente 1234    # etichette da Fattura cliente n. 1234
  python sap_clabel.py cerca "vite inox"       # ricerca articoli e selezione interattiva
  python sap_clabel.py articoli A001:5 A002:3  # codici articolo con quantita'
  python sap_clabel.py test                    # verifica connessione a SAP
  python sap_clabel.py stampanti               # elenca le stampanti installate

Opzioni per entrata / fattura / fattura-cliente / cerca / articoli:
  --anteprima   crea un PDF di prova invece di stampare
  --excel       crea il file Excel per Clabel trade invece di stampare
  --si          stampa senza chiedere conferma

Configurazione: config.ini nella stessa cartella dello script.
"""

import argparse
import configparser
import os
import re
import sys
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

if getattr(sys, "frozen", False):          # programma .exe creato con PyInstaller
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.ini")

COLONNE = ["Codice", "Descrizione", "EAN", "Prezzo"]


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------
def carica_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            f"File di configurazione non trovato: {CONFIG_PATH}\n"
            "Copia config.ini.example in config.ini e inserisci i dati di SAP."
        )
    cfg = configparser.ConfigParser()
    # utf-8-sig: accetta anche il BOM che Blocco note aggiunge salvando in UTF-8;
    # se il file e' stato salvato in ANSI, si riprova con la codifica di Windows
    try:
        cfg.read(CONFIG_PATH, encoding="utf-8-sig")
    except UnicodeDecodeError:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH, encoding="cp1252")
    except configparser.Error as e:
        sys.exit(f"Errore nel file config.ini:\n{e}")
    return cfg


def connetti(cfg):
    conn = _apri_connessione(cfg)
    # Se il listino ha "prezzi lordi" (IVA inclusa) non va aggiunta l'IVA
    lordo = "no"
    try:
        cur = conn.cursor()
        cur.execute("SELECT IsGrossPrc FROM OPLN WHERE ListNum = ?", [listino(cfg)])
        r = cur.fetchone()
        lordo = "si" if r and r[0] == "Y" else "no"
    except Exception:
        pass
    if not cfg.has_section("etichette"):
        cfg.add_section("etichette")
    cfg["etichette"]["_listino_lordo"] = lordo
    return conn


class _CursoreTds:
    """Adatta pytds alle query scritte con '?' (stile pyodbc)."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        if not isinstance(params, (list, tuple)):
            params = (params,)
        return self._cur.execute(sql.replace("?", "%s"), tuple(params))

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _ConnessioneTds:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _CursoreTds(self._conn.cursor())


def _apri_connessione(cfg):
    s = cfg["sap"]
    windows = s.get("autenticazione_windows", "no").lower() in ("si", "sì", "yes", "true", "1")
    try:
        import pyodbc
    except ImportError:
        pyodbc = None

    if pyodbc is None:
        # Driver in puro Python: non richiede compilazione ne' driver ODBC
        try:
            import pytds
        except ImportError:
            sys.exit("Manca il collegamento a SQL Server. Installa con:  pip install python-tds")
        try:
            opzioni = dict(dsn=s["server"], database=s["database"], login_timeout=10, autocommit=True)
            if windows:
                opzioni["use_sso"] = True
            else:
                opzioni.update(user=s["utente"], password=s["password"])
            return _ConnessioneTds(pytds.connect(**opzioni))
        except Exception as e:
            sys.exit(f"Connessione a SAP non riuscita ({s['server']}):\n{type(e).__name__}: {e}\n"
                     "Controlla server, database, utente e password in config.ini.")

    driver = s.get("driver", "ODBC Driver 17 for SQL Server")
    driver = s.get("driver", "ODBC Driver 17 for SQL Server")
    parti = [
        f"DRIVER={{{driver}}}",
        f"SERVER={s['server']}",
        f"DATABASE={s['database']}",
    ]
    if windows:
        parti.append("Trusted_Connection=yes")
    else:
        parti.append(f"UID={s['utente']}")
        parti.append(f"PWD={s['password']}")
    # Driver 18 richiede crittografia di default: accetta il certificato del server
    parti.append("TrustServerCertificate=yes")
    try:
        return pyodbc.connect(";".join(parti), timeout=10)
    except pyodbc.Error as e:
        sys.exit(f"Connessione a SAP non riuscita:\n{e}")


# --------------------------------------------------------------------------
# Query SAP B1
# --------------------------------------------------------------------------
# EAN: prima OITM.CodeBars, altrimenti il primo codice in OBCD (codici a barre multipli)
SQL_EAN = """
COALESCE(NULLIF(LTRIM(RTRIM(T0.CodeBars)), ''),
         (SELECT TOP 1 B.BcdCode FROM OBCD B WHERE B.ItemCode = T0.ItemCode ORDER BY B.BcdEntry))
"""

# Aliquota IVA: gruppo IVA vendite dell'articolo (OITM.VatGourpSa -> OVTG.Rate)
SQL_IVA = "V.Rate AS Iva"
SQL_JOIN_IVA = "LEFT JOIN OVTG V ON V.Code = T0.VatGourpSa"

SQL_ARTICOLI = f"""
SELECT T0.ItemCode, T0.ItemName, {SQL_EAN} AS EAN, P.Price, P.Currency, {SQL_IVA}
FROM OITM T0
LEFT JOIN ITM1 P ON P.ItemCode = T0.ItemCode AND P.PriceList = ?
{SQL_JOIN_IVA}
WHERE T0.ItemCode IN ({{segnaposto}})
"""

SQL_CERCA = f"""
SELECT TOP {{limite}} T0.ItemCode, T0.ItemName, {SQL_EAN} AS EAN, P.Price, P.Currency, {SQL_IVA}
FROM OITM T0
LEFT JOIN ITM1 P ON P.ItemCode = T0.ItemCode AND P.PriceList = ?
{SQL_JOIN_IVA}
WHERE T0.frozenFor = 'N'
  AND (T0.ItemCode LIKE ? OR T0.ItemName LIKE ? OR T0.CodeBars LIKE ?
       OR EXISTS (SELECT 1 FROM OBCD B2 WHERE B2.ItemCode = T0.ItemCode AND B2.BcdCode LIKE ?))
ORDER BY T0.ItemCode
"""

# Documenti da cui stampare: comando -> (testata, righe, nome, intestatario)
DOCUMENTI = {
    "entrata": ("OPDN", "PDN1", "Entrata merci", "fornitore"),
    "fattura": ("OPCH", "PCH1", "Fattura fornitore", "fornitore"),
    "fattura-cliente": ("OINV", "INV1", "Fattura cliente", "cliente"),
}

# Quantita' in unita' di magazzino: Quantity * NumPerMsr (pezzi per unita' di acquisto/vendita).
# Solo articoli di magazzino: spese di trasporto, servizi ecc. non hanno etichetta.
SQL_RIGHE_DOC = f"""
SELECT D.ItemCode, T0.ItemName, {SQL_EAN} AS EAN, P.Price, P.Currency,
       D.Quantity * ISNULL(NULLIF(D.NumPerMsr, 0), 1) AS QtaMagazzino,
       D.LineNum, {SQL_IVA}
FROM {{testata}} H
JOIN {{righe}} D ON D.DocEntry = H.DocEntry
JOIN OITM T0 ON T0.ItemCode = D.ItemCode
LEFT JOIN ITM1 P ON P.ItemCode = D.ItemCode AND P.PriceList = ?
{SQL_JOIN_IVA}
WHERE H.DocEntry = ? AND T0.InvntItem = 'Y'
ORDER BY D.LineNum
"""

# La numerazione riparte ogni anno: lo stesso DocNum esiste in piu' anni.
# Si cercano le testate (escluse quelle annullate) e si sceglie la piu' recente.
SQL_TESTATA_DOC = """
SELECT H.DocEntry, H.DocNum, H.DocDate, H.CardName, YEAR(H.DocDate) AS Anno
FROM {testata} H
WHERE H.DocNum = ? AND H.CANCELED = 'N' {filtri}
ORDER BY H.DocDate DESC, H.DocEntry DESC
"""


def listino(cfg):
    return cfg.getint("etichette", "listino", fallback=1)


def riga_da_record(rec):
    codice, descr, ean, prezzo, valuta = rec[0], rec[1], rec[2], rec[3], rec[4]
    return {
        "codice": codice,
        "descrizione": descr or "",
        "ean": (ean or "").strip(),
        "prezzo": prezzo,
        "valuta": valuta or "",
        "iva": rec[-1],          # aliquota % dal gruppo IVA vendite (None se non impostato)
    }


def articoli_per_codice(conn, cfg, codici):
    if not codici:
        return {}
    segnaposto = ",".join("?" for _ in codici)
    cur = conn.cursor()
    cur.execute(SQL_ARTICOLI.format(segnaposto=segnaposto), [listino(cfg), *codici])
    return {r[0].upper(): riga_da_record(r) for r in cur.fetchall()}


def cerca_articoli(conn, cfg, testo, limite=50):
    like = f"%{testo}%"
    cur = conn.cursor()
    cur.execute(SQL_CERCA.format(limite=int(limite)), [listino(cfg), like, like, like, like])
    return [riga_da_record(r) for r in cur.fetchall()]


# Tutti i codici a barre dell'articolo, con l'unita' di misura del gruppo UM a cui sono associati
SQL_BARCODE = """
SELECT B.ItemCode, B.BcdCode,
       CASE WHEN B.UomEntry = -1 THEN '' ELSE ISNULL(U.UomCode, '') END,
       CASE WHEN B.UomEntry = -1 THEN '' ELSE ISNULL(U.UomName, '') END
FROM OBCD B
LEFT JOIN OUOM U ON U.UomEntry = B.UomEntry
WHERE B.ItemCode IN ({segnaposto})
ORDER BY B.ItemCode, B.BcdEntry
"""


# Prezzo per ogni UM del gruppo UM dell'articolo, come lo calcola SAP B1:
# 1) prezzo specifico per UM nel listino (ITM9), se presente;
# 2) altrimenti prezzo di listino (ITM1, riferito alla sua UM di prezzo: ITM1.UomEntry,
#    o l'UM di magazzino) convertito con i rapporti del gruppo UM (UGP1.BaseQty / AltQty).
SQL_PREZZI_UM = """
SELECT T0.ItemCode, U.UomCode, P9.Price, P1.Price, G.BaseQty, G.AltQty,
       (SELECT G2.BaseQty / NULLIF(G2.AltQty, 0) FROM UGP1 G2
         WHERE G2.UgpEntry = T0.UgpEntry
           AND G2.UomEntry = ISNULL(NULLIF(P1.UomEntry, 0), T0.IUoMEntry)) AS FattorePrezzo
FROM OITM T0
JOIN UGP1 G ON G.UgpEntry = T0.UgpEntry
JOIN OUOM U ON U.UomEntry = G.UomEntry
LEFT JOIN ITM1 P1 ON P1.ItemCode = T0.ItemCode AND P1.PriceList = ?
LEFT JOIN ITM9 P9 ON P9.ItemCode = T0.ItemCode AND P9.PriceList = ? AND P9.UomEntry = G.UomEntry
WHERE T0.ItemCode IN ({segnaposto})
"""


def prezzi_um(conn, cfg, articoli):
    """Aggiunge a ogni articolo 'prezzi_um' = {codice UM: prezzo netto di listino}.
    Se la versione di SAP non ha le colonne necessarie, i prezzi restano quelli di listino."""
    codici = sorted({a["codice"] for a in articoli if "prezzi_um" not in a})
    trovati = {}
    try:
        cur = conn.cursor()
        for i in range(0, len(codici), 500):
            blocco = codici[i:i + 500]
            cur.execute(SQL_PREZZI_UM.format(segnaposto=",".join("?" for _ in blocco)),
                        [listino(cfg), listino(cfg), *blocco])
            for item, um, prezzo_um, prezzo, base, alt, fattore_prezzo in cur.fetchall():
                if prezzo_um is not None and Decimal(str(prezzo_um)) > 0:
                    valore = Decimal(str(prezzo_um))
                elif prezzo is not None and base and alt:
                    fattore = Decimal(str(base)) / Decimal(str(alt))
                    rif = Decimal(str(fattore_prezzo)) if fattore_prezzo else Decimal(1)
                    valore = Decimal(str(prezzo)) * fattore / (rif or Decimal(1))
                else:
                    continue
                trovati.setdefault(item.upper(), {})[um] = valore
    except Exception:
        trovati = {}
    for a in articoli:
        if "prezzi_um" not in a:
            a["prezzi_um"] = trovati.get(a["codice"].upper(), {})
    return articoli


def barcode_articoli(conn, articoli):
    """Aggiunge a ogni articolo la chiave 'barcodes': lista di (codice, UM, nome UM).
    Il codice a barre gia' scelto (art['ean']) resta quello proposto."""
    codici = sorted({a["codice"] for a in articoli if "barcodes" not in a})
    trovati = {}
    cur = conn.cursor()
    for i in range(0, len(codici), 500):          # SQL Server: max ~2100 parametri per query
        blocco = codici[i:i + 500]
        cur.execute(SQL_BARCODE.format(segnaposto=",".join("?" for _ in blocco)), blocco)
        for item, bcd, um, nome_um in cur.fetchall():
            bcd = (bcd or "").strip()
            if bcd:
                trovati.setdefault(item.upper(), []).append((bcd, um or "", nome_um or ""))
    for a in articoli:
        if "barcodes" in a:
            continue
        elenco = list(trovati.get(a["codice"].upper(), []))
        # il codice dell'anagrafica (OITM.CodeBars) potrebbe non comparire in OBCD
        if a["ean"] and all(b[0] != a["ean"] for b in elenco):
            elenco.insert(0, (a["ean"], "", ""))
        a["barcodes"] = elenco
    return articoli


def trova_documento(conn, tipo, docnum, anno=None, serie=None):
    """Ritorna (testata scelta, numero di documenti trovati con quel numero)."""
    filtri, params = "", [docnum]
    if anno:
        filtri += " AND YEAR(H.DocDate) = ?"
        params.append(anno)
    if serie:
        filtri += " AND H.Series = ?"
        params.append(serie)
    cur = conn.cursor()
    cur.execute(SQL_TESTATA_DOC.format(testata=DOCUMENTI[tipo][0], filtri=filtri), params)
    trovate = cur.fetchall()
    if not trovate:
        return None, 0
    t = trovate[0]
    return {"docentry": t[0], "docnum": t[1], "data": t[2], "fornitore": t[3] or "", "anno": t[4]}, len(trovate)


def righe_documento(conn, cfg, tipo, docentry):
    testata, righe_tab = DOCUMENTI[tipo][:2]
    cur = conn.cursor()
    cur.execute(SQL_RIGHE_DOC.format(testata=testata, righe=righe_tab), [listino(cfg), docentry])
    righe = []
    for r in cur.fetchall():
        art = riga_da_record(r)
        qta = Decimal(str(r[5] or 0))
        # quantita' frazionarie (kg, metri...) -> arrotonda per eccesso a etichette intere
        art["qta"] = int(qta.to_integral_value(rounding="ROUND_CEILING")) if qta > 0 else 0
        righe.append(art)
    return righe


# --------------------------------------------------------------------------
# Formattazione ed export Excel
# --------------------------------------------------------------------------
def aliquota(art, cfg):
    """Aliquota da applicare: 0 se il listino e' gia' lordo, altrimenti quella dell'articolo
    (o iva_predefinita del config se l'articolo non ha un gruppo IVA vendite)."""
    e = cfg["etichette"] if cfg.has_section("etichette") else {}
    if e.get("_listino_lordo") == "si":
        return Decimal(0)
    if str(e.get("iva_da_articolo", "si")).lower() in ("si", "sì", "yes", "1", "true") and art.get("iva") is not None:
        return Decimal(str(art["iva"]))
    return Decimal(str(e.get("iva_predefinita", "22")).replace(",", "."))


def perc(valore):
    v = Decimal(str(valore)).normalize()
    return f"{v:f}".replace(".", ",") + "%"


def prezzo_lordo(art, cfg):
    """Prezzo di vendita IVA inclusa, non arrotondato (None se l'articolo non ha prezzo)."""
    prezzo = art.get("prezzo")
    if prezzo is None:
        return None
    iva = aliquota(art, cfg)
    valore = Decimal(str(prezzo))
    if iva > 0:
        valore = valore * (Decimal(1) + iva / Decimal(100))
    return valore


def formatta_importo(valore, cfg):
    e = cfg["etichette"] if cfg.has_section("etichette") else {}
    valore = Decimal(str(valore)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    testo = f"{valore:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    simbolo = e.get("simbolo_valuta", "€")
    if not simbolo:
        return testo
    return f"{simbolo} {testo}" if e.get("simbolo_prima", "si").lower() in ("si", "sì", "yes", "1", "true") else f"{testo} {simbolo}"


def formatta_prezzo(art, cfg):
    prezzo = art.get("prezzo")
    if prezzo is None:
        return ""
    e = cfg["etichette"] if cfg.has_section("etichette") else {}
    valore = prezzo_lordo(art, cfg)
    valore = valore.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    testo = f"{valore:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    simbolo = e.get("simbolo_valuta", "€")
    if not simbolo:
        return testo
    return f"{simbolo} {testo}" if e.get("simbolo_prima", "si").lower() in ("si", "sì", "yes", "1", "true") else f"{testo} {simbolo}"


def espandi(articoli_con_qta, cfg):
    """Trasforma [(articolo, qta)] in righe Excel, una per etichetta."""
    righe = []
    for art, qta in articoli_con_qta:
        valori = [art["codice"], art["descrizione"], art["ean"], formatta_prezzo(art, cfg)]
        righe.extend([valori] * max(0, qta))
    return righe


def scrivi_excel(righe, cfg, nome_base):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        sys.exit("Manca il modulo openpyxl. Installa con:  pip install -r requirements.txt")

    if not righe:
        print("Nessuna etichetta da esportare.")
        return []

    e = cfg["etichette"] if cfg.has_section("etichette") else {}
    cartella = e.get("cartella_output", "") or os.path.join(SCRIPT_DIR, "export")
    os.makedirs(cartella, exist_ok=True)
    max_righe = int(e.get("max_righe_per_file", "0") or 0)
    blocchi = [righe] if max_righe <= 0 else [righe[i:i + max_righe] for i in range(0, len(righe), max_righe)]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    creati = []
    for n, blocco in enumerate(blocchi, 1):
        wb = Workbook()
        ws = wb.active
        ws.title = "Etichette"
        ws.append(COLONNE)
        for c in ws[1]:
            c.font = Font(bold=True)
        for valori in blocco:
            ws.append(valori)
        # Tutte le celle come testo: l'EAN non perde gli zeri iniziali
        # e non diventa notazione scientifica (8,0123E+12)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.number_format = "@"
                if c.value is not None:
                    c.value = str(c.value)
        for col, larghezza in zip("ABCD", (18, 45, 16, 12)):
            ws.column_dimensions[col].width = larghezza

        suffisso = f"_parte{n}" if len(blocchi) > 1 else ""
        percorso = os.path.join(cartella, f"{nome_base}_{timestamp}{suffisso}.xlsx")
        wb.save(percorso)
        creati.append(percorso)
    return creati


def impostazioni_etichetta(cfg):
    s = cfg["stampante"] if cfg.has_section("stampante") else {}
    si = lambda k, d: str(s.get(k, d)).strip().lower() in ("si", "sì", "yes", "1", "true")
    num = lambda k, d: float(str(s.get(k, d)).replace(",", "."))
    return {
        "nome": s.get("nome", "").strip(),
        "larghezza_mm": num("larghezza_mm", "50"),
        "altezza_mm": num("altezza_mm", "30"),
        "dpi": int(num("dpi", "203")),
        "margine_mm": num("margine_mm", "1.5"),
        "mostra_prezzo": si("mostra_prezzo", "si"),
        "mostra_codice": si("mostra_codice", "si"),
        "barcode_codice_se_manca_ean": si("barcode_codice_se_manca_ean", "si"),
        "conferma_sopra": int(num("conferma_sopra", "20")),
        "centra": si("centra_su_pagina", "si"),
        "sposta_x_mm": num("sposta_x_mm", "0"),
        "sposta_y_mm": num("sposta_y_mm", "0"),
    }


def prepara_lavori(articoli_con_qta, cfg, imp):
    """Un'immagine per articolo/codice a barre + numero di copie (ripetizioni unite)."""
    import etichetta
    lavori, indice = [], {}
    for art, qta in articoli_con_qta:
        if qta <= 0:
            continue
        chiave = (art["codice"], art["ean"])
        if chiave in indice:
            img, q = lavori[indice[chiave]]
            lavori[indice[chiave]] = (img, q + qta)
            continue
        dati = {
            "codice": art["codice"],
            "descrizione": art["descrizione"],
            "ean": art["ean"],
            "prezzo_testo": formatta_prezzo(art, cfg),
        }
        indice[chiave] = len(lavori)
        lavori.append((etichetta.disegna_etichetta(dati, imp), qta))
    return lavori


# --------------------------------------------------------------------------
# Cartellini prezzo da scaffale
# --------------------------------------------------------------------------
def impostazioni_cartellino(cfg):
    """Sezione [cartellino] di config.ini (rotolo per i cartellini sulla stessa Toshiba)."""
    c = cfg["cartellino"] if cfg.has_section("cartellino") else {}
    s = cfg["stampante"] if cfg.has_section("stampante") else {}
    num = lambda k, d: float(str(c.get(k, d)).replace(",", "."))
    return {
        "stampante": c.get("stampante", "").strip(),
        "larghezza_mm": num("larghezza_mm", "65"),
        "altezza_mm": num("altezza_mm", "40"),
        "dpi": int(float(str(c.get("dpi", s.get("dpi", "203"))).replace(",", "."))),
        "margine_mm": num("margine_mm", "1.5"),
        "nome_negozio": c.get("nome_negozio", "").strip(),
        "prezzo_unitario": c.get("prezzo_unitario", "auto").strip().lower(),
        "campo_quantita": c.get("campo_quantita", "").strip(),
        "campo_unita": c.get("campo_unita", "").strip(),
        "barcode_codice_se_manca_ean": str(s.get("barcode_codice_se_manca_ean", "si")).strip().lower()
                                       in ("si", "sì", "yes", "1", "true"),
        "centra": str(s.get("centra_su_pagina", "si")).strip().lower() in ("si", "sì", "yes", "1", "true"),
        "sposta_x_mm": num("sposta_x_mm", "0"),
        "sposta_y_mm": num("sposta_y_mm", "0"),
    }


# Contenuto netto dai dati di vendita dell'anagrafica (scheda Dati di vendita):
# peso (SWeight1, unita' in OWGT) e volume (SVolume, unita' di lunghezza al cubo in OLGT),
# riferiti all'unita' di vendita (NumInSale articoli).
SQL_CONTENUTO = """
SELECT T0.ItemCode, T0.SWeight1, W.WightInMG, T0.SVolume, L.SizeInMM, T0.NumInSale
FROM OITM T0
LEFT JOIN OWGT W ON W.UnitCode = T0.SWght1Unit
LEFT JOIN OLGT L ON L.UnitCode = T0.SVolUnit
WHERE T0.ItemCode IN ({segnaposto})
"""

SQL_CONTENUTO_CAMPI = "SELECT T0.ItemCode, T0.{quantita}, T0.{unita} FROM OITM T0 WHERE T0.ItemCode IN ({segnaposto})"

# unita' scritte nei campi utente -> (unita' del prezzo, fattore verso di essa)
_UNITA_CAMPO = {
    "kg": ("kg", 1), "g": ("kg", Decimal("0.001")), "gr": ("kg", Decimal("0.001")), "hg": ("kg", Decimal("0.1")),
    "l": ("litro", 1), "lt": ("litro", 1), "litri": ("litro", 1), "ml": ("litro", Decimal("0.001")),
    "cl": ("litro", Decimal("0.01")), "dl": ("litro", Decimal("0.1")),
    "m": ("metro", 1), "mt": ("metro", 1), "cm": ("metro", Decimal("0.01")),
    "pz": ("pezzo", 1), "n": ("pezzo", 1), "nr": ("pezzo", 1),
}


def contenuti(conn, articoli, imp):
    """Aggiunge a ogni articolo 'contenuto' = (quantita', unita') per il prezzo unitario, se si ricava."""
    modo = imp["prezzo_unitario"]
    codici = sorted({a["codice"] for a in articoli})
    trovati = {}
    if modo not in ("no", "") and codici:
        cur = conn.cursor()
        try:
            for i in range(0, len(codici), 500):
                blocco = codici[i:i + 500]
                seg = ",".join("?" for _ in blocco)
                if imp["campo_quantita"] and imp["campo_unita"]:
                    nomi = [imp["campo_quantita"], imp["campo_unita"]]
                    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in nomi):
                        raise ValueError("nomi dei campi non validi")
                    cur.execute(SQL_CONTENUTO_CAMPI.format(quantita=nomi[0], unita=nomi[1], segnaposto=seg), blocco)
                    for item, q, um in cur.fetchall():
                        unita = _UNITA_CAMPO.get(str(um or "").strip().lower().rstrip("."))
                        if q and unita and Decimal(str(q)) > 0:
                            trovati[item.upper()] = (Decimal(str(q)) * Decimal(str(unita[1])), unita[0])
                    continue
                cur.execute(SQL_CONTENUTO.format(segnaposto=seg), blocco)
                for item, peso, mg, volume, mm, per_vendita in cur.fetchall():
                    n = Decimal(str(per_vendita)) if per_vendita and Decimal(str(per_vendita)) > 0 else Decimal(1)
                    litri = kg = None
                    if volume and mm and Decimal(str(volume)) > 0:
                        litri = Decimal(str(volume)) * (Decimal(str(mm)) / 100) ** 3 / n
                    if peso and mg and Decimal(str(peso)) > 0:
                        kg = Decimal(str(peso)) * Decimal(str(mg)) / Decimal(1000000) / n
                    if modo == "peso":
                        litri = None
                    elif modo == "volume":
                        kg = None
                    if litri:
                        trovati[item.upper()] = (litri, "litro")
                    elif kg:
                        trovati[item.upper()] = (kg, "kg")
        except Exception:
            trovati = {}       # campi assenti in questa versione di SAP: niente prezzo unitario
    for a in articoli:
        a["contenuto"] = trovati.get(a["codice"].upper())
    return articoli


def testo_unitario(art, cfg):
    """'€ 12,50 al kg' dal prezzo e dal contenuto netto; vuoto se non si ricava o non serve."""
    c = art.get("contenuto")
    lordo = prezzo_lordo(art, cfg)
    if not c or lordo is None or art.get("um_etichetta"):
        return ""          # con un codice a barre di un'altra UM (es. confezione) il contenuto non vale
    quantita, unita = c
    if quantita <= 0 or quantita == 1:
        return ""          # il prezzo e' gia' quello di 1 kg / 1 litro / 1 pezzo
    return f"{formatta_importo(lordo / quantita, cfg)} al {unita}"


def prepara_cartellini(articoli_con_qta, cfg, imp, negozio=""):
    """Un cartellino per articolo/codice a barre (le copie restano quelle indicate)."""
    import etichetta
    lavori, indice = [], {}
    for art, qta in articoli_con_qta:
        if qta <= 0:
            continue
        chiave = (art["codice"], art["ean"])
        if chiave in indice:
            img, q = lavori[indice[chiave]]
            lavori[indice[chiave]] = (img, q + qta)
            continue
        dati = {
            "codice": art["codice"],
            "descrizione": art["descrizione"],
            "ean": art["ean"],
            "prezzo_testo": formatta_prezzo(art, cfg),
            "unitario_testo": testo_unitario(art, cfg),
            "negozio": imp["nome_negozio"] or negozio,
        }
        indice[chiave] = len(lavori)
        lavori.append((etichetta.disegna_cartellino(dati, imp), qta))
    return lavori


def esporta(articoli_con_qta, cfg, args, nome_base):
    riepilogo(articoli_con_qta, cfg)
    totale = sum(max(0, q) for _, q in articoli_con_qta)
    if totale == 0:
        print("Nessuna etichetta da stampare.")
        return

    if getattr(args, "excel", False):
        file_creati = scrivi_excel(espandi(articoli_con_qta, cfg), cfg, nome_base)
        for f in file_creati:
            print(f"File creato: {f}")
        print("\nImporta il file in Clabel trade (Stampa in serie > Importa Excel).")
        return

    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Manca il modulo Pillow. Installa con:  pip install -r requirements.txt")
    import etichetta

    imp = impostazioni_etichetta(cfg)
    print("Preparo le etichette...", flush=True)
    lavori = prepara_lavori(articoli_con_qta, cfg, imp)

    if getattr(args, "anteprima", False):
        e = cfg["etichette"] if cfg.has_section("etichette") else {}
        cartella = e.get("cartella_output", "") or os.path.join(SCRIPT_DIR, "export")
        percorso = os.path.join(cartella, f"anteprima_{nome_base}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        etichetta.salva_anteprima(lavori, percorso, imp["dpi"])
        print(f"\nAnteprima (una pagina per articolo): {percorso}")
        if sys.platform == "win32":
            try:
                os.startfile(percorso)
            except OSError:
                pass
        return

    print("Cerco la stampante...", flush=True)
    nome = etichetta.scegli_stampante(args.stampante or imp["nome"])
    print(f"Stampante: {nome}", flush=True)
    if totale > imp["conferma_sopra"] and not args.si:
        risposta = input(f"\nStampare {totale} etichette su '{nome}'? [S/n] ").strip().lower()
        if risposta not in ("", "s", "si", "sì", "y"):
            print("Stampa annullata.")
            return
    print("Invio alla stampante...", flush=True)
    try:
        dpi_stampante, pag_w, pag_h = etichetta.stampa(
            lavori, nome, imp["dpi"], f"Etichette SAP - {nome_base}",
            centra=imp["centra"], sposta_x_mm=imp["sposta_x_mm"], sposta_y_mm=imp["sposta_y_mm"])
    except Exception as e:
        sys.exit(f"Errore di stampa su '{nome}':\n{type(e).__name__}: {e}")
    print(f"\nInviate {totale} etichette a '{nome}'.")
    print(f"Formato pagina nel driver: {pag_w:.1f} x {pag_h:.1f} mm "
          f"(etichetta in config.ini: {imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm)")
    # la larghezza puo' essere maggiore (l'etichetta viene centrata); l'altezza deve coincidere,
    # altrimenti il rotolo avanza troppo (etichette vuote) o troppo poco
    if pag_w < imp["larghezza_mm"] - 1 or abs(pag_h - imp["altezza_mm"]) > 1:
        print("  Attenzione: il formato nel driver e' diverso dall'etichetta. Imposta nel driver "
              f"{imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm (Preferenze stampa > Formato carta).")
    if dpi_stampante != imp["dpi"]:
        print(f"Nota: il driver lavora a {dpi_stampante} dpi, in config.ini dpi = {imp['dpi']}. "
              f"Imposta dpi = {dpi_stampante} per un codice a barre piu' nitido.")


def riepilogo(articoli_con_qta, cfg=None):
    print()
    print(f"{'Codice':<16} {'Descrizione':<30} {'EAN':<14} {'IVA':>5} {'Prezzo':>11} {'Etich.':>6}")
    print("-" * 87)
    senza_ean = 0
    for art, qta in articoli_con_qta:
        if not art["ean"]:
            senza_ean += 1
        iva = perc(aliquota(art, cfg)) if cfg is not None else ""
        prezzo = formatta_prezzo(art, cfg) if cfg is not None else ""
        print(f"{art['codice'][:16]:<16} {art['descrizione'][:30]:<30} {(art['ean'] or '-')[:14]:<14} "
              f"{iva:>5} {prezzo or '-':>11} {qta:>6}")
    totale = sum(q for _, q in articoli_con_qta)
    print("-" * 87)
    print(f"Totale etichette: {totale}")
    if senza_ean:
        print(f"Attenzione: {senza_ean} articoli senza codice a barre in SAP.")


# --------------------------------------------------------------------------
# Comandi
# --------------------------------------------------------------------------
def cmd_test(args, cfg):
    conn = connetti(cfg)
    cur = conn.cursor()
    cur.execute("SELECT CompnyName FROM OADM")
    azienda = cur.fetchone()[0]
    cur.execute("SELECT ListName FROM OPLN WHERE ListNum = ?", listino(cfg))
    r = cur.fetchone()
    print(f"Connessione OK - Azienda: {azienda}")
    print(f"Listino prezzi {listino(cfg)}: {r[0] if r else 'NON TROVATO, controlla config.ini'}")
    if cfg["etichette"].get("_listino_lordo") == "si":
        print("  Il listino e' a prezzi lordi (IVA inclusa): l'IVA non viene aggiunta.")
    else:
        print("  Il listino e' a prezzi netti: a ogni articolo si aggiunge l'IVA del suo gruppo IVA vendite.")
        cur.execute("""
            SELECT ISNULL(T0.VatGourpSa, '(nessuno)'), V.Rate, COUNT(*)
            FROM OITM T0 LEFT JOIN OVTG V ON V.Code = T0.VatGourpSa
            WHERE T0.SellItem = 'Y' AND T0.frozenFor = 'N'
            GROUP BY T0.VatGourpSa, V.Rate ORDER BY COUNT(*) DESC""")
        print("  Gruppi IVA vendite usati dagli articoli:")
        for gruppo, rate, n in cur.fetchall():
            testo = perc(rate) if rate is not None else f"non impostato -> uso {perc(aliquota({}, cfg))}"
            print(f"    {gruppo:<12} {testo:<28} {n} articoli")


def cmd_documento(args, cfg):
    tipo = args.comando
    nome_doc = DOCUMENTI[tipo][2]
    conn = connetti(cfg)
    doc, quante = trova_documento(conn, tipo, args.numero, args.anno, args.serie)
    if not doc:
        anno = f" dell'anno {args.anno}" if args.anno else ""
        sys.exit(f"{nome_doc} n. {args.numero}{anno} non trovata.")
    data = doc["data"].strftime("%d/%m/%Y") if hasattr(doc["data"], "strftime") else str(doc["data"])
    print(f"\n{nome_doc} n. {doc['docnum']} del {data} - {doc['fornitore']}")
    if quante > 1:
        print(f"(ci sono {quante} documenti con questo numero in anni diversi: uso il piu' recente. "
              f"Per un altro anno aggiungi ad es. --anno {doc['anno'] - 1})")
    righe = righe_documento(conn, cfg, tipo, doc["docentry"])
    if not righe:
        sys.exit("Il documento non ha righe con articoli di magazzino.")
    if args.una_per_riga:
        lista = [(r, 1) for r in righe]
    elif args.quantita is not None:
        lista = [(r, args.quantita) for r in righe]
    else:
        lista = [(r, r["qta"]) for r in righe]
    nome_base = f"{tipo}_{doc['anno']}_{doc['docnum']}"

    if getattr(args, "scegli", False):
        try:
            import finestra
        except ImportError:
            sys.exit("La finestra di selezione non e' disponibile (manca tkinter in Python).\n"
                     "Reinstalla Python da python.org lasciando spuntato 'tcl/tk and IDLE'.")
        for art, q in lista:
            art["qta"] = q
        titolo = f"{nome_doc} n. {doc['docnum']} del {data} - {doc['fornitore']}"

        def anteprima(sel):
            esporta(sel, cfg, argparse.Namespace(anteprima=True, excel=False, stampante=None, si=True),
                    nome_base)

        print("Si e' aperta la finestra di selezione...", flush=True)
        scelti = finestra.scegli_etichette(titolo, [a for a, _ in lista],
                                          lambda a: formatta_prezzo(a, cfg), anteprima)
        if scelti is None:
            print("Annullato: nessuna etichetta stampata.")
            return
        lista = scelti
        args.si = True          # gia' confermato nella finestra

    esporta(lista, cfg, args, nome_base)


def cmd_articoli(args, cfg):
    richieste = []
    for voce in args.voci:
        codice, _, qta = voce.partition(":")
        try:
            richieste.append((codice.strip(), int(qta) if qta else 1))
        except ValueError:
            sys.exit(f"Quantita' non valida in '{voce}'. Formato: CODICE:QUANTITA")
    conn = connetti(cfg)
    trovati = articoli_per_codice(conn, cfg, [c for c, _ in richieste])
    lista, mancanti = [], []
    for codice, qta in richieste:
        art = trovati.get(codice.upper())
        if art:
            lista.append((art, qta))
        else:
            mancanti.append(codice)
    if mancanti:
        print("Articoli non trovati in SAP: " + ", ".join(mancanti))
    esporta(lista, cfg, args, "articoli")


def chiedi_selezione(risultati):
    """Formato: '1 3x5 7-9x2'  ->  indice (o intervallo) e quantita' opzionale."""
    while True:
        testo = input("\nNumeri da stampare (es. 1 3x5 7-9x2, invio = annulla): ").strip()
        if not testo:
            return []
        scelta, ok = [], True
        for parte in testo.replace(",", " ").split():
            idx, _, qta = parte.lower().partition("x")
            try:
                q = int(qta) if qta else 1
                if "-" in idx:
                    a, b = (int(x) for x in idx.split("-", 1))
                    indici = range(a, b + 1)
                else:
                    indici = [int(idx)]
                for i in indici:
                    if not 1 <= i <= len(risultati):
                        raise ValueError
                    scelta.append((risultati[i - 1], q))
            except ValueError:
                print(f"  '{parte}' non valido.")
                ok = False
                break
        if ok:
            return scelta


def cmd_cerca(args, cfg):
    conn = connetti(cfg)
    lista = []
    testo = args.testo
    while True:
        if not testo:
            testo = input("\nCerca articolo (codice, descrizione o EAN; INVIO a vuoto = STAMPA): ").strip()
            if not testo:
                break
        risultati = cerca_articoli(conn, cfg, testo, args.limite)
        if not risultati:
            print("  Nessun articolo trovato.")
        else:
            print()
            for i, a in enumerate(risultati, 1):
                print(f"{i:>3}. {a['codice'][:18]:<18} {a['descrizione'][:42]:<42} "
                      f"{(a['ean'] or '-')[:14]:<14} {formatta_prezzo(a, cfg)}")
            if len(risultati) == args.limite:
                print(f"  (mostrati i primi {args.limite}: affina la ricerca)")
            lista.extend(chiedi_selezione(risultati))
            print(f"  Etichette in elenco: {sum(q for _, q in lista)}")
        testo = None
    if not lista:
        print("Nessun articolo selezionato.")
        return
    esporta(lista, cfg, args, "articoli")


def main():
    p = argparse.ArgumentParser(description="Etichette da SAP Business One: stampa diretta o Excel per Clabel")
    sub = p.add_subparsers(dest="comando", required=True)

    uscita = argparse.ArgumentParser(add_help=False)
    gu = uscita.add_mutually_exclusive_group()
    gu.add_argument("--anteprima", action="store_true", help="crea un PDF di prova invece di stampare")
    gu.add_argument("--excel", action="store_true", help="crea il file Excel per Clabel trade invece di stampare")
    uscita.add_argument("--stampante", help="nome stampante (sovrascrive config.ini)")
    uscita.add_argument("--si", action="store_true", help="stampa senza chiedere conferma")

    sub.add_parser("test", help="verifica la connessione a SAP")
    sub.add_parser("stampanti", help="elenca le stampanti installate")

    aiuti = {
        "entrata": "etichette da un'Entrata merci",
        "fattura": "etichette da una Fattura fornitore",
        "fattura-cliente": "etichette da una Fattura cliente",
    }
    for tipo, aiuto in aiuti.items():
        e = sub.add_parser(tipo, parents=[uscita], help=aiuto)
        e.add_argument("numero", type=int, help="numero documento")
        e.add_argument("--anno", type=int, help="anno del documento (default: il piu' recente)")
        e.add_argument("--serie", type=int, help="serie di numerazione, se ne usate piu' d'una")
        g = e.add_mutually_exclusive_group()
        g.add_argument("--una-per-riga", action="store_true", help="1 etichetta per riga invece della quantita'")
        g.add_argument("--quantita", type=int, help="numero fisso di etichette per ogni riga")
        e.add_argument("--scegli", action="store_true",
                       help="apre una finestra per scegliere articoli e quantita' da stampare")

    a = sub.add_parser("articoli", parents=[uscita], help="etichette per codici articolo")
    a.add_argument("voci", nargs="+", metavar="CODICE[:QTA]")

    c = sub.add_parser("cerca", parents=[uscita], help="cerca articoli e seleziona in modo interattivo")
    c.add_argument("testo", nargs="?", help="testo da cercare")
    c.add_argument("--limite", type=int, default=50, help="massimo risultati mostrati (default 50)")

    args = p.parse_args()
    if args.comando == "stampanti":
        import etichetta
        predefinita = etichetta.scegli_stampante("")
        for nome in etichetta.elenco_stampanti():
            formato = ""
            if "tec" in nome.lower() or "sa4t" in nome.lower():
                try:
                    w, h, _ = etichetta.info_pagina(nome)
                    formato = f"   [formato driver {w:.1f} x {h:.1f} mm]"
                except Exception:
                    pass
            print(("* " if nome == predefinita else "  ") + nome + formato)
        print("\n* = quella che verra' usata se in config.ini 'nome' e' vuoto")
        return
    cfg = carica_config()
    if args.comando in DOCUMENTI:
        cmd_documento(args, cfg)
    else:
        {"test": cmd_test, "articoli": cmd_articoli, "cerca": cmd_cerca}[args.comando](args, cfg)


if __name__ == "__main__":
    main()
