# -*- coding: utf-8 -*-
"""
finestra.py - Tabella di selezione etichette (spunta + numero etichette per articolo).

TabellaEtichette: componente riutilizzabile (usato dal programma a finestre app.py).
scegli_etichette(): finestra singola usata dalla riga di comando con --scegli.
"""

import tkinter as tk
from tkinter import ttk, messagebox


def applica_stile(root):
    try:
        root.tk.call("tk", "scaling", 1.3)
    except tk.TclError:
        pass
    stile = ttk.Style(root)
    if "vista" in stile.theme_names():
        stile.theme_use("vista")
    stile.configure("Titolo.TLabel", font=("Segoe UI", 12, "bold"))
    stile.configure("Testa.TLabel", font=("Segoe UI", 9, "bold"))
    stile.configure("Totale.TLabel", font=("Segoe UI", 11, "bold"))
    stile.configure("Stampa.TButton", font=("Segoe UI", 10, "bold"))
    return stile


class TabellaEtichette(ttk.Frame):
    """Elenco articoli con spunta e quantita'. on_change(n_articoli, n_etichette) a ogni modifica."""

    INTESTAZIONI = ["Stampa", "Codice", "Descrizione", "Codice a barre", "Prezzo", "Etichette"]

    def __init__(self, parent, formatta_prezzo, on_change=None):
        super().__init__(parent)
        self.formatta_prezzo = formatta_prezzo
        self.on_change = on_change
        self.voci = []          # (articolo, IntVar spunta, StringVar quantita')
        self.scelte_bc = []     # (articolo, Combobox o None, Label prezzo)

        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        barra = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.tabella = ttk.Frame(self.canvas)
        self.tabella.bind("<Configure>",
                          lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._id = self.canvas.create_window((0, 0), window=self.tabella, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._id, width=e.width))
        self.canvas.configure(yscrollcommand=barra.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        # rotella del mouse solo quando il puntatore e' sopra la tabella
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._rotella))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self._costruisci([])

    def _rotella(self, evento):
        self.canvas.yview_scroll(-1 if evento.delta > 0 else 1, "units")

    @staticmethod
    def _qta(var):
        try:
            return max(0, int(str(var.get()).strip() or 0))
        except ValueError:
            return 0

    def _costruisci(self, elementi):
        """elementi: lista di (articolo, spuntato, quantita')."""
        for w in self.tabella.winfo_children():
            w.destroy()
        self.voci = []
        self.scelte_bc = []
        for col, testo in enumerate(self.INTESTAZIONI):
            ttk.Label(self.tabella, text=testo, style="Testa.TLabel").grid(
                row=0, column=col, sticky="w", padx=6, pady=(0, 4))
        ttk.Separator(self.tabella).grid(row=1, column=0, columnspan=6, sticky="ew", pady=(0, 4))
        self.tabella.columnconfigure(2, weight=1)
        for i, (art, spuntato, q) in enumerate(elementi):
            r = i + 2
            spunta = tk.IntVar(value=1 if spuntato else 0)
            qta = tk.StringVar(value=str(q))
            ttk.Checkbutton(self.tabella, variable=spunta, command=self._cambiato).grid(
                row=r, column=0, padx=(14, 6), pady=2)
            ttk.Label(self.tabella, text=art["codice"]).grid(row=r, column=1, sticky="w", padx=6)
            ttk.Label(self.tabella, text=(art["descrizione"] or "")[:70]).grid(
                row=r, column=2, sticky="w", padx=6)
            art.setdefault("ean_predefinito", art["ean"])
            art.setdefault("prezzo_base", art.get("prezzo"))
            barcodes = art.get("barcodes") or []
            attuale = next((b for b in barcodes if b[0] == art["ean"]), None)
            if attuale:
                self._applica_bc(art, attuale)
            if len(barcodes) > 1:
                cb = ttk.Combobox(self.tabella, state="readonly", width=26,
                                  values=[self._testo_bc(b) for b in barcodes])
                cb.set(self._testo_bc(attuale or barcodes[0]))
                cb.bind("<<ComboboxSelected>>",
                        lambda e, a=art, c=cb, bl=barcodes: self._scelto_bc(a, c, bl))
                cb.grid(row=r, column=3, sticky="w", padx=6, pady=1)
            else:
                testo = self._testo_bc(barcodes[0]) if barcodes else (art["ean"] or "-")
                cb = None
                ttk.Label(self.tabella, text=testo).grid(row=r, column=3, sticky="w", padx=6)
            lbl_prezzo = ttk.Label(self.tabella, text=self.formatta_prezzo(art) or "-")
            lbl_prezzo.grid(row=r, column=4, sticky="e", padx=6)
            self.scelte_bc.append((art, cb, lbl_prezzo))
            ttk.Spinbox(self.tabella, from_=0, to=9999, width=6, textvariable=qta, justify="right",
                        command=self._cambiato).grid(row=r, column=5, padx=6, pady=2)
            qta.trace_add("write", lambda *_: self._cambiato())
            self.voci.append((art, spunta, qta))
        self.canvas.yview_moveto(0)
        self._cambiato()

    @staticmethod
    def _testo_bc(b):
        codice, um = b[0], b[1]
        return f"{codice}  ({um})" if um else codice

    @staticmethod
    def _applica_bc(art, barcode):
        """Imposta il codice a barre e il prezzo della sua UM (se SAP ne ha uno)."""
        art["ean"] = barcode[0]
        prezzi = art.get("prezzi_um") or {}
        art["prezzo"] = prezzi.get(barcode[1], art.get("prezzo_base")) if barcode[1] else art.get("prezzo_base")
        art["um_etichetta"] = barcode[1]

    def _aggiorna_prezzo(self, art, lbl):
        if lbl is not None:
            lbl.configure(text=self.formatta_prezzo(art) or "-")

    def _scelto_bc(self, art, combo, barcodes):
        i = combo.current()
        if 0 <= i < len(barcodes):
            self._applica_bc(art, barcodes[i])
            lbl = next((l for a, _, l in self.scelte_bc if a is art), None)
            self._aggiorna_prezzo(art, lbl)

    def unita_misura(self):
        """Unita' di misura che hanno almeno un codice a barre negli articoli in elenco."""
        viste = []
        for art, _, _ in self.scelte_bc:
            for b in art.get("barcodes") or []:
                if b[1] and b[1] not in viste:
                    viste.append(b[1])
        return viste

    def imposta_um(self, um):
        """Sceglie per ogni articolo il codice a barre dell'UM indicata (vuota = quello predefinito).
        Restituisce quanti articoli hanno quella UM."""
        trovati = 0
        for art, cb, lbl in self.scelte_bc:
            barcodes = art.get("barcodes") or []
            if um:
                scelto = next((b for b in barcodes if b[1] == um), None)
            else:
                scelto = next((b for b in barcodes if b[0] == art.get("ean_predefinito")), None)
            if scelto:
                trovati += 1 if um else 0
                self._applica_bc(art, scelto)
                self._aggiorna_prezzo(art, lbl)
                if cb is not None:
                    cb.set(self._testo_bc(scelto))
        return trovati

    def _cambiato(self):
        if self.on_change:
            sel = self.selezione()
            self.on_change(len(sel), sum(q for _, q in sel))

    # --- API ------------------------------------------------------------
    def imposta(self, righe, spuntati=True):
        """Sostituisce l'elenco. Ogni articolo usa la sua chiave 'qta' come quantita' proposta."""
        self._costruisci([(a, spuntati and a.get("qta", 0) > 0, a.get("qta", 0)) for a in righe])

    def aggiungi_ricerca(self, risultati):
        """Mostra i risultati di una ricerca tenendo in cima gli articoli gia' spuntati."""
        tenuti = [(a, True, self._qta(q)) for a, s, q in self.voci if s.get()]
        codici = {a["codice"] for a, _, _ in tenuti}
        nuovi = [(a, False, 1) for a in risultati if a["codice"] not in codici]
        self._costruisci(tenuti + nuovi)

    def svuota(self):
        self._costruisci([])

    def tutti(self, valore):
        for _, s, _ in self.voci:
            s.set(valore)
        self._cambiato()

    def selezione(self):
        return [(a, self._qta(q)) for a, s, q in self.voci if s.get() and self._qta(q) > 0]

    def numero_righe(self):
        return len(self.voci)

    def nuovo_prezzo_base(self, art, netto):
        """Dopo un cambio di prezzo nel listino: aggiorna l'articolo e il prezzo mostrato.
        I prezzi delle altre UM (confezioni) vengono ricalcolati con la stessa proporzione."""
        vecchio = art.get("prezzo_base")
        art["prezzo_base"] = netto
        if vecchio:
            rapporto = netto / vecchio
            art["prezzi_um"] = {um: p * rapporto for um, p in (art.get("prezzi_um") or {}).items()}
        attuale = next((b for b in art.get("barcodes") or [] if b[0] == art.get("ean")), None)
        if attuale:
            self._applica_bc(art, attuale)
        else:
            art["prezzo"] = netto
        lbl = next((l for a, _, l in self.scelte_bc if a is art), None)
        self._aggiorna_prezzo(art, lbl)


