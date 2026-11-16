#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
acquisti.py - Richieste di acquisto e Ordini d'acquisto in SAP Business One
partendo dai file dell'app Android "Acquisti Magazzino".

Va copiato sul server nella STESSA cartella del programma Etichette SAP
(dove ci sono sap_clabel.py e config.ini): usa lo stesso collegamento a SAP.

  1. Crea file anagrafica  -> export/anagrafica_acquisti.json da mandare ai telefoni
  2. Apri file del telefono -> controlla le righe con SAP
  3. Crea documento in SAP  -> Richiesta di acquisto, oppure un Ordine per fornitore (DI API)

Finche' il programma e' aperto, i telefoni collegati al Wi-Fi aziendale possono inviare i documenti
e scaricare l'anagrafica direttamente (indirizzo e codice di accesso sono scritti nella finestra).

Avvio: doppio clic su acquisti.bat (oppure: python acquisti.py)
"""

import json
import math
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(SCRIPT_DIR, "export")
FILE_ANAGRAFICA = os.path.join(EXPORT_DIR, "anagrafica_acquisti.json")
FILE_REGISTRO = os.path.join(EXPORT_DIR, "acquisti_creati.json")
FILE_CODICE = os.path.join(EXPORT_DIR, "acquisti_codice.txt")
DIR_RICEVUTI = os.path.join(EXPORT_DIR, "ricevuti")
NOME_ARCHIVIO = "creati"      # sottocartella di "ricevuti" con i documenti gia' creati in SAP
PORTA = 8765

_TIPI_SQL = {11: "2012", 12: "2014", 13: "2016", 14: "2017", 15: "2019", 16: "2022"}
_OGG_RICHIESTA = 1470000113   # BoObjectTypes.oPurchaseRequest
_OGG_ORDINE = 22              # BoObjectTypes.oPurchaseOrders


class ErroreSAP(Exception):
    pass


# --------------------------------------------------------------------------
# Lettura da SAP (SQL, sola lettura)
# --------------------------------------------------------------------------
def _righe(conn, sql, params=()):
    """Una query su un cursore nuovo, letta tutta subito.
    Con python-tds aprire un altro cursore chiude quello precedente: mai riusarne uno."""
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    return cur.fetchall()


def _a_blocchi(valori, n=500):
    valori = list(valori)
    for i in range(0, len(valori), n):
        yield valori[i:i + n]


def _num(v):
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0
    f = round(f, 3)
    return int(f) if f == int(f) else f


def _testo(v):
    return (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v).strip())


def utente_sap(conn, utente):
    """Il codice utente scritto esattamente come in SAP (serve come richiedente della richiesta)."""
    try:
        r = _righe(conn, "SELECT USER_CODE FROM OUSR WHERE UPPER(USER_CODE) = UPPER(?)", (utente,))
        return _testo(r[0][0]) if r else utente
    except Exception:
        return utente


def versione_sql(conn):
    try:
        r = _righe(conn, "SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int)")
        return int(r[0][0]) if r and r[0][0] is not None else 0
    except Exception:
        return 0


SQL_ARTICOLI = """
SELECT T0.ItemCode, T0.ItemName,
       CASE WHEN ISNULL(T0.BuyUnitMsr, '') <> '' THEN T0.BuyUnitMsr ELSE T0.InvntryUom END,
       T0.NumInBuy, T0.CardCode, T0.OnHand, T0.OnOrder, T0.IsCommited,
       CASE WHEN ISNULL(T0.ByWh, 'N') = 'Y'
            THEN ISNULL((SELECT SUM(W.MinStock) FROM OITW W WHERE W.ItemCode = T0.ItemCode), 0)
            ELSE ISNULL(T0.MinLevel, 0) END,
       CASE WHEN ISNULL(T0.ByWh, 'N') = 'Y'
            THEN ISNULL((SELECT SUM(W.MaxStock) FROM OITW W WHERE W.ItemCode = T0.ItemCode), 0)
            ELSE ISNULL(T0.MaxLevel, 0) END,
       T0.MinOrdrQty, T0.OrdrMulti
