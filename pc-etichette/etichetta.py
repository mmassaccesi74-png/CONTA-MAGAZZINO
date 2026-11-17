# -*- coding: utf-8 -*-
"""
etichetta.py - Disegno etichette e stampa diretta su stampante termica Windows
(pensato per Toshiba TEC B-SA4T 203 dpi, funziona con qualunque driver Windows).

L'etichetta viene disegnata come immagine alla risoluzione esatta della stampante,
con le barre del codice a barre allineate ai punti della testina: cosi' il codice
resta leggibile anche con moduli stretti.
"""

import os
import sys

# --------------------------------------------------------------------------
# Codici a barre (generati in proprio: larghezze in moduli interi)
# --------------------------------------------------------------------------
_EAN_L = ["0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011"]
_EAN_R = ["".join("1" if b == "0" else "0" for b in p) for p in _EAN_L]
_EAN_G = [p[::-1] for p in _EAN_R]
_EAN_PARITA = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
               "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]

_C128 = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]


def _check_ean(cifre):
    """Cifra di controllo EAN/UPC: pesi 3,1,3,1... partendo dalla cifra piu' a destra."""
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(cifre)))
    return str((10 - s % 10) % 10)


def _ean13(c):
    parita = _EAN_PARITA[int(c[0])]
    sx = "".join((_EAN_L if p == "L" else _EAN_G)[int(d)] for p, d in zip(parita, c[1:7]))
    dx = "".join(_EAN_R[int(d)] for d in c[7:13])
    return "101" + sx + "01010" + dx + "101"


def _ean8(c):
    return "101" + "".join(_EAN_L[int(d)] for d in c[:4]) + "01010" + "".join(_EAN_R[int(d)] for d in c[4:]) + "101"


def _code128(testo):
    """Code128 compatto: set C (coppie di cifre) per le sequenze numeriche lunghe, set B per il resto."""
    if any(not 32 <= ord(ch) <= 126 for ch in testo):
        return None
    n = len(testo)

    def cifre_da(i):
        j = i
        while j < n and testo[j].isdigit():
            j += 1
        return j - i

    valori, corrente, i = [], None, 0
    while i < n:
        r = cifre_da(i)
        # set C conviene per >= 4 cifre (o 2 se e' l'intero testo / la parte finale o iniziale)
        usa_c = r >= 4 or (r >= 2 and r == n - i and corrente is None) or (r == n and r % 2 == 0)
        if usa_c:
            if r % 2:                       # cifra dispari: la prima va in set B
                if corrente != "B":
                    valori.append(104 if corrente is None else 100)
                    corrente = "B"
                valori.append(ord(testo[i]) - 32)
                i += 1
                r -= 1
            if corrente != "C":
                valori.append(105 if corrente is None else 99)
                corrente = "C"
            for k in range(i, i + r, 2):
                valori.append(int(testo[k:k + 2]))
            i += r
        else:
            if corrente != "B":
                valori.append(104 if corrente is None else 100)
                corrente = "B"
            valori.append(ord(testo[i]) - 32)
            i += 1
    check = (valori[0] + sum(v * k for k, v in enumerate(valori[1:], 1))) % 103
    larghezze = "".join(_C128[v] for v in valori + [check, 106])
    moduli, nero = [], True
    for w in larghezze:
        moduli.append(("1" if nero else "0") * int(w))
        nero = not nero
    return "".join(moduli)


def codifica_barcode(valore):
    """Restituisce (tipo, moduli '1010...', testo_leggibile) oppure None."""
    v = (valore or "").strip()
    if not v:
        return None
    if v.isdigit():
        if len(v) == 13 and _check_ean(v[:12]) == v[12]:
            return "EAN13", _ean13(v), v
        if len(v) == 12 and _check_ean(v[:11]) == v[11]:      # UPC-A -> EAN-13 con 0 iniziale
            return "EAN13", _ean13("0" + v), v
        if len(v) == 8 and _check_ean(v[:7]) == v[7]:
            return "EAN8", _ean8(v), v
    moduli = _code128(v)
    return ("CODE128", moduli, v) if moduli else None


