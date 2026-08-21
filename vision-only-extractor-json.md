# Vision-LLM-only extractor output — JSON shape

The 15 correctness extractors that do **not** run full OCR text (`ocr_text`)
all return the **same envelope** and differ only in the per-page field dict.

This is the actual structure each extractor's `process_*_files(...)` returns.
Source: `service/document_validator/ocr_json_extractor/extractByfileType/*.py`
(the `page_results.append({...})` and `results.append({...})` blocks).

> **Naming.** The earlier classification doc (`docs/document-classification.md`)
> calls these the "Vision-LLM classification only" extractors — they read the
> rendered page image with a multimodal model and return structured JSON, and
> never call the central-OCR `/process/ocr` text endpoint.

---

## 1. Common envelope (all 15 extractors)

```json
[
  {
    "filename": "หนังสือรับรองบริษัท.pdf",
    "fileType": "<the exact Thai filetype string the frontend sent>",
    "total_pages": 2,
    "pages": [
      {
        "page": 1,
        "rotated_base64": "data:image/jpeg;base64,/9j/4AA...",
        "...per-extractor fields..."
      }
    ]
  }
]
```

- Return value is a `list[dict]` — one entry per uploaded file.
- `pages` is a `list[dict]` — one entry per rendered page (PDF pages, or the
  single image for image uploads).
- `rotated_base64` is the JPEG data URL of the page **after** detect-rotation.
- All field dicts below are **extracted by the Vision LLM from the image**;
  keys are English (per `docs/extractor-key-mapping.md`).
- Each `is_X` is a bool the LLM emits from the classify prompt; missing →
  `False` (extractors default with `parsed.get("is_X", False)`).

---

## 2. Per-extractor per-page fields

### 2.1 `juristic` — `สำเนาหนังสือรับรองนิติบุคคล / สำเนารับรองบุคคลธรรมดา`
Used by Q1, Q2, Q11.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "doc_types": {
    "is_juristic_cert": true,
    "is_passport": false,
    "is_id_card": false,
    "is_house_registration": false
  },
  "juristic_cert_data": {
    "company_name": "บริษัท สยามฟู้ดโปรดักส์ จำกัด",
    "directors": ["นายสมชาย ใจดี", "นางสาวสมหญิง รักดี"],
    "headquarters_address": "123 ถ.สุขุมวิท ...",
    "company_stamp_present": true,
    "company_objectives": ["ผลิตและจำหน่ายแป้งมันสำปะหลัง", "รับซื้อมันสำปะหลัง"]
  },
  "passport_data": [
    {
      "passport_number": "AB1234567",
      "full_name": "JOHN DOE",
      "date_of_birth": "01/01/1990",
      "type": "P",
      "country": "Thailand",
      "expiry_date": "01/01/2030"
    }
  ],
  "id_card_data": [
    {
      "id_number": "...",
      "full_name": "...",
      "date_of_birth": "...",
      "address": "...",
      "expiry_date": "...",
      "copy_certified_signed": true
    }
  ],
  "house_registration_data": [
    {
      "house_info": {
        "house_id_number": "...",
        "address": "...",
        "village_name": "...",
        "house_name": "...",
        "house_type": "...",
        "house_style": "...",
        "house_number_assigned_date": "..."
      },
      "residents": [
        {
          "sequence_number": "...",
          "full_name": "...",
          "nationality": "...",
          "gender": "...",
          "date_of_birth": "...",
          "id_number": "..."
        }
      ],
      "copy_certified_signed": true
    }
  ]
}
```

> `passport_data` / `id_card_data` / `house_registration_data` are `list`s
> because one image can contain multiple such documents. A page with none of
> a given type has the key present but the list empty.

### 2.2 `land_map` — cadastral map
Filetype: `สำเนาแผนผังรวมที่ดินหรือระวางแผนที่ของเอกสารสิทธิ์จากสำนักงานที่ดินในท้องที่ที่จะตั้งโรงงาน`
Used by Q3.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_cadastral_map": true
}
```

### 2.3 `factory_location_map` (×3 strings) — factory location / surroundings
Filetypes: `ภาพแผนที่จาก Google Maps`, `ภาพ Polygon แผนที่`, `อัปโหลดแผนที่โดยสังเขป`
Used by Q13.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_factory_location_map": true,
  "location_description": "objective free-text description the LLM produced of the location and surroundings"
}
```

### 2.4 `poa_revenue_stamp` — power of attorney + revenue stamp
Filetype: `หนังสือมอบอำนาจพร้อมติดอากรแสตมป์ (ถ้ามี)`
Used by Q2.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_poa_with_stamp": true,
  "poa_data": {
    "principal_name": "...",
    "principal_address": "...",
    "principal_position": "...",
    "principal_company_name": "...",
    "authorized_actions": ["action 1", "action 2"],
    "principal_signed": true,
    "agent": {
      "agent_full_name": "...",
      "agent_address": "...",
      "agent_signed": true
    },
    "witnesses": [
      { "witness_full_name": "...", "witness_signed": true },
      { "witness_full_name": "...", "witness_signed": true }
    ],
    "has_revenue_stamp": true,
    "has_company_stamp": true
  }
}
```

### 2.5 `attchment` — attached identity docs (PoA case)
Filetype: `แนบเอกสารมอบอำนาจ กรณีผู้กรอกเอกสารเป็นตัวแทนผู้ประกอบการ`
Used by Q2.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "doc_types": {
    "is_passport": false,
    "is_id_card": true,
    "is_house_registration": true
  },
  "passport_data": [ { "...same as juristic passport_data..." } ],
  "id_card_data": [ { "...same as juristic id_card_data..." } ],
  "house_registration_data": [ { "...same as juristic house_registration_data..." } ]
}
```

### 2.6 `name_change` — name-change certificate
Filetype: `ใบสำคัญการเปลี่ยนชื่อ`
Used by Q4.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_name_change_cert": true
}
```