FROM OITM T0
WHERE T0.PrchseItem = 'Y' AND ISNULL(T0.frozenFor, 'N') <> 'Y'
"""


def crea_anagrafica(conn):
    """Dati per l'app: articoli acquistabili con giacenze e scorte, codici a barre, fornitori, magazzini.
    Colonne articolo: [descrizione, UM acquisto, pezzi per UM acquisto, fornitore abituale,
                       giacenza, in ordine, impegnato, scorta min, scorta max, ordine minimo, multiplo]"""
    articoli = {}
    for r in _righe(conn, SQL_ARTICOLI):
        per = _num(r[3]) or 1
        articoli[_testo(r[0])] = [_testo(r[1]), _testo(r[2]), per, _testo(r[4]),
                                  _num(r[5]), _num(r[6]), _num(r[7]), _num(r[8]), _num(r[9]), _num(r[10]), _num(r[11])]

    barcode = {}
    for cod, bc in _righe(conn, "SELECT ItemCode, CodeBars FROM OITM WHERE ISNULL(CodeBars, '') <> ''"):
        if _testo(cod) in articoli:
            barcode[_testo(bc)] = _testo(cod)
    try:
        for bc, cod in _righe(conn, "SELECT BcdCode, ItemCode FROM OBCD"):
            if _testo(cod) in articoli and _testo(bc):
                barcode.setdefault(_testo(bc), _testo(cod))
    except Exception:
        pass   # versioni di SAP senza la tabella dei codici a barre multipli

    fornitori = {_testo(c): _testo(n) for c, n in _righe(
        conn, "SELECT CardCode, CardName FROM OCRD WHERE CardType = 'S' AND ISNULL(frozenFor, 'N') <> 'Y' ORDER BY CardName")}

    try:
        mags = _righe(conn, "SELECT WhsCode, WhsName FROM OWHS WHERE ISNULL(Inactive, 'N') <> 'Y' ORDER BY WhsCode")
    except Exception:
        mags = _righe(conn, "SELECT WhsCode, WhsName FROM OWHS ORDER BY WhsCode")

    azienda, mag_pred = "", ""
    try:
        r = _righe(conn, "SELECT CompnyName, DfltWhs FROM OADM")
        if r:
            azienda, mag_pred = _testo(r[0][0]), _testo(r[0][1])
    except Exception:
        pass

    return {"formato": "anagrafica-acquisti-1", "azienda": azienda, "creato": datetime.now().astimezone().isoformat(timespec="seconds"),
            "magazzino_predefinito": mag_pred, "magazzini": [[_testo(c), _testo(n)] for c, n in mags],
            "fornitori": fornitori, "articoli": articoli, "barcode": barcode}


def salva_anagrafica(conn, percorso=FILE_ANAGRAFICA):
    dati = crea_anagrafica(conn)
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, separators=(",", ":"))
    return dati


# --------------------------------------------------------------------------
# File del telefono
# --------------------------------------------------------------------------
def leggi_file(percorso):
    try:
        with open(percorso, encoding="utf-8-sig") as f:
            dati = json.load(f)
    except (OSError, ValueError) as e:
        raise ErroreSAP(f"Non riesco a leggere il file:\n{percorso}\n{e}")
    return valida(dati)


def valida(dati):
    if not isinstance(dati, dict) or dati.get("formato") != "documento-acquisti-1":
        if isinstance(dati, dict) and dati.get("formato") == "conteggio-inventario-1":
            raise ErroreSAP("Questo e' un file di conteggio inventario: aprilo nel programma Etichette SAP, scheda Inventario.")
        raise ErroreSAP("Il file non e' un documento creato dall'app Acquisti Magazzino.")
    if dati.get("tipo") not in ("richiesta", "ordine"):
        raise ErroreSAP("Tipo di documento non riconosciuto nel file.")
    try:
        dati["_consegna"] = datetime.strptime(dati.get("consegna", ""), "%Y-%m-%d").date()
    except ValueError:
        raise ErroreSAP("Nel file manca la data di consegna richiesta.")
    return dati


def controlla(conn, dati):
    """Verifica le righe con SAP. Ritorna (righe_ok, scartate).
    righe_ok: dict con codice, descrizione, um, quantita, fornitore, nome_fornitore, magazzino.
    Le righe con lo stesso articolo (e stesso fornitore) vengono sommate."""
    righe = [r for r in dati.get("righe", []) if isinstance(r, dict)]
    mag = _testo(dati.get("magazzino"))
    ordine = dati["tipo"] == "ordine"

    mag_ok = bool(_righe(conn, "SELECT WhsCode FROM OWHS WHERE WhsCode = ?", (mag,))) if mag else False

    # codici a barre non riconosciuti dal telefono -> codice articolo
    da_barcode = {}
    senza = sorted({_testo(r.get("barcode")) for r in righe if not _testo(r.get("codice")) and _testo(r.get("barcode"))})
    for blocco in _a_blocchi(senza):
        segni = ",".join("?" * len(blocco))
        for bc, cod in _righe(conn, f"SELECT CodeBars, ItemCode FROM OITM WHERE CodeBars IN ({segni})", blocco):
            da_barcode[_testo(bc)] = _testo(cod)
        try:
            for bc, cod in _righe(conn, f"SELECT BcdCode, ItemCode FROM OBCD WHERE BcdCode IN ({segni})", blocco):
                da_barcode.setdefault(_testo(bc), _testo(cod))
        except Exception:
            pass

    codici = sorted({_testo(r.get("codice")) or da_barcode.get(_testo(r.get("barcode")), "") for r in righe} - {""})
    art = {}
    for blocco in _a_blocchi(codici):
        segni = ",".join("?" * len(blocco))
        for c, nome, um, acq, bloccato, forn in _righe(conn, f"""
                SELECT ItemCode, ItemName, CASE WHEN ISNULL(BuyUnitMsr, '') <> '' THEN BuyUnitMsr ELSE InvntryUom END,
                       PrchseItem, ISNULL(frozenFor, 'N'), CardCode
                FROM OITM WHERE ItemCode IN ({segni})""", blocco):
            art[_testo(c).upper()] = (_testo(c), _testo(nome), _testo(um), _testo(acq), _testo(bloccato), _testo(forn))

    forn_codici = sorted(({_testo(r.get("fornitore")) for r in righe} | {a[5] for a in art.values()}) - {""})
    forn = {}
    for blocco in _a_blocchi(forn_codici):
        segni = ",".join("?" * len(blocco))
        for c, nome, tipo, bloccato in _righe(
                conn, f"SELECT CardCode, CardName, CardType, ISNULL(frozenFor, 'N') FROM OCRD WHERE CardCode IN ({segni})", blocco):
            forn[_testo(c).upper()] = (_testo(c), _testo(nome), _testo(tipo), _testo(bloccato))

    ok, scartate = {}, []
    for r in righe:
        cod_tel, bc = _testo(r.get("codice")), _testo(r.get("barcode"))
        mostra = cod_tel or bc
        try:
            q = float(r.get("quantita") or 0)
        except (TypeError, ValueError):
            q = 0

        def scarta(motivo, _r=r, _m=mostra, _q=q):
            scartate.append({"codice": _m, "descrizione": _testo(_r.get("descrizione")), "quantita": _q,
                             "fornitore": _testo(_r.get("fornitore")), "motivo": motivo})

        if q <= 0 or math.isnan(q):
            scarta("Quantita' non valida"); continue
        if not mag_ok:
            scarta(f"Magazzino '{mag}' non trovato in SAP"); continue
        a = art.get((cod_tel or da_barcode.get(bc, "")).upper())
        if not a:
            scarta("Codice a barre non trovato in SAP" if not cod_tel else "Articolo non trovato in SAP"); continue
        if a[3] != "Y":
            scarta("In SAP non e' un articolo d'acquisto"); continue
        if a[4] == "Y":
            scarta("Articolo bloccato (inattivo) in SAP"); continue

        cod_f = _testo(r.get("fornitore")) or (a[5] if ordine else "")   # ordine senza fornitore: quello abituale dell'articolo
        f = forn.get(cod_f.upper()) if cod_f else None
        if ordine:
            if not f:
                scarta("Fornitore mancante o non trovato in SAP"); continue
            if f[2] != "S":
                scarta(f"{f[0]} non e' un fornitore in SAP"); continue
            if f[3] == "Y":
                scarta(f"Fornitore {f[0]} bloccato (inattivo) in SAP"); continue
        elif f and (f[2] != "S" or f[3] == "Y"):
            f = None   # nella richiesta il fornitore e' solo un suggerimento: se non va bene si lascia vuoto

        chiave = (f[0] if (f and ordine) else "", a[0])
        if chiave in ok:
            ok[chiave]["quantita"] += q
        else:
            ok[chiave] = {"codice": a[0], "descrizione": a[1], "um": a[2], "quantita": q,
                          "fornitore": f[0] if f else "", "nome_fornitore": f[1] if f else "", "magazzino": mag}
    righe_ok = sorted(ok.values(), key=lambda x: ((x["nome_fornitore"] if ordine else ""), x["codice"]))
    return righe_ok, scartate


def piano(dati, righe_ok):
    """Elenco dei documenti da creare: una richiesta unica, oppure un ordine per fornitore."""
    note = _testo(dati.get("note"))
    firma = "App Acquisti: " + " - ".join(x for x in (_testo(dati.get("nome")), _testo(dati.get("operatore"))) if x)
    commento = (f"{note} [{firma}]" if note else firma)[:254]
    base = {"consegna": dati["_consegna"], "commento": commento}
    if dati["tipo"] == "richiesta":
        return [dict(base, tipo="richiesta", fornitore="", nome_fornitore="", righe=righe_ok)] if righe_ok else []
    gruppi = {}
    for r in righe_ok:
        gruppi.setdefault(r["fornitore"], []).append(r)
    return [dict(base, tipo="ordine", fornitore=f, nome_fornitore=rr[0]["nome_fornitore"], righe=rr) for f, rr in gruppi.items()]


# --------------------------------------------------------------------------
# Registro dei documenti gia' creati (per non creare due volte lo stesso)
# --------------------------------------------------------------------------
def _chiave_file(dati):
    # l'identificativo del documento sul telefono: se viene inviato due volte, il programma se ne accorge
    return str(dati.get("id") or dati.get("esportato") or "")


def leggi_registro():
    try:
        with open(FILE_REGISTRO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def segna_creato(dati, creati):
    reg = leggi_registro()
    reg[_chiave_file(dati)] = {"quando": datetime.now().isoformat(timespec="seconds"), "nome": dati.get("nome", ""),
                               "documenti": [f"{c['titolo']} n. {c['numero']}" for c in creati]}
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(FILE_REGISTRO, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# Creazione dei documenti in SAP (DI API)
# --------------------------------------------------------------------------
def _testo_errore_com(e):
    try:
        info = getattr(e, "excepinfo", None)
        if info and len(info) > 2 and info[2]:
            return str(info[2]).strip()
    except Exception:
        pass
    return str(e)


def _data_com(d):
    # mezzogiorno: SAP usa solo il giorno, e cosi' un eventuale scarto di fuso orario non lo cambia
    return datetime(d.year, d.month, d.day, 12, 0, 0)


def crea_documenti(sap_cfg, utente, password, vers_sql, documenti, richiedente=""):
    """Crea in SAP i documenti del piano, tutti o nessuno.
    Ritorna una lista di dict: titolo, numero (DocNum), doc_entry, fornitore, nome_fornitore, righe."""
    if not documenti:
        raise ErroreSAP("Non ci sono righe valide da mandare in SAP.")
    try:
        import pythoncom
        from win32com.client import gencache, constants
    except ImportError:
        raise ErroreSAP("La creazione del documento funziona solo su Windows con pywin32 installato.")

    pythoncom.CoInitialize()
    try:
        try:
            comp = gencache.EnsureDispatch("SAPbobsCOM.Company")
        except Exception as e:
            raise ErroreSAP("DI API di SAP Business One non trovata su questo computer.\n"
                            "Usa il programma sul server SAP (dove e' installato il client SAP).\n"
                            f"Dettaglio: {_testo_errore_com(e)}")
        anno_sql = (sap_cfg.get("tipo_server_sql", "") or _TIPI_SQL.get(vers_sql, "")).strip()
        try:
            tipo = getattr(constants, f"dst_MSSQL{anno_sql}")
        except AttributeError:
            raise ErroreSAP(f"Versione di SQL Server non riconosciuta ({vers_sql}). "
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
        licenze = sap_cfg.get("license_server", "") or f"{server.split(chr(92))[0]}:30000"
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
            oggi = _data_com(date.today())
            creati = []
            comp.StartTransaction()
            try:
                for d in documenti:
                    richiesta = d["tipo"] == "richiesta"
                    titolo = "Richiesta di acquisto" if richiesta else "Ordine d'acquisto"
                    consegna = _data_com(d["consegna"])
                    doc = comp.GetBusinessObject(_OGG_RICHIESTA if richiesta else _OGG_ORDINE)
                    doc.DocDate = oggi
                    doc.TaxDate = oggi
                    if richiesta:
                        doc.RequriedDate = consegna          # (scritto cosi' nella DI API)
                        doc.DocDueDate = _data_com(max(d["consegna"], date.today() + timedelta(days=30)))   # "Valido fino a"
                        try:
                            doc.ReqType = 12                 # richiedente = utente SAP
                            doc.Requester = richiedente or utente
                        except Exception:
                            pass
                    else:
                        doc.CardCode = d["fornitore"]
                        doc.DocDueDate = consegna            # data di consegna dell'ordine
                    if d["commento"]:
                        doc.Comments = d["commento"]
                    for i, r in enumerate(d["righe"]):
                        if i:
                            doc.Lines.Add()
                        doc.Lines.ItemCode = r["codice"]
                        doc.Lines.Quantity = float(r["quantita"])
                        doc.Lines.WarehouseCode = r["magazzino"]
                        if richiesta:
                            doc.Lines.RequiredDate = consegna
                            if r.get("fornitore"):
                                doc.Lines.LineVendor = r["fornitore"]
                        else:
                            doc.Lines.ShipDate = consegna
                        # nessun prezzo: lo mette SAP dal listino del fornitore
                    try:
                        esito = doc.Add()
                    except Exception as e:
                        raise ErroreSAP(f"SAP ha rifiutato {titolo}"
                                        f"{' per ' + d['nome_fornitore'] if d['nome_fornitore'] else ''}: {_testo_errore_com(e)}")
                    if esito != 0:
                        raise ErroreSAP(f"SAP ha rifiutato {titolo}"
                                        f"{' per ' + d['nome_fornitore'] if d['nome_fornitore'] else ''}: {comp.GetLastErrorDescription()}")
                    entry = int(comp.GetNewObjectKey())
                    numero = entry
                    try:
                        if doc.GetByKey(entry):
                            numero = doc.DocNum
                    except Exception:
                        pass
                    creati.append({"titolo": titolo, "numero": numero, "doc_entry": entry, "fornitore": d["fornitore"],
                                   "nome_fornitore": d["nome_fornitore"], "righe": len(d["righe"])})
            except Exception:
                try:
                    if comp.InTransaction:
                        comp.EndTransaction(constants.wf_RollBack)
                except Exception:
                    pass
                raise
            try:
                comp.EndTransaction(constants.wf_Commit)
            except Exception as e:
                raise ErroreSAP(f"SAP non ha confermato i documenti: {_testo_errore_com(e)}")
            return creati
        finally:
            try:
                comp.Disconnect()
            except Exception:
                pass
    finally:
        pythoncom.CoUninitialize()


# --------------------------------------------------------------------------
# Ricezione diretta dai telefoni (Wi-Fi aziendale)
# --------------------------------------------------------------------------
def codice_accesso():
    """Codice a 6 cifre che i telefoni devono conoscere: creato la prima volta, poi sempre lo stesso."""
    try:
        with open(FILE_CODICE, encoding="utf-8") as f:
            c = f.read().strip()
        if c:
            return c
    except OSError:
        pass
    c = f"{secrets.randbelow(900000) + 100000}"
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(FILE_CODICE, "w", encoding="utf-8") as f:
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


def salva_ricevuto(dati):
    """Salva un documento arrivato da un telefono. Un nuovo invio dello stesso documento sostituisce il precedente."""
    valida(dati)
    gia = leggi_registro().get(_chiave_file(dati))
    if gia:
        raise ErroreSAP("Questo documento e' gia' stato creato in SAP (" + ", ".join(gia.get("documenti", [])) +
                        "). Per ordinare altro crea un documento nuovo nell'app.")
    dati = {k: v for k, v in dati.items() if not k.startswith("_")}
    dati["ricevuto"] = datetime.now().isoformat(timespec="seconds")
    nome = re.sub(r"[^A-Za-z0-9_-]", "", str(dati.get("id", ""))) or datetime.now().strftime("%Y%m%d%H%M%S")
    os.makedirs(DIR_RICEVUTI, exist_ok=True)
    percorso = os.path.join(DIR_RICEVUTI, f"{nome}.json")
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1)
    return percorso


def archivia(percorso):
    """Toglie dall'elenco un documento ricevuto, conservandolo nella sottocartella dei creati."""
    if os.path.dirname(os.path.abspath(percorso)) != os.path.abspath(DIR_RICEVUTI):
        return False          # file aperto a mano da un'altra cartella: non si tocca
    arch = os.path.join(DIR_RICEVUTI, NOME_ARCHIVIO)
    try:
        os.makedirs(arch, exist_ok=True)
        os.replace(percorso, os.path.join(arch, datetime.now().strftime("%Y%m%d_%H%M%S_") + os.path.basename(percorso)))
        return True
    except OSError:
        return False


