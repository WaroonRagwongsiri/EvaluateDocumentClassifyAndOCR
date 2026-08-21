# Document Classification — file types and OCR/Vision-LLM handling

A reference for **how each uploaded file type is classified and read**
before / during the completeness and correctness checks.

> **There is no separate upfront "classification" stage.** Classification
> happens *lazily, inside each correctness extractor*, and only for the file
> types the active questions actually need. The two checks treat files very
> differently (see below). Completeness does not classify or OCR at all.

Source of truth for this doc:
- `service/document_validator/questions/correctness/pipeline.py` (`FILETYPE_EXTRACTORS`, `CORRECTNESS_QUESTIONS`)
- extractor modules in `service/document_validator/ocr_json_extractor/extractByfileType/*.py`
- `service/document_validator/ocr/file_utils.py` (`collect_files_by_filetype`)
- `service/document_validator/ocr/image_utils.py` (`process_file_pages`, detect-rotation)

---

## 1. How classification actually works

### 1.1 Completeness (Q1–Q18) — no OCR, no AI classification

Completeness only checks whether an attachment with an **exact filetype
string** is present (`_has_filetype` in `questions/completeness/common.py`).
Pure string matching against `dataBase64Pdf` / `dataBase64Image`.

- No OCR.
- No Vision-LLM classification.
- Matching is **exact `==`** (`collect_files_by_filetype`) — no normalization.

### 1.2 Correctness (Q1–Q19) — per-filetype extractor

`correctness/pipeline.py` runs an **extractor per file type** for every active
question. Each extractor does some combination of:

1. **PDF → image render** (PyMuPDF; `page.rotation` applied automatically).
2. **Detect-rotation** (central OCR `POST /process/detect-rotation`) for
   single-page images, then rotate. PDFs and multi-page images skip this.
3. **Vision-LLM classification** — a multimodal model call that returns
   structured JSON (`is_X` flags, names, license numbers, etc.) directly from
   the rendered page image. This is the "is this the right kind of document /
   what does it contain" step.
4. **Full OCR text extraction** (central OCR `POST /process/ocr`, producing
   `ocr_text`) — **only some extractors** do this, on pages that pass the
   Vision-LLM check. The text feeds the TempRAG content-completeness questions
   (Q14–Q19).

So the real split in the code is:
- **Vision-LLM classification only** — the extractor reads the image with the
  multimodal model and returns structured JSON. No `ocr_text`.
- **Vision-LLM classification + full OCR text** — same as above, plus the
  extractor calls the OCR endpoint to pull the page text for downstream RAG.

> **Terminology note.** In this doc "Vision-LLM classification" = the
> multimodal `is_X` / structured-JSON call. "OCR" = full text extraction via
> the central OCR `/process/ocr` endpoint. Both use the central-document
> -processing service; only the text-extraction endpoint produces `ocr_text`.

---

## 2. Master list — all file types

23 filetype strings → 21 unique extractor slugs (`factory_location_map` maps to
3 strings). All correctness extractors call detect-rotation (except PDFs /
multi-page images, handled by PyMuPDF).