# --------------------------------------------------------------------------
# Disegno etichetta
# --------------------------------------------------------------------------
_FONT_NORMALE = ["arial.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_FONT_GRASSETTO = ["arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_cache_font = {}


def _font(grassetto, px):
    from PIL import ImageFont
    chiave = (grassetto, px)
    if chiave not in _cache_font:
        for nome in (_FONT_GRASSETTO if grassetto else _FONT_NORMALE):
            try:
                _cache_font[chiave] = ImageFont.truetype(nome, px)
                break
            except OSError:
                continue
        else:
            _cache_font[chiave] = ImageFont.load_default()
    return _cache_font[chiave]


def _larghezza(draw, testo, font):
    return draw.textlength(testo, font=font)


def _adatta_testo(draw, testo, max_w, max_h, max_righe, grassetto, min_px=10):
    """Sceglie la dimensione piu' grande che fa stare il testo nel riquadro (a capo automatico)."""
    parole = testo.split()
    righe = [testo]
    px = int(max_h / max_righe / 1.15)
    while px >= min_px:
        font = _font(grassetto, px)
        righe, corrente = [], ""
        for p in parole:
            prova = (corrente + " " + p).strip()
            if _larghezza(draw, prova, font) <= max_w:
                corrente = prova
            else:
                if corrente:
                    righe.append(corrente)
                corrente = p
        if corrente:
            righe.append(corrente)
        if len(righe) <= max_righe and all(_larghezza(draw, r, font) <= max_w for r in righe):
            return font, righe, px
        px -= 1
    # non entra nemmeno al minimo: tronca
    px = min_px
    font = _font(grassetto, px)
    righe, corrente = [], ""
    for p in parole:
        prova = (corrente + " " + p).strip()
        if _larghezza(draw, prova, font) <= max_w or not corrente:
            corrente = prova
        else:
            righe.append(corrente)
            corrente = p
    righe.append(corrente)
    troncato = len(righe) > max_righe
    righe = righe[:max_righe]
    if not troncato and all(_larghezza(draw, r, font) <= max_w for r in righe):
        return font, righe, px
    while righe and _larghezza(draw, righe[-1] + "…", font) > max_w:
        righe[-1] = righe[-1][:-1]
    if righe:
        righe[-1] = righe[-1].rstrip() + "…"
    return font, righe, px


def _disegna_barcode(draw, codice, x0, y0, max_w, h, cifre_h, larghezza_etichetta=None):
    tipo, moduli, leggibile = codice
    quiet = 9 if tipo.startswith("EAN") else 10
    tot = larghezza_etichetta or max_w
    # barre piu' spesse possibile: il codice deve stare nell'area utile e, con la zona
    # di rispetto (quiet zone), nella larghezza intera dell'etichetta (i margini sono bianchi)
    modulo = 1
    for k in (4, 3, 2):
        # zona di rispetto minima su ogni lato: 6 moduli (bastano ai lettori comuni;
        # meglio di barre larghe 1 punto, che in stampa termica diventano illeggibili)
        if len(moduli) * k <= max_w and (len(moduli) + 2 * min(quiet, 6)) * k <= tot:
            modulo = k
            break
    larghezza = len(moduli) * modulo
    x = x0 + (max_w - larghezza) // 2
    for i, m in enumerate(moduli):
        if m == "1":
            draw.rectangle([x + i * modulo, y0, x + (i + 1) * modulo - 1, y0 + h - 1], fill=0)
    font = _font(False, max(10, int(cifre_h / 1.1)))
    tw = _larghezza(draw, leggibile, font)
    draw.text((x0 + (max_w - tw) / 2, y0 + h + 1), leggibile, font=font, fill=0)
    return modulo


def disegna_etichetta(dati, impostazioni):
    """
    dati: dict con codice, descrizione, ean, prezzo_testo
    impostazioni: dict con larghezza_mm, altezza_mm, dpi, margine_mm,
                  mostra_prezzo, mostra_codice, barcode_codice_se_manca_ean
    Ritorna immagine PIL in bianco/nero alla risoluzione della stampante.
    """
    from PIL import Image, ImageDraw

    dpm = impostazioni["dpi"] / 25.4
    W = int(round(impostazioni["larghezza_mm"] * dpm))
    H = int(round(impostazioni["altezza_mm"] * dpm))
    m = int(round(impostazioni["margine_mm"] * dpm))
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    area_w = W - 2 * m
    area_h = H - 2 * m

    valore_bc = dati.get("ean") or (dati.get("codice") if impostazioni["barcode_codice_se_manca_ean"] else "")
    codice_bc = codifica_barcode(valore_bc)
    mostra_prezzo = impostazioni["mostra_prezzo"] and dati.get("prezzo_testo")
    mostra_codice = impostazioni["mostra_codice"]

    # ripartizione verticale
    h_desc = int(area_h * (0.32 if codice_bc else 0.50))
    h_riga = int(area_h * 0.25) if (mostra_prezzo or mostra_codice) else 0
    h_bc_tot = area_h - h_desc - h_riga - int(1.0 * dpm) if codice_bc else 0
    if not codice_bc:
        h_desc = area_h - h_riga

    y = m
    # descrizione (max 2 righe, grassetto)
    font, righe, px = _adatta_testo(d, dati.get("descrizione") or "", area_w, h_desc, 2, True,
                                    min_px=min(int(2.6 * dpm), int(h_desc / 2 / 1.15)))
    for r in righe:
        d.text((m, y), r, font=font, fill=0)
        y += int(px * 1.15)
    y = m + h_desc

    # riga codice (sinistra) + prezzo (destra)
    if h_riga:
        larg_prezzo = 0
        if mostra_prezzo:
            fp, rp, pxp = _adatta_testo(d, dati["prezzo_testo"], area_w * (0.62 if mostra_codice else 1.0), h_riga, 1, True)
            larg_prezzo = _larghezza(d, rp[0], fp)
            d.text((W - m - larg_prezzo, y + (h_riga - pxp * 1.15) / 2), rp[0], font=fp, fill=0)
        if mostra_codice and dati.get("codice"):
            spazio = area_w - larg_prezzo - int(2 * dpm)
            fc, rc, pxc = _adatta_testo(d, dati["codice"], spazio, min(h_riga, int(3.2 * dpm)), 1, False)
            d.text((m, y + (h_riga - pxc * 1.15) / 2), rc[0], font=fc, fill=0)
        y += h_riga

    # codice a barre in basso
    if codice_bc:
        cifre_h = max(int(2.2 * dpm), int(h_bc_tot * 0.22))
        _disegna_barcode(d, codice_bc, m, y + int(1.0 * dpm), area_w, h_bc_tot - cifre_h - 2, cifre_h, W)

    return img.point(lambda p: 0 if p < 128 else 255, "1")


# --------------------------------------------------------------------------
# Stampa su Windows
# --------------------------------------------------------------------------
def _win32():
    if sys.platform != "win32":
        sys.exit("La stampa diretta funziona solo su Windows. Usa --anteprima o --excel.")
    try:
        import win32print
        import win32ui
        from PIL import ImageWin
        return win32print, win32ui, ImageWin
    except ImportError:
        sys.exit("Manca il modulo pywin32. Installa con:  pip install -r requirements.txt")


def elenco_stampanti():
    win32print, _, _ = _win32()
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def scegli_stampante(nome_config):
    """Nome da config, altrimenti la prima che contiene 'SA4T', altrimenti la predefinita."""
    win32print, _, _ = _win32()
    stampanti = elenco_stampanti()
    if nome_config:
        if nome_config in stampanti:
            return nome_config
        simili = [s for s in stampanti if nome_config.lower() in s.lower()]
        if simili:
            return simili[0]
        sys.exit(f"Stampante '{nome_config}' non trovata. Installate: " + "; ".join(stampanti))
    toshiba = [s for s in stampanti if "sa4t" in s.lower().replace("-", "")]
    return toshiba[0] if toshiba else win32print.GetDefaultPrinter()


def info_pagina(nome_stampante):
    """Formato pagina impostato nel driver, in mm: (larghezza, altezza, dpi_x)."""
    _, win32ui, _ = _win32()
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(nome_stampante)
    try:
        dpi_x, dpi_y = hdc.GetDeviceCaps(88), hdc.GetDeviceCaps(90)
        larg = hdc.GetDeviceCaps(110) * 25.4 / dpi_x      # PHYSICALWIDTH
        alt = hdc.GetDeviceCaps(111) * 25.4 / dpi_y       # PHYSICALHEIGHT
        return larg, alt, dpi_x
    finally:
        hdc.DeleteDC()


def stampa(lavori, nome_stampante, dpi, titolo="Etichette SAP B1",
           centra=True, sposta_x_mm=0.0, sposta_y_mm=0.0):
    """
    lavori: lista di (immagine, copie). Un solo job di stampa, una pagina per etichetta.
    centra: centra l'etichetta sulla larghezza della pagina del driver (le Toshiba TEC
            tengono il rotolo centrato sotto la testina: se il formato nel driver e' piu'
            largo dell'etichetta, senza centratura il contenuto finisce a sinistra).
    sposta_x_mm / sposta_y_mm: regolazione fine (+ = verso destra / verso il basso).
    Ritorna (dpi_x, larghezza_pagina_mm, altezza_pagina_mm).
    """
    _, win32ui, ImageWin = _win32()
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(nome_stampante)
    dpi_x, dpi_y = hdc.GetDeviceCaps(88), hdc.GetDeviceCaps(90)   # LOGPIXELSX / LOGPIXELSY
    pag_w, pag_h = hdc.GetDeviceCaps(110), hdc.GetDeviceCaps(111)  # PHYSICALWIDTH / HEIGHT
    off_x, off_y = hdc.GetDeviceCaps(112), hdc.GetDeviceCaps(113)  # PHYSICALOFFSETX / Y
    hdc.StartDoc(titolo)
    try:
        for img, copie in lavori:
            dib = ImageWin.Dib(img.convert("RGB"))
            w = int(round(img.width * dpi_x / dpi))
            h = int(round(img.height * dpi_y / dpi))
            # coordinate GDI: (0,0) = inizio area stampabile, che parte off_x/off_y dal bordo carta
            x = ((pag_w - w) // 2 if centra and pag_w > w else 0) - off_x
            y = -off_y
            x += int(round(sposta_x_mm * dpi_x / 25.4))
            y += int(round(sposta_y_mm * dpi_y / 25.4))
            for _ in range(copie):
                hdc.StartPage()
                dib.draw(hdc.GetHandleOutput(), (x, y, x + w, y + h))
                hdc.EndPage()
        hdc.EndDoc()
    except Exception:
        hdc.AbortDoc()
        raise
    finally:
        hdc.DeleteDC()
    return dpi_x, pag_w * 25.4 / dpi_x, pag_h * 25.4 / dpi_y


def salva_anteprima(lavori, percorso, dpi):
    """PDF con una pagina per articolo (non ripete le copie)."""
    immagini = [img.convert("L") for img, _ in lavori]
    if not immagini:
        return None
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    immagini[0].save(percorso, save_all=True, append_images=immagini[1:], resolution=dpi)
    return percorso