def elenco_ricevuti(massimo=60):
    """[(percorso, dati)] dei documenti ricevuti ancora da creare in SAP, dal piu' recente.
    Quelli che risultano gia' creati vengono archiviati e non compaiono."""
    reg = leggi_registro()
    try:
        nomi = [os.path.join(DIR_RICEVUTI, n) for n in os.listdir(DIR_RICEVUTI) if n.lower().endswith(".json")]
    except OSError:
        return []
    out = []
    for p in sorted(nomi, key=os.path.getmtime, reverse=True)[:massimo]:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and _chiave_file(d) in reg:
            archivia(p)
        elif isinstance(d, dict):
            out.append((p, d))
    return out


def avvia_ricezione(coda, codice, porta=PORTA, file_anagrafica=None):
    """Avvia in sottofondo il servizio che riceve dai telefoni. Ritorna il server (server.shutdown() per fermarlo)."""
    cache = {"mtime": None, "parti": None}
    blocco = threading.Lock()
    sbagliati = {}     # indirizzo -> [numero di codici sbagliati, fino a quando e' sospeso]
    MAX_SBAGLIATI, SOSPENSIONE = 5, 60

    def anagrafica():
        percorso = file_anagrafica or FILE_ANAGRAFICA
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
                self._rispondi(403, {"errore": "Codice di accesso sbagliato: e' scritto nella finestra del programma Acquisti SAP."})
            return False

        def do_OPTIONS(self):
            self._rispondi(204, b"")

        def do_GET(self):
            if not self._autorizzato():
                return
            percorso = self.path.split("?")[0].rstrip("/")
            if percorso == "/ping":
                return self._rispondi(200, {"ok": True, "programma": "acquisti-sap"})
            m = re.fullmatch(r"/anagrafica/(\d+)", percorso)
            if m:
                try:
                    parti = anagrafica()
                except (OSError, ValueError):
                    return self._rispondi(404, {"errore": "Sul server non c'e' ancora l'anagrafica: premi 'Crea file anagrafica' nel programma Acquisti SAP."})
                i = int(m.group(1))
                if i < len(parti):
                    return self._rispondi(200, parti[i])
            self._rispondi(404, {"errore": "Richiesta non riconosciuta."})

        def do_POST(self):
            try:
                lung = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                lung = 0
            corpo = self.rfile.read(lung) if 0 < lung <= 5_000_000 else b""
            if lung > 5_000_000:
                self.close_connection = True
            if not self._autorizzato():
                return
            if self.path.split("?")[0].rstrip("/") != "/documento":
                return self._rispondi(404, {"errore": "Richiesta non riconosciuta."})
            try:
                dati = json.loads(corpo.decode("utf-8"))
                percorso = salva_ricevuto(dati)
            except ErroreSAP as e:
                return self._rispondi(400, {"errore": str(e)})
            except (ValueError, OSError) as e:
                return self._rispondi(400, {"errore": f"Documento non valido: {e}"})
            coda.put(percorso)
            self._rispondi(200, {"ok": True, "righe": len(dati.get("righe", []))})

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass   # collegamenti interrotti dal telefono: niente righe di errore nella finestra nera

    server = Server(("0.0.0.0", porta), Gestore)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# Finestra
