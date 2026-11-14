# Processo Jurídico (PDF) para Markdown

Converte autos judiciais em PDF (especialmente **PJe Trabalhista**) para Markdown limpo e estruturado usando **PaddleOCR-VL-1.6**, preservando a numeração de folhas (`Fl. N`) e os identificadores dos documentos (`ID xxxxxxx`) enquanto elimina textos repetitivos.

## O que o conversor faz

1. **Verificação e correção automática de orientação (`0°`, `90°`, `180°`, `270°`):**
   - Antes do reconhecimento visual, cada página escaneada ou com imagem passa pelo classificador `PP-LCNet_x1_0_doc_ori_safetensors` (com validação em duas etapas) para endireitar páginas deitadas ou de cabeça para baixo (comuns em CNHs, CTPS, TRCTs, holerites e laudos digitalizados).
2. **Análise de layout e OCR multimodal com `PaddleOCR-VL-1.6`:**
   - Páginas nativas digitais têm o texto extraído diretamente com rapidez; páginas escaneadas, documentos colados como imagem ou tabelas complexas passam por `PP-DocLayoutV3_safetensors` (que já descarta blocos visuais `header`, `footer`, `number`, `aside_text` e `seal`) e pelo VLM `PaddleOCR-VL-1.6` (convertendo texto, títulos e tabelas OTSL para Markdown).
3. **Preservação de `Fl.` e `ID`:**
   - Agrupa o processo por documento (`## [ID xxxxxxx] Título (Tipo) — Fls. X–Y`) cruzando os carimbos laterais/rodapés com o `SUMÁRIO` do PJe, e insere marcadores exatos de folha (`**[Fl. X | ID xxxxxxx]**`) em todas as páginas.
4. **Eliminação de textos repetitivos em 6 camadas:**
   - **Rodapés e carimbos do PJe:** `Documento assinado eletronicamente por...`, `Número do processo:...`, `Número do documento: 250...`, links `https://pje.trt...`, código de barras/QR Code, `Fls.: N` solto e `Página X de Y`.
   - **Cabeçalhos padrão da Justiça do Trabalho:** blocos `PODER JUDICIÁRIO / JUSTIÇA DO TRABALHO / TRT / VARA DO TRABALHO / ATOrd... / RECLAMANTE: ... / RECLAMADO: ...`, `PAGINA_CAPA_PROCESSO_PJE` e `Termo de Conclusão` burocrático antes de `Vistos, etc.`.
   - **Intimações automáticas do PJe:** quando uma `Intimação` ou `Notificação` automática reproduz no próprio corpo o inteiro teor do despacho, decisão ou sentença (`Fica V. Sa. intimado para tomar ciência do(a) Despacho/Decisão/Sentença ID xxxxxxx...`), mantém o cabeçalho e o aviso da intimação e suprime a cópia repetida do ato.
   - **Documentos reanexados (duplo carimbo):** quando petições ou despachos anteriores são reanexados a mandados/certidões (páginas com dois IDs onde o ID original já foi emitido nos autos), substitui as dezenas de páginas duplicadas por uma referência concisa às folhas originais, preservando todos os marcadores `Fl.` e `ID`.
   - **Cabeçalhos e rodapés de escritórios, peritos e empresas:** detecta por frequência (e descartando os blocos `header`/`footer` do layout visual) endereços, telefones, e-mails, OABs e slogans que se repetem nas margens de 3 ou mais páginas.
   - **Boilerplate de mídia/áudio do PJe e tabelas de sumário bruto.**

## Usar pela janela

Abra **Conversor Processo para Markdown** (atalho nesta pasta) ou execute `gui.pyw`, e arraste PDFs ou pastas para a lista. Soltar PDFs em cima do atalho também funciona.

- A lista mostra o número CNJ identificado e a situação de cada processo; embaixo aparecem a página atual e o tempo estimado restante.
- A opção **Forçar OCR visual (PaddleOCR-VL-1.6) em todas as páginas** fica desligada por padrão (modo híbrido inteligente: usa texto nativo nas páginas digitais e aciona o `PaddleOCR-VL-1.6` + corretor de orientação nas páginas escaneadas/imagens).
- Duplo clique num processo concluído abre o arquivo `.md` gerado.

## Usar pela linha de comando

```powershell
.venv\Scripts\python proc2md.py Processo_0020121-25.2025.5.04.0523.pdf
.venv\Scripts\python proc2md.py pasta-com-processos -o pasta-de-saida --skip-existing
.venv\Scripts\python proc2md.py processo.pdf --ocr-all
```

| Opção | O que faz |
| --- | --- |
| `-o` | Arquivo `.md` (quando há um único PDF) ou pasta de saída. Padrão: ao lado de cada PDF. |
| `--ocr-all` | Passa todas as páginas pelo pipeline visual (`PP-LCNet_doc_ori` + `PP-DocLayoutV3` + `PaddleOCR-VL-1.6`), mesmo as que já possuem texto digital. |
| `--skip-existing` | Em lote, pula os PDFs cujo `.md` já existe. |

## Instalar

Requer Python 3.12. O `torch` e os modelos só são carregados quando alguma página precisa do OCR visual; processos 100% digitais convertem sem eles. Para o OCR visual, instale o `torch` no sistema (por exemplo, PyTorch ROCm para GPUs AMD ou CUDA para GPUs NVIDIA).

```powershell
python -m venv --system-site-packages .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Testar

```powershell
.venv\Scripts\python test_proc2md.py
```

Os testes unitários e o de ponta a ponta com páginas digitais rodam em qualquer máquina, sem GPU nem `torch` (precisam só de `reportlab`). O teste do pipeline visual roda quando o `torch` está instalado. Juntos, cobrem a correção de orientação em páginas rotacionadas em `90°` e `270°`, OCR com `PaddleOCR-VL-1.6`, conversão de tabelas OTSL para Markdown, limpeza de carimbos/cabeçalhos do PJe, deduplicação de intimação automática e de documentos reanexados).

## Arquivos

| Arquivo | Papel |
| --- | --- |
| `proc2md.py` | Motor de conversão (`PaddleVLEngine`, orientação, layout, limpeza PJe) e interface de linha de comando. |
| `gui.pyw` | Interface gráfica (CustomTkinter + Drag-and-Drop). |
| `procmeta.py` | Funções leves compartilhadas entre CLI e GUI (extração de número CNJ e busca de PDFs). |
| `test_proc2md.py` | Suíte de testes automatizados. |
| `app.ico`, `tools/make_icon.py` | Ícone do aplicativo e script gerador. |
