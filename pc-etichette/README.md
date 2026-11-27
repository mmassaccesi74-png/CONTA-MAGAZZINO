# Programma Etichette SAP (copia dei file del server)

Vanno nella cartella del programma sul server (con `config.ini`, che non e' qui).

- `app.py` finestra principale; `finestra.py` tabella articoli
- `sap_clabel.py` letture da SAP, prezzi, etichette e cartellini; `etichetta.py` disegno e stampa Toshiba
- `inventario.py` scheda Inventario, ricezione dai telefoni (porta 8766), DI API
- `etichette_rete.py` stampa dall'app Android Etichette SAP (stessa porta e stesso codice dell'inventario)
- `prezzi.py` Prezzi tondi: prezzo al pubblico tondo -> netto nel listino (DI API), registro in `export/prezzi_modificati.csv`
