"""
Generuje ikony aplikacji z pliku brand/logo-source.jpg.

    python brand/build_assets.py

Zrodlo jest JPG-iem z WYPALONA szachownica - to nie przezroczystosc, tylko realne
piksele. Szachownica jest czysto szara (nasycenie ~0), a logo ma nasycenie 64-159,
wiec prog na nasyceniu rozdziela je czysto.

Kreski logo maja ~31 px przy boku 732 px. Przy zmniejszeniu do 16 px zostaje z nich
0,7 px i znak robi sie wyprany, dlatego kazdy rozmiar dostaje wlasna, pogrubiona
wersje - promien dobrany tak, by kreska mialka po zmniejszeniu co najmniej 1,5 px.
"""
import io
import struct
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

KORZEN = Path(__file__).resolve().parent.parent
ZRODLO = KORZEN / "brand" / "logo-source.jpg"
CEL = KORZEN / "Interface" / "static"

# Prog nasycenia z miekkim przejsciem - zachowuje wygladzone krawedzie.
SAT_LO, SAT_HI = 28.0, 72.0
# rozmiar -> promien pogrubienia (0 = bez zmian)
ROZMIARY = {16: 22, 32: 10, 48: 5, 64: 3}


def wytnij_tlo(img: Image.Image):
    """Zwraca (rgb, alpha) - szachownica staje sie przezroczysta."""
    a = np.asarray(img.convert("RGB")).astype(np.float64)
    sat = a.max(axis=2) - a.min(axis=2)
    alpha = np.clip((sat - SAT_LO) / (SAT_HI - SAT_LO), 0, 1)
    # Piksele czesciowo przezroczyste niosa domieszke szarego tla; podstawiamy pod nie
    # kolor najblizszego w pelni krytego piksela, inaczej po zmniejszeniu wychodzi
    # szara obwodka.
    opaque = alpha > 0.85
    idx = ndimage.distance_transform_edt(~opaque, return_distances=False, return_indices=True)
    return a[idx[0], idx[1]], alpha


def kadruj(rgb, alpha, y0, y1, pad=12):
    al = alpha[y0:y1]
    col = np.where(al.any(axis=0))[0]
    row = np.where(al.any(axis=1))[0]
    x0, x1 = max(0, col.min() - pad), min(al.shape[1], col.max() + 1 + pad)
    ry0, ry1 = max(0, row.min() - pad), min(al.shape[0], row.max() + 1 + pad)
    out = np.zeros((ry1 - ry0, x1 - x0, 4), dtype=np.uint8)
    out[..., :3] = np.round(rgb[y0:y1][ry0:ry1, x0:x1]).astype(np.uint8)
    out[..., 3] = np.round(al[ry0:ry1, x0:x1] * 255).astype(np.uint8)
    return Image.fromarray(out)


def na_kwadrat(img: Image.Image) -> Image.Image:
    """Favicon musi byc kwadratem, inaczej przegladarka rozciagnie znak."""
    w, h = img.size
    bok = max(w, h)
    plotno = Image.new("RGBA", (bok, bok), (0, 0, 0, 0))
    plotno.paste(img, ((bok - w) // 2, (bok - h) // 2), img)
    return plotno


def main() -> None:
    src = Image.open(ZRODLO)
    rgb, alpha = wytnij_tlo(src)

    # Przerwa miedzy znakiem a napisem - dzieli logo na dwa zasoby.
    maska = alpha > 0.1
    prof = maska.sum(axis=1)
    rows = np.where(maska.any(axis=1))[0]
    best, run = (0, 0), 0
    for y in range(rows.min(), rows.max() + 1):
        if prof[y] == 0:
            run += 1
        else:
            if run > best[0]:
                best = (run, y - run)
            run = 0
    granica = best[1]

    znak = na_kwadrat(kadruj(rgb, alpha, rows.min(), granica))

    # Pelny wordmark - proporcje liczymy z jego wlasnego kadru, nie z kwadratu znaku.
    pelne = kadruj(rgb, alpha, rows.min(), rows.max() + 1)
    pw, ph = pelne.size
    pelne.resize((800, round(800 * ph / pw)), Image.LANCZOS).save(
        CEL / "logo.png", optimize=True)

    m = np.asarray(znak).astype(np.float64)
    al, kol = m[..., 3], m[..., :3]
    op = al > 200
    idx = ndimage.distance_transform_edt(~op, return_distances=False, return_indices=True)
    kolory = kol[idx[0], idx[1]]

    def wariant(promien: int, bok: int) -> Image.Image:
        a2 = ndimage.grey_dilation(al, size=(promien * 2 + 1,) * 2) if promien else al
        out = np.zeros(m.shape, dtype=np.uint8)
        out[..., :3] = np.round(kolory).astype(np.uint8)
        out[..., 3] = np.round(a2).astype(np.uint8)
        return Image.fromarray(out).resize((bok, bok), Image.LANCZOS)

    png = {}
    for bok, promien in ROZMIARY.items():
        png[bok] = wariant(promien, bok)
        png[bok].save(CEL / f"favicon-{bok}.png")

    wariant(0, 96).save(CEL / "logo-mark.png")

    # iOS podklada czern pod przezroczystosc, wiec ta ikona dostaje tlo.
    tlo = Image.new("RGBA", (180, 180), (255, 253, 246, 255))
    znak180 = wariant(1, 152)
    tlo.paste(znak180, (14, 14), znak180)
    tlo.convert("RGB").save(CEL / "apple-touch-icon.png")

    # ICO skladamy recznie: PIL potrafi tylko przeskalowac jedno zrodlo, a kazdy
    # rozmiar ma u nas inna grubosc kreski.
    wpisy = []
    for bok in (16, 32, 48):
        buf = io.BytesIO()
        png[bok].save(buf, format="PNG")
        wpisy.append((bok, buf.getvalue()))
    naglowek = struct.pack("<HHH", 0, 1, len(wpisy))
    offset = 6 + 16 * len(wpisy)
    katalog, dane = b"", b""
    for bok, blob in wpisy:
        katalog += struct.pack("<BBBBHHII", bok, bok, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        dane += blob
    (CEL / "favicon.ico").write_bytes(naglowek + katalog + dane)

    print(f"Zapisano do {CEL}: favicon.ico, favicon-16/32/48/64.png, "
          f"apple-touch-icon.png, logo-mark.png, logo.png")


if __name__ == "__main__":
    main()
