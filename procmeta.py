"""Funções leves usadas pela linha de comando e pela janela: a janela as importa sem carregar os modelos."""
import re
from pathlib import Path

CNJ_RE = re.compile(r"(?<!\d)\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}(?!\d)")


def process_number(text: str) -> str:
    """Extrai o número CNJ do processo (ex.: '0020121-88.2026.5.04.0523') do nome do arquivo ou texto."""
    m = CNJ_RE.search(text)
    if m:
        return m.group(0)
    # Formato de 20 dígitos corridos sem pontuação
    m20 = re.search(r"(?<!\d)(\d{7})(\d{2})(\d{4})(\d)(\d{2})(\d{4})(?!\d)", text)
    if m20:
        a, b, c, d, e, f = m20.groups()
        return f"{a}-{b}.{c}.{d}.{e}.{f}"
    return ""


def find_pdfs(inputs: list[Path]) -> list[Path]:
    """Arquivos como vieram; de cada pasta, os PDFs dela (sem descer em subpastas), em ordem alfabética."""
    pdfs: list[Path] = []
    for item in inputs:
        pdfs += sorted(p for p in item.iterdir() if p.suffix.lower() == ".pdf") if item.is_dir() else [item]
    return list(dict.fromkeys(pdfs))
