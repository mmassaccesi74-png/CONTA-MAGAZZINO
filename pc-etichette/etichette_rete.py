# -*- coding: utf-8 -*-
"""
etichette_rete.py - Stampa delle etichette dall'app Android "Etichette SAP".

Il telefono (Wi-Fi aziendale) parla con il programma Etichette SAP aperto sul server, sulla
stessa porta e con lo stesso codice di accesso dell'inventario (vedi inventario.avvia_ricezione).
Il programma legge SAP e stampa sulla Toshiba: il telefono sceglie solo articoli e quantita'.

  GET  /etichette/info                              impostazioni utili al telefono
  GET  /etichette/cerca?q=testo                     ricerca per codice, descrizione o codice a barre
  GET  /etichette/documento?tipo=entrata&numero=N&anno=AAAA   righe di Entrata merci / Fattura fornitore
  POST /etichette/anteprima  {codice, ean, tipo}    immagine PNG dell'etichetta o del cartellino
  POST /etichette/stampa     {tipo, righe:[{codice, ean, qta}], operatore, forza, stampante (facoltativa)}

Prezzi e descrizioni vengono sempre riletti da SAP al momento della stampa.
"""

import base64
import io
import threading
from datetime import datetime
from urllib.parse import parse_qs

import sap_clabel as s

MAX_RIGHE = 300            # articoli diversi per stampa
MAX_ETICHETTE = 2000       # etichette totali per stampa
MAX_COPIE = 999            # etichette per singolo articolo
TIPI_DOC = ("entrata", "fattura")


class ErroreEtichette(Exception):
    pass


def _testo(ex):
    if isinstance(ex, SystemExit):          # le funzioni di sap_clabel/etichetta escono con sys.exit("messaggio")
        return str(ex.code)
    return str(ex) or type(ex).__name__


