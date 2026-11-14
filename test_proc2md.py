"""Autoteste completo do conversor de processos jurídicos (PJe) para Markdown com PaddleOCR-VL-1.6."""
import importlib.util
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from proc2md import (
    PageData,
    PaddleVLEngine,
    clean_auto_intimacao_lines,
    convert,
    detect_repetitive_margin_lines,
    extract_stamps_and_fls,
    inherit_missing_doc_ids,
    otsl_to_markdown,
    parse_summary_catalog,
    strip_pje_standard_lines,
    trim_vlm_repetition,
)
from procmeta import find_pdfs, process_number

HAS_TORCH = importlib.util.find_spec("torch") is not None


def load_font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Arial no Windows, DejaVu no Linux."""
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def test_procmeta() -> None:
    assert process_number("Processo_0020121-88.2026.5.04.0523.pdf") == "0020121-88.2026.5.04.0523"
    assert process_number("ATOrd 00202251720255040523") == "0020225-17.2025.5.04.0523"
    assert process_number("sem_numero") == ""
    assert process_number("protocolo 12345678901234567890") == "", "20 dígitos sem DV válido não é número CNJ"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for name in ["b.pdf", "a.PDF", "notas.txt"]:
            (d / name).write_bytes(b"")
        assert [p.name for p in find_pdfs([d])] == ["a.PDF", "b.pdf"]


def test_otsl_to_markdown() -> None:
    raw = (
        "<fcel>Documentos<lcel><lcel><nl>"
        "<fcel>Verba<fcel>Valor<fcel>Reflexos<nl>"
        "<fcel>Horas Extras<fcel>R$ 1.500,00<fcel>FGTS + 40%<nl>"
        "<fcel>Adicional Noturno<fcel>R$ 420,00<fcel>13º e Férias"
    )
    md = otsl_to_markdown(raw)
    assert "**Documentos**" in md
    assert "| Verba | Valor | Reflexos |" in md
    assert "| Horas Extras | R$ 1.500,00 | FGTS + 40% |" in md


def test_trim_vlm_repetition() -> None:
    assert trim_vlm_repetition("a\nlinha X\nlinha X\nlinha X\nlinha X\nfim") == "a\nlinha X\nfim"
    assert trim_vlm_repetition("ok\nok") == "ok\nok"
    assert trim_vlm_repetition("Total: " + "R$ 0,00 " * 30) == "Total: R$ 0,00 "


def test_inherit_missing_doc_ids() -> None:
    pages = [PageData(1, 1, doc_id="aaaaaaa"), PageData(2, 2), PageData(3, 3, doc_id="aaaaaaa"),
             PageData(4, 4), PageData(5, 5, doc_id="bbbbbbb")]
    inherit_missing_doc_ids(pages)
    assert [p.doc_id for p in pages] == ["aaaaaaa", "aaaaaaa", "aaaaaaa", "", "bbbbbbb"]


def test_extract_stamps_and_fls() -> None:
    sample = (
        "Texto da página.\n"
        "Documento assinado eletronicamente por WILIAM MARQUES TOLAZZI, em 17/02/2026, às 17:22:03 - 74744e2\n"
        "Documento assinado eletronicamente por ANA PAULA PEREIRA DA SILVA, em 09/04/2026, às 08:23:41 - b86b833\n"
        "Fls.: 93\n"
    )
    stamps, fls = extract_stamps_and_fls(sample)
    assert fls == 93
    assert len(stamps) == 2
    assert stamps[0].id == "74744e2" and stamps[1].id == "b86b833"


def test_strip_pje_standard_lines() -> None:
    raw_lines = [
        "PODER JUDICIÁRIO",
        "JUSTIÇA DO TRABALHO",
        "TRIBUNAL REGIONAL DO TRABALHO DA 4ª REGIÃO",
        "3ª VARA DO TRABALHO DE ERECHIM",
        "ATOrd 0020121-88.2026.5.04.0523",
        "RECLAMANTE: ALAN CARLOS PALUDO",
        "RECLAMADO: PESENATTO CONSULTORIA EM PROTECAO VEICULAR LTDA E",
        "OUTROS (1)",
        "Termo de Conclusão",
        "Nesta data, faço os presentes autos conclusos ao(à) Exmo(a). Juiz(íza) do Trabalho.",
        "Erechim, 27 de março de 2026.",
        "Daniela Santa Catarina",
        "Analista Judiciária",
        "Vistos, etc.",
        "1. Recebo a petição inicial.",
        "Página 1 de 2",
        "Documento assinado eletronicamente por ADRIANA KUNRATH, em 27/03/2026, às 19:14:06 - d51f601",
        "https://pje.trt4.jus.br/pjekz/validacao/26032719140659000000185225053?instancia=1",
        "Número do processo: 0020121-88.2026.5.04.0523",
        "Número do documento: 26032719140659000000185225053",
        "Fls.: 70",
    ]
    cleaned = strip_pje_standard_lines(raw_lines)
    assert cleaned == ["Vistos, etc.", "1. Recebo a petição inicial."], cleaned


def test_clean_auto_intimacao() -> None:
    lines = [
        "INTIMAÇÃO",
        "DESTINATÁRIO: ALAN CARLOS PALUDO",
        "Fica V. Sa. intimado para tomar ciência do Despacho ID d51f601 proferido nos autos.",
        "Vistos, etc.",
        "1. Recebo a petição inicial e designo audiência.",
        "2. Intimem-se as partes.",
    ]
    cleaned, ref_id = clean_auto_intimacao_lines(lines, own_doc_id="19c710f")
    assert ref_id == "d51f601"
    assert "1. Recebo a petição inicial" not in "\n".join(cleaned)
    assert "ID `d51f601`" in "\n".join(cleaned)


def test_repetitive_margins_and_catalog() -> None:
    pages = [
        PageData(
            page_no=i,
            fls=i,
            doc_id="74744e2",
            lines=[
                "Rua Antônio Bordignon, nº42, bairro Cerâmica, CEP 99.709-561, Erechim-RS",
                "(054) 9.9121-5488 | wtolazzi@hotmail.com",
                f"Parágrafo exclusivo de conteúdo jurídico da página {i}.",
            ],
        )
        for i in range(2, 6)
    ]
    rep = detect_repetitive_margin_lines(pages)
    assert len(rep) == 2
    sum_text = (
        "SUMÁRIO\nDocumentos\nId. Data da\nAssinatura Documento Tipo\n"
        "74744e2 17/02/2026 17:22 Petição Inicial Petição Inicial\n"
        "d51f601 27/03/2026 19:14 Despacho Despacho\n"
    )
    cat, sum_pages = parse_summary_catalog(["capa", sum_text])
    assert sum_pages == {2}
    assert cat["74744e2"].title == "Petição Inicial" and cat["d51f601"].doc_type == "Despacho"


def make_rotated_scanned_image(angle_cw: int = 90) -> Image.Image:
    """Cria uma imagem de página escaneada com título, texto e tabela e a rotaciona em `angle_cw` graus no sentido horário."""
    img = Image.new("RGB", (1654, 2339), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title = load_font(["C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf"], 46)
    f_body = load_font(["C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"], 34)

    d.text((180, 220), "COMPROVANTE DE PAGAMENTO SALARIAL", font=f_title, fill=(0, 0, 0))
    y = 340
    for line in [
        "Empregador: METALURGICA ERECHIM LTDA",
        "Empregado: JOAO DA SILVA SANTOS",
        "Competência: Outubro de 2025 — Valor líquido recebido: R$ 3.450,00",
        "Declaro que recebi a importância líquida discriminada neste recibo.",
    ]:
        d.text((180, y), line, font=f_body, fill=(0, 0, 0))
        y += 68

    # Desenha uma tabela simples
    x0, y0, cw, rh = 180, 700, 420, 85
    rows = [
        ("Rubrica", "Referência", "Valor"),
        ("Salário Base", "220h", "R$ 3.000,00"),
        ("Insalubridade", "20%", "R$ 450,00"),
    ]
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            bx0 = x0 + c_idx * cw
            by0 = y0 + r_idx * rh
            d.rectangle([bx0, by0, bx0 + cw, by0 + rh], outline=(0, 0, 0), width=3)
            d.text((bx0 + 24, by0 + 22), cell, font=f_body, fill=(0, 0, 0))

    # Gira no sentido horário por angle_cw graus (em PIL, rotate(-angle_cw))
    return img.rotate(-angle_cw, expand=True) if angle_cw else img


def build_synthetic_pje_pdf(pdf_path: Path, with_scan: bool = True) -> None:
    """Gera um PDF de processo PJe com:
    - Pág. 1: Capa do PJe
    - Pág. 2-4: Petição Inicial (ID 74744e2) com cabeçalho/rodapé repetitivo de advogado
    - Pág. 5: Documento escaneado rotacionado em 90° (ID 9e48afe)
    - Pág. 6: Despacho (ID d51f601) com cabeçalho padrão PJe + Termo de Conclusão
    - Pág. 7: Intimação automática (ID 19c710f) repetindo o texto do Despacho ID d51f601
    - Pág. 8: Reanexação duplicada da Petição Inicial (carimbo duplo 74744e2 + b86b833)
    - Pág. 9: Sumário PJe
    Sem `with_scan`, a pág. 5 não é gerada e o PDF converte sem o OCR visual.
    """
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    w, h = A4

    # Pág. 1: Capa
    c.setFont("Helvetica", 10)
    c.drawString(500, 810, "Fls.: 1")
    c.drawString(72, 770, "Poder Judiciário")
    c.drawString(72, 754, "Justiça do Trabalho")
    c.drawString(72, 738, "Tribunal Regional do Trabalho da 4ª Região")
    c.drawString(72, 718, "Ação Trabalhista - Rito Ordinário")
    c.drawString(72, 700, "0020121-88.2026.5.04.0523")
    c.drawString(72, 680, "PARA ACESSAR O SUMÁRIO, CLIQUE AQUI")
    c.drawString(72, 650, "Data da Autuação: 17/02/2026")
    c.drawString(72, 632, "Valor da causa: R$ 50.000,00")
    c.drawString(72, 608, "Partes:")
    c.drawString(72, 590, "RECLAMANTE: JOAO DA SILVA SANTOS")
    c.drawString(72, 572, "RECLAMADO: METALURGICA ERECHIM LTDA")
    c.drawString(72, 40, "PAGINA_CAPA_PROCESSO_PJE")
    c.showPage()

    # Págs. 2-4: Petição Inicial (ID 74744e2) com cabeçalho/rodapé repetitivo do advogado nas 3 páginas
    for i, (sec_title, sec_text) in enumerate([
        (
            "I - DOS FATOS",
            "O reclamante foi admitido em 02/01/2024 para exercer a função de soldador industrial, trabalhando exposto a agentes químicos e ruído excessivo sem proteção adequada durante toda a jornada contratual.",
        ),
        (
            "II - DO DIREITO",
            "Faz jus ao pagamento de diferenças de adicional de insalubridade em grau máximo, conforme preceitua o artigo 192 da Consolidação das Leis do Trabalho e a Norma Regulamentadora nº 15 do Ministério do Trabalho.",
        ),
        (
            "III - DOS PEDIDOS",
            "Requer a procedência total da ação trabalhista com a condenação da reclamada ao pagamento das verbas postuladas, reflexos em férias, décimo terceiro salário, aviso prévio e depósitos de FGTS com multa de 40%.",
        ),
    ], start=2):
        c.setFont("Helvetica", 9)
        c.drawString(500, 810, f"Fls.: {i}")
        c.drawString(72, 790, "ADVOCACIA EXEMPLO ASSOCIADOS - OAB/RS 999.999")
        c.drawString(72, 776, "Rua Sete de Setembro, nº 100, Centro, Erechim-RS - contato@exemplo.adv.br")
        c.setFont("Helvetica", 11)
        c.drawString(72, 700, "AO JUÍZO DO TRABALHO DE ERECHIM-RS" if i == 2 else sec_title)
        c.drawString(72, 660, sec_text[:95])
        c.drawString(72, 640, sec_text[95:])
        c.setFont("Helvetica", 8)
        c.drawString(480, 70, f"Página {i - 1} de 3")
        c.drawString(40, 30, "Documento assinado eletronicamente por ADVOGADO EXEMPLO, em 17/02/2026, às 17:22:03 - 74744e2")
        if i == 4:
            c.drawString(40, 18, "https://pje.trt4.jus.br/pjekz/validacao/26021717143370800000182408847?instancia=1")
            c.drawString(40, 8, "Número do documento: 26021717143370800000182408847")
        c.showPage()

    # Pág. 5: Documento escaneado rotacionado 90° (só o carimbo do PJe é texto; o recibo está na imagem rotacionada)
    if with_scan:
        draw_scanned_page(c, w, h)

    draw_tail_pages(c)


def draw_scanned_page(c: canvas.Canvas, w: float, h: float) -> None:
    rot_img = make_rotated_scanned_image(90)
    c.setFont("Helvetica", 9)
    c.drawString(500, 810, "Fls.: 5")
    c.drawImage(ImageReader(rot_img), 36, 90, width=w - 72, height=h - 150, preserveAspectRatio=True)
    c.setFont("Helvetica", 8)
    c.drawString(40, 30, "Documento assinado eletronicamente por ADVOGADO EXEMPLO, em 17/02/2026, às 17:22:03 - 9e48afe")
    c.showPage()


def draw_tail_pages(c: canvas.Canvas) -> None:
    # Pág. 6: Despacho (ID d51f601)
    c.setFont("Helvetica", 9)
    c.drawString(500, 810, "Fls.: 6")
    y = 780
    for ln in [
        "PODER JUDICIÁRIO",
        "JUSTIÇA DO TRABALHO",
        "TRIBUNAL REGIONAL DO TRABALHO DA 4ª REGIÃO",
        "3ª VARA DO TRABALHO DE ERECHIM",
        "ATOrd 0020121-88.2026.5.04.0523",
        "RECLAMANTE: JOAO DA SILVA SANTOS",
        "RECLAMADO: METALURGICA ERECHIM LTDA",
        "Termo de Conclusão",
        "Nesta data, faço os presentes autos conclusos ao(à) Exmo(a). Juiz(íza) do Trabalho.",
        "Erechim, 27 de março de 2026.",
        "Daniela Santa Catarina",
        "Analista Judiciária",
        "Vistos, etc.",
        "1. Designo perícia técnica de insalubridade no local de trabalho do autor, nomeando-se perito engenheiro de segurança.",
        "2. Intimem-se as partes para apresentação de quesitos e indicação de assistentes técnicos no prazo de 5 dias.",
    ]:
        c.drawString(72, y, ln)
        y -= 18
    c.setFont("Helvetica", 8)
    c.drawString(40, 30, "Documento assinado eletronicamente por JUIZA DO TRABALHO, em 27/03/2026, às 19:14:06 - d51f601")
    c.showPage()

    # Pág. 7: Intimação automática (ID 19c710f) repetindo o texto do Despacho ID d51f601
    c.setFont("Helvetica", 9)
    c.drawString(500, 810, "Fls.: 7")
    y = 780
    for ln in [
        "PODER JUDICIÁRIO",
        "JUSTIÇA DO TRABALHO",
        "TRIBUNAL REGIONAL DO TRABALHO DA 4ª REGIÃO",
        "3ª VARA DO TRABALHO DE ERECHIM",
        "ATOrd 0020121-88.2026.5.04.0523",
        "RECLAMANTE: JOAO DA SILVA SANTOS",
        "RECLAMADO: METALURGICA ERECHIM LTDA",
        "INTIMAÇÃO",
        "Fica V. Sa. intimado para tomar ciência do Despacho ID d51f601 proferido nos autos.",
        "Termo de Conclusão",
        "Nesta data, faço os presentes autos conclusos ao(à) Exmo(a). Juiz(íza) do Trabalho.",
        "Vistos, etc.",
        "1. Designo perícia técnica de insalubridade no local de trabalho do autor, nomeando-se perito engenheiro de segurança.",
        "2. Intimem-se as partes para apresentação de quesitos e indicação de assistentes técnicos no prazo de 5 dias.",
    ]:
        c.drawString(72, y, ln)
        y -= 18
    c.setFont("Helvetica", 8)
    c.drawString(40, 30, "Documento assinado eletronicamente por JUIZA DO TRABALHO, em 27/03/2026, às 19:15:06 - 19c710f")
    c.showPage()

    # Pág. 8: Cópia reanexada da Petição Inicial (carimbo duplo 74744e2 + b86b833)
    c.setFont("Helvetica", 9)
    c.drawString(500, 810, "Fls.: 8")
    c.drawString(72, 700, "AO JUÍZO DO TRABALHO DE ERECHIM-RS")
    c.drawString(72, 660, "I - DOS FATOS: O reclamante foi admitido em 02/01/2024 para exercer a função de soldador.")
    c.setFont("Helvetica", 8)
    c.drawString(40, 42, "Documento assinado eletronicamente por ADVOGADO EXEMPLO, em 17/02/2026, às 17:22:03 - 74744e2")
    c.drawString(40, 28, "Documento assinado eletronicamente por SECRETARIA VARA, em 09/04/2026, às 08:23:41 - b86b833")
    c.showPage()

    # Pág. 9: Sumário PJe
    c.setFont("Helvetica", 9)
    c.drawString(500, 810, "Fls.: 9")
    y = 780
    for ln in [
        "SUMÁRIO",
        "Documentos",
        "Id. Data da",
        "Assinatura Documento Tipo",
        "74744e2 17/02/2026 17:22 Petição Inicial Petição Inicial",
        "9e48afe 17/02/2026 17:22 Comprovante Salarial Recibo",
        "d51f601 27/03/2026 19:14 Despacho Despacho",
        "19c710f 27/03/2026 19:15 Intimação Intimação",
    ]:
        c.drawString(72, y, ln)
        y -= 18
    c.save()


def test_end_to_end_digital() -> None:
    """Processo só com páginas digitais: converte sem carregar torch nem os modelos."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "Processo_0020121-88.2026.5.04.0523.pdf"
        md_path = Path(tmp) / "saida.md"
        build_synthetic_pje_pdf(pdf_path, with_scan=False)
        assert convert(pdf_path, md_path) is None, "não deveria ter carregado o PaddleVLEngine"
        md = md_path.read_text(encoding="utf-8")
        assert not (Path(tmp) / "saida.md.tmp").exists()
        assert "# Processo Trabalhista nº 0020121-88.2026.5.04.0523" in md
        for doc_id in ("74744e2", "d51f601", "19c710f", "b86b833"):
            assert doc_id in md, f"ID {doc_id} ausente no Markdown"
        assert "contato@exemplo.adv.br" not in md
        assert "Daniela Santa Catarina" not in md
        assert "https://pje.trt4.jus.br/pjekz/validacao" not in md
        assert md.count("Designo perícia técnica de insalubridade") == 1
        assert "Reprodução anexa do documento ID `74744e2`" in md
        assert "## Sumário de Documentos do Processo" in md


