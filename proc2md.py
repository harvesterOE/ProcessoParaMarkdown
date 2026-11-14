"""Processo jurídico em PDF (PJe Trabalhista) -> Markdown limpo e estruturado.

Usa:
- PP-LCNet_x1_0_doc_ori_safetensors: verifica e corrige a orientação da página (0°, 90°, 180°, 270°).
- PP-DocLayoutV3_safetensors: detecta o layout (títulos, texto, tabelas, cabeçalhos, rodapés, selos).
- PaddleOCR-VL-1.6: reconhece texto ("OCR:"), tabelas ("Table Recognition:") e fórmulas ("Formula Recognition:").

Preserva as marcações de folha/página (Fls.) e o ID de cada documento do PJe, eliminando:
- cabeçalhos e rodapés repetitivos (do PJe, de advogados, peritos e empresas);
- carimbos de assinatura eletrônica, URLs de validação, códigos de conferência e contadores "Página X de Y";
- marcadores padrão do PJe Trabalhista ("Termo de Conclusão", instruções de mídia, cabeçalho repetido das partes);
- texto repetido do despacho/decisão/sentença no corpo da intimação automática;
- cópias integrais de documentos reanexados automaticamente em mandados e notificações.

Uso: python proc2md.py processo.pdf [outro.pdf pasta/ ...] [-o saída] [--ocr-all]
Janela: gui.pyw (arrastar e soltar).
"""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import groupby
import difflib
import html
import os
from pathlib import Path
import re
import sys
import warnings

from PIL import Image, ImageOps
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from procmeta import find_pdfs, process_number

# torch/transformers só são importados quando alguma página precisa do OCR visual (PaddleVLEngine):
# processos 100% digitais convertem sem carregar a pilha de GPU.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

ORI_MODEL_ID = "PaddlePaddle/PP-LCNet_x1_0_doc_ori_safetensors"
LAYOUT_MODEL_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
VL_MODEL_ID = "PaddlePaddle/PaddleOCR-VL-1.6"

# Rótulos do PP-DocLayoutV3 que são cabeçalho/rodapé/numeração/ornamento e não entram no corpo do Markdown
SKIP_LAYOUT_LABELS = {"header", "footer", "number", "seal", "aside_text", "vision_footnote"}
TITLE_LAYOUT_LABELS = {"doc_title", "paragraph_title"}
TEXT_LAYOUT_LABELS = {
    "text", "content", "abstract", "footnote", "figure_title",
    "reference", "reference_content", "algorithm",
}

# Expressões regulares para carimbos e marcadores do PJe Trabalhista
STAMP_PJE_RE = re.compile(
    r"(?:PJe\s*|Dados:\s*\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}\s*[+-]\d{2}'\d{2}'\s*)?"
    r"Documento\s+assinado\s+eletronicamente\s+por\s+(?P<signer>.+?),\s*em\s*"
    r"(?P<date>\d{2}/\d{2}/\d{4}),\s*[àa]s\s*(?P<time>\d{2}:\d{2}(?::\d{2})?)\s*-\s*(?P<id>[0-9a-f]{7})\b",
    re.IGNORECASE,
)
STAMP_ALT_RE = re.compile(
    r"Assinado\s+eletronicamente\s+por:\s*(?P<signer>.+?)\s*-\s*"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<time>\d{2}:\d{2}(?::\d{2})?)\s*-\s*(?P<id>[0-9a-f]{7})\b",
    re.IGNORECASE,
)
FLS_RE = re.compile(r"^\s*Fls\.?:\s*(?P<fls>\d+)\s*$", re.IGNORECASE | re.MULTILINE)

# Linhas de rodapé/validação que devem ser sempre removidas do corpo
PJE_FOOTER_LINE_RES = [
    STAMP_PJE_RE,
    STAMP_ALT_RE,
    re.compile(r"^ZapSign\s+[0-9a-f\-]{20,}\.?.*Documento\s+assinado\s+eletronicamente.*$", re.I),
    re.compile(r"^https?://[^\s]*pje[^\s]*/validacao[^\s]*$", re.I),
    re.compile(r"^N[úu]mero\s+do\s+processo:\s*[\d\-.]+$", re.I),
    re.compile(r"^N[úu]mero\s+do\s+documento:\s*\d{15,}$", re.I),
    re.compile(r"^Fls\.?:\s*\d+$", re.I),
    re.compile(r"^P[áa]gina\s+\d+\s*(?:de|/)\s*\d+$", re.I),
    re.compile(r"^P[áa]g\.?\s*\d+\s*(?:de|/)\s*\d+$", re.I),
    re.compile(r"^PARA\s+ACESSAR\s+O\s+SUM[ÁA]RIO,\s*CLIQUE\s+AQUI$", re.I),
    re.compile(r"^PAGINA_CAPA_PROCESSO_PJE\b", re.I),
    re.compile(r"^Consulte\s+a\s+autenticidade\s+deste\s+documento\b", re.I),
    re.compile(r"^Assinado\s+utilizando\s+o\(s\)\s+seguinte\(s\)\s+selo\(s\)\s+do\s+govbr\b", re.I),
]

# Cabeçalho padrão de atos judiciais do PJe Trabalhista (PODER JUDICIÁRIO / JUSTIÇA DO TRABALHO / TRT / VARA / Classe / Partes)
PJE_COURT_HEADER_RES = [
    re.compile(r"^PODER\s+JUDICI[ÁA]RIO$", re.I),
    re.compile(r"^JUSTI[ÇC]A\s+DO\s+TRABALHO$", re.I),
    re.compile(r"^TRIBUNAL\s+REGIONAL\s+DO\s+TRABALHO\s+DA\s+\d+[ªºa]?\s+REGI[ÃA]O(?:\s*-\s*[12][º°]\s*Grau)?$", re.I),
    re.compile(r"^TRIBUNAL\s+SUPERIOR\s+DO\s+TRABALHO$", re.I),
    re.compile(r"^(?:\d+[ªºa]?\s+)?VARA\s+DO\s+TRABALHO\s+DE\s+.+$", re.I),
    re.compile(r"^POSTO\s+AVAN[ÇC]ADO\s+DA\s+JUSTI[ÇC]A\s+DO\s+TRABALHO\s+.+$", re.I),
    re.compile(r"^CEJUSC\b.*$", re.I),
    re.compile(r"^Ju[íi]zo\s+Auxiliar\s+de\s+Execu[çc][ãa]o\b.*$", re.I),
    re.compile(r"^\d+[ªºa]?\s+TURMA$", re.I),
    re.compile(r"^GABINETE\s+D[OAE]\s+.+$", re.I),
    re.compile(r"^Processo\s+Judicial\s+Eletr[ôo]nico(?:\s*-\s*PJe)?$", re.I),
    re.compile(
        r"^(?:ATOrd|ATSum|ROT|RORSum|AP|AIRO|AIAP|CartPrec\w*|CumSen\w*|ExProv\w*|HTE|TutCaut\w*|ConPag|Monit|AlvJud|RPV|Precat)\s*"
        r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}$",
        re.I,
    ),
]

