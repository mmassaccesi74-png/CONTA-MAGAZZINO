# -*- coding: utf-8 -*-
"""
app.py - Programma a finestre per stampare le etichette da SAP Business One
sulla stampante termica (Toshiba TEC B-SA4T).

Si avvia con "Etichette SAP.pyw" (doppio clic o collegamento sul desktop).
"""

import os
import queue
import re
import sys
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import sap_clabel as s
import etichetta
from finestra import TabellaEtichette, applica_stile
import inventario
import prezzi
from decimal import Decimal

TITOLO = "Etichette SAP B1"
ICONA = os.path.join(s.SCRIPT_DIR, "etichette.ico")


class Errore(Exception):
    pass


def _messaggio(ex):
    """Testo leggibile da un'eccezione o da un sys.exit() delle funzioni di sap_clabel."""
    if isinstance(ex, SystemExit):
        return str(ex.code)
    return f"{type(ex).__name__}: {ex}"


class App:
    PREDEFINITO = "Predefinito"

    def __init__(self, root):
        self.root = root
        self.combo_um = {}
        self.conn = None
        self.doc_corrente = None
        root.title(TITOLO)
        root.geometry("1020x640")
        root.minsize(820, 480)
        applica_stile(root)
        if sys.platform == "win32" and os.path.exists(ICONA):
            try:
                root.iconbitmap(ICONA)
            except tk.TclError:
                pass
        root.report_callback_exception = self._errore_imprevisto

        try:
            self.cfg = s.carica_config()
        except SystemExit as e:
            messagebox.showerror(TITOLO, _messaggio(e))
            root.destroy()
            raise SystemExit(1)

        self.stato = tk.StringVar(value="Pronto.")
        schede = ttk.Notebook(root)
        schede.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        schede.add(self._scheda_documenti(schede), text="  Entrata merci / Fattura fornitore  ")
        schede.add(self._scheda_ricerca(schede), text="  Ricerca articoli  ")
        schede.add(self._scheda_inventario(schede), text="  Inventario  ")
        schede.add(self._scheda_impostazioni(schede), text="  Impostazioni  ")
        ttk.Label(root, textvariable=self.stato, relief="sunken", anchor="w",
                  padding=(8, 3)).pack(fill="x", side="bottom")
        self.e_numero.focus_set()
        self._avvia_ricezione()

    # ------------------------------------------------------------------ utilita'
    def _errore_imprevisto(self, tipo, valore, tb):
        testo = "".join(traceback.format_exception(tipo, valore, tb))
        try:
            with open(os.path.join(s.SCRIPT_DIR, "errori.log"), "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now():%d/%m/%Y %H:%M:%S}\n{testo}")
        except OSError:
            pass
        messagebox.showerror(TITOLO, f"Errore imprevisto:\n{valore}\n\nDettagli salvati in errori.log")

    def _occupato(self, testo):
        self.stato.set(testo)
        self.root.config(cursor="watch")
        self.root.update_idletasks()

    def _libero(self, testo=None):
        self.root.config(cursor="")
        if testo is not None:
            self.stato.set(testo)

    def _db(self):
        if self.conn is None:
            self.conn = s.connetti(self.cfg)
        return self.conn

    def _interroga(self, funzione, *argomenti):
        """Esegue una lettura su SAP; se la connessione e' caduta riprova una volta."""
        for tentativo in (1, 2):
            try:
                return funzione(self._db(), *argomenti)
            except SystemExit:
                raise
            except Exception:
                self.conn = None
                if tentativo == 2:
                    raise

    def _pulsantiera(self, parent, tabella, variabile_totale, nome_base):
        basso = ttk.Frame(parent, padding=(0, 6, 0, 0))
        basso.pack(fill="x")
        ttk.Label(basso, textvariable=variabile_totale, style="Totale.TLabel").pack(anchor="w", pady=(0, 6))
        riga = ttk.Frame(basso)
        riga.pack(fill="x")
        ttk.Button(riga, text="Seleziona tutto", command=lambda: tabella.tutti(1)).pack(side="left")
        ttk.Button(riga, text="Deseleziona tutto", command=lambda: tabella.tutti(0)).pack(side="left", padx=6)
        ttk.Label(riga, text="Codice a barre UM:").pack(side="left", padx=(18, 4))
        combo = ttk.Combobox(riga, state="readonly", width=14, values=[self.PREDEFINITO])
        combo.set(self._um_preferita() or self.PREDEFINITO)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda e: self._scelta_um(tabella, combo))
        self.combo_um[id(tabella)] = combo
        ttk.Button(riga, text="Stampa", style="Stampa.TButton",
                   command=lambda: self.stampa(tabella.selezione(), nome_base())).pack(side="right")
        ttk.Button(riga, text="Anteprima PDF",
                   command=lambda: self.stampa(tabella.selezione(), nome_base(), anteprima=True)
                   ).pack(side="right", padx=6)
        riga2 = ttk.Frame(basso)
        riga2.pack(fill="x", pady=(6, 0))
        ttk.Button(riga2, text="Prezzi tondi...",
                   command=lambda: self.prezzi_tondi(tabella)).pack(side="left")
        ttk.Label(riga2, text="Cartellini prezzo da scaffale (uno per articolo spuntato):").pack(side="left", padx=(18, 0))
        ttk.Button(riga2, text="Stampa cartellini", style="Stampa.TButton",
                   command=lambda: self.stampa_cartellini(tabella.selezione(), nome_base())).pack(side="right")
        ttk.Button(riga2, text="Anteprima cartellini",
                   command=lambda: self.stampa_cartellini(tabella.selezione(), nome_base(), anteprima=True)
                   ).pack(side="right", padx=6)

    # ------------------------------------------------- codici a barre per UM
    def _um_preferita(self):
        e = self.cfg["etichette"] if self.cfg.has_section("etichette") else {}
        return e.get("um_barcode", "").strip()

    def _aggiorna_um(self, tabella):
        """Dopo un caricamento: aggiorna le UM disponibili e applica quella preferita."""
        combo = self.combo_um[id(tabella)]
        um_disponibili = tabella.unita_misura()
        combo["values"] = [self.PREDEFINITO] + um_disponibili
        preferita = self._um_preferita()
        if preferita and preferita in um_disponibili:
            tabella.imposta_um(preferita)
            combo.set(preferita)
        else:
            combo.set(self.PREDEFINITO)

    def _scelta_um(self, tabella, combo):
        um = "" if combo.get() == self.PREDEFINITO else combo.get()
        trovati = tabella.imposta_um(um)
        self._salva_config("etichette", "um_barcode", um)
        if um:
            mancanti = tabella.numero_righe() - trovati
            nota = f" ({mancanti} senza codice per {um}: resta quello predefinito)" if mancanti else ""
            self.stato.set(f"Codice a barre dell'UM {um} su {trovati} articoli{nota}. Scelta ricordata.")
        else:
            self.stato.set("Codici a barre predefiniti dell'anagrafica.")

    def _salva_config(self, sezione, chiave, valore):
        """Scrive chiave = valore in config.ini mantenendo commenti e ordine delle righe."""
        try:
            with open(s.CONFIG_PATH, encoding="utf-8-sig") as fh:
                righe = fh.read().splitlines()
            in_sezione, inserita, fine_sezione = False, False, None
            for i, riga in enumerate(righe):
                testo = riga.strip()
                if testo.startswith("[") and testo.endswith("]"):
                    if in_sezione and fine_sezione is None:
                        fine_sezione = i
                    in_sezione = testo[1:-1].strip().lower() == sezione
                elif in_sezione and re.match(rf"^{re.escape(chiave)}\s*=", testo):
                    righe[i] = f"{chiave} = {valore}"
                    inserita = True
                    break
            if not inserita:
                if in_sezione and fine_sezione is None:
                    fine_sezione = len(righe)
                if fine_sezione is not None:
                    while fine_sezione > 0 and not righe[fine_sezione - 1].strip():
                        fine_sezione -= 1
                    righe.insert(fine_sezione, f"{chiave} = {valore}")
                else:
                    righe += ["", f"[{sezione}]", f"{chiave} = {valore}"]
            with open(s.CONFIG_PATH, "w", encoding="utf-8") as fh:
                fh.write("\n".join(righe) + "\n")
        except OSError as e:
            messagebox.showerror(TITOLO, f"Non riesco a salvare config.ini:\n{e}")
            return False
        if not self.cfg.has_section(sezione):
            self.cfg.add_section(sezione)
        self.cfg[sezione][chiave] = valore
        return True

    @staticmethod
    def _testo_totale(variabile):
        return lambda na, ne: variabile.set(f"Selezionati {na} articoli  -  Totale etichette: {ne}")

    def _prezzo(self, art):
        return s.formatta_prezzo(art, self.cfg)

    # ---------------------------------------------------------- scheda documenti
    def _scheda_documenti(self, parent):
        f = ttk.Frame(parent, padding=10)
        alto = ttk.Frame(f)
        alto.pack(fill="x")
        self.tipo_doc = tk.StringVar(value="entrata")
        ttk.Label(alto, text="Documento:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(alto, text="Entrata merci", value="entrata",
                        variable=self.tipo_doc).grid(row=0, column=1, padx=(6, 12))
        ttk.Radiobutton(alto, text="Fattura fornitore", value="fattura",
                        variable=self.tipo_doc).grid(row=0, column=2, padx=(0, 24))
        ttk.Label(alto, text="Numero:").grid(row=0, column=3)
        self.e_numero = ttk.Entry(alto, width=10)
        self.e_numero.grid(row=0, column=4, padx=(6, 16))
        ttk.Label(alto, text="Anno:").grid(row=0, column=5)
        self.e_anno = ttk.Entry(alto, width=7)
        self.e_anno.grid(row=0, column=6, padx=(6, 4))
        ttk.Label(alto, text="(vuoto = il piu' recente)", foreground="#666").grid(row=0, column=7, padx=(0, 16))
        ttk.Button(alto, text="Carica", style="Stampa.TButton", command=self.carica_documento).grid(row=0, column=8)
        for e in (self.e_numero, self.e_anno):
            e.bind("<Return>", lambda ev: self.carica_documento())

        self.info_doc = tk.StringVar(value="Scegli il tipo di documento, scrivi il numero e premi Carica (o Invio).")
        ttk.Label(f, textvariable=self.info_doc, style="Titolo.TLabel").pack(anchor="w", pady=(12, 2))
        ttk.Label(f, text="Togli la spunta agli articoli da non stampare e correggi il numero di etichette "
                          "dove serve. Doppio clic su un prezzo per cambiarlo.", foreground="#555").pack(anchor="w", pady=(0, 6))

        self.tot_doc = tk.StringVar(value="")
        self.tab_doc = TabellaEtichette(f, self._prezzo, self._testo_totale(self.tot_doc))
        self.tab_doc.pack(fill="both", expand=True)
        self.tab_doc.on_prezzo = lambda a: self.prezzi_tondi(self.tab_doc, [a], modifica=True)
        self._pulsantiera(f, self.tab_doc, self.tot_doc,
                          lambda: self.doc_corrente or "documento")
        return f

    def carica_documento(self):
        numero = self.e_numero.get().strip()
        anno = self.e_anno.get().strip()
        if not numero.isdigit():
            messagebox.showwarning(TITOLO, "Scrivi il numero del documento (solo cifre).")
            self.e_numero.focus_set()
            return
        if anno and not (anno.isdigit() and len(anno) == 4):
            messagebox.showwarning(TITOLO, "L'anno deve essere di 4 cifre, oppure lascialo vuoto.")
            return
        tipo = self.tipo_doc.get()
        nome_doc = s.DOCUMENTI[tipo][2]
        self._occupato(f"Leggo {nome_doc} n. {numero} da SAP...")
        try:
            doc, quante = self._interroga(s.trova_documento, tipo, int(numero), int(anno) if anno else None)
            if not doc:
                self.tab_doc.svuota()
                self.doc_corrente = None
                quale = f" dell'anno {anno}" if anno else ""
                self.info_doc.set(f"{nome_doc} n. {numero}{quale} non trovata.")
                self._libero("Documento non trovato.")
                return
            righe = self._interroga(s.righe_documento, self.cfg, tipo, doc["docentry"])
            self._interroga(s.barcode_articoli, righe)
            self._interroga(s.prezzi_um, self.cfg, righe)
        except (Exception, SystemExit) as e:
            self._libero("Errore di lettura da SAP.")
            messagebox.showerror(TITOLO, f"Impossibile leggere il documento:\n{_messaggio(e)}")
            return
        data = doc["data"].strftime("%d/%m/%Y") if hasattr(doc["data"], "strftime") else str(doc["data"])
        self.info_doc.set(f"{nome_doc} n. {doc['docnum']} del {data} - {doc['fornitore']}")
        self.doc_corrente = f"{tipo}_{doc['anno']}_{doc['docnum']}"
        self.tab_doc.imposta(righe)
        self._aggiorna_um(self.tab_doc)
        nota = f" ({quante} documenti con questo numero: mostrato il piu' recente)" if quante > 1 and not anno else ""
        if not righe:
            self._libero("Il documento non ha articoli di magazzino da etichettare.")
        else:
            self._libero(f"Caricati {len(righe)} articoli.{nota}")

    # ----------------------------------------------------------- scheda ricerca
    def _scheda_ricerca(self, parent):
        f = ttk.Frame(parent, padding=10)
        alto = ttk.Frame(f)
        alto.pack(fill="x")
        ttk.Label(alto, text="Cerca (codice, descrizione o EAN):").pack(side="left")
        self.e_cerca = ttk.Entry(alto, width=40)
        self.e_cerca.pack(side="left", padx=8)
        self.e_cerca.bind("<Return>", lambda ev: self.cerca())
        ttk.Button(alto, text="Cerca", style="Stampa.TButton", command=self.cerca).pack(side="left")
        ttk.Button(alto, text="Svuota elenco", command=lambda: self.tab_ric.svuota()).pack(side="right")
        ttk.Label(f, text="Spunta gli articoli da stampare. Quelli spuntati restano in elenco anche "
                          "se fai un'altra ricerca.", foreground="#555").pack(anchor="w", pady=(10, 6))
        self.tot_ric = tk.StringVar(value="")
        self.tab_ric = TabellaEtichette(f, self._prezzo, self._testo_totale(self.tot_ric))
        self.tab_ric.pack(fill="both", expand=True)
        self.tab_ric.on_prezzo = lambda a: self.prezzi_tondi(self.tab_ric, [a], modifica=True)
        self._pulsantiera(f, self.tab_ric, self.tot_ric, lambda: "articoli")
        return f

    def cerca(self):
        testo = self.e_cerca.get().strip()
        if not testo:
            return
        self._occupato(f"Cerco '{testo}'...")
        try:
            risultati = self._interroga(s.cerca_articoli, self.cfg, testo, 100)
            self._interroga(s.barcode_articoli, risultati)
            self._interroga(s.prezzi_um, self.cfg, risultati)
        except (Exception, SystemExit) as e:
            self._libero("Errore di ricerca.")
            messagebox.showerror(TITOLO, f"Ricerca non riuscita:\n{_messaggio(e)}")
            return
        self.tab_ric.aggiungi_ricerca(risultati)
        self._aggiorna_um(self.tab_ric)
        altro = " (mostrati i primi 100: affina la ricerca)" if len(risultati) == 100 else ""
        self._libero(f"Trovati {len(risultati)} articoli per '{testo}'.{altro}")
        self.e_cerca.select_range(0, "end")

    # ------------------------------------------------------- scheda impostazioni
    # --------------------------------------------------------- scheda inventario
    def _scheda_inventario(self, parent):
        f = ttk.Frame(parent, padding=10)
        self.conteggio = []
        self.percorsi_conteggio = []

        passo1 = ttk.LabelFrame(f, text=" 1. Anagrafica per il telefono ", padding=8)
        passo1.pack(fill="x")
        ttk.Label(passo1, text="Crea il file con articoli, codici a barre per UM e magazzini. I telefoni sul Wi-Fi "
                               "aziendale lo scaricano dall'app (Impostazioni > Scarica anagrafica dal server).",
                  wraplength=900, justify="left").pack(anchor="w")
        riga1 = ttk.Frame(passo1)
        riga1.pack(fill="x", pady=(6, 0))
        ttk.Button(riga1, text="Crea file anagrafica", command=self.esporta_anagrafica).pack(side="left")
        self.info_rete = tk.StringVar(value="")
        ttk.Label(riga1, textvariable=self.info_rete, style="Testa.TLabel").pack(side="left", padx=16)

        passo2 = ttk.LabelFrame(f, text=" 2. Conteggio dal telefono ", padding=8)
        passo2.pack(fill="both", expand=True, pady=(10, 0))
        riga = ttk.Frame(passo2)
        riga.pack(fill="x")
        ttk.Label(riga, text="Ricevuti dai telefoni (selezionane uno o piu': vengono sommati):").pack(side="left")
        ttk.Button(riga, text="Apri file conteggio...", command=self.apri_conteggio).pack(side="right")
        ttk.Button(riga, text="Togli dall'elenco", command=self.togli_ricevuti).pack(side="right", padx=6)
        ttk.Button(riga, text="Carica selezionati", style="Stampa.TButton",
                   command=self.carica_ricevuti).pack(side="right")
        colr = ("sessione", "operatore", "righe", "ricevuto")
        titr = ("Sessione", "Operatore", "Righe", "Ricevuto")
        largr = (260, 180, 70, 140)
        cont_r = ttk.Frame(passo2)
        cont_r.pack(fill="x", pady=(6, 0))
        self.tv_ric = ttk.Treeview(cont_r, columns=colr, show="headings", height=4, selectmode="extended")
        for c, t, w in zip(colr, titr, largr):
            self.tv_ric.heading(c, text=t)
            self.tv_ric.column(c, width=w, anchor="e" if c == "righe" else "w", stretch=(c == "sessione"))
        self.tv_ric.bind("<Double-1>", lambda e: self.carica_ricevuti())
        barra_r = ttk.Scrollbar(cont_r, orient="vertical", command=self.tv_ric.yview)
        self.tv_ric.configure(yscrollcommand=barra_r.set)
        self.tv_ric.pack(side="left", fill="x", expand=True)
        barra_r.pack(side="right", fill="y")
        self.info_inv = tk.StringVar(value="Seleziona i conteggi ricevuti e premi Carica selezionati, "
                                           "oppure apri i file inviati dall'app.")
        ttk.Label(passo2, textvariable=self.info_inv, style="Testa.TLabel").pack(anchor="w", pady=(8, 0))

        colonne = ("magazzino", "codice", "descrizione", "um", "contato", "giacenza", "differenza")
        titoli = ("Mag.", "Codice", "Descrizione", "UM", "Contato", "Giacenza SAP", "Differenza")
        larghezze = (60, 130, 360, 50, 90, 100, 90)
        contenitore = ttk.Frame(passo2)
        contenitore.pack(fill="both", expand=True, pady=(4, 0))
        self.tv_inv = ttk.Treeview(contenitore, columns=colonne, show="headings", height=8)
        for c, t, w in zip(colonne, titoli, larghezze):
            self.tv_inv.heading(c, text=t)
            self.tv_inv.column(c, width=w, anchor="e" if c in ("contato", "giacenza", "differenza") else "w",
                               stretch=(c == "descrizione"))
        self.tv_inv.tag_configure("meno", foreground="#b3261e")
        self.tv_inv.tag_configure("piu", foreground="#1b6e2e")
        barra = ttk.Scrollbar(contenitore, orient="vertical", command=self.tv_inv.yview)
        self.tv_inv.configure(yscrollcommand=barra.set)
        self.tv_inv.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.problemi_inv = tk.Text(passo2, height=2, wrap="word", font=("Segoe UI", 9), foreground="#8a4b00")
        self.problemi_inv.pack(fill="x", pady=(6, 0))
        self.problemi_inv.configure(state="disabled")

        passo3 = ttk.LabelFrame(f, text=" 3. Crea il documento in SAP ", padding=8)
        passo3.pack(fill="x", pady=(10, 0))
        g = ttk.Frame(passo3)
        g.pack(fill="x")
        ttk.Label(g, text="Data conteggio:").grid(row=0, column=0, sticky="w")
        self.e_data_inv = ttk.Entry(g, width=11)
        self.e_data_inv.insert(0, datetime.now().strftime("%d/%m/%Y"))
        self.e_data_inv.grid(row=0, column=1, padx=(6, 16))
        ttk.Label(g, text="Note:").grid(row=0, column=2)
        self.e_note_inv = ttk.Entry(g, width=40)
        self.e_note_inv.grid(row=0, column=3, padx=(6, 16))
        ttk.Label(g, text="Utente SAP:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.e_utente_sap = ttk.Entry(g, width=16)
        self.e_utente_sap.insert(0, self.cfg["sap"].get("utente_sap", "") if self.cfg.has_section("sap") else "")
        self.e_utente_sap.grid(row=1, column=1, padx=(6, 16), pady=(6, 0), sticky="w")
        ttk.Label(g, text="Password SAP:").grid(row=1, column=2, pady=(6, 0))
        self.e_pw_sap = ttk.Entry(g, width=18, show="*")
        self.e_pw_sap.grid(row=1, column=3, padx=(6, 16), pady=(6, 0), sticky="w")
        ttk.Button(g, text="Crea Conteggio inventario in SAP", style="Stampa.TButton",
                   command=self.crea_conteggio).grid(row=0, column=4, rowspan=2, padx=(10, 0))
        ttk.Label(passo3, text="Il documento registra solo le quantita' contate: le giacenze cambiano quando "
                               "in SAP esegui la Registrazione inventario.", foreground="#555").pack(anchor="w", pady=(6, 0))
        return f

    def esporta_anagrafica(self):
        percorso = os.path.join(self._cartella_export(), "anagrafica_inventario.json")
        self._occupato("Creo il file anagrafica (puo' richiedere qualche secondo)...")
        try:
            cur = self._db().cursor()
            cur.execute("SELECT CompnyName FROM OADM")
            azienda = cur.fetchone()[0]
            n_art, n_bc = self._interroga(inventario.esporta_anagrafica, percorso, azienda)
        except (Exception, SystemExit) as e:
            self.conn = None
            self._libero("Errore.")
            messagebox.showerror(TITOLO, f"Non riesco a creare il file:\n{_messaggio(e)}")
            return
        mb = os.path.getsize(percorso) / 1048576
        self._libero(f"Anagrafica creata: {n_art} articoli, {n_bc} codici a barre ({mb:.1f} MB).")
        messagebox.showinfo(TITOLO, f"File creato:\n{percorso}\n\n{n_art} articoli, {n_bc} codici a barre.\n\n"
                                    "Sui telefoni: app Conta Magazzino > Impostazioni > Scarica anagrafica dal server.\n"
                                    "Senza collegamento puoi mandare il file e caricarlo a mano nell'app.")

    # --------------------------------------------- ricezione diretta dai telefoni
    def _avvia_ricezione(self):
        self.coda_ric = queue.Queue()
        self.ricevuti = {}          # id riga dell'elenco -> percorso
        cartella = self._cartella_export()
        try:
            codice = inventario.codice_accesso(cartella)
            inventario.avvia_ricezione(cartella, codice, self.coda_ric.put)
            self.info_rete.set(f"Per i telefoni:  indirizzo {inventario.indirizzo_locale()}:{inventario.PORTA}"
                               f"   codice {codice}")
        except OSError as e:
            self.info_rete.set(f"Ricezione dai telefoni non attiva: la porta {inventario.PORTA} non si apre ({e}). "
                               "Forse il programma e' gia' aperto in un'altra finestra.")
        self._aggiorna_ricevuti()
        self.root.after(1000, self._controlla_coda)

    def _controlla_coda(self):
        arrivati = []
        try:
            while True:
                arrivati.append(self.coda_ric.get_nowait())
        except queue.Empty:
            pass
        if arrivati:
            self._aggiorna_ricevuti()
            self.root.bell()
            self.stato.set(f"Conteggio ricevuto dal telefono ({datetime.now():%H:%M}). "
                           "Lo trovi nella scheda Inventario.")
        self.root.after(1000, self._controlla_coda)

    def _aggiorna_ricevuti(self):
        selezionati = {self.ricevuti.get(i) for i in self.tv_ric.selection()}
        self.tv_ric.delete(*self.tv_ric.get_children())
        self.ricevuti = {}
        for percorso, d in inventario.elenco_ricevuti(self._cartella_export()):
            try:
                quando = datetime.fromisoformat(str(d.get("ricevuto", ""))).strftime("%d/%m/%Y %H:%M")
            except ValueError:
                quando = ""
            righe = d.get("righe") if isinstance(d.get("righe"), list) else []
            iid = self.tv_ric.insert("", "end", values=(d.get("sessione", "") or "(senza nome)",
                                                        d.get("operatore", "") or "Telefono", len(righe), quando))
            self.ricevuti[iid] = percorso
            if percorso in selezionati:
                self.tv_ric.selection_add(iid)

    def carica_ricevuti(self):
        percorsi = [self.ricevuti[i] for i in self.tv_ric.selection() if i in self.ricevuti]
        if not percorsi:
            messagebox.showinfo(TITOLO, "Seleziona nell'elenco i conteggi da caricare "
                                        "(con Ctrl o Maiusc ne scegli piu' di uno).")
            return
        self._carica_conteggi(percorsi)

    def togli_ricevuti(self):
        percorsi = [self.ricevuti[i] for i in self.tv_ric.selection() if i in self.ricevuti]
        if not percorsi:
            messagebox.showinfo(TITOLO, "Seleziona nell'elenco i conteggi da togliere.")
            return
        if not messagebox.askyesno(TITOLO, f"Togliere {len(percorsi)} conteggi dall'elenco?\n\n"
                                           "Restano conservati nella cartella conteggi_ricevuti\\usati."):
            return
        for p in percorsi:
            inventario.archivia(self._cartella_export(), p)
        self._aggiorna_ricevuti()

    def apri_conteggio(self):
        percorsi = filedialog.askopenfilenames(
            title="File conteggio dai telefoni (puoi sceglierne piu' di uno)", initialdir=self._cartella_export(),
            filetypes=[("Conteggio inventario", "*.json"), ("Tutti i file", "*.*")])
        if not percorsi:
            return
        self._carica_conteggi(list(percorsi))

    def _carica_conteggi(self, percorsi):
        self._occupato("Leggo il conteggio e le giacenze da SAP...")
        try:
            righe, sessioni = [], []
            for percorso in percorsi:
                sess, r = inventario.leggi_conteggio(percorso)
                righe.extend(r)
                if sess and sess not in sessioni:
                    sessioni.append(sess)
            sessione = ", ".join(sessioni)
            righe_ok, problemi = self._interroga(inventario.prepara_conteggio, righe)
        except (Exception, SystemExit) as e:
            self._libero("Errore.")
            messagebox.showerror(TITOLO, f"Non riesco a leggere il conteggio:\n{_messaggio(e)}")
            return
        self.conteggio = righe_ok
        self.percorsi_conteggio = list(percorsi)
        self.tv_inv.delete(*self.tv_inv.get_children())
        for r in righe_ok:
            diff = r["differenza"]
            tag = "meno" if diff < 0 else ("piu" if diff > 0 else "")
            self.tv_inv.insert("", "end", values=(
                r["magazzino"], r["codice"], r["descrizione"], r["um_mag"],
                self._num(r["contato"]), self._num(r["giacenza"]),
                ("+" if diff > 0 else "") + self._num(diff)), tags=(tag,))
        self.problemi_inv.configure(state="normal")
        self.problemi_inv.delete("1.0", "end")
        self.problemi_inv.insert("end", "\n".join(problemi) if problemi else "Nessun problema: tutte le righe sono valide.")
        self.problemi_inv.configure(state="disabled")
        diversi = sum(1 for r in righe_ok if r["differenza"] != 0)
        nome = f"'{sessione}': " if sessione else ""
        file_txt = f" da {len(percorsi)} file" if len(percorsi) > 1 else ""
        self.info_inv.set(f"{nome}{len(righe_ok)} articoli{file_txt}, {diversi} con differenze, {len(problemi)} avvisi.")
        self._libero(f"Conteggio caricato: {', '.join(os.path.basename(p) for p in percorsi)}.")

    @staticmethod
    def _num(d):
        testo = f"{d:f}"
        if "." in testo:
            testo = testo.rstrip("0").rstrip(".")
        return testo.replace(".", ",")

    def crea_conteggio(self):
        if not self.conteggio:
            messagebox.showinfo(TITOLO, "Prima apri un file di conteggio.")
            return
        try:
            data = datetime.strptime(self.e_data_inv.get().strip(), "%d/%m/%Y")
        except ValueError:
            messagebox.showwarning(TITOLO, "Scrivi la data nel formato gg/mm/aaaa.")
            return
        utente, pw = self.e_utente_sap.get().strip(), self.e_pw_sap.get()
        if not utente or not pw:
            messagebox.showwarning(TITOLO, "Inserisci utente e password con cui entri in SAP.")
            return
        n = len(self.conteggio)
        if not messagebox.askyesno(TITOLO, f"Creare in SAP un Conteggio inventario con {n} righe "
                                           f"in data {data:%d/%m/%Y}?\n\nLe giacenze non vengono modificate."):
            return
        self._occupato("Mi collego a SAP e creo il documento...")
        try:
            ver = inventario.versione_sql(self._db())
            docentry, docnum = inventario.crea_conteggio_sap(
                self.cfg["sap"], ver, utente, pw, self.conteggio, data, self.e_note_inv.get().strip())
        except (Exception, SystemExit) as e:
            self._libero("Documento non creato.")
            messagebox.showerror(TITOLO, f"Documento non creato:\n{_messaggio(e)}")
            return
        finally:
            self.e_pw_sap.delete(0, "end")
        self._salva_config("sap", "utente_sap", utente)
        # i conteggi ricevuti appena usati escono dall'elenco (conservati in conteggi_ricevuti\usati)
        for p in self.percorsi_conteggio:
            inventario.archivia(self._cartella_export(), p)
        self.percorsi_conteggio = []
        self.conteggio = []
        self._aggiorna_ricevuti()
        self._libero(f"Creato Conteggio inventario n. {docnum}.")
        messagebox.showinfo(TITOLO, f"Creato in SAP il Conteggio inventario n. {docnum}.\n\n"
                                    "Controllalo in SAP: Gestione magazzino > Transazioni inventario > "
                                    "Conteggio inventario.")

    def _scheda_impostazioni(self, parent):
        f = ttk.Frame(parent, padding=14)
        sap = self.cfg["sap"] if self.cfg.has_section("sap") else {}
        imp = s.impostazioni_etichetta(self.cfg)
        griglia = ttk.Frame(f)
        griglia.pack(fill="x")
        righe = [
            ("Server SAP", sap.get("server", "")),
            ("Database", sap.get("database", "")),
            ("Listino prezzi", str(s.listino(self.cfg))),
            ("Formato etichetta", f"{imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm, {imp['dpi']} dpi"),
        ]
        for r, (k, v) in enumerate(righe):
            ttk.Label(griglia, text=k + ":", style="Testa.TLabel").grid(row=r, column=0, sticky="w", pady=3)
            ttk.Label(griglia, text=v).grid(row=r, column=1, sticky="w", padx=10)

        ttk.Label(griglia, text="Stampante:", style="Testa.TLabel").grid(row=len(righe), column=0, sticky="w", pady=3)
        self.cb_stampante = ttk.Combobox(griglia, width=50, state="readonly")
        self.cb_stampante.grid(row=len(righe), column=1, sticky="w", padx=10)
        ttk.Button(griglia, text="Salva come predefinita",
                   command=self.salva_stampante).grid(row=len(righe), column=2)
        self._carica_stampanti()

        pulsanti = ttk.Frame(f)
        pulsanti.pack(fill="x", pady=(16, 8))
        ttk.Button(pulsanti, text="Test connessione SAP", command=self.test_connessione).pack(side="left")
        ttk.Button(pulsanti, text="Apri config.ini",
                   command=lambda: self._apri(s.CONFIG_PATH)).pack(side="left", padx=6)
        ttk.Button(pulsanti, text="Apri cartella anteprime",
                   command=lambda: self._apri(self._cartella_export())).pack(side="left")

        self.log = tk.Text(f, height=12, wrap="word", font=("Consolas", 10))
        self.log.pack(fill="both", expand=True)
        self.log.insert("end", "Le modifiche a config.ini valgono dal prossimo avvio del programma.\n")
        self.log.configure(state="disabled")
        return f

    def _scrivi_log(self, testo):
        self.log.configure(state="normal")
        self.log.insert("end", testo + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _carica_stampanti(self):
        try:
            elenco = etichetta.elenco_stampanti()
        except (Exception, SystemExit):
            elenco = []
        self.cb_stampante["values"] = elenco
        nome = s.impostazioni_etichetta(self.cfg)["nome"]
        scelta = next((p for p in elenco if nome and nome.lower() in p.lower()), None)
        if not scelta:
            try:
                scelta = etichetta.scegli_stampante("") if elenco else ""
            except (Exception, SystemExit):
                scelta = ""
        self.cb_stampante.set(scelta)

    @staticmethod
    def _nome_stabile(nome):
        # "TEC B-SA4T Etichette (redirected 26)" -> "TEC B-SA4T Etichette": il numero cambia a ogni accesso
        return re.sub(r"\s*\(redirected \d+\)\s*$", "", nome).strip()

    def salva_stampante(self):
        nome = self._nome_stabile(self.cb_stampante.get())
        if nome and self._salva_config("stampante", "nome", nome):
            self.stato.set(f"Stampante predefinita: {nome}")
            self._scrivi_log(f"Stampante salvata in config.ini: {nome}")

    def test_connessione(self):
        self._occupato("Test connessione...")
        try:
            self.conn = None
            cur = self._db().cursor()
            cur.execute("SELECT CompnyName FROM OADM")
            azienda = cur.fetchone()[0]
            cur.execute("SELECT ListName FROM OPLN WHERE ListNum = ?", [s.listino(self.cfg)])
            r = cur.fetchone()
            self._scrivi_log(f"Connessione OK - Azienda: {azienda}")
            self._scrivi_log(f"Listino {s.listino(self.cfg)}: {r[0] if r else 'NON TROVATO'}"
                             + (" (prezzi lordi)" if self.cfg['etichette'].get('_listino_lordo') == 'si'
                                else " (prezzi netti + IVA dell'articolo)"))
            self._libero("Connessione a SAP riuscita.")
        except (Exception, SystemExit) as e:
            self.conn = None
            self._scrivi_log("ERRORE: " + _messaggio(e))
            self._libero("Connessione a SAP non riuscita.")

    def _cartella_export(self):
        e = self.cfg["etichette"] if self.cfg.has_section("etichette") else {}
        cartella = e.get("cartella_output", "") or os.path.join(s.SCRIPT_DIR, "export")
        os.makedirs(cartella, exist_ok=True)
        return cartella

    @staticmethod
    def _apri(percorso):
        try:
            os.startfile(percorso)
        except (AttributeError, OSError) as e:
            messagebox.showerror(TITOLO, f"Impossibile aprire:\n{percorso}\n{e}")

    # ------------------------------------------------------------- prezzi tondi
    def prezzi_tondi(self, tabella, articoli=None, modifica=False):
        """modifica=True: un solo articolo (doppio clic sul prezzo), si apre subito la richiesta del prezzo."""
        if articoli is None:
            articoli = [a for a, _ in tabella.selezione()]
        try:
            self._occupato("Leggo il listino da SAP...")
            dec = self._interroga(prezzi.decimali_prezzo)
            info = self._interroga(prezzi.info_listino, s.listino(self.cfg))
            self._interroga(prezzi.costi, self.cfg, articoli)
        except (Exception, SystemExit) as e:
            self._libero("Errore di lettura da SAP.")
            messagebox.showerror(TITOLO, f"Impossibile leggere il listino:\n{_messaggio(e)}")
            return
        self._libero()
        f = FinestraPrezzi(self, tabella, articoli, dec, info)
        if modifica and f.righe:
            f.w.update_idletasks()
            f.modifica_riga(next(iter(f.righe)))

    # ------------------------------------------------------- cartellini scaffale
    def stampa_cartellini(self, lista, nome_base, anteprima=False):
        if not lista:
            messagebox.showinfo(TITOLO, "Nessun articolo selezionato.")
            return
        articoli = [a for a, _ in lista]
        imp = s.impostazioni_cartellino(self.cfg)
        try:
            self._occupato("Preparo i cartellini...")
            negozio = ""
            if not imp["nome_negozio"]:
                negozio = self._interroga(self._nome_azienda)
            self._interroga(s.contenuti, articoli, imp)
            lavori = s.prepara_cartellini([(a, 1) for a in articoli], self.cfg, imp, negozio)
            if anteprima:
                percorso = os.path.join(self._cartella_export(),
                                        f"cartellini_{nome_base}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
                etichetta.salva_anteprima(lavori, percorso, imp["dpi"])
                self._libero(f"Anteprima cartellini creata: {percorso}")
                self._apri(percorso)
                return
            nome = etichetta.scegli_stampante(imp["stampante"] or self.cb_stampante.get()
                                              or s.impostazioni_etichetta(self.cfg)["nome"])
            self._libero()
            # il rotolo dei cartellini e' diverso da quello delle etichette: il formato nel driver deve coincidere
            try:
                pag_w, pag_h, _ = etichetta.info_pagina(nome)
            except Exception:
                pag_w = pag_h = None
            if pag_h is not None and (pag_w < imp["larghezza_mm"] - 1 or abs(pag_h - imp["altezza_mm"]) > 1):
                if not messagebox.askyesno(
                        TITOLO, f"Nel driver di {nome} il formato carta e' {pag_w:.0f} x {pag_h:.0f} mm, "
                                f"il cartellino e' {imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm.\n\n"
                                "Hai messo il rotolo dei cartellini e impostato il formato nel driver "
                                "(Preferenze stampa > Formato carta)?\n\nStampare lo stesso?"):
                    self.stato.set("Stampa cartellini annullata.")
                    return
            elif len(lavori) > 1 and not messagebox.askyesno(
                    TITOLO, f"Stampare {len(lavori)} cartellini su\n{nome}?\n\n"
                            "Controlla che nella stampante ci sia il rotolo dei cartellini."):
                self.stato.set("Stampa cartellini annullata.")
                return
            self._occupato(f"Invio {len(lavori)} cartellini a {nome}...")
            etichetta.stampa(lavori, nome, imp["dpi"], f"Cartellini SAP - {nome_base}",
                             centra=imp["centra"], sposta_x_mm=imp["sposta_x_mm"], sposta_y_mm=imp["sposta_y_mm"])
        except (Exception, SystemExit) as e:
            self._libero("Errore di stampa.")
            messagebox.showerror(TITOLO, f"Cartellini non stampati:\n{_messaggio(e)}")
            return
        self._libero(f"Inviati {len(lavori)} cartellini a {nome}.")

    @staticmethod
    def _nome_azienda(conn):
        cur = conn.cursor()
        cur.execute("SELECT CompnyName FROM OADM")
        r = cur.fetchone()
        return (r[0] if r else "") or ""

    # ------------------------------------------------------------------ stampa
    def stampa(self, lista, nome_base, anteprima=False):
        if not lista:
            messagebox.showinfo(TITOLO, "Nessun articolo selezionato.")
            return
        totale = sum(q for _, q in lista)
        imp = s.impostazioni_etichetta(self.cfg)
        try:
            self._occupato("Preparo le etichette...")
            lavori = s.prepara_lavori(lista, self.cfg, imp)
            if anteprima:
                percorso = os.path.join(self._cartella_export(),
                                        f"anteprima_{nome_base}_{datetime.now():%Y%m%d_%H%M%S}.pdf")
                etichetta.salva_anteprima(lavori, percorso, imp["dpi"])
                self._libero(f"Anteprima creata: {percorso}")
                self._apri(percorso)
                return
            nome = etichetta.scegli_stampante(self.cb_stampante.get() or imp["nome"])
            self._libero()
            if totale > imp["conferma_sopra"] and not messagebox.askyesno(
                    TITOLO, f"Stampare {totale} etichette su\n{nome}?"):
                self.stato.set("Stampa annullata.")
                return
            self._occupato(f"Invio {totale} etichette a {nome}...")
            _, pag_w, pag_h = etichetta.stampa(
                lavori, nome, imp["dpi"], f"Etichette SAP - {nome_base}",
                centra=imp["centra"], sposta_x_mm=imp["sposta_x_mm"], sposta_y_mm=imp["sposta_y_mm"])
        except (Exception, SystemExit) as e:
            self._libero("Errore di stampa.")
            messagebox.showerror(TITOLO, f"Stampa non riuscita:\n{_messaggio(e)}")
            return
        avviso = ""
        if pag_w < imp["larghezza_mm"] - 1 or abs(pag_h - imp["altezza_mm"]) > 1:
            avviso = (f"  -  ATTENZIONE: formato nel driver {pag_w:.1f} x {pag_h:.1f} mm, "
                      f"etichetta {imp['larghezza_mm']:g} x {imp['altezza_mm']:g} mm")
        self._libero(f"Inviate {totale} etichette a {nome}.{avviso}")


class FinestraPrezzi:
    """Proposta di prezzi IVA inclusa tondi per gli articoli spuntati, scritti nel listino via DI API."""

    COLONNE = ("codice", "descrizione", "iva", "attuale", "nuovo", "netto", "ricarico", "stato")
    TITOLI = ("Codice", "Descrizione", "IVA", "Ivato attuale", "Ivato nuovo", "Nuovo netto", "Ricarico", "Stato")
    LARGHEZZE = (110, 300, 50, 90, 90, 100, 70, 260)

    def __init__(self, app, tabella, articoli, decimali, info_listino):
        self.app, self.tabella, self.dec, self.info = app, tabella, decimali, info_listino
        self.cfg = app.cfg
        self.listino = s.listino(self.cfg)
        self.righe = {}            # iid -> riga di proposta
        self.da_costo = False
        p = self.cfg["prezzi"] if self.cfg.has_section("prezzi") else {}
        fattore = info_listino["fattore"]
        try:
            self.soglia = Decimal(str(p.get("soglia_ricarico", "")).replace(",", "."))
        except Exception:
            self.soglia = None
        if not self.soglia:
            self.soglia = (fattore - Decimal("0.1")) if fattore and fattore > 1 else Decimal("1.3")

        w = self.w = tk.Toplevel(app.root)
        w.title(f"Prezzi tondi - {info_listino['nome']} (listino {self.listino})")
        w.geometry("1150x560")
        w.transient(app.root)
        alto = ttk.Frame(w, padding=10)
        alto.pack(fill="x")
        ttk.Label(alto, text="Prezzo al pubblico (IVA inclusa):").pack(side="left")
        self.cb_metodo = ttk.Combobox(alto, state="readonly", width=16, values=list(prezzi.METODI))
        self.cb_metodo.set(p.get("metodo", "") if p.get("metodo", "") in prezzi.METODI else "Ai 5 centesimi")
        self.cb_metodo.pack(side="left", padx=6)
        self.cb_dir = ttk.Combobox(alto, state="readonly", width=14, values=list(prezzi.DIREZIONI))
        self.cb_dir.set(p.get("direzione", "") if p.get("direzione", "") in prezzi.DIREZIONI else "Per eccesso")
        self.cb_dir.pack(side="left")
        for cb in (self.cb_metodo, self.cb_dir):
            cb.bind("<<ComboboxSelected>>", lambda e: self._ricalcola())
        ttk.Button(alto, text=f"Articoli con ricarico sotto {self._num(self.soglia)}",
                   command=self._ricarico_basso).pack(side="right")

        ttk.Label(w, text="Doppio clic su una riga per scrivere un altro prezzo. Vengono aggiornate solo le righe "
                          "'Da aggiornare'. I prezzi scritti diventano manuali in SAP: non seguono piu' il "
                          "prezzo d'acquisto.", foreground="#555", padding=(10, 0), wraplength=1100,
                  justify="left").pack(anchor="w")
        cont = ttk.Frame(w, padding=(10, 6))
        cont.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(cont, columns=self.COLONNE, show="headings", selectmode="extended")
        for c, t, lg in zip(self.COLONNE, self.TITOLI, self.LARGHEZZE):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=lg, stretch=(c in ("descrizione", "stato")),
                           anchor="e" if c in ("iva", "attuale", "nuovo", "netto", "ricarico") else "w")
        self.tv.tag_configure("ok", foreground="#1b6e2e")
        self.tv.tag_configure("no", foreground="#8a4b00")
        barra = ttk.Scrollbar(cont, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=barra.set)
        self.tv.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", self._modifica)
        self.tv.bind("<Delete>", lambda e: self._togli())

        basso = ttk.Frame(w, padding=10)
        basso.pack(fill="x")
        ttk.Button(basso, text="Togli righe selezionate", command=self._togli).pack(side="left")
        self.info_var = tk.StringVar()
        ttk.Label(basso, textvariable=self.info_var, style="Testa.TLabel").pack(side="left", padx=12)
        ttk.Button(basso, text=f"Aggiorna listino {self.listino} in SAP", style="Stampa.TButton",
                   command=self._aggiorna).pack(side="right")
        self.e_pw = ttk.Entry(basso, width=14, show="*")
        self.e_pw.pack(side="right", padx=(4, 10))
        ttk.Label(basso, text="Password:").pack(side="right")
        self.e_ut = ttk.Entry(basso, width=12)
        self.e_ut.insert(0, self.cfg["sap"].get("utente_sap", "") if self.cfg.has_section("sap") else "")
        self.e_ut.pack(side="right", padx=(4, 10))
        ttk.Label(basso, text="Utente SAP:").pack(side="right")

        self.articoli = articoli
        self._ricalcola()
        if not articoli:
            self.info_var.set("Nessun articolo spuntato: spunta gli articoli nella tabella, "
                              "oppure cerca quelli con ricarico basso.")

    @staticmethod
    def _num(d):
        return App._num(Decimal(str(d)))

    def _metodo(self):
        return self.cb_metodo.get(), prezzi.DIREZIONI[self.cb_dir.get()]

    def _ricalcola(self):
        metodo, direzione = self._metodo()
        if self.da_costo:
            righe = [prezzi.proposta_da_costo(a, self.cfg, self.info["fattore"], metodo, direzione, self.dec)
                     for a in self.articoli]
        else:
            righe = [prezzi.proposta(a, self.cfg, metodo, direzione, self.dec) for a in self.articoli]
        self.tv.delete(*self.tv.get_children())
        self.righe = {}
        for r in righe:
            self.righe[self.tv.insert("", "end")] = r
        self._mostra()

    def _mostra(self):
        euro = lambda v: s.formatta_importo(v, self.cfg) if v is not None else "-"
        da_fare = 0
        for iid, r in self.righe.items():
            if not self.tv.exists(iid):
                continue
            ok = r["stato"] == "Da aggiornare"
            da_fare += ok
            costo = r.get("costo")
            ricarico = r.get("ricarico") or (r["netto"] / costo if costo and r["netto"] else None)
            self.tv.item(iid, values=(r["codice"], r["descrizione"][:60], prezzi._it(r["iva"]) + "%",
                                      euro(r["lordo"]), euro(r["nuovo"]),
                                      prezzi._it(r["nuovo_netto"])
                                      if r["nuovo_netto"] and r["stato"] != "Gia' tondo" else "-",
                                      self._num(Decimal(str(ricarico)).quantize(Decimal("0.01"))) if ricarico else "",
                                      r["stato"]),
                         tags=("ok" if ok else "no",))
        self.info_var.set(f"{da_fare} prezzi da aggiornare su {len(self.tv.get_children())} righe.")

    def _modifica(self, evento):
        iid = self.tv.identify_row(evento.y)
        if iid:
            self.modifica_riga(iid)

    def modifica_riga(self, iid):
        if iid not in self.righe:
            return
        r = self.righe[iid]
        from tkinter import simpledialog
        attuale = prezzi._it(r["nuovo"]) if r["nuovo"] is not None else ""
        testo = simpledialog.askstring(TITOLO, f"{r['codice']} - {r['descrizione'][:50]}\n\n"
                                               "Prezzo al pubblico IVA inclusa:", initialvalue=attuale, parent=self.w)
        if testo is None:
            return
        t = testo.replace("€", "").replace(" ", "").strip()
        if "," in t:
            t = t.replace(".", "").replace(",", ".")
        try:
            valore = Decimal(t)
        except Exception:
            messagebox.showwarning(TITOLO, "Scrivi un prezzo, ad esempio 4,50.", parent=self.w)
            return
        prezzi.imposta_nuovo(r, valore, self.dec)
        self._mostra()

    def _togli(self):
        for iid in self.tv.selection():
            self.tv.delete(iid)
            self.righe.pop(iid, None)
        self._mostra()

    def _ricarico_basso(self):
        try:
            self.app._occupato("Cerco gli articoli con prezzo manuale e ricarico basso...")
            trovati = self.app._interroga(prezzi.ricarico_basso, self.cfg, self.soglia)
            self.app._interroga(s.barcode_articoli, trovati)
            self.app._interroga(s.prezzi_um, self.cfg, trovati)
        except (Exception, SystemExit) as e:
            self.app._libero("Errore di lettura da SAP.")
            messagebox.showerror(TITOLO, f"Ricerca non riuscita:\n{_messaggio(e)}", parent=self.w)
            return
        self.app._libero(f"Articoli con ricarico sotto {self._num(self.soglia)}: {len(trovati)}.")
        if not trovati:
            messagebox.showinfo(TITOLO, f"Nessun articolo con prezzo manuale e ricarico sotto {self._num(self.soglia)}.",
                                parent=self.w)
            return
        self.articoli, self.da_costo = trovati, True
        self._ricalcola()
        self.info_var.set(self.info_var.get() + f"  Proposta: costo x {self._num(self.info['fattore'] or 1)}, "
                                                "poi arrotondato.")

    def _aggiorna(self):
        righe = [r for iid, r in self.righe.items() if self.tv.exists(iid) and r["stato"] == "Da aggiornare"]
        if not righe:
            messagebox.showinfo(TITOLO, "Nessun prezzo da aggiornare.", parent=self.w)
            return
        utente, pw = self.e_ut.get().strip(), self.e_pw.get()
        if not utente or not pw:
            messagebox.showwarning(TITOLO, "Inserisci utente e password con cui entri in SAP.", parent=self.w)
            return
        if not messagebox.askyesno(TITOLO, f"Aggiornare {len(righe)} prezzi nel listino {self.listino} "
                                           f"({self.info['nome']})?\n\nCambiano anche in cassa e nei documenti. "
                                           "I prezzi diventano manuali: non seguono piu' il prezzo d'acquisto.",
                                   parent=self.w):
            return
        self.app._occupato(f"Mi collego a SAP e aggiorno {len(righe)} prezzi...")
        try:
            ver = inventario.versione_sql(self.app._db())
            fatte, errori = prezzi.aggiorna_listino(self.cfg["sap"], ver, utente, pw, self.listino, righe)
        except (Exception, SystemExit) as e:
            self.app._libero("Prezzi non aggiornati.")
            messagebox.showerror(TITOLO, f"Prezzi non aggiornati:\n{_messaggio(e)}", parent=self.w)
            return
        finally:
            self.e_pw.delete(0, "end")
        self.app._salva_config("sap", "utente_sap", utente)
        self.app._salva_config("prezzi", "metodo", self.cb_metodo.get())
        self.app._salva_config("prezzi", "direzione", self.cb_dir.get())
        prezzi.registra(self.app._cartella_export(), self.listino, utente, fatte)
        for r in fatte:
            r["netto"], r["lordo"] = r["nuovo_netto"], r["nuovo"]
            r["stato"] = "Aggiornato in SAP"
            if any(a is r["art"] for a, _, _ in self.tabella.voci):
                self.tabella.nuovo_prezzo_base(r["art"], r["nuovo_netto"])
        for r, motivo in errori:
            r["stato"] = f"Non aggiornato: {motivo}"[:200]
        self._mostra()
        self.app._libero(f"Aggiornati {len(fatte)} prezzi nel listino {self.listino}.")
        testo = f"Aggiornati {len(fatte)} prezzi nel listino {self.listino}."
        if errori:
            testo += f"\n\n{len(errori)} non aggiornati: il motivo e' nella colonna Stato."
        testo += "\n\nI prezzi nella tabella delle etichette sono gia' quelli nuovi: puoi stampare i cartellini."
        messagebox.showinfo(TITOLO, testo, parent=self.w)


def main():
    # con pythonw.exe non c'e' console: evita errori sulle print delle funzioni condivise
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    root = tk.Tk()
    try:
        App(root)
    except SystemExit:
        return
    root.mainloop()


if __name__ == "__main__":
    main()
