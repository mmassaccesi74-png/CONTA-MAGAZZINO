# Acquisti Magazzino – app Android

App per preparare **Richieste di acquisto** e **Ordini d'acquisto** per SAP Business One.

- Articoli scelti con il codice a barre (fotocamera o palmare laser), per codice/descrizione,
  oppure dalla scheda **Sotto scorta** (giacenza + in ordine − impegnato sotto la scorta minima di SAP).
- Per ogni riga: quantità in unità di acquisto e fornitore (proposto quello abituale dell'articolo).
- Sul documento: tipo (richiesta o ordine), magazzino, data di consegna richiesta, note.
- **Invia al server**: con il telefono sul Wi-Fi aziendale il documento arriva direttamente al programma
  `pc-acquisti/acquisti.py` aperto sul server (porta 8765, codice di accesso mostrato nella finestra).
  Allo stesso modo l'app scarica l'anagrafica. In alternativa **Invia come file** (WhatsApp, email, Drive).

## Programma sul server (`pc-acquisti`)
Copiare `acquisti.py` e `acquisti.bat` nella cartella del programma Etichette SAP
(usa lo stesso `config.ini` e `sap_clabel.py`). Avvio: doppio clic su `acquisti.bat`.

1. **Crea file anagrafica** → `export/anagrafica_acquisti.json` da caricare nell'app (Impostazioni).
2. **Apri file del telefono** → controllo delle righe con SAP.
3. **Crea documento in SAP** → una Richiesta di acquisto, oppure un Ordine per ogni fornitore (DI API).
   I prezzi li assegna SAP dal listino del fornitore.

## Scaricare l'app
Sezione **Releases** del repository > **Acquisti Magazzino** > `AcquistiMagazzino.apk`.

## Compilazione
Automatica con GitHub Actions (`.github/workflows/app-acquisti.yml`). Interfaccia in `www/index.html`, progetto Capacitor 8 in `android/`.