PJE_PARTY_HEADER_RE = re.compile(
    r"^(?:RECLAMANTE|RECLAMAD[OA]|AUTOR(?:A)?|R[ÉE]U|RECORRENTE|RECORRID[OA]|AGRAVANTE|AGRAVAD[OA]|"
    r"EXEQUENTE|EXECUTAD[OA]|EMBARGANTE|EMBARGAD[OA]|IMPETRANTE|IMPETRAD[OA]|DEPRECANTE|DEPRECAD[OA]|"
    r"REQUERENTE|REQUERID[OA]|RELATOR(?:A)?|TERCEIRO\s+INTERESSADO)\s*:\s*.+$",
    re.I,
)
PJE_OUTROS_RE = re.compile(r"^OUTROS\s*\(\d+\)\s*$", re.I)

# Instruções repetitivas de página de "ARQUIVO DE MÍDIA" do PJe
PJE_MEDIA_BOILERPLATE_RES = [
    re.compile(r"^Para\s+abrir\s+o\s+link\s+em\s+nova\s+aba,\s+pressione\s+simultaneamente\b.*$", re.I),
    re.compile(r"^Caso\s+a\s+m[íi]dia\s+n[ãa]o\s+reproduza\s+no\s+navegador\s+utilizado\b.*$", re.I),
    re.compile(r"^solicitar\s+o\s+download\s+do\s+arquivo\.?$", re.I),
]

# Intimação/Notificação automática do PJe que cola o texto do documento referenciado logo abaixo
AUTO_INTIMACAO_RE = re.compile(
    r"Fica\s+V\.?\s*Sa?\.?[ªa]?\s+(?:intimad[oa]|notificad[oa])(?:\([oa]\))?\s+"
    r"(?:para\s+tomar\s+ci[êe]ncia\s+d[oa]|para\s+ci[êe]ncia\s+quanto\s+ao\s+inteiro\s+teor\s+d[oa])\s*"
    r"(?P<kind>Despacho|Decis[ãa]o|Senten[çc]a|Ac[óo]rd[ãa]o|Ato\s+Ordinat[óo]rio|Certid[ãa]o)\s+"
    r"(?:de\s+)?ID\s+(?P<ref_id>[0-9a-f]{7})\b"
    r"(?:\s+proferid[oa]\s+nos\s+autos)?(?:\s*,\s*cujo\s+(?:dispositivo|teor)\s+consta\s+a\s+seguir\s*:?)?",
    re.IGNORECASE,
)

# Artefatos ocasionais de OCR em brasões/logotipos isolados
OCR_LOGO_ARTIFACTS = {"中华人民共和国国徽", "AI", "Pje", "PJe"}


@dataclass
class StampInfo:
    id: str
    signer: str = ""
    date: str = ""
    time: str = ""


@dataclass
class DocCatalogEntry:
    id: str
    date: str = ""
    title: str = ""
    doc_type: str = ""


@dataclass
class PageData:
    page_no: int
    fls: int
    doc_id: str = ""
    orig_id: str = ""
    signer: str = ""
    sign_date: str = ""
    sign_time: str = ""
    rotation_fixed: int = 0
    used_ocr: bool = False
    is_cover: bool = False
    is_summary: bool = False
    lines: list[str] = field(default_factory=list)


def _from_pretrained_local_first(cls, model_id: str, **kwargs):
    """Carrega do cache local imediatamente (sem tentar rede); só baixa do Hub se ainda não estiver em cache."""
    try:
        return cls.from_pretrained(model_id, local_files_only=True, **kwargs)
    except Exception:
        return cls.from_pretrained(model_id, local_files_only=False, **kwargs)


class PaddleVLEngine:
    """Agrupa os três modelos do pipeline PaddleOCR-VL-1.6 (orientação + layout + VLM)."""

    def __init__(self, device: str | None = None) -> None:
        import torch
        from transformers import (
            AutoImageProcessor,
            AutoModelForImageClassification,
            AutoModelForImageTextToText,
            AutoModelForObjectDetection,
            AutoProcessor,
        )

        # MIOpen (ROCm no Windows) falha com miopenStatusUnknownError; os kernels nativos do torch funcionam na GPU.
        torch.backends.cudnn.enabled = False
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.dtype = torch.bfloat16 if self.device == "cuda" else torch.float32

        # 1. Classificador de orientação de página (0°, 90°, 180°, 270°)
        self.ori_processor = _from_pretrained_local_first(AutoImageProcessor, ORI_MODEL_ID)
        self.ori_model = _from_pretrained_local_first(AutoModelForImageClassification, ORI_MODEL_ID).to(self.device).eval()

        # 2. Detector de layout e ordem de leitura (PP-DocLayoutV3)
        self.layout_processor = _from_pretrained_local_first(AutoImageProcessor, LAYOUT_MODEL_ID)
        self.layout_model = _from_pretrained_local_first(AutoModelForObjectDetection, LAYOUT_MODEL_ID).to(self.device).eval()

        # 3. Modelo visão-linguagem principal (PaddleOCR-VL-1.6)
        self.vl_processor = _from_pretrained_local_first(AutoProcessor, VL_MODEL_ID)
        self.vl_processor.tokenizer.padding_side = "left"
        self.vl_processor.image_processor.size = {"shortest_edge": 28 * 28 * 4, "longest_edge": 768 * 28 * 28}
        self.vl_model = _from_pretrained_local_first(
            AutoModelForImageTextToText, VL_MODEL_ID, dtype=self.dtype
        ).to(self.device).eval()

    def predict_orientation(self, img: Image.Image, strip_margins: bool = True) -> tuple[int, float, float]:
        """Devolve (ângulo_previsto, confiança_prevista, confiança_em_0_graus) para a imagem."""
        import torch

        w, h = img.size
        body = img.crop((int(w * 0.03), int(h * 0.05), int(w * 0.97), int(h * 0.92))) if strip_margins else img
        inv = ImageOps.invert(body.convert("L")).point(lambda x: 255 if x > 30 else 0)
        bb = inv.getbbox()
        if bb and (bb[2] - bb[0]) >= 60 and (bb[3] - bb[1]) >= 40:
            body = body.crop(bb)
        with torch.inference_mode():
            inp = self.ori_processor(images=body, return_tensors="pt").to(self.device)
            out = self.ori_model(**inp)
            logits = out.logits if hasattr(out, "logits") else out.last_hidden_state
            probs = torch.softmax(logits, dim=-1)[0]
            pred = int(probs.argmax(-1).item())
            angle = int(self.ori_model.config.id2label[pred])
            conf = float(probs[pred].item())
            conf_0 = float(probs[0].item())
        return angle, conf, conf_0

    def verify_and_fix_orientation(self, img: Image.Image) -> tuple[Image.Image, int]:
        """Verifica se a página está rotacionada (90°, 180°, 270°) e corrige para 0° (em pé).
        Confirma a rotação verificando se a imagem corrigida passa a ser classificada como 0°."""
        angle, conf, _ = self.predict_orientation(img, strip_margins=True)
        if angle in (90, 180, 270) and conf >= 0.68:
            rotated = img.rotate(angle, expand=True)
            after_angle, _, after_conf_0 = self.predict_orientation(rotated, strip_margins=False)
            if after_angle == 0 and after_conf_0 >= conf:
                return rotated, angle
        return img, 0

    def detect_layout(self, img: Image.Image, threshold: float = 0.45) -> list[tuple[str, float, list[int]]]:
        """Devolve lista de (rótulo, score, [x0, y0, x1, y1]) na ordem de leitura do PP-DocLayoutV3."""
        import torch

        with torch.inference_mode():
            inp = self.layout_processor(images=img, return_tensors="pt").to(self.device)
            out = self.layout_model(**inp)
            res = self.layout_processor.post_process_object_detection(
                out, threshold=threshold, target_sizes=[img.size[::-1]]
            )[0]
        blocks: list[tuple[str, float, list[int]]] = []
        w, h = img.size
        for score, label_id, box in zip(res["scores"], res["labels"], res["boxes"]):
            lab = self.layout_model.config.id2label[int(label_id.item())]
            x0, y0, x1, y1 = [round(v) for v in box.tolist()]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            if x1 - x0 >= 12 and y1 - y0 >= 10:
                blocks.append((lab, float(score.item()), [x0, y0, x1, y1]))
        return blocks

    def recognize_crop(self, crop: Image.Image, task: str = "ocr", max_new_tokens: int = 1024) -> str:
        """Executa o PaddleOCR-VL-1.6 em um recorte com o prompt da tarefa ('ocr', 'table', 'formula')."""
        import torch

        prompts = {
            "ocr": "OCR:",
            "table": "Table Recognition:",
            "formula": "Formula Recognition:",
        }
        prompt = prompts.get(task, "OCR:")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": crop},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        inputs = self.vl_processor.apply_chat_template(
            [messages],
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.device)
        with torch.inference_mode():
            generated = self.vl_model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                use_cache=True,
            )
        prompt_len = inputs["input_ids"].shape[1]
        text = self.vl_processor.decode(generated[0, prompt_len:], skip_special_tokens=True)
        return text.strip()