| # | Slug | Filetype string (exact, Thai) | Detect-rotate | Vision-LLM classify (`is_X`) | Full OCR text (`ocr_text`) | Correctness Q |
|---|------|-----|:--:|:--:|:--:|---|
| 1 | `juristic` | สำเนาหนังสือรับรองนิติบุคคล / สำเนารับรองบุคคลธรรมดา | ✓ | ✓ | — | Q1, Q2, Q11 |
| 2 | `land_map` | สำเนาแผนผังรวมที่ดินหรือระวางแผนที่ของเอกสารสิทธิ์จากสำนักงานที่ดินในท้องที่ที่จะตั้งโรงงาน | ✓ | ✓ (`is_cadastral_map`) | — | Q3 |
| 3 | `factory_location_map` | ภาพแผนที่จาก Google Maps | ✓ | ✓ | — | Q13 |
| 4 | `factory_location_map` | ภาพ Polygon แผนที่ | ✓ | ✓ | — | Q13 |
| 5 | `factory_location_map` | อัปโหลดแผนที่โดยสังเขป | ✓ | ✓ | — | Q13 |
| 6 | `poa_revenue_stamp` | หนังสือมอบอำนาจพร้อมติดอากรแสตมป์ (ถ้ามี) | ✓ | ✓ | — | Q2 |
| 7 | `attchment` | แนบเอกสารมอบอำนาจ กรณีผู้กรอกเอกสารเป็นตัวแทนผู้ประกอบการ | ✓ | ✓ | — | Q2 |
| 8 | `name_change` | ใบสำคัญการเปลี่ยนชื่อ | ✓ | ✓ (`is_name_change_cert`) | — | Q4 |
| 9 | `production_diagram` | แผนผังกระบวนการผลิต | ✓ | ✓ (`is_process_diagram`) | — | Q9 |
| 10 | `building_diagram` | แผนผังสิ่งปลูกสร้างภายในบริเวณโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วนไม่เล็กกว่า 1:500 | ✓ | ✓ (`is_building_diagram`, `scale_gt_1_500`) | — | Q6 |
| 11 | `machine_diagram` | แผนผังแสดงการติดตั้งเครื่องจักร | ✓ | ✓ (`is_machine_diagram`) | — | Q7, Q8 |
| 12 | `land_doc` | เอกสารสิทธิของที่ดินที่ตั้งโรงงาน | ✓ | ✓ (`page_type`, `land_data`, `registration_data`) | — | Q11, Q12 |
| 13 | `consent` | หนังสือยินยอมให้ตั้งโรงงานในที่ดิน กรณีเป็นที่ดินเอกชนและผู้ยื่นคำขามิใช่เจ้าของที่ดิน | ✓ | ✓ (`is_land_consent`, `is_lease_agreement`) | — | Q11 |
| 14 | `house_registration` | สำเนาทะเบียนบ้านของสถานที่ตั้งโรงงาน (ถ้ามี) | ✓ | ✓ (`is_house_registration`) | — | Q12 |
| 15 | `engineer_license` | สำเนาใบอนุญาตประกอบวิชาชีพวิศวกร | ✓ | ✓ (`is_engineer_license`) | — | Q5, Q6, Q7, Q10 |
| 16 | `safety_cert` | หนังสือแสดงความมั่นคง แข็งแรง และความปลอดภัยของอาคารโรงงาน | ✓ | ✓ (`is_safety_cert`) | — | Q10 |
| 17 | `building_plan` | แบบแปลนอาคารโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วน | ✓ | ✓ (`is_building_plan`) | — | Q5 |
| 18 | `waste` | เอกสารแสดงคำอธิบายถึงรายละเอียดชนิด รหัสของเสีย ปริมาณ วิธีการจัดเก็บ สถานที่จัดเก็บ วิธีการกำจัด รหัสวิธีกำจัด รวมถึงการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหายอันตราย และการควบคุมกากอุตสาหกรรม | ✓ | ✓ (`is_waste_document`) | **✓** | Q14 |
| 19 | `emissions` | เอกสารแสดงคำอธิบายถึงรายละเอียด วิธีการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหาย อันตราย และการควบคุมการปล่อยมลพิษอื่น ๆ เช่น มลพิษทางเสียง แสง ความสั่นสะเทือน | ✓ | ✓ (`is_emissions_document`) | **✓** | Q15 |
| 20 | `factory_operation_risk` | รายงานการวิเคราะห์ความเสี่ยงจากอันตรายที่เกิดจากการประกอบกิจการโรงงาน | ✓ | ✓ (`is_operation_risk`) | **✓** | Q16 |
| 21 | `environmental_risk` | รายงานเกี่ยวกับการศึกษามาตรการป้องกันและแก้ไขผลกระทบต่อคุณภาพสิ่งแวดล้อมและความปลอดภัย | ✓ | ✓ (`is_environmental_risk`) | **✓** | Q17 |
| 22 | `eia` | รายงานการประเมินผลกระทบสิ่งแวดล้อม (EIA) แสดงผลการพิจารณาเห็นชอบรายงานฯ จากหน่วยงานที่เกี่ยวข้อง | ✓ | ✓ (`is_eia`) | **✓** | Q18 |
| 23 | `iee` | มติคณะกรรมการผู้ชำนาญการรายงานการวิเคราะห์ผลกระทบสิ่งแวดล้อมเบื้องต้น (IEE) | ✓ | ✓ (`is_iee`) | **✓** | Q19 |