### 2.7 `production_diagram` — production process diagram
Filetype: `แผนผังกระบวนการผลิต`
Used by Q9.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_process_diagram": true,
  "process_data": {
    "process_diagram_title": "...",
    "diagram_description": "..."
  }
}
```

### 2.8 `building_diagram` — structures diagram (with scale)
Filetype: `แผนผังสิ่งปลูกสร้างภายในบริเวณโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วนไม่เล็กกว่า 1:500`
Used by Q6.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_building_diagram": true,
  "engineers": [ { "name": "...", "position": "..." } ],
  "scale_gt_1_500": true
}
```

> `scale_gt_1_500` is a deterministic bool the extractor derives from the
> scale the LLM reads off the diagram; it gates Q6's scale requirement.

### 2.9 `machine_diagram` — machinery installation diagram
Filetype: `แผนผังแสดงการติดตั้งเครื่องจักร`
Used by Q7, Q8.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_machine_diagram": true,
  "engineers": [ { "name": "...", "position": "..." } ],
  "machinery_data": {
    "machinery_list": [ { "name": "...", "quantity": 1 } ],
    "diagram_description": "..."
  }
}
```

### 2.10 `land_doc` — land-rights document
Filetype: `เอกสารสิทธิของที่ดินที่ตั้งโรงงาน`
Used by Q11, Q12.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_land_doc_page": true,
  "page_type": "land_title",
  "land_data": {
    "land_title_number": "...",
    "address": "...",
    "owner_name": "..."
  },
  "registration_data": {
    "date": "...",
    "type": "...",
    "grantor": "...",
    "grantee": "...",
    "area_per_contract": "...",
    "remaining_area": "...",
    "current_owner": "...",
    "district": "..."
  }
}
```

> `page_type` is `"land_title"` or `"registration_index"` (or `false` /
> missing when the page is neither). Only the matching data block is
> populated for a given page.

### 2.11 `consent` — consent letter / lease
Filetype: `หนังสือยินยอมให้ตั้งโรงงานในที่ดิน กรณีเป็นที่ดินเอกชนและผู้ยื่นคำขามิใช่เจ้าของที่ดิน`
Used by Q11.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "doc_types": {
    "is_id_card": false,
    "is_passport": false,
    "is_land_consent": true,
    "is_lease_agreement": false,
    "is_company_cert": false
  },
  "id_card_data": [ { "...id_card dict..." } ],
  "passport_data": [ { "...passport dict..." } ],
  "land_consent_data": {
    "consenters": [ { "full_name": "...", "address": "...", "company_name": "...", "consenter_signed": true } ],
    "land_use_applicant": { "full_name": "...", "address": "...", "company_name": "..." }
  },
  "lease_agreement_data": {
    "lessor": { "full_name": "...", "company_name": "..." },
    "lessee": { "full_name": "..." }
  },
  "company_cert_data": { "...company certificate dict..." }
}
```

### 2.12 `house_registration` — copy of house registration
Filetype: `สำเนาทะเบียนบ้านของสถานที่ตั้งโรงงาน (ถ้ามี)`
Used by Q12.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_house_registration": true,
  "house_registration_data": {
    "house_info": {
      "house_id_number": "...",
      "address": "...",
      "village_name": "...",
      "house_name": "...",
      "house_type": "...",
      "house_style": "...",
      "house_number_assigned_date": "..."
    },
    "copy_certified_signed": true
  }
}
```

### 2.13 `engineer_license` — professional engineering license
Filetype: `สำเนาใบอนุญาตประกอบวิชาชีพวิศวกร`
Used by Q5, Q6, Q7, Q10.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_engineer_license": true,
  "engineer_license_data": {
    "full_name": "...",
    "level": "...",
    "discipline": "...",
    "license_number": "...",
    "issue_date": "...",
    "expiry_date": "..."
  }
}
```

### 2.14 `safety_cert` — factory building safety certificate
Filetype: `หนังสือแสดงความมั่นคง แข็งแรง และความปลอดภัยของอาคารโรงงาน`
Used by Q10.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_safety_cert": true,
  "cert_data": {
    "certifier_name": "...",
    "level": "...",
    "discipline": "...",
    "license_number": "...",
    "issue_date": "...",
    "expiry_date": "...",
    "certified_content": "...",
    "certified_location_address": "...",
    "certifier_signed": true
  }
}
```

### 2.15 `building_plan` — factory building plan
Filetype: `แบบแปลนอาคารโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วน`
Used by Q5.

```json
{
  "page": 1,
  "rotated_base64": "data:image/jpeg;base64,...",
  "is_building_plan": true,
  "engineers": [ { "name": "...", "position": "..." } ]
}
```

---

## 3. Notes

- **Empty / fail-safe.** An extractor returns `[]` if the input file list is
  empty, if no pages can be decoded, or on any exception. Downstream
  correctness questions then see `ext[slug] == []` and fall back to
  `ไม่สามารถตรวจสอบได้` (or `สอดคล้อง` for the optional name-change Q4).
- **A page can carry multiple doc types.** `juristic`, `attchment`, and
  `consent` classify a page into up to several types at once (`doc_types`),
  then extract a data block for each type found. The other extractors ask a
  single `is_X` question.
- **The 6 OCR-text extractors are NOT here.** `waste`, `emissions`,
  `factory_operation_risk`, `environmental_risk`, `eia`, `iee` add an
  `"ocr_text"` string field on pages that pass their `is_X` check (full text
  via central OCR `/process/ocr`), which feeds TempRAG for Q14–Q19. See
  `docs/document-classification.md` §2.1.