def trim_vlm_repetition(text: str) -> str:
    """Corta os laços de repetição do VLM (a mesma linha ou o mesmo trecho gerado sem parar até o limite de tokens):
    3+ linhas idênticas seguidas viram uma; um trecho de 8+ caracteres repetido 5+ vezes seguidas vira um."""
    out: list[str] = []
    for _key, grp in groupby(text.splitlines(), key=str.strip):
        grp = list(grp)
        out += grp[:1] if len(grp) >= 3 and _key else grp
    return re.sub(r"(.{8,200}?)\1{4,}", r"\1", "\n".join(out), flags=re.S)


def otsl_to_markdown(raw: str) -> str:
    """Converte a saída de tabela do PaddleOCR-VL-1.6 (formato OTSL <fcel>/<lcel>/<ucel>/<ecel>/<nl> ou HTML)
    em uma tabela Markdown (GFM)."""
    text = raw.strip()
    if not text:
        return ""

    # Caso 1: tags OTSL (<fcel>, <ecel>, <lcel>, <ucel>, <xcel>, <nl>)
    if "<fcel>" in text or "<ecel>" in text or "<nl>" in text:
        row_chunks = [r.strip() for r in re.split(r"<nl>|\n+", text) if r.strip()]
        grid: list[list[tuple[str, str]]] = []
        cell_re = re.compile(r"<(fcel|ecel|lcel|ucel|xcel)>(.*?)(?=<(?:fcel|ecel|lcel|ucel|xcel|nl)>|$)", re.S)
        for chunk in row_chunks:
            matches = cell_re.findall(chunk)
            if not matches:
                continue
            row: list[tuple[str, str]] = []
            for tag, val in matches:
                cleaned = " ".join(val.strip().split()).replace("|", r"\|")
                row.append((tag, cleaned))
            grid.append(row)
        if not grid:
            return re.sub(r"<(?:fcel|ecel|lcel|ucel|xcel|nl)>", " ", text).strip()

        caption = ""
        # Se a primeira linha for um título mesclado na largura toda (ex.: <fcel>Documentos<lcel><lcel><lcel>)
        if (
            len(grid) >= 2
            and len(grid[0]) >= 2
            and grid[0][0][0] == "fcel"
            and all(t == "lcel" for t, _ in grid[0][1:])
            and grid[0][0][1]
        ):
            caption = f"**{grid[0][0][1]}**\n\n"
            grid = grid[1:]

        ncols = max((len(r) for r in grid), default=0)
        if ncols == 0:
            return caption.strip()

        str_rows: list[list[str]] = []
        for r_idx, row in enumerate(grid):
            vals: list[str] = []
            for c_idx in range(ncols):
                if c_idx < len(row):
                    tag, val = row[c_idx]
                    if tag == "ucel" and str_rows and c_idx < len(str_rows[-1]):
                        val = ""
                    vals.append(val)
                else:
                    vals.append("")
            if any(vals):
                str_rows.append(vals)

        if not str_rows:
            return caption.strip()

        header = str_rows[0]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row_vals in str_rows[1:]:
            lines.append("| " + " | ".join(row_vals) + " |")
        return caption + "\n".join(lines)

    # Caso 2: tabela em HTML (<table>...</table>)
    if "<table" in text.lower() and "<tr" in text.lower():
        rows: list[list[str]] = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.I | re.S)
            if cells:
                cleaned = [
                    " ".join(html.unescape(re.sub(r"<[^>]+>", " ", c)).split()).replace("|", r"\|")
                    for c in cells
                ]
                if any(cleaned):
                    rows.append(cleaned)
        if rows:
            ncols = max(len(r) for r in rows)
            padded = [r + [""] * (ncols - len(r)) for r in rows]
            lines = [
                "| " + " | ".join(padded[0]) + " |",
                "| " + " | ".join("---" for _ in padded[0]) + " |",
            ]
            for r in padded[1:]:
                lines.append("| " + " | ".join(r) + " |")
            return "\n".join(lines)

    return text


def merge_text_blocks(
    blocks: list[tuple[str, float, list[int]]], page_w: int, page_h: int
) -> list[tuple[str, list[int]]]:
    """Agrupa blocos adjacentes de texto corrido na mesma coluna em recortes maiores para o PaddleOCR-VL-1.6,
    preservando tabelas, fórmulas e títulos em blocos próprios."""
    merged: list[tuple[str, list[int]]] = []
    max_crop_h = int(page_h * 0.48)
    max_gap_y = int(page_h * 0.06)

    for lab, _score, box in blocks:
        if lab in SKIP_LAYOUT_LABELS:
            continue
        kind = (
            "table" if lab == "table"
            else "formula" if lab == "formula"
            else "title" if lab in TITLE_LAYOUT_LABELS
            else "text" if lab in TEXT_LAYOUT_LABELS
            else "image" if lab == "image"
            else "other"
        )
        if kind in ("image", "other"):
            continue
        if (
            kind == "text"
            and merged
            and merged[-1][0] == "text"
            and 0 <= box[1] - merged[-1][1][3] <= max_gap_y
            and (max(box[3], merged[-1][1][3]) - min(box[1], merged[-1][1][1])) <= max_crop_h
            and abs(box[0] - merged[-1][1][0]) < page_w * 0.35
        ):
            prev = merged[-1][1]
            merged[-1] = ("text", [min(prev[0], box[0]), min(prev[1], box[1]), max(prev[2], box[2]), max(prev[3], box[3])])
        else:
            merged.append((kind, list(box)))
    return merged


