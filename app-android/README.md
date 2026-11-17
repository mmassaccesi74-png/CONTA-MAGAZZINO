# Conta Magazzino – app Android

App per il conteggio dell'inventario con SAP Business One (insieme al programma Etichette SAP).

- Lettura codici a barre con la fotocamera (ML Kit, funziona senza internet) o con palmare laser.
- Le letture restano sul telefono.
- **Invia al server**: con il telefono sul Wi-Fi aziendale il conteggio arriva direttamente nella scheda Inventario
  del programma Etichette SAP aperto sul server (porta 8766, codice di accesso mostrato nella scheda).
  Allo stesso modo l'app scarica l'anagrafica. In alternativa **Invia come file** (WhatsApp, email, Drive).

## Programma sul server (`pc-etichette`)
`inventario.py` e `app.py` vanno copiati nella cartella del programma Etichette SAP (sostituiscono quelli esistenti).
Sul server serve una volta la regola del firewall per la porta 8766.

## Scaricare l'app
Sezione **Releases** del repository > ultima versione > `ContaMagazzino.apk`.

## Compilazione
Automatica con GitHub Actions a ogni modifica della cartella `app-android`
(`.github/workflows/app-android.yml`). Interfaccia in `www/index.html`, progetto Capacitor 8 in `android/`.
