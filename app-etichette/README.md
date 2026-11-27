# Etichette SAP – app Android

Stampa le etichette prodotto (e i cartellini da scaffale) dal telefono, sulla Toshiba TEC B-SA4T del server.

- **Articoli:** leggi il codice a barre con la fotocamera o il palmare laser, oppure cerca per codice,
  descrizione o EAN. Vedi prezzo e anteprima dell'etichetta, scegli il codice a barre (UM) e quante etichette.
  **Stampa subito** oppure **Aggiungi alla lista**.
- **Documento:** carica un'Entrata merci o una Fattura fornitore per numero (e anno), togli le righe che non
  servono, correggi le quantità e stampa.
- **Da stampare:** la lista accumulata; stampa le etichette o un cartellino per articolo.

L'app non tiene dati di SAP: ogni ricerca e ogni stampa passano dal programma **Etichette SAP** aperto sul server,
che rilegge prezzi e descrizioni da SAP al momento della stampa.

## Collegamento
Telefono sul Wi-Fi aziendale. In Impostazioni: indirizzo e codice scritti nella scheda **Inventario**
del programma Etichette SAP (porta 8766, gli stessi di Conta Magazzino).

## Programma sul server (`pc-etichette`)
Serve `etichette_rete.py` insieme ad `app.py` e `inventario.py` aggiornati, nella cartella del programma
Etichette SAP. Le etichette escono sulla stampante salvata come predefinita in Impostazioni del programma
(`[stampante] nome` in `config.ini`); i cartellini su quella di `[cartellino] stampante`, se indicata.

## Scaricare l'app
Sezione **Releases** del repository > **Etichette SAP** > `EtichetteSAP.apk`.

## Compilazione
Automatica con GitHub Actions (`.github/workflows/app-etichette.yml`). Interfaccia in `www/index.html`,
progetto Capacitor 8 in `android/`.