### 2.1 Summary by processing mode

- **Vision-LLM classification only — 15 slugs, no OCR text.**
  `juristic`, `land_map`, `factory_location_map` (×3), `poa_revenue_stamp`,
  `attchment`, `name_change`, `production_diagram`, `building_diagram`,
  `machine_diagram`, `land_doc`, `consent`, `house_registration`,
  `engineer_license`, `safety_cert`, `building_plan`.
  These ask the multimodal model to return structured JSON (names, license
  numbers, `is_X` flags) directly from the rendered page image.
- **Full OCR text extraction — 6 slugs.**
  `waste`, `emissions`, `factory_operation_risk`, `environmental_risk`, `eia`,
  `iee`. These run the Vision check first, and on a positive page call
  `ocr_client.ocr_file_bytes(...)` to get `ocr_text`, which then feeds the
  TempRAG content-completeness questions (Q14–Q19).
- **Detect-rotation** — every extractor calls it first (central OCR
  `/process/detect-rotation`), unless the file is a PDF (PyMuPDF applies
  `page.rotation` at render time) or a multi-page image.

---

## 3. Completeness-only file types (no extractor)

These are presence-checked in completeness but have **no correctness
extractor**, so they get **neither OCR nor Vision-LLM classification** — ever.

| Completeness Q | Filetype string | Gets OCR? | Gets Vision classify? |
|---|---|:--:|:--:|
| Q12 | เอกสารแสดงสิทธิหรือเอกสารที่แสดงการดำเนินการอันจะได้มาซึ่งสิทธิการใช้ประโยชน์ในที่ดินจากหน่วยงานที่มีอำนาจ | ✗ | ✗ |

(All other completeness filetypes have a matching correctness extractor in
the master list above.)

---

## 4. Cross-cutting pitfalls

### 4.1 The EIA filetype string diverges between the two systems

Completeness Q16 looks for
`รายงานการประเมินผลกระทบสิ่งแวดล้อม (EIA) แสดงผลการพิจารณาเห็นชอบรายงานฯ`
(no suffix), but the correctness `eia` extractor expects the same string
**with ` จากหน่วยงานที่เกี่ยวข้อง` appended**. A single frontend upload can
satisfy at most one of the two for the same EIA document. (IEE strings match
between both.) See `docs/question-howto.md` §2.5.

### 4.2 Matching is exact `==`

`collect_files_by_filetype` (`ocr/file_utils.py`) matches the Thai filetype
string with no normalization. A trailing space, a missing `(ถ้ามี)`, or a
missing ` จากหน่วยงานที่เกี่ยวข้อง` makes the file invisible to both the
completeness presence check and the correctness extractor.

### 4.3 `factory_location_map` — three strings, one extractor

The same `process_factory_location_map_files` extractor serves three distinct
frontend filetype strings (`ภาพแผนที่จาก Google Maps`, `ภาพ Polygon แผนที่`,
`อัปโหลดแผนที่โดยสังเขป`). Correctness Q13 runs a priority chain that picks the
most precise map type attached (Polygon > Google Maps > แผนที่โดยสังเขป) and
ignores coarser ones.

### 4.4 Shared extractors and disabling questions

Commenting out an entry in `CORRECTNESS_QUESTIONS` disables that Q **and**
stops its exclusive extractors from running. But shared extractors keep
running while any Q that uses them stays enabled: `engineer_license`
(Q5/6/7/10), `machine_diagram` (Q7/8), `juristic` (Q1/2/11), `land_doc`
(Q11/12).
