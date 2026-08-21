# Evaluate webapp plan — document classifier + OCR / ADE

A plan for a standalone **evaluation webapp** that, given an uploaded PDF /
image + a filetype string, runs **only the document-classifier + OCR / ADE
stage** — i.e. the extractor — and shows the raw JSON result **before** it is
consumed by completeness / correctness. This is for inspecting extractor
quality, not for running the question checks.

This doc has three parts:
1. **Pipeline flow** — which files in this repo do the work today, in order.
2. **Map** — filetype string (exact, Thai) × slug × Full-OCR vs JSON, with the
   exact output keys.
3. **Webapp design** — what to build and how it plugs into the existing code.

---

## Part 1 — Pipeline flow (which files, in order)

There is **no separate classifier stage**. Classification + OCR + ADE happen
**inside each correctness extractor**, lazily. The flow when the existing
correctness pipeline runs is below. For the evaluate webapp you reuse the
boxed steps (1–4); you skip the question dispatch (step 5).

```
┌──────────────────────────────────────────────────────────────────────┐
│ 0. ENTRY (request arrives)                                          │
│    elicense_check.py / document_validator.py                        │
│       └─ verify_api_key, then asyncio.create_task(background)       │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│ 1. ORCHESTRATION — decide which extractors run                       │
│    service/document_validator/questions/correctness/pipeline.py     │
│       • FILETYPE_EXTRACTORS   (slug, filetype_string, fn) — 23 rows │
│       • CORRECTNESS_QUESTIONS (question_number, slugs)              │
│       • active_slugs()        — slugs needed by enabled questions   │
│       • run_all_extractors()  — THE ENTRY POINT                     │
│            └─ _run_one_extractor() per (slug, filetype)             │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│ 2. FILE MATCHING — make the file visible to the extractor           │
│    service/document_validator/ocr/file_utils.py                     │
│       └─ collect_files_by_filetype(pdf, image, filetype)            │
│            EXACT == on the Thai filetype string. No normalization.  │
│            A mismatch → file invisible (returns []).                │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│ 3. EXTRACTOR (per file) — classify + OCR + ADE  ← YOU WANT THIS     │
│    service/document_validator/ocr_json_extractor/extractByfileType/  │
│       └─ process_<slug>_files(files)                               │
│                                                                      │
│    3a. PAGE PREP (OCR: detect-rotation)                             │
│        service/document_validator/ocr/image_utils.py                │
│           └─ process_file_pages(file_info, ocr_client, dpi=300)     │
│                ├─ decode_bytes / pdf_bytes_to_page_images           │
│                └─ CentralOCRClient.detect_rotation()  POST /process │
│                   /detect-rotation   → angle 0/90/180/270 → rotate  │
│                                                                      │
│    3b. ADE (Vision-LLM → JSON)                                       │
│        service/llm/chatcompletions_service.py                       │
│           └─ ChatCompletionsService.chat([system prompt,            │
│                user:[image_url(data URL), text]])                   │
│                → returns {"content": "<JSON text>"}; extractor      │
│                  parses is_X flags + data dicts.                    │
│                                                                      │
│    3c. FULL OCR TEXT (only the 6 OCR-text slugs)                    │
│        service/document_validator/ocr/central_ocr_client.py          │
│           └─ CentralOCRClient.ocr_file_bytes()  POST /process/ocr   │
│                → ocr_text (str), only on pages where is_X == true.  │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│ 4. EXTRACTOR OUTPUT — {slug: [result dicts]}                        │
│    Each result dict: {filename, fileType, total_pages,              │
│                       pages:[{page, rotated_base64, ...fields}]}   │
│    The ...fields differ per slug (see Part 2).                      │
│    ← THIS is what the evaluate webapp should display.               │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│ 5. QUESTION DISPATCH (SKIP for evaluate webapp)                     │
│    pipeline.run_correctness_questions()                            │
│       └─ dispatch[number] → process_questionN(ext, dataString,...)│
│    service/document_validator/questions/correctness/q1.py … q19.py │
│    → verdicts {Number, Question, Answer, Reason, Suggestion}       │
└──────────────────────────────────────────────────────────────────────┘
```