def scegli_etichette(titolo, righe, formatta_prezzo, anteprima=None):
    """Finestra singola: restituisce [(articolo, qta)] da stampare oppure None se annullato."""
    risultato = {"lista": None}
    root = tk.Tk()
    root.title("Etichette da stampare")
    root.geometry("980x560")
    root.minsize(760, 360)
    applica_stile(root)

    alto = ttk.Frame(root, padding=(12, 10, 12, 4))
    alto.pack(fill="x")
    ttk.Label(alto, text=titolo, style="Titolo.TLabel").pack(anchor="w")
    ttk.Label(alto, text="Togli la spunta agli articoli da non stampare e correggi il numero "
                         "di etichette dove serve.").pack(anchor="w", pady=(2, 0))

    totale = tk.StringVar()
    tab = TabellaEtichette(root, formatta_prezzo,
                           lambda na, ne: totale.set(f"Selezionati {na} articoli - Totale etichette: {ne}"))
    tab.pack(fill="both", expand=True, padx=12, pady=6)
    tab.imposta(righe)

    basso = ttk.Frame(root, padding=(12, 8, 12, 12))
    basso.pack(fill="x")
    ttk.Label(basso, textvariable=totale, style="Totale.TLabel").pack(anchor="w", pady=(0, 8))
    pulsanti = ttk.Frame(basso)
    pulsanti.pack(fill="x")

    def stampa():
        sel = tab.selezione()
        if not sel:
            messagebox.showinfo("Stampa", "Nessun articolo selezionato.", parent=root)
            return
        n = sum(q for _, q in sel)
        if n > 20 and not messagebox.askyesno("Conferma", f"Stampare {n} etichette?", parent=root):
            return
        risultato["lista"] = sel
        root.destroy()

    def fai_anteprima():
        sel = tab.selezione()
        if not sel:
            messagebox.showinfo("Anteprima", "Nessun articolo selezionato.", parent=root)
        elif anteprima:
            anteprima(sel)

    ttk.Button(pulsanti, text="Seleziona tutto", command=lambda: tab.tutti(1)).pack(side="left")
    ttk.Button(pulsanti, text="Deseleziona tutto", command=lambda: tab.tutti(0)).pack(side="left", padx=6)
    ttk.Button(pulsanti, text="Annulla", command=root.destroy).pack(side="right")
    ttk.Button(pulsanti, text="Stampa", style="Stampa.TButton", command=stampa).pack(side="right", padx=6)
    if anteprima:
        ttk.Button(pulsanti, text="Anteprima PDF", command=fai_anteprima).pack(side="right")
    root.bind("<Escape>", lambda e: root.destroy())

    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    return risultato["lista"]