def ocr_page_with_vlm(
    img: Image.Image, engine: PaddleVLEngine
) -> tuple[list[str], int, list[StampInfo], int | None]:
    """Corrige a orientação da página, detecta o layout com PP-DocLayoutV3 e lê os blocos com PaddleOCR-VL-1.6.
    Também extrai carimbos de ID e Fls. dos blocos de cabeçalho/rodapé caso o PDF seja 100% imagem."""
    oriented_img, angle = engine.verify_and_fix_orientation(img)
    w, h = oriented_img.size
    raw_blocks = engine.detect_layout(oriented_img)

    # Se precisar extrair ID/Fls de página 100% escaneada sem camada de texto, guarda recortes de footer/number
    meta_crops: list[Image.Image] = []
    for lab, _score, box in raw_blocks:
        if lab in ("footer", "number", "header") and (box[1] < h * 0.12 or box[3] > h * 0.88):
            pad_box = (max(0, box[0] - 4), max(0, box[1] - 4), min(w, box[2] + 4), min(h, box[3] + 4))
            meta_crops.append(oriented_img.crop(pad_box))

    merged_blocks = merge_text_blocks(raw_blocks, w, h)

    # Página escaneada onde o documento inteiro (ex.: CNH, print de IRPF, comprovante) foi classificado como 'image'
    content_area = sum((b[2] - b[0]) * (b[3] - b[1]) for _, b in merged_blocks)
    if content_area < 0.08 * w * h:
        img_boxes = [
            b for lab, _, b in raw_blocks
            if lab == "image" and (b[2] - b[0]) * (b[3] - b[1]) >= 0.04 * w * h
        ]
        if img_boxes:
            x0 = max(0, min(b[0] for b in img_boxes) - 6)
            y0 = max(0, min(b[1] for b in img_boxes) - 6)
            x1 = min(w, max(b[2] for b in img_boxes) + 6)
            y1 = min(h, max(b[3] for b in img_boxes) + 6)
            merged_blocks.append(("text", [x0, y0, x1, y1]))
        elif not merged_blocks:
            # Fallback: corpo central da página (sem faixa de cabeçalho/rodapé do PJe)
            merged_blocks.append(("text", [int(w * 0.03), int(h * 0.06), int(w * 0.97), int(h * 0.92)]))

    lines: list[str] = []
    for kind, box in merged_blocks:
        pad = 6
        cbox = (max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(h, box[3] + pad))
        crop = oriented_img.crop(cbox)
        if kind == "table":
            raw_tbl = engine.recognize_crop(crop, task="table", max_new_tokens=1200)
            md_tbl = otsl_to_markdown(raw_tbl)
            if md_tbl:
                lines.extend(md_tbl.splitlines())
                lines.append("")
        elif kind == "formula":
            raw_f = engine.recognize_crop(crop, task="formula", max_new_tokens=256)
            if raw_f:
                lines.append(f"$${raw_f}$$")
        elif kind == "title":
            raw_t = trim_vlm_repetition(engine.recognize_crop(crop, task="ocr", max_new_tokens=256))
            cleaned_t = " ".join(raw_t.split())
            if cleaned_t and cleaned_t not in OCR_LOGO_ARTIFACTS:
                lines.append(f"### {cleaned_t}")
        else:
            raw_txt = trim_vlm_repetition(engine.recognize_crop(crop, task="ocr", max_new_tokens=1024))
            for ln in raw_txt.splitlines():
                s = ln.strip()
                if s and s not in OCR_LOGO_ARTIFACTS:
                    lines.append(s)

    ocr_stamps: list[StampInfo] = []
    ocr_fls: int | None = None
    for mcrop in meta_crops:
        mtxt = engine.recognize_crop(mcrop, task="ocr", max_new_tokens=160)
        st, fl = extract_stamps_and_fls(mtxt)
        ocr_stamps.extend(st)
        if fl is not None and ocr_fls is None:
            ocr_fls = fl

    return lines, angle, ocr_stamps, ocr_fls


def extract_stamps_and_fls(text: str) -> tuple[list[StampInfo], int | None]:
    """Extrai todos os carimbos de assinatura eletrônica do PJe (ID de 7 caracteres hex, signatário, data/hora)
    e a numeração de folha ('Fls.: N') presentes no texto de uma página."""
    stamps: list[StampInfo] = []
    for rx in (STAMP_PJE_RE, STAMP_ALT_RE):
        for m in rx.finditer(text):
            stamps.append(
                StampInfo(
                    id=m.group("id").lower(),
                    signer=" ".join(m.group("signer").split()),
                    date=m.group("date"),
                    time=m.group("time"),
                )
            )
    fls_match = FLS_RE.search(text)
    fls = int(fls_match.group("fls")) if fls_match else None
    return stamps, fls


def parse_summary_catalog(pages_text: list[str]) -> tuple[dict[str, DocCatalogEntry], set[int]]:
    """Analisa o SUMÁRIO final do PJe (e certidões de distribuição) para mapear ID -> (data, título, tipo).
    Devolve (catálogo_por_id, conjunto_de_páginas_1_indexed_que_são_sumário)."""
    catalog: dict[str, DocCatalogEntry] = {}
    summary_pages: set[int] = set()
    in_summary = False

    # No SUMÁRIO do PJe, cada entrada começa com "<id_hex_7> <dd/mm/aaaa hh:mm> <documento...>"
    entry_start_re = re.compile(r"^([0-9a-f]{7})\s+(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?)\s+(.+)$", re.I)
    # Na Certidão de Distribuição: "<id_hex_7> <documento> <tipo> <tamanho_kb>"
    dist_start_re = re.compile(r"^([0-9a-f]{7})\s+(?!\d{2}/\d{2}/\d{4})(.+)$", re.I)

    known_types = [
        "Petição Inicial", "Procuração", "Declaração de Hipossuficiência", "Documento de Identificação",
        "Cadastro de Pessoas Físicas (CPF)", "Cadastro Nacional de Pessoa Jurídica (CNPJ)", "Recibo",
        "Documento Diverso", "Certidão", "Despacho", "Intimação", "Notificação", "Mandado",
        "Contestação", "Sentença", "Decisão", "Ata da Audiência", "Laudo Pericial", "Manifestação",
        "Recurso Ordinário", "Contrarrazões", "Acórdão", "Termo de Rescisão de Contrato de Trabalho (TRCT)",
        "Cartão de Ponto/Controle de Frequência", "Atestado de Saúde Ocupacional (ASO)", "Contrato",
        "Comprovante de Depósito Judicial", "Substabelecimento", "Razões Finais", "Cálculos", "Planilha de Cálculo",
    ]

    for idx, raw in enumerate(pages_text):
        pno = idx + 1
        lines = [
            ln.strip() for ln in raw.splitlines()
            if ln.strip() and not FLS_RE.match(ln.strip())
        ]
        if not lines:
            continue
        if lines[0].upper() == "SUMÁRIO" or (
            len(lines) > 1 and "SUMÁRIO" in lines[0].upper() and "DOCUMENTOS" in lines[1].upper()
        ):
            in_summary = True
        if in_summary:
            summary_pages.add(pno)

        cur_id, cur_date, cur_parts = "", "", []

        def flush_entry() -> None:
            nonlocal cur_id, cur_date, cur_parts
            if not cur_id or not cur_parts:
                return
            full = " ".join(cur_parts)
            full = re.sub(r"\s+\d{3,10}$", "", full).strip()  # remove tamanho em KB se vier de certidão
            doc_title, doc_type = full, ""
            for kt in sorted(known_types, key=len, reverse=True):
                if full.endswith(kt):
                    doc_type = kt
                    doc_title = full[: -len(kt)].strip() or kt
                    break
            if not doc_type:
                # Se repetir a mesma expressão duas vezes ("Despacho Despacho"), separa
                words = full.split()
                half = len(words) // 2
                if half >= 1 and words[:half] == words[half:]:
                    doc_title = doc_type = " ".join(words[:half])
            catalog[cur_id] = DocCatalogEntry(id=cur_id, date=cur_date, title=doc_title, doc_type=doc_type)
            cur_id, cur_date, cur_parts = "", "", []

        for ln in lines:
            if any(rx.search(ln) for rx in PJE_FOOTER_LINE_RES):
                continue
            if ln in ("SUMÁRIO", "Documentos", "Id.", "Data da", "Assinatura", "Documento", "Tipo", "Id. Data da", "Assinatura Documento Tipo"):
                continue
            m = entry_start_re.match(ln)
            if m:
                flush_entry()
                cur_id, cur_date, cur_parts = m.group(1).lower(), m.group(2), [m.group(3).strip()]
            elif in_summary and cur_id:
                cur_parts.append(ln)
            elif "Documentos do Processo" in raw:
                m2 = dist_start_re.match(ln)
                if m2:
                    flush_entry()
                    cur_id, cur_date, cur_parts = m2.group(1).lower(), "", [m2.group(2).strip()]
                elif cur_id and not ln.startswith("Documento assinado"):
                    cur_parts.append(ln)
        flush_entry()

    return catalog, summary_pages


