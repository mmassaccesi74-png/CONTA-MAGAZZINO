# -*- coding: utf-8 -*-
"""
prezzi.py - Prezzi al pubblico tondi.

Il listino usato dalla cassa e' senza IVA: per avere un prezzo IVA inclusa tondo
(es. € 4,50) si calcola il netto che, con l'IVA dell'articolo, da' esattamente quel prezzo,
e lo si scrive nel listino tramite la DI API di SAP. SAP segna quei prezzi come "manuali":
non vengono piu' ricalcolati dal listino base, per questo c'e' anche il controllo
degli articoli con ricarico sceso sotto la soglia.
"""

import csv
import os
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

import inventario
import sap_clabel as s

CENT = Decimal("0.01")

# nome mostrato -> (tipo, valore)
METODI = {
    "Ai 5 centesimi": ("intervallo", Decimal("0.05")),
    "Ai 10 centesimi": ("intervallo", Decimal("0.10")),
    "Ai 50 centesimi": ("intervallo", Decimal("0.50")),
    "All'euro": ("intervallo", Decimal("1")),
    "Finale ,90": ("finale", Decimal("0.90")),
    "Finale ,99": ("finale", Decimal("0.99")),
    "Finale ,50": ("finale", Decimal("0.50")),
}
DIREZIONI = {"Per eccesso": "eccesso", "Al piu' vicino": "vicino", "Per difetto": "difetto"}


class ErrorePrezzi(Exception):
    pass


def arrotonda(valore, metodo, direzione):
    """Prezzo IVA inclusa arrotondato secondo il metodo (nome in METODI) e la direzione."""
    tipo, v = METODI[metodo]
    valore = Decimal(str(valore))
    if tipo == "intervallo":
        modo = {"eccesso": ROUND_CEILING, "difetto": ROUND_FLOOR, "vicino": ROUND_HALF_UP}[direzione]
        return ((valore / v).quantize(Decimal(1), rounding=modo) * v).quantize(CENT)
    # finale fisso: n + v con n intero
    sotto = (valore - v).quantize(Decimal(1), rounding=ROUND_FLOOR) + v
    sopra = sotto if sotto == valore else sotto + 1
    if direzione == "eccesso":
        scelto = sopra
    elif direzione == "difetto":
        scelto = sotto
    else:
        scelto = sopra if (sopra - valore) <= (valore - sotto) else sotto
    if scelto <= 0:
        scelto = sopra
    return scelto.quantize(CENT)


def lordo_da_netto(netto, iva):
    return (Decimal(str(netto)) * (1 + Decimal(str(iva)) / 100)).quantize(CENT, rounding=ROUND_HALF_UP)


def netto_per(lordo, iva, decimali):
    """Netto (con i decimali dei prezzi di SAP) che con l'IVA da' esattamente 'lordo'. None se non esiste."""
    lordo, iva = Decimal(str(lordo)), Decimal(str(iva))
    passo = Decimal(1).scaleb(-decimali)
    base = (lordo / (1 + iva / 100)).quantize(passo, rounding=ROUND_HALF_UP)
    for k in (0, -1, 1, -2, 2, -3, 3):
        n = base + k * passo
        if n > 0 and lordo_da_netto(n, iva) == lordo:
            return n
    return None


def decimali_prezzo(conn):
    try:
        cur = conn.cursor()
        cur.execute("SELECT PriceDec FROM OADM")
        r = cur.fetchone()
        return int(r[0]) if r and r[0] is not None else 2
    except Exception:
        return 2


def proposta(art, cfg, metodo, direzione, decimali):
    """Una riga di proposta per un articolo della tabella etichette."""
    iva = s.aliquota(art, cfg)
    netto = art.get("prezzo_base", art.get("prezzo"))
    riga = {"codice": art["codice"], "descrizione": art.get("descrizione", ""), "iva": iva,
            "netto": None if netto is None else Decimal(str(netto)), "art": art,
            "costo": art.get("costo"), "lordo": None, "nuovo": None, "nuovo_netto": None, "stato": ""}
    if netto is None or Decimal(str(netto)) <= 0:
        riga["stato"] = "Senza prezzo nel listino"
        return riga
    riga["lordo"] = lordo_da_netto(netto, iva)
    imposta_nuovo(riga, arrotonda(riga["lordo"], metodo, direzione), decimali)
    return riga