# --------------------------------------------------------------------------
def _collega():
    """Usa config.ini e il collegamento del programma Etichette SAP (sap_clabel.py)."""
    try:
        import sap_clabel
    except ImportError:
        raise ErroreSAP("Non trovo sap_clabel.py: copia acquisti.py nella cartella del programma Etichette SAP.")
    try:
        cfg = sap_clabel.carica_config()
        return cfg, sap_clabel.connetti(cfg)
    except SystemExit as e:
        raise ErroreSAP(str(e) or "Collegamento a SAP non riuscito.")
    except Exception as e:
        raise ErroreSAP(f"Collegamento a SAP non riuscito:\n{e}")


def _q(v):
    return f"{v:g}".replace(".", ",")


def main():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    stato = {"cfg": None, "conn": None, "dati": None, "percorso": "", "ok": [], "scartate": [], "piano": []}

    def collega():
        if stato["conn"] is None:
            stato["cfg"], stato["conn"] = _collega()
        return stato["conn"]

    def occupato(si):
        fin.config(cursor="watch" if si else "")
        fin.update_idletasks()

    def fai(azione):
        def esegui():
            occupato(True)
            try:
                azione()
            except ErroreSAP as e:
                messagebox.showerror("Acquisti SAP", str(e))
            except Exception as e:
                stato["conn"] = None    # al prossimo tentativo si ricollega
                messagebox.showerror("Acquisti SAP", f"Errore imprevisto:\n{e}")
            finally:
                occupato(False)
        return esegui

    def anagrafica():
        dati = salva_anagrafica(collega())
        info.set(f"Anagrafica creata: {len(dati['articoli'])} articoli, {len(dati['fornitori'])} fornitori.")
        messagebox.showinfo("Acquisti SAP", "File creato:\n" + FILE_ANAGRAFICA +
                            "\n\nMandalo ai telefoni (WhatsApp, email, Drive) e caricalo nell'app: Impostazioni > Anagrafica acquisti.")

    def mostra():
        tab.delete(*tab.get_children())
        dati = stato["dati"]
        for d in stato["piano"]:
            testa = tab.insert("", "end", text=("Richiesta di acquisto" if d["tipo"] == "richiesta" else f"Ordine a {d['nome_fornitore']} ({d['fornitore']})"),
                               values=("", "", "", f"{len(d['righe'])} righe"), open=True, tags=("testa",))
            for r in d["righe"]:
                tab.insert(testa, "end", text=r["codice"], values=(r["descrizione"], _q(r["quantita"]), r["um"],
                           (r["nome_fornitore"] if d["tipo"] == "richiesta" else "OK")))
        if stato["scartate"]:
            testa = tab.insert("", "end", text="Righe scartate (non vanno in SAP)", values=("", "", "", f"{len(stato['scartate'])} righe"), open=True, tags=("scarto",))
            for r in stato["scartate"]:
                tab.insert(testa, "end", text=r["codice"], values=(r["descrizione"], _q(r["quantita"]), "", r["motivo"]), tags=("scarto",))
        tipo = "Richiesta di acquisto" if dati["tipo"] == "richiesta" else "Ordine d'acquisto"
        info.set(f"{tipo} \"{dati.get('nome', '')}\" di {dati.get('operatore') or 'telefono'} · magazzino {dati.get('magazzino', '')} · "
                 f"consegna {dati['_consegna'].strftime('%d/%m/%Y')} · {len(stato['ok'])} righe valide, {len(stato['scartate'])} scartate"
                 + (f" · Note: {dati['note']}" if dati.get("note") else ""))
        n = len(stato["piano"])
        b_crea.config(state="normal" if n else "disabled",
                      text="Crea Richiesta di acquisto in SAP" if dati["tipo"] == "richiesta" else f"Crea {n} Ordin{'e' if n == 1 else 'i'} d'acquisto in SAP")

    def carica(percorso):
        dati = leggi_file(percorso)
        ok, scartate = controlla(collega(), dati)
        stato.update(dati=dati, percorso=percorso, ok=ok, scartate=scartate, piano=piano(dati, ok))
        mostra()

    def apri():
        percorso = filedialog.askopenfilename(title="File dell'app Acquisti Magazzino", filetypes=[("File acquisti", "*.json"), ("Tutti i file", "*.*")])
        if percorso:
            carica(percorso)

    ricevuti = []

    def aggiorna_ricevuti():
        ricevuti[:] = elenco_ricevuti()
        lista.delete(0, "end")
        for _, d in ricevuti:
            quando = str(d.get("ricevuto", ""))[5:16].replace("T", " ")
            quando = f"{quando[3:5]}/{quando[0:2]} {quando[6:]}" if len(quando) >= 11 else quando
            lista.insert("end", f"{quando}  {'Ordine' if d.get('tipo') == 'ordine' else 'Richiesta'} - "
                                f"{d.get('operatore') or 'telefono'} ({len(d.get('righe', []))} righe)")

    def elimina_ricevuto():
        sel = lista.curselection()
        if not sel or sel[0] >= len(ricevuti):
            raise ErroreSAP("Fai prima clic sul documento da eliminare, nell'elenco a sinistra.")
        percorso, d = ricevuti[sel[0]]
        if not messagebox.askyesno("Acquisti SAP", f"Elimino dall'elenco il documento \"{d.get('nome', '')}\" di {d.get('operatore') or 'telefono'}?\n"
                                                   "Non verra' creato in SAP. Sul telefono resta.", default="no"):
            return
        try:
            os.remove(percorso)
        except OSError as e:
            raise ErroreSAP(f"Non riesco a eliminare il file:\n{e}")
        if stato["percorso"] == percorso:
            stato.update(dati=None, percorso="", ok=[], scartate=[], piano=[])
            tab.delete(*tab.get_children())
            b_crea.config(state="disabled", text="3. Crea documento in SAP")
            info.set("Documento eliminato dall'elenco.")
        aggiorna_ricevuti()

    def scegli_ricevuto(_evento=None):
        sel = lista.curselection()
        if sel and sel[0] < len(ricevuti):
            fai(lambda: carica(ricevuti[sel[0]][0]))()

    def controlla_coda():
        arrivati = 0
        try:
            while True:
                coda.get_nowait()
                arrivati += 1
        except queue.Empty:
            pass
        if arrivati:
            aggiorna_ricevuti()
            fin.bell()
            d = ricevuti[0][1] if ricevuti else {}
            info.set(f"Arrivato dal telefono di {d.get('operatore') or '?'}: {'Ordine' if d.get('tipo') == 'ordine' else 'Richiesta'} "
                     f"\"{d.get('nome', '')}\". Fai clic sul documento nell'elenco a sinistra per controllarlo.")
        fin.after(1000, controlla_coda)

    def crea():
        dati = stato["dati"]
        if not dati or not stato["piano"]:
            return
        if not utente.get().strip() or not password.get():
            raise ErroreSAP("Scrivi utente e password di SAP Business One.")
        gia = leggi_registro().get(_chiave_file(dati))
        if gia and not messagebox.askyesno("Acquisti SAP", "Da questo file sono gia' stati creati in SAP:\n" + "\n".join(gia.get("documenti", [])) +
                                           f"\n({gia.get('quando', '')})\n\nVuoi crearli di nuovo?", default="no"):
            return
        if stato["scartate"] and not messagebox.askyesno("Acquisti SAP", f"{len(stato['scartate'])} righe sono scartate e NON andranno in SAP.\nContinuo con le altre?"):
            return
        conn = collega()
        creati = crea_documenti(stato["cfg"]["sap"], utente.get().strip(), password.get(), versione_sql(conn), stato["piano"],
                                richiedente=utente_sap(conn, utente.get().strip()))
        segna_creato(dati, creati)
        aggiorna_ricevuti()        # il documento creato esce dall'elenco e finisce nell'archivio
        stato["piano"] = []        # lo stesso documento non si puo' creare una seconda volta
        testo = "\n".join(f"{c['titolo']} n. {c['numero']}" + (f" - {c['nome_fornitore']}" if c["nome_fornitore"] else "") + f" ({c['righe']} righe)" for c in creati)
        b_crea.config(state="disabled")
        info.set("Creato in SAP: " + testo.replace("\n", " · "))
        messagebox.showinfo("Acquisti SAP", "Creato in SAP:\n\n" + testo + "\n\nLo trovi in Acquisti > " +
                            ("Richiesta di acquisto." if dati["tipo"] == "richiesta" else "Ordine d'acquisto."))

    fin = tk.Tk()
    fin.title("Acquisti SAP - richieste e ordini dall'app Acquisti Magazzino")
    fin.geometry("1180x640")
    fin.minsize(900, 480)

    alto = ttk.Frame(fin, padding=10)
    alto.pack(fill="x")
    ttk.Button(alto, text="1. Crea file anagrafica", command=fai(anagrafica)).pack(side="left")
    ttk.Button(alto, text="2. Apri file del telefono", command=fai(apri)).pack(side="left", padx=8)
    rete = tk.StringVar()
    ttk.Label(alto, textvariable=rete, font=("Segoe UI", 10, "bold")).pack(side="right")

    info = tk.StringVar(value="Crea il file anagrafica per i telefoni, oppure apri un file ricevuto dall'app.")
    ttk.Label(fin, textvariable=info, padding=(10, 0, 10, 8), wraplength=1140, justify="left").pack(fill="x")

    centro = ttk.Frame(fin, padding=(10, 0))
    centro.pack(fill="both", expand=True)
    sinistra = ttk.Frame(centro)
    sinistra.pack(side="left", fill="y", padx=(0, 8))
    ttk.Label(sinistra, text="Ricevuti dai telefoni, da creare in SAP").pack(anchor="w")
    ttk.Button(sinistra, text="Elimina dall'elenco", command=fai(elimina_ricevuto)).pack(side="bottom", anchor="w", pady=(6, 0))
    lista = tk.Listbox(sinistra, width=44, activestyle="none", exportselection=False)
    lista.pack(fill="both", expand=True)
    lista.bind("<<ListboxSelect>>", scegli_ricevuto)
    tab = ttk.Treeview(centro, columns=("desc", "qta", "um", "stato"), selectmode="browse")
    for col, titolo, larg, anc in (("#0", "Documento / Articolo", 230, "w"), ("desc", "Descrizione", 380, "w"), ("qta", "Quantita'", 80, "e"),
                                   ("um", "UM", 60, "w"), ("stato", "Fornitore / Esito", 220, "w")):
        tab.heading(col, text=titolo)
        tab.column(col, width=larg, anchor=anc, stretch=col in ("#0", "desc", "stato"))
    tab.tag_configure("testa", font=("Segoe UI", 9, "bold"))
    tab.tag_configure("scarto", foreground="#b3261e")
    barra = ttk.Scrollbar(centro, orient="vertical", command=tab.yview)
    tab.configure(yscrollcommand=barra.set)
    tab.pack(side="left", fill="both", expand=True)
    barra.pack(side="left", fill="y")

    basso = ttk.Frame(fin, padding=10)
    basso.pack(fill="x")
    ttk.Label(basso, text="Utente SAP").pack(side="left")
    utente = ttk.Entry(basso, width=16)
    utente.pack(side="left", padx=(4, 12))
    ttk.Label(basso, text="Password").pack(side="left")
    password = ttk.Entry(basso, width=16, show="*")
    password.pack(side="left", padx=(4, 12))
    b_crea = ttk.Button(basso, text="3. Crea documento in SAP", command=fai(crea), state="disabled")
    b_crea.pack(side="right")

    coda = queue.Queue()
    try:
        codice = codice_accesso()
        avvia_ricezione(coda, codice)
        rete.set(f"Per i telefoni:  indirizzo {indirizzo_locale()}:{PORTA}   codice {codice}")
    except OSError as e:
        rete.set("Ricezione dai telefoni non attiva")
        info.set(f"Non riesco ad aprire la porta {PORTA} per ricevere dai telefoni ({e}). Forse il programma e' gia' aperto in un'altra finestra. "
                 "I file si possono comunque aprire a mano.")
    aggiorna_ricevuti()
    fin.after(1000, controlla_coda)
    fin.mainloop()


if __name__ == "__main__":
    main()