def test_end_to_end_convert() -> None:
    """Pipeline visual completo (PaddleOCR-VL-1.6); precisa de torch e dos modelos."""
    engine = PaddleVLEngine()
    # 1. Testa diretamente a verificação e correção de orientação em imagem rotacionada 90° e 270°
    for angle_cw in (90, 270):
        rot_img = make_rotated_scanned_image(angle_cw)
        fixed_img, detected_angle = engine.verify_and_fix_orientation(rot_img)
        assert detected_angle == angle_cw, f"Esperava {angle_cw}°, obteve {detected_angle}°"
        assert fixed_img.height > fixed_img.width, "Imagem corrigida deveria voltar ao formato retrato (em pé)"

    # 2. Testa a conversão completa do processo PJe sintético (incluindo a página 5 rotacionada e escaneada)
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "Processo_0020121-88.2026.5.04.0523.pdf"
        md_path = Path(tmp) / "Processo_0020121-88.2026.5.04.0523.md"
        build_synthetic_pje_pdf(pdf_path)
        convert(pdf_path, md_path, engine=engine)
        md = md_path.read_text(encoding="utf-8")

        # Preservação do número do processo, de todas as folhas (Fls. 1..8) e de todos os IDs de documentos
        assert "# Processo Trabalhista nº 0020121-88.2026.5.04.0523" in md
        for fl in range(1, 9):
            assert f"Fl. {fl}" in md, f"Marcação da Fl. {fl} ausente"
        for doc_id in ("74744e2", "9e48afe", "d51f601", "19c710f", "b86b833"):
            assert doc_id in md, f"ID {doc_id} ausente no Markdown"

        # Página 5 (escaneada e girada em 90°): orientação corrigida e texto/tabela lidos pelo PaddleOCR-VL-1.6
        assert "Orientação corrigida (90°)" in md
        assert "COMPROVANTE" in md.upper() or "PAGAMENTO" in md.upper() or "METALURGICA" in md.upper()

        # Eliminação de cabeçalhos/rodapés repetitivos, links de validação e texto duplicado da intimação
        assert "PARA ACESSAR O SUMÁRIO, CLIQUE AQUI" not in md
        assert "PAGINA_CAPA_PROCESSO_PJE" not in md
        assert "https://pje.trt4.jus.br/pjekz/validacao" not in md
        assert "Número do documento:" not in md
        assert "Página 1 de 3" not in md
        assert "contato@exemplo.adv.br" not in md, "Cabeçalho repetitivo do advogado não foi removido"
        assert "Daniela Santa Catarina" not in md, "Termo de Conclusão burocrático não foi removido"

        # O texto do despacho ("Designo perícia técnica...") deve aparecer 1 única vez (no Despacho ID d51f601),
        # e NÃO ser repetido no corpo da Intimação automática ID 19c710f
        assert md.count("Designo perícia técnica de insalubridade") == 1
        assert "Reprodução anexa do documento ID `74744e2`" in md


if __name__ == "__main__":
    test_procmeta()
    test_trim_vlm_repetition()
    test_inherit_missing_doc_ids()
    test_otsl_to_markdown()
    test_extract_stamps_and_fls()
    test_strip_pje_standard_lines()
    test_clean_auto_intimacao()
    test_repetitive_margins_and_catalog()
    test_end_to_end_digital()
    if HAS_TORCH:
        test_end_to_end_convert()
        print("OK")
    else:
        print("OK (sem torch: teste do OCR visual pulado)")
