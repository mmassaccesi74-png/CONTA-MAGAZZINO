# Conta Magazzino – app Android

App per il conteggio dell'inventario con SAP Business One (insieme al programma Etichette SAP).

- Lettura codici a barre con la fotocamera (ML Kit, funziona senza internet) o con palmare laser.
- Anagrafica articoli caricata dal file `anagrafica_inventario.json` creato dal programma Etichette SAP.
- Le letture restano sul telefono; a fine conteggio si invia il file `conteggio_*.json`
  (WhatsApp, email, Drive) e si apre nel programma Etichette SAP, scheda Inventario.

## Scaricare l'app
Sezione **Releases** del repository > ultima versione > `ContaMagazzino.apk`.

## Compilazione
Automatica con GitHub Actions a ogni modifica della cartella `app-android`
(`.github/workflows/app-android.yml`). Interfaccia in `www/index.html`, progetto Capacitor 8 in `android/`.