def is_pje_cover_page(text: str) -> bool:
    """Identifica a página de capa gerada pelo PJe Trabalhista."""
    return "PAGINA_CAPA_PROCESSO_PJE" in text or (
        "PARA ACESSAR O SUMÁRIO, CLIQUE AQUI" in text.upper() and "DATA DA AUTUAÇÃO" in text.upper()
    )


def format_cover_markdown(text: str, fls: int) -> list[str]:
    """Formata a capa do processo PJe de forma limpa, sem links mortos nem marcadores internos."""
    out = [f"**[Fl. {fls} | Capa do Processo]**", ""]
    for raw_ln in text.splitlines():
        ln = " ".join(raw_ln.split())
        if not ln:
            continue
        ln = re.sub(r"\bPAGINA_CAPA_PROCESSO_PJE\b\s*", "", ln).strip()
        if not ln or any(rx.search(ln) for rx in PJE_FOOTER_LINE_RES):
            continue
        if ln.lower() in ("poder judiciário", "justiça do trabalho", "processo judicial eletrônico"):
            continue
        if re.match(r"^(?:RECLAMANTE|RECLAMAD[OA]|ADVOGAD[OA]|PERIT[OA]|AUTOR|R[ÉE]U|EXEQUENTE|EXECUTAD[OA]):", ln, re.I):
            out.append(f"- **{ln.partition(':')[0].strip()}:** {ln.partition(':')[2].strip()}")
        elif re.match(r"^(?:Data da Autua[çc][ãa]o|Valor da causa):", ln, re.I):
            out.append(f"- **{ln.partition(':')[0].strip()}:** {ln.partition(':')[2].strip()}")
        elif ln.lower() == "partes:":
            out.append("\n### Partes e Procuradores")
        else:
            out.append(ln)
    return out


def strip_pje_standard_lines(lines: list[str], is_first_page_of_doc: bool = True) -> list[str]:
    """Elimina linhas de rodapé/validação do PJe, cabeçalhos padrão da Justiça do Trabalho,
    bloco repetitivo de partes no topo de atos judiciais, 'Termo de Conclusão' e instruções de mídia."""
    # 1. Remove rodapés de assinatura/validação, Fls., Página X de Y e instruções de mídia
    filtered: list[str] = []
    for raw_ln in lines:
        ln = re.sub(r"([A-Za-zÀ-ÿ])\ufffd([A-Za-zÀ-ÿ])", r"\1-\2", raw_ln)
        ln = ln.replace("\ufffd", "")
        ln = " ".join(ln.replace("\xa0", " ").split())
        if not ln or ln in OCR_LOGO_ARTIFACTS:
            continue
        if any(rx.search(ln) for rx in PJE_FOOTER_LINE_RES):
            continue
        if any(rx.search(ln) for rx in PJE_MEDIA_BOILERPLATE_RES):
            continue
        # Remove carimbo inline caso tenha vindo colado no final de outra linha
        ln = STAMP_PJE_RE.sub("", ln).strip()
        ln = STAMP_ALT_RE.sub("", ln).strip()
        if ln:
            filtered.append(ln)

    # 2. Remove cabeçalho padrão do PJe no topo da página (PODER JUDICIÁRIO / TRT / VARA / ATOrd / Partes)
    idx = 0
    saw_court_header = False
    in_party_block = False
    while idx < min(len(filtered), 18):
        ln = filtered[idx]
        if any(rx.match(ln) for rx in PJE_COURT_HEADER_RES):
            saw_court_header = True
            idx += 1
            continue
        if saw_court_header and (
            PJE_PARTY_HEADER_RE.match(ln)
            or PJE_OUTROS_RE.match(ln)
            or (in_party_block and re.search(r"\bE\s+OUTROS\s*\(\d+\)\s*$", ln, re.I))
        ):
            in_party_block = True
            idx += 1
            continue
        break
    filtered = filtered[idx:]

    # 3. Remove o bloco burocrático "Termo de Conclusão / Nesta data, faço os presentes autos conclusos..."
    # quando seguido de "Vistos, etc."
    if len(filtered) >= 4 and re.match(r"^Termo\s+de\s+Conclus[ãa]o$", filtered[0], re.I):
        vistos_idx = next(
            (i for i, l in enumerate(filtered[:10]) if re.match(r"^Vistos\b", l, re.I)),
            None,
        )
        if vistos_idx is not None:
            filtered = filtered[vistos_idx:]

    return filtered


def norm_header_footer_key(line: str) -> str:
    """Chave normalizada para detectar cabeçalhos e rodapés repetitivos entre páginas (advogados, peritos, empresas)."""
    s = " ".join(line.lower().split())
    s = re.sub(r"\b(?:p[áa]gina|p[áa]g\.?|fls?\.?)\s*\d+(?:\s*(?:de|/)\s*\d+)?\b", "", s)
    s = re.sub(r"\d+", "#", s)
    return s.strip(" -–—•|.,;:/")


def detect_repetitive_margin_lines(pages: list[PageData]) -> set[str]:
    """Identifica linhas curtas nas margens superior/inferior (primeiras 3 e últimas 3 linhas de cada página)
    que se repetem em >= 3 páginas do processo (ex.: endereço/telefone/e-mail de escritório de advocacia,
    credenciais de perito, cabeçalho de empresa)."""
    counts: Counter[str] = Counter()
    for p in pages:
        if p.is_cover or p.is_summary or len(p.lines) < 2:
            continue
        top_n = min(3, max(1, len(p.lines) - 1))
        bot_n = min(2, max(0, len(p.lines) - top_n - 1))
        margin_candidates = p.lines[:top_n] + (p.lines[-bot_n:] if bot_n else [])
        seen_on_page: set[str] = set()
        for ln in margin_candidates:
            if len(ln) > 120 or ln.startswith(("#", "|", "**[Fl.", "*(")):
                continue
            key = norm_header_footer_key(ln)
            if len(key) >= 6:
                seen_on_page.add(key)
        for key in seen_on_page:
            counts[key] += 1

    # Linhas que se repetem nas margens de 3 ou mais páginas
    return {k for k, c in counts.items() if c >= 3}


