| # | ชื่อฟิลด์ภาษาไทย (HTML/dataString) | API Data Field | คำอธิบาย | หมายเหตุ |
|---|---|---|---|---|
| 1 | — | `attachment.juristic_person_certificate` | สำเนาหนังสือรับรองนิติบุคคล / สำเนารับรองบุคคลธรรมดา | JSON string ของไฟล์, บังคับ |
| 2 | — | `attachment.land_map_diagram` | สำเนาแผนผังรวมที่ดินหรือระวางแผนที่ของเอกสารสิทธิ์จากสำนักงานที่ดินในท้องที่ที่จะตั้งโรงงาน | JSON string ของไฟล์ |
| 3 | — | `attachment.factory_building_plan` | แบบแปลนอาคารโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วน | JSON string ของไฟล์, บังคับ |
| 4 | — | `attachment.factory_safety_certificate` | หนังสือแสดงความมั่นคง แข็งแรง และความปลอดภัย ของอาคารโรงงาน | JSON string ของไฟล์, บังคับ |
| 5 | — | `attachment.factory_building_diagram` | แผนผังสิ่งปลูกสร้างภายในบริเวณโรงงานขนาดเหมาะสมและถูกต้องตามมาตรส่วนไม่เล็กกว่า 1:500 | JSON string ของไฟล์, บังคับ |
| 6 | — | `attachment.machine_installation_diagram` | แผนผังแสดงการติดตั้งเครื่องจักร | JSON string ของไฟล์, บังคับ |
| 7 | — | `attachment.waste_document` | เอกสารแสดงคำอธิบายถึงรายละเอียดชนิด รหัสของเสีย ปริมาณ วิธีการจัดเก็บ สถานที่จัดเก็บ วิธีการกำจัด รหัสวิธีกำจัด รวมถึงการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหายอันตราย และการควบคุมกากอุตสาหกรรม | JSON string ของไฟล์, บังคับ |
| 8 | — | `attachment.emissions_document` | เอกสารแสดงคำอธิบายถึงรายละเอียด วิธีการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหาย อันตราย และการควบคุมการปล่อยมลพิษอื่น ๆ เช่น มลพิษทางเสียง แสง ความสั่นสะเทือน | JSON string ของไฟล์ |
| 9 | — | `attachment.another_document` | เอกสารอื่นๆ | skip |
| 10 | — | `attachment.name_change_certificate` | ใบสำคัญการเปลี่ยนชื่อ | JSON string ของไฟล์ |
| 11 | — | `attachment.power_of_attorney_with_revenue_stamp` | หนังสือมอบอำนาจพร้อมติดอากรแสตมป์ (ถ้ามี) | JSON string ของไฟล์ |
| 12 | — | `attachment.factory_document_of_right` | เอกสารสิทธิของที่ดินที่ตั้งโรงงาน | JSON string ของไฟล์, บังคับ |
| 13 | — | `attachment.consent_document_to_set_up_factory` | หนังสือยินยอมให้ตั้งโรงงานในที่ดิน กรณีเป็นที่ดินเอกชนและผู้ยื่นคําขอมิใช่เจ้าของที่ดิน | JSON string ของไฟล์ |
| 14 | — | `attachment.power_of_attorney_competent_authority` | เอกสารแสดงสิทธิหรือเอกสารที่แสดงการดำเนินการอันจะได้มาซึ่งสิทธิการใช้ประโยชน์ในที่ดินจากหน่วยงานที่มีอำนาจ | JSON string ของไฟล์ |
| 15 | — | `attachment.copy_of_house_registration_factory_location` | สำเนาทะเบียนบ้านของสถานที่ตั้งโรงงาน (ถ้ามี) | JSON string ของไฟล์ |
| 16 | — | `attachment.copy_of_professional_engineering_license` | สำเนาใบอนุญาตประกอบวิชาชีพวิศวกร | JSON string ของไฟล์ |
| 17 | — | `attachment.factory_operation_risk` | รายงานการวิเคราะห์ความเสี่ยงจากอันตรายที่เกิดจากการประกอบกิจการโรงงาน | JSON string ของไฟล์ |
| 18 | — | `attachment.environmental_risk` | รายงานเกี่ยวกับการศึกษามาตรการป้องกันและแก้ไขผลกระทบต่อคุณภาพสิ่งแวดล้อมและความปลอดภัย | JSON string ของไฟล์ |
| 19 | — | `attachment.environmental_impact_eia` | รายงานการประเมินผลกระทบสิ่งแวดล้อม (EIA) แสดงผลการพิจารณาเห็นชอบรายงานฯ จากหน่วยงานที่เกี่ยวข้อง | JSON string ของไฟล์, เฉพาะกรณี EIA |
| 20 | — | `attachment.environmental_impact_iee` | มติคณะกรรมการผู้ชำนาญการรายงานการวิเคราะห์ผลกระทบสิ่งแวดล้อมเบื้องต้น (IEE) | JSON string ของไฟล์, เฉพาะกรณี IEE |
| 21 | — | `attachment.officer_document_requested` | เอกสารที่เจ้าหน้าที่ขอเพิ่มเติม | skip |
| 22 | — | `attachment.applicant_signature` | ลายเซ็นผู้ยื่นคำขอ | URL รูปภาพ, skip จากการจำแนกประเภท (ไม่ใช่เอกสาร) |
