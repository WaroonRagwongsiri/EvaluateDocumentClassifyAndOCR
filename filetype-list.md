# Filetype strings accepted by the classifier/extractor pipeline

Source of truth: `FILETYPE_EXTRACTORS` in
`service/document_validator/questions/correctness/pipeline.py`.

- **23 filetype strings** total
- **21 distinct document types** (slugs) — `factory_location_map` accepts
  3 different filetype strings from the frontend
- Matching is **exact `==`** in `collect_files_by_filetype`
  (`service/document_validator/ocr/file_utils.py`) against the Thai string
  the frontend sends — no normalization.

| # | Slug | Filetype string (exact match vs frontend) | Extractor |
|---|------|-------------------------------------------|-----------|
| 1 | `juristic` | สำเนาหนังสือรับรองนิติบุคคล / สำเนารับรองบุคคลธรรมดา | `process_juristic_person_certificate_files` |
| 2 | `land_map` | สำเนาแผนผังรวมที่ดินหรือระวางแผนที่ของเอกสารสิทธิ์จากสำนักงานที่ดินในท้องที่ที่จะตั้งโรงงาน | `process_land_map_files` |
| 3 | `factory_location_map` | ภาพแผนที่จาก Google Maps | `process_factory_location_map_files` |
| 4 | `factory_location_map` | ภาพ Polygon แผนที่ | `process_factory_location_map_files` |
| 5 | `factory_location_map` | อัปโหลดแผนที่โดยสังเขป | `process_factory_location_map_files` |
| 6 | `poa_revenue_stamp` | หนังสือมอบอำนาจพร้อมติดอากรแสตมป์ (ถ้ามี) | `process_power_of_attorney_with_revenue_stamp_files` |
| 7 | `attchment` | แนบเอกสารมอบอำนาจ กรณีผู้กรอกเอกสารเป็นตัวแทนผู้ประกอบการ | `process_attchment_files` |
| 8 | `name_change` | ใบสำคัญการเปลี่ยนชื่อ | `process_name_change_certificate_files` |
| 9 | `production_diagram` | แผนผังกระบวนการผลิต | `process_production_process_diagram_files` |
| 10 | `building_diagram` | แผนผังสิ่งปลูกสร้างภายในบริเวณโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วนไม่เล็กกว่า 1:500 | `process_factory_building_diagram_files` |
| 11 | `machine_diagram` | แผนผังแสดงการติดตั้งเครื่องจักร | `process_machine_installation_diagram_files` |
| 12 | `land_doc` | เอกสารสิทธิของที่ดินที่ตั้งโรงงาน | `process_factory_document_of_right_files` |
| 13 | `consent` | หนังสือยินยอมให้ตั้งโรงงานในที่ดิน กรณีเป็นที่ดินเอกชนและผู้ยื่นคำขอมิใช่เจ้าของที่ดิน | `process_consent_document_to_set_up_factory_files` |
| 14 | `house_registration` | สำเนาทะเบียนบ้านของสถานที่ตั้งโรงงาน (ถ้ามี) | `process_copy_of_house_registration_factory_location_files` |
| 15 | `engineer_license` | สำเนาใบอนุญาตประกอบวิชาชีพวิศวกร | `process_copy_of_professional_engineering_license_files` |
| 16 | `safety_cert` | หนังสือแสดงความมั่นคง แข็งแรง และความปลอดภัยของอาคารโรงงาน | `process_factory_safety_certificate_files` |
| 17 | `building_plan` | แบบแปลนอาคารโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วน | `process_factory_building_plan_files` |
| 18 | `waste` | เอกสารแสดงคำอธิบายถึงรายละเอียดชนิด รหัสของเสีย ปริมาณ วิธีการจัดเก็บ สถานที่จัดเก็บ วิธีการกำจัด รหัสวิธีกำจัด รวมถึงการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหายอันตราย และการควบคุมกากอุตสาหกรรม | `process_waste_document_files` |
| 19 | `emissions` | เอกสารแสดงคำอธิบายถึงรายละเอียด วิธีการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหาย อันตราย และการควบคุมการปล่อยมลพิษอื่น ๆ เช่น มลพิษทางเสียง แสง ความสั่นสะเทือน | `process_emissions_document_files` |
| 20 | `factory_operation_risk` | รายงานการวิเคราะห์ความเสี่ยงจากอันตรายที่เกิดจากการประกอบกิจการโรงงาน | `process_factory_operation_risk_files` |
| 21 | `environmental_risk` | รายงานเกี่ยวกับการศึกษามาตรการป้องกันและแก้ไขผลกระทบต่อคุณภาพสิ่งแวดล้อมและความปลอดภัย | `process_environmental_risk_files` |
| 22 | `eia` | รายงานการประเมินผลกระทบสิ่งแวดล้อม (EIA) แสดงผลการพิจารณาเห็นชอบรายงานฯ จากหน่วยงานที่เกี่ยวข้อง | `process_environmental_impact_eia_files` |
| 23 | `iee` | มติคณะกรรมการผู้ชำนาญการรายงานการวิเคราะห์ผลกระทบสิ่งแวดล้อมเบื้องต้น (IEE) | `process_environmental_impact_iee_files` |

> Note: the inline comment above `FILETYPE_EXTRACTORS` in `pipeline.py` says
> "20 รายการ" — that count is stale; the actual list has 23 entries.