def clean_auto_intimacao_lines(lines: list[str], own_doc_id: str) -> tuple[list[str], str | None]:
    """Se as linhas forem de uma Intimação/Notificação automática do PJe que reproduz o texto do despacho/decisão/sentença
    referenciado logo abaixo ('Fica V. Sa. intimado para tomar ciência do Despacho ID xxxxxxx...'),
    mantém o cabeçalho da intimação e o aviso, eliminando o texto repetido do documento referenciado.
    Devolve (linhas_limpas, id_referenciado_se_detectado)."""
    for i, ln in enumerate(lines[:8]):
        m = AUTO_INTIMACAO_RE.search(ln)
        if m:
            kind = m.group("kind")
            ref_id = m.group("ref_id").lower()
            if ref_id != own_doc_id:
                kept = lines[: i + 1]
                # Remove ": cujo dispositivo/teor consta a seguir:" se ficou no final da frase
                kept[-1] = re.sub(
                    r",?\s*cujo\s+(?:dispositivo|teor)\s+consta\s+a\s+seguir\s*:?\s*$",
                    ".",
                    kept[-1],
                    flags=re.I,
                )
                kept.append(f"*(Texto referente ao {kind} ID `{ref_id}` suprimido por já constar nos autos.)*")
                return kept, ref_id
    return lines, None


def page_needs_visual_ocr(page: pdfium.PdfPage, raw_text: str, stripped_lines: list[str], ocr_all: bool) -> bool:
    """Decide se a página precisa passar pelo pipeline visual (PP-LCNet_doc_ori + PP-DocLayoutV3 + PaddleOCR-VL-1.6)."""
    if ocr_all:
        return True
    body_chars = sum(len(ln) for ln in stripped_lines)
    if body_chars < 140:
        return True
    if "\ufffd" in raw_text and raw_text.count("\ufffd") > 3:
        return True
    # Verifica se há imagens bitmap grandes na página (ex.: prints de documentos, CNH, recibos colados)
    w, h = page.get_size()
    page_area = max(1.0, w * h)
    try:
        objs = list(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))
    except Exception:
        objs = []
    img_area = 0.0
    for obj in objs:
        try:
            l, b, r, t = obj.get_bounds()
            area = max(0.0, (r - l) * (t - b))
            if area > 0.05 * page_area:
                img_area += area
        except Exception:
            continue
    # Se imagens grandes ocupam parte relevante da página e há pouco texto digital
    if img_area > 0.15 * page_area and body_chars < 900:
        return True
    return False


def _is_caps_heading(s: str) -> bool:
    """Identifica títulos em caixa alta (mesmo que contenham conectivos curtos como 'e', 'de', '§6º')."""
    if len(s) > 100:
        return s.isupper()
    if s.isupper():
        return True
    words = re.findall(r"[A-Za-zÀ-ÿ]+", s)
    if not words:
        return False
    long_words = [w for w in words if len(w) >= 3]
    return bool(long_words) and all(w.isupper() for w in long_words)


def join_wrapped_lines(lines: list[str]) -> list[str]:
    """Reagrupa linhas quebradas no meio de frases pela diagramação do PDF em parágrafos fluidos,
    preservando tabelas Markdown, listas, títulos e campos 'Chave: Valor'."""
    out: list[str] = []
    field_re = re.compile(r"^[A-ZÀ-Ú][A-Za-zÀ-ÿ0-9\s/().\-]{1,35}:\s+.+$")
    list_re = re.compile(r"^(?:[-*•]|\d+\s*[–\-.)]|[a-z][.)]|[IVXLCDM]+\s*[–\-.)])\s+")
    end_punct_re = re.compile(r'[.!?:;”"\')]$')

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if (
            s.startswith(("#", "|", "**[Fl.", "*(", "- **", "$$"))
            or list_re.match(s)
            or field_re.match(s)
            or _is_caps_heading(s)
        ):
            out.append(s)
            continue
        if (
            out
            and not out[-1].startswith(("#", "|", "**[Fl.", "*(", "- **", "$$"))
            and not _is_caps_heading(out[-1])
            and not field_re.match(out[-1])
            and not end_punct_re.search(out[-1])
        ):
            if re.search(r"[a-zà-ÿ]-$", out[-1]) and s[:1].islower():
                out[-1] = out[-1][:-1] + s
            elif out[-1].endswith("/") or s.startswith("/"):
                out[-1] = out[-1] + s
            else:
                out[-1] = f"{out[-1]} {s}"
        else:
            out.append(s)
    return out