### File inventory for the evaluate webapp

| Layer | File | What you reuse |
|---|---|---|
| Orchestration | `service/document_validator/questions/correctness/pipeline.py` | `FILETYPE_EXTRACTORS` (the slug↔filetype↔fn table), `collect_files_by_filetype` import |
| File matching | `service/document_validator/ocr/file_utils.py` | `collect_files_by_filetype` |
| Page prep (OCR) | `service/document_validator/ocr/image_utils.py` | `process_file_pages`, `image_to_data_url` |
| OCR client | `service/document_validator/ocr/central_ocr_client.py` | `CentralOCRClient.detect_rotation`, `.ocr_file_bytes` |
| ADE client | `service/llm/chatcompletions_service.py` | `ChatCompletionsService.chat` |
| Extractors (per slug) | `service/document_validator/ocr_json_extractor/extractByfileType/*.py` | the `process_<slug>_files` functions |
| Config | `.env` | `CENTRAL_OCR_BASE_URL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |

You do **not** need: `questions/correctness/q1.py`–`q19.py`, `questions/completeness/*`,
`document_validator.py`'s webhook/background logic, Flowise, the rule files,
or `master/` geography.

---

## Part 2 — Map: filetype × slug × Full-OCR vs JSON

23 filetype strings → 21 unique extractor slugs (`factory_location_map` maps
to 3 strings). Every extractor also does **detect-rotation** (unless PDF or
multi-page image). The split below is the one that matters for the webapp:
**Full OCR text** extractors return `ocr_text`; **JSON** extractors return
structured `is_X` + data dicts and never call the OCR text endpoint.

### 2.1 Full-OCR-text extractors (6 slugs) — `is_X` + `ocr_text`

All 6 share the identical minimal per-page shape:

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_<x>": true,
  "ocr_text": "<full page text from POST /process/ocr>"
}
```

| Slug | Filetype string (exact, Thai) | `is_X` key | Extra output keys |
|---|---|---|---|
| `waste` | เอกสารแสดงคำอธิบายถึงรายละเอียดชนิด รหัสของเสีย ปริมาณ วิธีการจัดเก็บ สถานที่จัดเก็บ วิธีการกำจัด รหัสวิธีกำจัด รวมถึงการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหายอันตราย และการควบคุมกากอุตสาหกรรม | `is_waste_document` | `ocr_text` |
| `emissions` | เอกสารแสดงคำอธิบายถึงรายละเอียด วิธีการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหาย อันตราย และการควบคุมการปล่อยมลพิษอื่น ๆ เช่น มลพิษทางเสียง แสง ความสั่นสะเทือน | `is_emissions_document` | `ocr_text` |
| `factory_operation_risk` | รายงานการวิเคราะห์ความเสี่ยงจากอันตรายที่เกิดจากการประกอบกิจการโรงงาน | `is_operation_risk` | `ocr_text` |
| `environmental_risk` | รายงานเกี่ยวกับการศึกษามาตรการป้องกันและแก้ไขผลกระทบต่อคุณภาพสิ่งแวดล้อมและความปลอดภัย | `is_environmental_risk` | `ocr_text` |
| `eia` | รายงานการประเมินผลกระทบสิ่งแวดล้อม (EIA) แสดงผลการพิจารณาเห็นชอบรายงานฯ จากหน่วยงานที่เกี่ยวข้อง | `is_eia` | `ocr_text` |
| `iee` | มติคณะกรรมการผู้ชำนาญการรายงานการวิเคราะห์ผลกระทบสิ่งแวดล้อมเบื้องต้น (IEE) | `is_iee` | `ocr_text` |

### 2.2 JSON (Vision-LLM only) extractors (15 slugs) — structured `is_X` + data dicts

Per-page shape: `{page, rotated_base64, ...fields}` — no `ocr_text`. Full
nested key detail in `docs/vision-only-extractor-json.md`.

| Slug | Filetype string (exact, Thai) | `is_X` / top-level keys |
|---|---|---|
| `juristic` | สำเนาหนังสือรับรองนิติบุคคล / สำเนารับรองบุคคลธรรมดา | `doc_types{is_juristic_cert,is_passport,is_id_card,is_house_registration}` + `juristic_cert_data`, `passport_data[]`, `id_card_data[]`, `house_registration_data[]` |
| `land_map` | สำเนาแผนผังรวมที่ดินหรือระวางแผนที่ของเอกสารสิทธิ์จากสำนักงานที่ดินในท้องที่ที่จะตั้งโรงงาน | `is_cadastral_map` |
| `factory_location_map` | ภาพแผนที่จาก Google Maps | `is_factory_location_map` + `location_description` |
| `factory_location_map` | ภาพ Polygon แผนที่ | `is_factory_location_map` + `location_description` |
| `factory_location_map` | อัปโหลดแผนที่โดยสังเขป | `is_factory_location_map` + `location_description` |
| `poa_revenue_stamp` | หนังสือมอบอำนาจพร้อมติดอากรแสตมป์ (ถ้ามี) | `is_poa_with_stamp` + `poa_data{principal_*, agent{}, witnesses[], has_revenue_stamp, has_company_stamp}` |
| `attchment` | แนบเอกสารมอบอำนาจ กรณีผู้กรอกเอกสารเป็นตัวแทนผู้ประกอบการ | `doc_types{is_passport,is_id_card,is_house_registration}` + `passport_data[]`, `id_card_data[]`, `house_registration_data[]` |
| `name_change` | ใบสำคัญการเปลี่ยนชื่อ | `is_name_change_cert` |
| `production_diagram` | แผนผังกระบวนการผลิต | `is_process_diagram` + `process_data{process_diagram_title, diagram_description}` |
| `building_diagram` | แผนผังสิ่งปลูกสร้างภายในบริเวณโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วนไม่เล็กกว่า 1:500 | `is_building_diagram` + `engineers[{name,position}]` + `scale_gt_1_500` |
| `machine_diagram` | แผนผังแสดงการติดตั้งเครื่องจักร | `is_machine_diagram` + `engineers[{name,position}]` + `machinery_data{machinery_list[{name,quantity}], diagram_description}` |
| `land_doc` | เอกสารสิทธิของที่ดินที่ตั้งโรงงาน | `is_land_doc_page` + `page_type`(land_title\|registration_index) + `land_data{}`, `registration_data{}` |
| `consent` | หนังสือยินยอมให้ตั้งโรงงานในที่ดิน กรณีเป็นที่ดินเอกชนและผู้ยื่นคำขามิใช่เจ้าของที่ดิน | `doc_types{is_id_card,is_passport,is_land_consent,is_lease_agreement,is_company_cert}` + `id_card_data[]`, `passport_data[]`, `land_consent_data{}`, `lease_agreement_data{}`, `company_cert_data{}` |
| `house_registration` | สำเนาทะเบียนบ้านของสถานที่ตั้งโรงงาน (ถ้ามี) | `is_house_registration` + `house_registration_data{house_info{}, copy_certified_signed}` |
| `engineer_license` | สำเนาใบอนุญาตประกอบวิชาชีพวิศวกร | `is_engineer_license` + `engineer_license_data{full_name,level,discipline,license_number,issue_date,expiry_date}` |
| `safety_cert` | หนังสือแสดงความมั่นคง แข็งแรง และความปลอดภัยของอาคารโรงงาน | `is_safety_cert` + `cert_data{certifier_name,level,discipline,license_number,issue_date,expiry_date,certified_content,certified_location_address,certifier_signed}` |
| `building_plan` | แบบแปลนอาคารโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วน | `is_building_plan` + `engineers[{name,position}]` |

### 2.3 Quick counts

- 23 filetype strings → 21 unique slugs (`factory_location_map` × 3).
- 6 slugs → **Full OCR text** (`ocr_text`).
- 15 slugs → **JSON** (structured `is_X` + data dicts, no `ocr_text`).
- All 21 do **detect-rotation** (skipped for PDFs / multi-page images).

---

## Part 3 — Webapp design

### 3.1 What it does

One page: upload a file, pick (or paste) a **filetype string**, click **Run
extractor**. The webapp calls the matching `process_<slug>_files` directly and
renders the raw JSON. No completeness, no correctness, no Flowise.

### 3.2 Backend — a single FastAPI route (reuse, don't rewrite)

Add one route to a small new app (or to `main.py` behind a flag) that wraps the
existing extractor. Do **not** reimplement the page-prep/ADE/OCR logic — call
the real extractor so the eval reflects production truth.

```python
# app: evaluate_api.py (or a route in main.py)
import base64
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware

from service.document_validator.questions.correctness.pipeline import (
    FILETYPE_EXTRACTORS,
)  # the source of truth for slug <-> filetype <-> fn

# Build {filetype_string: fn} straight from the production table.
_BY_FILETYPE = {ft: fn for (_slug, ft, fn) in FILETYPE_EXTRACTORS}

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.post("/evaluate/extract")
async def evaluate_extract(file: UploadFile, filetype: str = Form(...)):
    """Run ONE extractor by exact filetype string; return its raw JSON.

    filetype MUST match FILETYPE_EXTRACTORS byte-for-byte (same exact-==
    rule the real pipeline uses). Unknown filetype -> 400.
    """
    fn = _BY_FILETYPE.get(filetype)
    if fn is None:
        return {"error": "unknown filetype string", "filetype": filetype}
    raw = await file.read()
    b64 = base64.b64encode(raw).decode()
    files = [{"filename": file.filename, "base64": b64, "fileType": filetype}]
    results = await fn(files)          # detect-rotate + (ADE | full-OCR) -> JSON
    return {"filetype": filetype, "results": results}
```

Why pull from `FILETYPE_EXTRACTORS` instead of a hand-maintained dict: the
webapp stays correct when slugs are added/commented out in `pipeline.py`. The
only cost is that commenting out a `CORRECTNESS_QUESTIONS` entry removes the
*question* but the extractor fn is still in `FILETYPE_EXTRACTORS`, so the
webapp can still call it — which is what you want for eval.

For the dropdown options, expose the list too:

```python
@app.get("/evaluate/filetypes")
def list_filetypes():
    return [{"filetype": ft, "slug": slug} for (slug, ft, _fn) in FILETYPE_EXTRACTORS]
```

### 3.3 Frontend — minimal

- A dropdown populated from `/evaluate/filetypes` (shows the Thai string +
  slug).
- A file picker (PDF or image).
- A "Run" button → `POST /evaluate/extract` (multipart: `file` + `filetype`).
- A JSON viewer for `{results}`. Render `rotated_base64` thumbnails
  alongside each page's JSON so you can eyeball "is the page actually what
  `is_X` says it is."
- Optional toggles (advanced):
  - `ocr_client=None` → skip detect-rotation (tests ADE alone).
  - a "raw OCR text" tab for the 6 full-OCR slugs (`ocr_text` per page).

### 3.4 Config / run

- Reuses `.env` as-is: `CENTRAL_OCR_BASE_URL`, `LLM_BASE_URL`, `LLM_API_KEY`,
  `LLM_MODEL`.
- Run alongside the main service or standalone:
  `.venv/bin/python -m uvicorn evaluate_api:app --port 5050 --reload`
- No DB, no webhook, no API key needed for local eval (add one if exposed).

### 3.5 What to watch for (eval criteria)

Per page, compare the JSON against the image:
- **JSON slugs:** is `is_X` correct? Are the data dicts (names, license
  numbers, addresses) right? `juristic`/`attchment`/`consent` classify a page
  into multiple types at once — check `doc_types` is right for mixed uploads.
- **Full-OCR slugs:** is `is_X` correct, and is `ocr_text` clean and complete?
  These feed TempRAG downstream, so garbled text = downstream failures.
- **Common failure modes:** wrong filetype string (exact-`==` miss → `[]`),
  detect-rotation timeout (page stays sideways → ADE misreads), LLM returns
  non-JSON (extractor leaves `is_X=False` / data empty).

### 3.6 Out of scope for this webapp

The question checks (completeness Q1–Q18, correctness Q1–Q19), the pre-OCR
required-gates, Flowise, the rule files, and the webhook flow. The webapp
stops at the extractor output (Part 1, step 4) on purpose.