class ServizioEtichette:
    """Risponde alle richieste dell'app. avvisa(testo) viene chiamata (da un altro thread) dopo ogni stampa."""

    def __init__(self, cfg, avvisa=None):
        self.cfg = cfg
        self.avvisa = avvisa or (lambda testo: None)
        self.conn = None
        self.blocco = threading.Lock()      # una richiesta alla volta: connessione SQL e stampante non si condividono

    # ------------------------------------------------------------ SAP
    def _interroga(self, funzione, *argomenti):
        """Lettura su SAP con una connessione propria; se e' caduta riprova una volta."""
        for tentativo in (1, 2):
            try:
                if self.conn is None:
                    self.conn = s.connetti(self.cfg)
                return funzione(self.conn, *argomenti)
            except SystemExit as e:
                self.conn = None
                raise ErroreEtichette(_testo(e))
            except Exception:
                self.conn = None
                if tentativo == 2:
                    raise

    def _um_preferita(self):
        e = self.cfg["etichette"] if self.cfg.has_section("etichette") else {}
        return e.get("um_barcode", "").strip()

    def _completa(self, articoli):
        self._interroga(s.barcode_articoli, articoli)
        self._interroga(s.prezzi_um, self.cfg, articoli)
        return articoli

    # ------------------------------------------------------------ codici a barre e prezzi
    @staticmethod
    def _prezzo_per(art, barcode):
        """Prezzo netto di listino per il codice a barre (quello della sua UM, se SAP ne ha uno)."""
        base = art.get("prezzo_base")
        if barcode and barcode[1]:
            return (art.get("prezzi_um") or {}).get(barcode[1], base)
        return base

    def _scegli(self, art, ean="", usa_preferita=True):
        """Imposta su art il codice a barre richiesto (o quello dell'UM preferita, o il predefinito)."""
        art.setdefault("ean_predefinito", art["ean"])
        art.setdefault("prezzo_base", art.get("prezzo"))
        bcs = art.get("barcodes") or []
        scelto = next((b for b in bcs if ean and b[0] == ean), None)
        if not scelto and not ean and usa_preferita and self._um_preferita():
            scelto = next((b for b in bcs if b[1] == self._um_preferita()), None)
        if not scelto:
            scelto = next((b for b in bcs if b[0] == art["ean_predefinito"]), None)
        if scelto:
            art["ean"] = scelto[0]
            art["prezzo"] = self._prezzo_per(art, scelto)
            art["um_etichetta"] = scelto[1]
        return art

    def _json(self, art, qta=None):
        """L'articolo come lo vede il telefono."""
        codici = []
        for b in art.get("barcodes") or []:
            p = self._prezzo_per(art, b)
            codici.append([b[0], b[1], b[2], s.formatta_prezzo({"prezzo": p, "iva": art.get("iva")}, self.cfg) if p is not None else ""])
        d = {"codice": art["codice"], "descrizione": art["descrizione"], "ean": art["ean"],
             "um": art.get("um_etichetta", ""), "prezzo": s.formatta_prezzo(art, self.cfg), "barcode": codici}
        if qta is not None:
            d["qta"] = qta
        return d

    # ------------------------------------------------------------ richieste
    def info(self):
        imp = s.impostazioni_etichetta(self.cfg)
        imp_c = s.impostazioni_cartellino(self.cfg)
        return {"ok": True, "azienda": self._azienda(), "listino": s.listino(self.cfg),
                "stampante": imp["nome"] or "(automatica: Toshiba SA4T)",
                "stampante_cartellini": imp_c["stampante"] or imp["nome"] or "(come le etichette)",
                "etichetta": f"{imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm",
                "cartellino": f"{imp_c['larghezza_mm']:g} x {imp_c['altezza_mm']:g} mm",
                "conferma_sopra": imp["conferma_sopra"], "um_preferita": self._um_preferita(),
                "stampanti": self._stampanti()}

    @staticmethod
    def _stampanti():
        """Le stampanti installate sul server, con il formato carta del driver (per sceglierle dal telefono)."""
        import etichetta
        elenco = []
        try:
            nomi = etichetta.elenco_stampanti()
        except BaseException:
            return elenco
        for nome in nomi:
            try:
                w, h, _ = etichetta.info_pagina(nome)
                formato = f"{w:.0f} x {h:.0f} mm"
            except BaseException:
                formato = ""
            elenco.append({"nome": nome, "formato": formato})
        return elenco

    def _azienda(self):
        def leggi(conn):
            cur = conn.cursor()
            cur.execute("SELECT CompnyName FROM OADM")
            r = cur.fetchone()
            return (r[0] if r else "") or ""
        try:
            return self._interroga(leggi)
        except Exception:
            return ""

    def _per_barcode(self, conn, testo):
        cur = conn.cursor()
        cur.execute("SELECT ItemCode FROM OITM WHERE CodeBars = ?", [testo])
        r = cur.fetchone()
        if r:
            return r[0]
        try:
            cur = conn.cursor()
            cur.execute("SELECT TOP 1 ItemCode FROM OBCD WHERE BcdCode = ?", [testo])
            r = cur.fetchone()
            return r[0] if r else None
        except Exception:
            return None

    def cerca(self, testo):
        testo = (testo or "").strip()
        if len(testo) < 2:
            raise ErroreEtichette("Scrivi almeno 2 caratteri.")
        # codice a barre o codice articolo esatto: un solo articolo, con quel codice a barre gia' scelto
        codice = self._interroga(self._per_barcode, testo)
        letto = testo if codice else ""
        if not codice:
            trovati = self._interroga(s.articoli_per_codice, self.cfg, [testo])
            codice = trovati[testo.upper()]["codice"] if testo.upper() in trovati else None
        if codice:
            arts = list(self._interroga(s.articoli_per_codice, self.cfg, [codice]).values())
            self._completa(arts)
            for a in arts:
                self._scegli(a, letto)
            return {"articoli": [self._json(a) for a in arts], "esatto": True, "letto": letto}
        arts = self._interroga(s.cerca_articoli, self.cfg, testo, 60)
        self._completa(arts)
        for a in arts:
            self._scegli(a)
        return {"articoli": [self._json(a) for a in arts], "esatto": False, "altri": len(arts) >= 60}

    def documento(self, tipo, numero, anno=None):
        if tipo not in TIPI_DOC:
            raise ErroreEtichette("Tipo di documento non riconosciuto.")
        try:
            numero = int(str(numero).strip())
            anno = int(str(anno).strip()) if anno not in (None, "") else None
        except ValueError:
            raise ErroreEtichette("Numero e anno devono essere cifre.")
        nome_doc = s.DOCUMENTI[tipo][2]
        doc, quante = self._interroga(s.trova_documento, tipo, numero, anno)
        if not doc:
            raise ErroreEtichette(f"{nome_doc} n. {numero}{f' del {anno}' if anno else ''} non trovata in SAP.")
        righe = self._interroga(s.righe_documento, self.cfg, tipo, doc["docentry"])
        self._completa(righe)
        for a in righe:
            self._scegli(a)
        data = doc["data"].strftime("%d/%m/%Y") if hasattr(doc["data"], "strftime") else str(doc["data"])
        return {"titolo": f"{nome_doc} n. {doc['docnum']} del {data}", "intestatario": doc["fornitore"],
                "anno": doc["anno"], "altri_anni": quante > 1 and not anno,
                "righe": [self._json(a, a.get("qta", 0)) for a in righe]}

    def _articoli_richiesti(self, righe):
        """[(articolo con il codice a barre scelto, quantita')] riletti da SAP."""
        if not isinstance(righe, list) or not righe:
            raise ErroreEtichette("Nessun articolo da stampare.")
        if len(righe) > MAX_RIGHE:
            raise ErroreEtichette(f"Troppi articoli in una volta (massimo {MAX_RIGHE}): dividi la stampa.")
        richieste = []
        for r in righe:
            if not isinstance(r, dict) or not str(r.get("codice") or "").strip():
                raise ErroreEtichette("Riga senza codice articolo.")
            try:
                q = int(r.get("qta", 1))
            except (TypeError, ValueError):
                raise ErroreEtichette(f"Quantita' non valida per {r.get('codice')}.")
            if q < 0 or q > MAX_COPIE:
                raise ErroreEtichette(f"Quantita' non valida per {r.get('codice')} (da 0 a {MAX_COPIE}).")
            if q:
                richieste.append((str(r["codice"]).strip(), str(r.get("ean") or "").strip(), q))
        if not richieste:
            raise ErroreEtichette("Tutte le quantita' sono a zero.")
        codici = sorted({c for c, _, _ in richieste})
        trovati = {}
        for i in range(0, len(codici), 500):
            trovati.update(self._interroga(s.articoli_per_codice, self.cfg, codici[i:i + 500]))
        mancanti = [c for c in codici if c.upper() not in trovati]
        if mancanti:
            raise ErroreEtichette("Articoli non trovati in SAP: " + ", ".join(mancanti[:10]))
        self._completa(list(trovati.values()))
        lista = []
        for codice, ean, q in richieste:
            art = dict(trovati[codice.upper()])       # una copia per riga: lo stesso articolo puo' avere codici a barre diversi
            lista.append((self._scegli(art, ean, usa_preferita=False), q))
        return lista

    def anteprima(self, dati):
        tipo = dati.get("tipo", "etichette")
        lista = self._articoli_richiesti([{"codice": dati.get("codice"), "ean": dati.get("ean"), "qta": 1}])
        if tipo == "cartellini":
            imp = s.impostazioni_cartellino(self.cfg)
            self._interroga(s.contenuti, [lista[0][0]], imp)
            negozio = "" if imp["nome_negozio"] else self._azienda()
            img = s.prepara_cartellini(lista, self.cfg, imp, negozio)[0][0]
        else:
            img = s.prepara_lavori(lista, self.cfg, s.impostazioni_etichetta(self.cfg))[0][0]
        buf = io.BytesIO()
        img.convert("L").save(buf, "PNG", optimize=True)
        return {"png": base64.b64encode(buf.getvalue()).decode("ascii"), "larghezza": img.width, "altezza": img.height}

    def stampa(self, dati):
        import etichetta
        tipo = dati.get("tipo", "etichette")
        if tipo not in ("etichette", "cartellini"):
            raise ErroreEtichette("Tipo di stampa non riconosciuto.")
        lista = self._articoli_richiesti(dati.get("righe"))
        chi = str(dati.get("operatore") or "").strip()[:40]
        forza = bool(dati.get("forza"))
        scelta = str(dati.get("stampante") or "").strip()
        if scelta:
            try:
                installate = etichetta.elenco_stampanti()
            except BaseException:
                installate = []
            if scelta not in installate:
                raise ErroreEtichette(f"La stampante \"{scelta}\" non c'e' piu' sul server: scegline un'altra in Impostazioni (Aggiorna elenco).")
        try:
            if tipo == "cartellini":
                imp = s.impostazioni_cartellino(self.cfg)
                articoli = [a for a, _ in lista]
                self._interroga(s.contenuti, articoli, imp)
                negozio = "" if imp["nome_negozio"] else self._azienda()
                lavori = s.prepara_cartellini([(a, q) for a, q in lista], self.cfg, imp, negozio)
                nome = scelta or etichetta.scegli_stampante(imp["stampante"] or s.impostazioni_etichetta(self.cfg)["nome"])
            else:
                imp = s.impostazioni_etichetta(self.cfg)
                lavori = s.prepara_lavori(lista, self.cfg, imp)
                nome = scelta or etichetta.scegli_stampante(imp["nome"])
            totale = sum(q for _, q in lavori)
            if totale > MAX_ETICHETTE:
                raise ErroreEtichette(f"Troppe etichette in una volta ({totale}, massimo {MAX_ETICHETTE}): dividi la stampa.")
            try:
                pag_w, pag_h, _ = etichetta.info_pagina(nome)
            except Exception:
                pag_w = pag_h = None
            formato_diverso = pag_h is not None and (pag_w < imp["larghezza_mm"] - 1 or abs(pag_h - imp["altezza_mm"]) > 1)
            if tipo == "cartellini" and formato_diverso and not forza:
                # il rotolo dei cartellini e' diverso: si chiede conferma al telefono prima di sprecare carta
                return {"ok": False, "conferma": f"Nel driver di {nome} il formato carta e' {pag_w:.0f} x {pag_h:.0f} mm, "
                                                 f"il cartellino e' {imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm.\n\n"
                                                 "Hai messo il rotolo dei cartellini e impostato il formato nel driver? Stampare lo stesso?"}
            titolo = f"{'Cartellini' if tipo == 'cartellini' else 'Etichette'} SAP - telefono" + (f" {chi}" if chi else "")
            etichetta.stampa(lavori, nome, imp["dpi"], titolo,
                             centra=imp["centra"], sposta_x_mm=imp["sposta_x_mm"], sposta_y_mm=imp["sposta_y_mm"])
        except SystemExit as e:
            raise ErroreEtichette(_testo(e))
        avviso = ""
        if tipo == "etichette" and formato_diverso:
            avviso = (f"Attenzione: nel driver il formato e' {pag_w:.1f} x {pag_h:.1f} mm, "
                      f"l'etichetta {imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm.")
        if tipo == "cartellini":
            cosa = "cartellino inviato" if totale == 1 else "cartellini inviati"
        else:
            cosa = "etichetta inviata" if totale == 1 else "etichette inviate"
        self.avvisa(f"Dal telefono{' di ' + chi if chi else ''} ({datetime.now():%H:%M}): {totale} {cosa} a {nome}.")
        return {"ok": True, "stampate": totale, "stampante": nome, "avviso": avviso}

    # ------------------------------------------------------------ collegamento con il server HTTP
    def get(self, percorso, query):
        """Ritorna (stato, risposta) oppure None se il percorso non e' delle etichette."""
        if not percorso.startswith("/etichette/"):
            return None
        q = {k: v[0] for k, v in parse_qs(query or "").items()}
        azioni = {
            "/etichette/info": lambda: self.info(),
            "/etichette/cerca": lambda: self.cerca(q.get("q", "")),
            "/etichette/documento": lambda: self.documento(q.get("tipo", ""), q.get("numero", ""), q.get("anno")),
        }
        return self._esegui(azioni.get(percorso))

    def post(self, percorso, dati):
        if not percorso.startswith("/etichette/"):
            return None
        if not isinstance(dati, dict):
            return 400, {"errore": "Richiesta non valida."}
        azioni = {
            "/etichette/anteprima": lambda: self.anteprima(dati),
            "/etichette/stampa": lambda: self.stampa(dati),
        }
        return self._esegui(azioni.get(percorso))

    def _esegui(self, azione):
        if azione is None:
            return 404, {"errore": "Richiesta non riconosciuta: aggiorna il programma Etichette SAP sul server."}
        with self.blocco:
            try:
                return 200, azione()
            except ErroreEtichette as e:
                return 400, {"errore": str(e)}
            except SystemExit as e:
                return 400, {"errore": _testo(e)}
            except Exception as e:
                self.conn = None
                return 500, {"errore": f"Errore sul server: {type(e).__name__}: {e}"}
