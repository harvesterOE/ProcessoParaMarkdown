"""Desenha o ícone do conversor de processos (página jurídica com marcação '#' e faixa dourada, num quadrado bordô) em app.ico.

Rodar de novo só ao mudar o desenho:  .venv\\Scripts\\python tools\\make_icon.py [previa.png]
Segue o mesmo formato dos ícones de PdfParaEpub (azul) e EpubReader (verde), com cor bordô jurídico.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

TILE, PAGE, FOLD = "#7A283B", "#FFFFFF", "#E8CDD3"
INK, MUTED_INK, ACCENT = "#7A283B", "#D9B4BD", "#F2B84B"
SIZES = [16, 20, 24, 32, 40, 48, 64, 256]
SS = 8


def draw(size: int) -> Image.Image:
    """Desenhado numa grade de 256 unidades com superamostragem 8x."""
    k = size * SS / 256
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    def box(x0, y0, x1, y1, color, radius=0):
        d.rounded_rectangle([x0 * k, y0 * k, x1 * k, y1 * k], radius=radius * k, fill=color)

    def poly(points, color):
        d.polygon([(x * k, y * k) for x, y in points], fill=color)

    box(8, 8, 248, 248, TILE, radius=56)
    small, medium = size < 32, 32 <= size < 64
    left, right, top, bottom = (58, 198, 30, 226) if small else (64, 192, 36, 220)
    fold = 0 if size < 24 else 40
    poly([(left, top), (right - fold, top), (right, top + fold), (right, bottom), (left, bottom)], PAGE)
    if fold:
        poly([(right - fold, top), (right, top + fold), (right - fold, top + fold)], FOLD)

    # Faixa dourada representando a limpeza/filtragem dos autos para Markdown
    bar_y = 148
    half = 16 if small else 11
    box(30 if small else 38, bar_y - half, 226 if small else 218, bar_y + half, ACCENT, radius=half)

    if not small:
        # Linhas de texto limpo estruturado acima da faixa e linhas descartadas apagadas abaixo
        h = 15 if medium else 12
        box(left + 18, 66, left + 38, 66 + h, ACCENT, radius=h / 2)
        box(left + 46, 66, right - fold - 10, 66 + h, INK, radius=h / 2)
        if not medium:
            box(left + 18, 92, right - 20, 92 + h, INK, radius=h / 2)
            box(left + 18, 116, right - 38, 116 + h, INK, radius=h / 2)
        else:
            box(left + 18, 98, right - 22, 98 + h, INK, radius=h / 2)
        # Rodapé apagado (eliminado) abaixo da faixa
        box(left + 18, 178, right - 26, 178 + h, MUTED_INK, radius=h / 2)
        if not medium:
            box(left + 18, 198, right - 60, 198 + 10, MUTED_INK, radius=5)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    frames = [draw(s) for s in SIZES]
    ico = Path(__file__).resolve().parent.parent / "app.ico"
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print("gravado:", ico)
    if len(sys.argv) > 1:
        strip = Image.new("RGBA", (760, 420), "#F3F3F3")
        ImageDraw.Draw(strip).rectangle([0, 210, 760, 420], fill="#202124")
        for _row, y in enumerate((30, 240)):
            x = 20
            for f in frames[:-1]:
                strip.alpha_composite(f, (x, y + (64 - f.height) // 2))
                x += f.width + 18
            strip.alpha_composite(draw(128), (x + 10, y - 10))
            strip.alpha_composite(frames[-1].resize((160, 160), Image.LANCZOS), (x + 160, y - 5))
        strip.save(sys.argv[1])


if __name__ == "__main__":
    main()