def convert(
    pdf_path: Path,
    md_path: Path,
    ocr_all: bool = False,
    engine: PaddleVLEngine | None = None,
    report: Callable[..., None] = lambda msg, transient=False: None,
) -> PaddleVLEngine | None:
    """Converte um processo jurídico em PDF para Markdown limpo e estruturado.
    Devolve a instância de `PaddleVLEngine` (se carregada) para reaproveitamento em lote."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        total_pages = len(pdf)
        raw_texts: list[str] = [pdf[i].get_textpage().get_text_range() for i in range(total_pages)]
        catalog, summary_pages = parse_summary_catalog(raw_texts)

        page_stamps: list[tuple[list[StampInfo], int | None]] = [
            extract_stamps_and_fls(t) for t in raw_texts
        ]

        pages_data: list[PageData] = []
        seen_primary_ids: dict[str, tuple[int, int]] = {}  # doc_id -> (fl_ini, fl_fim)
        skip_intimacao_doc_id: str | None = None
        skip_intimacao_ref_id: str | None = None

        for idx in range(total_pages):
            pno = idx + 1
            raw_t = raw_texts[idx]
            stamps, fls = page_stamps[idx]
            fls_no = fls if fls is not None else pno

            doc_id = stamps[-1].id if stamps else ""
            orig_id = stamps[0].id if len(stamps) > 1 and stamps[0].id != doc_id else ""
            signer = stamps[-1].signer if stamps else ""
            sign_date = stamps[-1].date if stamps else ""
            sign_time = stamps[-1].time if stamps else ""

            p_data = PageData(
                page_no=pno,
                fls=fls_no,
                doc_id=doc_id,
                orig_id=orig_id,
                signer=signer,
                sign_date=sign_date,
                sign_time=sign_time,
            )

            # 1. Capa do processo PJe
            if pno == 1 and is_pje_cover_page(raw_t):
                p_data.is_cover = True
                p_data.lines = format_cover_markdown(raw_t, fls_no)
                pages_data.append(p_data)
                report(f"página {pno}/{total_pages}", transient=True)
                continue

            # 2. Páginas de SUMÁRIO ao final do processo
            if pno in summary_pages:
                p_data.is_summary = True
                pages_data.append(p_data)
                report(f"página {pno}/{total_pages}", transient=True)
                continue

            # 3. Cópia integral reanexada de documento já emitido anteriormente (ex.: Inicial/Despacho anexo a Mandado)
            if orig_id and orig_id in seen_primary_ids:
                orig_range = seen_primary_ids[orig_id]
                fl_str = f"fl. {orig_range[0]}" if orig_range[0] == orig_range[1] else f"fls. {orig_range[0]}–{orig_range[1]}"
                p_data.lines = [
                    f"*(Reprodução anexa do documento ID `{orig_id}`, já constante às {fl_str}.)*"
                ]
                pages_data.append(p_data)
                report(f"página {pno}/{total_pages}", transient=True)
                continue

            # 4. Continuação de Intimação automática cujo texto repetido já foi suprimido na 1ª página da intimação
            if doc_id and doc_id == skip_intimacao_doc_id:
                p_data.lines = [
                    f"*(Continuação do texto repetido do documento ID `{skip_intimacao_ref_id}` na intimação `{doc_id}`.)*"
                ]
                pages_data.append(p_data)
                report(f"página {pno}/{total_pages}", transient=True)
                continue

            # Extrai e limpa preliminarmente o texto digital da página
            raw_lines = [ln.strip() for ln in raw_t.splitlines() if ln.strip()]
            cleaned_digital = strip_pje_standard_lines(raw_lines)

            # Verifica se é a 1ª página de uma Intimação automática que repete documento anterior
            cleaned_digital, ref_id = clean_auto_intimacao_lines(cleaned_digital, doc_id)
            if ref_id is not None:
                skip_intimacao_doc_id = doc_id
                skip_intimacao_ref_id = ref_id
                p_data.lines = cleaned_digital
                pages_data.append(p_data)
                report(f"página {pno}/{total_pages}", transient=True)
                continue

            # 5. Decide se usa o pipeline visual (PP-LCNet_doc_ori + PP-DocLayoutV3 + PaddleOCR-VL-1.6)
            if page_needs_visual_ocr(pdf[idx], raw_t, cleaned_digital, ocr_all):
                if engine is None:
                    report("carregando modelos…")
                    engine = PaddleVLEngine()
                scale = 180 / 72
                pil_img = pdf[idx].render(scale=scale).to_pil().convert("RGB")
                vlm_lines, rot_angle, ocr_stamps, ocr_fls = ocr_page_with_vlm(pil_img, engine)
                p_data.used_ocr = True
                p_data.rotation_fixed = rot_angle
                if not p_data.doc_id and ocr_stamps:
                    p_data.doc_id = ocr_stamps[-1].id
                    p_data.signer = ocr_stamps[-1].signer
                    p_data.sign_date = ocr_stamps[-1].date
                    p_data.sign_time = ocr_stamps[-1].time
                if fls is None and ocr_fls is not None:
                    p_data.fls = ocr_fls
                cleaned_vlm = strip_pje_standard_lines(vlm_lines)
                cleaned_vlm, ref_id_ocr = clean_auto_intimacao_lines(cleaned_vlm, p_data.doc_id)
                if ref_id_ocr is not None and p_data.doc_id:
                    skip_intimacao_doc_id = p_data.doc_id
                    skip_intimacao_ref_id = ref_id_ocr
                p_data.lines = cleaned_vlm if cleaned_vlm else cleaned_digital
            else:
                p_data.lines = cleaned_digital

            if p_data.doc_id and not p_data.orig_id:
                if p_data.doc_id not in seen_primary_ids:
                    seen_primary_ids[p_data.doc_id] = (p_data.fls, p_data.fls)
                else:
                    first_fl, _ = seen_primary_ids[p_data.doc_id]
                    seen_primary_ids[p_data.doc_id] = (first_fl, p_data.fls)

            pages_data.append(p_data)
            report(f"página {pno}/{total_pages}", transient=True)

        report("montando o Markdown…")
        markdown_text = assemble_markdown(pdf_path, raw_texts, pages_data, catalog)
        # Grava ao lado e troca no fim: um .md pela metade nunca fica no lugar (o --skip-existing o pularia)
        tmp_path = md_path.with_name(md_path.name + ".tmp")
        tmp_path.write_text(markdown_text, encoding="utf-8")
        os.replace(tmp_path, md_path)
        return engine
    finally:
        pdf.close()


def inherit_missing_doc_ids(pages: list[PageData]) -> None:
    """Página sem carimbo legível (ex.: escaneada sem camada de texto) entre páginas do mesmo documento
    herda o ID dele, para não quebrar o documento em dois blocos com uma página "Sem ID" no meio."""
    for i in range(1, len(pages) - 1):
        p, prev, nxt = pages[i], pages[i - 1], pages[i + 1]
        if p.doc_id or p.is_cover or p.is_summary or prev.is_cover or nxt.is_summary:
            continue
        if prev.doc_id and (prev.doc_id, prev.orig_id) == (nxt.doc_id, nxt.orig_id):
            p.doc_id, p.orig_id = prev.doc_id, prev.orig_id
            p.signer, p.sign_date, p.sign_time = prev.signer, prev.sign_date, prev.sign_time


def deduplicate_consecutive_notices(pages: list[PageData]) -> None:
    """Se duas intimações/notificações consecutivas para destinatários distintos tiverem corpo idêntico (>= 92% igual),
    mantém os dados únicos (ex.: DESTINATÁRIO) e substitui o restante por referência à anterior."""
    prev_doc_id = ""
    prev_body = ""
    for p in pages:
        if p.is_cover or p.is_summary or not p.doc_id or p.doc_id == prev_doc_id or not p.lines:
            continue
        body_str = "\n".join(p.lines)
        if (
            prev_body
            and len(body_str) > 250
            and re.search(r"\b(?:INTIMA[ÇC][ÃA]O|NOTIFICA[ÇC][ÃA]O|MANDADO)\b", body_str, re.I)
            and difflib.SequenceMatcher(None, prev_body, body_str).ratio() >= 0.90
        ):
            header_lines = [
                ln for ln in p.lines[:5]
                if re.match(r"^(?:INTIMA[ÇC][ÃA]O|NOTIFICA[ÇC][ÃA]O|MANDADO.*|DESTINAT[ÁA]RIO.*|AUDI[ÊE]NCIA.*)$", ln, re.I)
            ]
            p.lines = header_lines + [
                f"*(Teor idêntico ao documento ID `{prev_doc_id}` imediatamente anterior.)*"
            ]
        else:
            prev_doc_id = p.doc_id
            prev_body = body_str


def assemble_markdown(
    pdf_path: Path,
    raw_texts: list[str],
    pages: list[PageData],
    catalog: dict[str, DocCatalogEntry],
) -> str:
    """Monta o documento Markdown final com cabeçalho do processo, seções por ID de documento,
    marcadores de página/folha e sumário estruturado."""
    inherit_missing_doc_ids(pages)

    # 1. Remove linhas de margem repetitivas (escritórios de advocacia, peritos, empresas) e reagrupa parágrafos
    rep_margins = detect_repetitive_margin_lines(pages)
    for p in pages:
        if p.is_cover or p.is_summary or not p.lines:
            continue
        if rep_margins and len(p.lines) >= 2:
            top_n = min(3, max(1, len(p.lines) - 1))
            bot_n = min(2, max(0, len(p.lines) - top_n - 1))
            new_lines: list[str] = []
            for idx, ln in enumerate(p.lines):
                in_margin = idx < top_n or (bot_n > 0 and idx >= len(p.lines) - bot_n)
                if in_margin and len(ln) <= 120 and norm_header_footer_key(ln) in rep_margins:
                    continue
                new_lines.append(ln)
            p.lines = new_lines
        p.lines = join_wrapped_lines(p.lines)

    # 2. Deduplica notificações/mandados consecutivos idênticos
    deduplicate_consecutive_notices(pages)

    # 3. Calcula o intervalo de folhas (Fls.) de cada bloco de documento (ID)
    doc_ranges: dict[int, tuple[int, int]] = {}
    i = 0
    while i < len(pages):
        key = (pages[i].doc_id, pages[i].orig_id, pages[i].is_cover, pages[i].is_summary)
        j = i
        while j + 1 < len(pages) and (
            pages[j + 1].doc_id, pages[j + 1].orig_id, pages[j + 1].is_cover, pages[j + 1].is_summary
        ) == key:
            j += 1
        doc_ranges[i] = (pages[i].fls, pages[j].fls)
        i = j + 1

    # 4. Título principal do arquivo Markdown
    cnj = process_number(pdf_path.stem) or process_number("\n".join(raw_texts[:3]))
    title = f"# Processo Trabalhista nº {cnj}" if cnj else f"# {pdf_path.stem}"
    out: list[str] = [title, ""]

    cur_key: tuple[str, str, bool, bool] | None = None
    for idx, p in enumerate(pages):
        if p.is_summary:
            continue
        key = (p.doc_id, p.orig_id, p.is_cover, p.is_summary)
        if key != cur_key:
            cur_key = key
            fl_start, fl_end = doc_ranges.get(idx, (p.fls, p.fls))
            fl_span = f"Fl. {fl_start}" if fl_start == fl_end else f"Fls. {fl_start}–{fl_end}"
            if p.is_cover:
                out.append("## Capa do Processo")
                out.append("")
            elif p.doc_id:
                cat = catalog.get(p.doc_id)
                if p.orig_id:
                    orig_cat = catalog.get(p.orig_id)
                    orig_label = f" ({orig_cat.title})" if orig_cat and orig_cat.title else ""
                    out.append("---")
                    out.append("")
                    out.append(f"## Documento ID `{p.doc_id}` — Anexo (original ID `{p.orig_id}`{orig_label})")
                elif cat and cat.title:
                    type_suffix = f" ({cat.doc_type})" if cat.doc_type and cat.doc_type.lower() != cat.title.lower() else ""
                    out.append("---")
                    out.append("")
                    out.append(f"## Documento ID `{p.doc_id}` — {cat.title}{type_suffix}")
                else:
                    out.append("---")
                    out.append("")
                    out.append(f"## Documento ID `{p.doc_id}`")

                meta_parts = [fl_span]
                if p.signer:
                    meta_parts.append(f"Assinado por: {p.signer}")
                if p.sign_date:
                    dt = f"{p.sign_date} {p.sign_time}".strip()
                    meta_parts.append(f"Data: {dt}")
                elif cat and cat.date:
                    meta_parts.append(f"Data: {cat.date}")
                out.append(f"*{' | '.join(meta_parts)}*")
                out.append("")

        # Marcação de página e ID preservada em todas as páginas
        if not p.is_cover:
            id_badge = f"ID: `{p.doc_id}`" if p.doc_id else "Sem ID"
            if p.orig_id:
                id_badge += f" (orig.: `{p.orig_id}`)"
            page_badge = f"Fl. {p.fls}" if p.fls == p.page_no else f"Fl. {p.fls} | Pág. {p.page_no}"
            if p.rotation_fixed:
                page_badge += f" | Orientação corrigida ({p.rotation_fixed}°)"
            out.append(f"**[{page_badge} | {id_badge}]**")
            out.append("")

        if p.lines:
            for ln in p.lines:
                out.append(ln)
                if not ln.startswith("|"):
                    out.append("")

    # 5. Se houver SUMÁRIO de documentos do PJe, inclui tabela limpa ao final com marcação das páginas de sumário
    summary_fls = [p.fls for p in pages if p.is_summary]
    if catalog:
        out.append("---")
        out.append("")
        fl_sum = (
            f" (Fls. {summary_fls[0]}–{summary_fls[-1]})"
            if len(summary_fls) > 1
            else f" (Fl. {summary_fls[0]})" if summary_fls
            else ""
        )
        out.append(f"## Sumário de Documentos do Processo{fl_sum}")
        out.append("")
        if summary_fls:
            out.append(f"**[Fls. {summary_fls[0]}–{summary_fls[-1]} | Sumário PJe]**" if len(summary_fls) > 1 else f"**[Fl. {summary_fls[0]} | Sumário PJe]**")
            out.append("")
        out.append("| ID | Data | Documento | Tipo |")
        out.append("| --- | --- | --- | --- |")
        for entry in catalog.values():
            t_clean = entry.title.replace("|", r"\|")
            ty_clean = (entry.doc_type or entry.title).replace("|", r"\|")
            out.append(f"| `{entry.id}` | {entry.date} | {t_clean} | {ty_clean} |")
        out.append("")

    # Normaliza múltiplas linhas em branco consecutivas
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entradas", type=Path, nargs="+", metavar="PDF_OU_PASTA",
                    help="um ou mais PDFs de processos jurídicos, ou pastas com PDFs (lote)")
    ap.add_argument("-o", "--out", type=Path,
                    help="arquivo .md (um PDF só) ou pasta de saída (padrão: ao lado de cada PDF)")
    ap.add_argument("--ocr-all", action="store_true",
                    help="força o OCR visual (PaddleOCR-VL-1.6) também nas páginas que já possuem camada de texto digital")
    ap.add_argument("--skip-existing", action="store_true",
                    help="lote: pula PDFs cujo arquivo .md já existe")
    a = ap.parse_args()

    entradas, out = a.entradas, a.out
    if out is None and len(entradas) == 2 and entradas[1].suffix.lower() == ".md":
        entradas, out = entradas[:1], entradas[1]
    missing = [str(p) for p in entradas if not p.exists()]
    if missing:
        ap.error("não encontrado: " + ", ".join(missing))
    pdfs = find_pdfs(entradas)
    if not pdfs:
        ap.error("nenhum PDF nas entradas")
    single_file = out is not None and out.suffix.lower() == ".md"
    if len(pdfs) > 1 and single_file:
        ap.error("saída .md única só vale para um único PDF")
    if out and not single_file:
        out.mkdir(parents=True, exist_ok=True)

    tty = sys.stdout.isatty()
    if not tty:
        sys.stdout.reconfigure(encoding="utf-8")
    on_page_line = False

    def report(msg: str, transient: bool = False) -> None:
        nonlocal on_page_line
        if not tty:
            print(msg, flush=True)
            return
        print(("\r" if on_page_line else "") + f"{msg:<55}", end="" if transient else "\n", flush=True)
        on_page_line = transient

    engine: PaddleVLEngine | None = None
    failed = 0
    for i, pdf in enumerate(pdfs, 1):
        md = out if single_file else (out or pdf.parent) / (pdf.stem + ".md")
        report(f"[{i}/{len(pdfs)}] {pdf.name}")
        if a.skip_existing and md.exists():
            report(f"já existe, pulado: {md}")
            continue
        try:
            engine = convert(pdf, md, ocr_all=a.ocr_all, engine=engine, report=report)
            report(f"Markdown gerado: {md}")
        except Exception as e:
            failed += 1
            report(f"ERRO: {pdf.name}: {e}")
    if len(pdfs) > 1:
        report(f"{len(pdfs) - failed} de {len(pdfs)} convertidos" + (f", {failed} com erro" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