def imposta_nuovo(riga, nuovo, decimali):
    """Imposta il prezzo IVA inclusa voluto e ricalcola netto e stato."""
    nuovo = Decimal(str(nuovo)).quantize(CENT)
    riga["nuovo"] = nuovo
    riga["nuovo_netto"] = netto_per(nuovo, riga["iva"], decimali) if nuovo > 0 else None
    if nuovo <= 0:
        riga["stato"] = "Prezzo non valido"
    elif riga["nuovo_netto"] is None:
        riga["stato"] = f"Con {decimali} decimali nel listino non si ottiene esattamente questo prezzo"
    elif riga["lordo"] is not None and nuovo == riga["lordo"]:
        riga["stato"] = "Gia' tondo"
    else:
        riga["stato"] = "Da aggiornare"
    costo = riga.get("costo")
    if riga["nuovo_netto"] and costo:
        riga["ricarico"] = (riga["nuovo_netto"] / Decimal(str(costo))).quantize(CENT)
    return riga


# ---------------------------------------------------------------------------
# Articoli con prezzo manuale e ricarico basso
# ---------------------------------------------------------------------------
SQL_LISTINO = "SELECT ListName, Factor, BASE_NUM FROM OPLN WHERE ListNum = ?"

# Base del ricarico: il prezzo del listino base dell'articolo (ITM1.BasePLNum), oppure l'ultimo
# prezzo d'acquisto (OITM.LastPurPrc) quando la base e' quella e non ha righe in ITM1.
SQL_MANUALI = f"""
SELECT T0.ItemCode, T0.ItemName, {s.SQL_EAN} AS EAN, P.Price, P.Currency, V.Rate,
       COALESCE(NULLIF(B.Price, 0), NULLIF(T0.LastPurPrc, 0)) AS Costo
FROM OITM T0
JOIN ITM1 P ON P.ItemCode = T0.ItemCode AND P.PriceList = ? AND P.Ovrwritten = 'Y'
LEFT JOIN ITM1 B ON B.ItemCode = T0.ItemCode AND B.PriceList = P.BasePLNum AND P.BasePLNum > 0
{s.SQL_JOIN_IVA}
WHERE T0.frozenFor = 'N' AND T0.SellItem = 'Y'
ORDER BY T0.ItemCode
"""


def info_listino(conn, listino):
    cur = conn.cursor()
    cur.execute(SQL_LISTINO, [listino])
    r = cur.fetchone()
    if not r:
        return {"nome": f"Listino {listino}", "fattore": None}
    return {"nome": r[0] or f"Listino {listino}", "fattore": Decimal(str(r[1])) if r[1] else None}


def ricarico_basso(conn, cfg, soglia):
    """Articoli del listino con prezzo manuale e prezzo/costo sotto la soglia: lista di articoli
    nello stesso formato della tabella etichette, con in piu' 'costo'."""
    cur = conn.cursor()
    cur.execute(SQL_MANUALI, [s.listino(cfg)])
    trovati = []
    for codice, nome, ean, prezzo, valuta, iva, costo in cur.fetchall():
        if prezzo is None or not costo or Decimal(str(costo)) <= 0:
            continue
        if Decimal(str(prezzo)) / Decimal(str(costo)) < soglia:
            trovati.append({"codice": codice, "descrizione": nome or "", "ean": (ean or "").strip(),
                            "prezzo": prezzo, "valuta": valuta or "", "iva": iva,
                            "prezzo_base": prezzo, "costo": Decimal(str(costo))})
    return trovati


SQL_COSTI = """
SELECT T0.ItemCode, COALESCE(NULLIF(B.Price, 0), NULLIF(T0.LastPurPrc, 0))
FROM OITM T0
LEFT JOIN ITM1 P ON P.ItemCode = T0.ItemCode AND P.PriceList = ?
LEFT JOIN ITM1 B ON B.ItemCode = T0.ItemCode AND B.PriceList = P.BasePLNum AND P.BasePLNum > 0
WHERE T0.ItemCode IN ({segnaposto})
"""


def costi(conn, cfg, articoli):
    """Aggiunge 'costo' (prezzo del listino base o ultimo prezzo d'acquisto) per mostrare il ricarico."""
    codici = sorted({a["codice"] for a in articoli if "costo" not in a})
    trovati = {}
    try:
        cur = conn.cursor()
        for i in range(0, len(codici), 500):
            blocco = codici[i:i + 500]
            cur.execute(SQL_COSTI.format(segnaposto=",".join("?" for _ in blocco)), [s.listino(cfg), *blocco])
            for codice, costo in cur.fetchall():
                if costo:
                    trovati[codice.upper()] = Decimal(str(costo))
    except Exception:
        return articoli
    for a in articoli:
        if "costo" not in a:
            a["costo"] = trovati.get(a["codice"].upper())
    return articoli


def proposta_da_costo(art, cfg, fattore, metodo, direzione, decimali):
    """Per gli articoli con ricarico basso: prezzo = costo x fattore del listino, poi arrotondato."""
    riga = proposta(art, cfg, metodo, direzione, decimali)
    if art.get("costo") and fattore:
        teorico = lordo_da_netto(Decimal(str(art["costo"])) * fattore, riga["iva"])
        imposta_nuovo(riga, arrotonda(teorico, metodo, direzione), decimali)
    return riga


# ---------------------------------------------------------------------------
# Scrittura in SAP (DI API)
# ---------------------------------------------------------------------------
def aggiorna_listino(sap_cfg, versione_sql, utente, password, listino, righe):
    """Scrive in SAP il nuovo netto di ogni riga nel listino. Ritorna (aggiornate, errori)."""
    aggiornate, errori = [], []
    with inventario.sessione_di(sap_cfg, versione_sql, utente, password) as (comp, constants):
        for r in righe:
            try:
                art = comp.GetBusinessObject(constants.oItems)
                if not art.GetByKey(r["codice"]):
                    errori.append((r, "articolo non trovato"))
                    continue
                pl = art.PriceList
                trovata = False
                for i in range(pl.Count):
                    pl.SetCurrentLine(i)
                    if pl.PriceList == listino:
                        pl.Price = float(r["nuovo_netto"])
                        trovata = True
                        break
                if not trovata:
                    errori.append((r, f"listino {listino} non presente nell'articolo"))
                    continue
                if art.Update() != 0:
                    errori.append((r, comp.GetLastErrorDescription()))
                    continue
                aggiornate.append(r)
            except Exception as e:
                errori.append((r, inventario._testo_errore_com(e)))
    return aggiornate, errori


def registra(cartella, listino, utente, righe):
    """Tiene traccia dei prezzi cambiati (prima/dopo) in export/prezzi_modificati.csv."""
    percorso = os.path.join(cartella, "prezzi_modificati.csv")
    nuovo = not os.path.exists(percorso)
    try:
        with open(percorso, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            if nuovo:
                w.writerow(["Data", "Utente", "Listino", "Codice", "Descrizione", "IVA %",
                            "Netto prima", "Netto dopo", "Ivato prima", "Ivato dopo"])
            ora = datetime.now().strftime("%d/%m/%Y %H:%M")
            for r in righe:
                w.writerow([ora, utente, listino, r["codice"], r["descrizione"], _it(r["iva"]),
                            _it(r["netto"]), _it(r["nuovo_netto"]), _it(r["lordo"]), _it(r["nuovo"])])
        return percorso
    except OSError:
        return None


def _it(v):
    if v is None:
        return ""
    testo = f"{Decimal(str(v)):f}"
    if "." in testo:
        testo = testo.rstrip("0").rstrip(".")
    return testo.replace(".", ",")
