"""Vendored, trimmed copy of elicense-test-post-to-api/build_dataset.py.

Only the pieces the Request-Body modal needs: ``build_datastring`` (the 6 Thai
form-data blocks), ``build_attachments`` (PDF/image attachment resolution against
a local doc root), ``GeoResolver`` (master-data province/district/subdistrict
name lookup), and the supporting ``ATTACH_ORDER`` / ``ATTACH_FILETYPE`` /
``local_path_for_url`` / ``parse_json_col`` helpers.

Stripped from the original: the ``__main__``/CLI, ``materialize_files`` (disk
tree builder), the PyMuPDF ``render_pdf_to_pngs`` manifest helper, and the
``build_filekey_attachments`` / ``bucket_of_url`` / ``filekey_path_for`` filekey
variant — none are used by the eval app.

This module is stdlib-only (json, os, urllib.parse) so it adds no new deps.

IMPORTANT: the original ``_fetch_aws_file`` network branch is intentionally
DISABLED here. Our files are local; an offline eval app must never hit S3. A
missing file resolves to a MISSING entry and nothing more — no network fallback.
``MASTER`` and ``doc_dir`` come from :mod:`eval.config` (single source of truth)
with the verified defaults baked in there.
"""
from __future__ import annotations

import json
import os
from urllib.parse import unquote

# --- eval app config (env-driven; see eval/config.py) -----------------------
# Imported lazily-ish at module load. Both paths have verified defaults in
# config; building the modal always reads them through here.
from eval import config

# Master-data dir (province/district/subdistrict JSONs). Resolved per-call by
# callers that build a GeoResolver, but exposed here for symmetry with the
# original module so the constant name ``MASTER`` is preserved.
MASTER = str(config.GEO_MASTER_DIR)

# Attachment-order + filetype labels (from field-map-thai-to-api.md step 7).
# filetype = the Thai description of each declared-category attachment.
# This order is used to "map every type" — each category gets >=1 entry.
ATTACH_ORDER = [
    "juristic_person_certificate",
    "land_map_diagram",
    "factory_building_plan",
    "factory_building_plan_certifier",
    "factory_safety_certificate",
    "factory_building_diagram",
    "machine_installation_diagram",
    "machine_installation_diagram_certifier",
    "waste_document",
    "waste_document_certifier",
    "emissions_document",
    "emissions_document_certifier",
    "another_document",
    "name_change_certificate",
    "power_of_attorney_with_revenue_stamp",
    "factory_document_of_right",
    "consent_document_to_set_up_factory",
    "power_of_attorney_competent_authority",
    "copy_of_house_registration_factory_location",
    "copy_of_professional_engineering_license",
    "factory_operation_risk",
    "environmental_risk",
    "environmental_impact_eia",
    "environmental_impact_iee",
    "officer_document_requested",
    "applicant_signature",
]
ATTACH_FILETYPE = {
    # filetype = full Thai label per the field map (canonical system wording).
    # "(ถ้ามี)" kept where the manual has it (e.g. #14 house_registration).
    "juristic_person_certificate": "สำเนาหนังสือรับรองนิติบุคคล / สำเนารับรองบุคคลธรรมดา",
    "land_map_diagram": "สำเนาแผนผังรวมที่ดินหรือระวางแผนที่ของเอกสารสิทธิ์จากสำนักงานที่ดินในท้องที่ที่จะตั้งโรงงาน",
    "factory_building_plan": "แบบแปลนอาคารโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วน",
    "factory_building_plan_certifier": "ผู้รับรองแบบแปลนอาคารโรงงาน",
    "factory_safety_certificate": "หนังสือแสดงความมั่นคง แข็งแรง และความปลอดภัยของอาคารโรงงาน",
    "factory_building_diagram": "แผนผังสิ่งปลูกสร้างภายในบริเวณโรงงานขนาดเหมาะสมและถูกต้องตามมาตราส่วนไม่เล็กกว่า 1:500",
    "machine_installation_diagram": "แผนผังแสดงการติดตั้งเครื่องจักร",
    "machine_installation_diagram_certifier": "ผู้รับรองแผนผังติดตั้งเครื่องจักร",
    "waste_document": "เอกสารแสดงคำอธิบายถึงรายละเอียดชนิด รหัสของเสีย ปริมาณ วิธีการจัดเก็บ สถานที่จัดเก็บ วิธีการกำจัด รหัสวิธีกำจัด รวมถึงการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหายอันตราย และการควบคุมกากอุตสาหกรรม",
    "waste_document_certifier": "ผู้รับรองเอกสารของเสีย",
    "emissions_document": "เอกสารแสดงคำอธิบายถึงรายละเอียด วิธีการป้องกันเหตุเดือดร้อนรำคาญ ความเสียหาย อันตราย และการควบคุมการปล่อยมลพิษอื่น ๆ เช่น มลพิษทางเสียง แสง ความสั่นสะเทือน",
    "emissions_document_certifier": "ผู้รับรองเอกสารมลพิษ",
    "another_document": "เอกสารอื่นๆ",
    "name_change_certificate": "ใบสำคัญการเปลี่ยนชื่อ",
    "power_of_attorney_with_revenue_stamp": "หนังสือมอบอำนาจพร้อมติดอากรแสตมป์ (ถ้ามี)",
    "factory_document_of_right": "เอกสารสิทธิของที่ดินที่ตั้งโรงงาน",
    "consent_document_to_set_up_factory": "หนังสือยินยอมให้ตั้งโรงงานในที่ดิน กรณีเป็นที่ดินเอกชนและผู้ยื่นคำขอมิใช่เจ้าของที่ดิน",
    "power_of_attorney_competent_authority": "เอกสารแสดงสิทธิหรือเอกสารที่แสดงการดำเนินการอันจะได้มาซึ่งสิทธิการใช้ประโยชน์ในที่ดินจากหน่วยงานที่มีอำนาจ",
    "copy_of_house_registration_factory_location": "สำเนาทะเบียนบ้านของสถานที่ตั้งโรงงาน (ถ้ามี)",
    "copy_of_professional_engineering_license": "สำเนาใบอนุญาตประกอบวิชาชีพวิศวกร",
    "factory_operation_risk": "รายงานการวิเคราะห์ความเสี่ยงจากอันตรายที่เกิดจากการประกอบกิจการโรงงาน",
    "environmental_risk": "รายงานเกี่ยวกับการศึกษามาตรการป้องกันและแก้ไขผลกระทบต่อคุณภาพสิ่งแวดล้อมและความปลอดภัย",
    "environmental_impact_eia": "รายงานการประเมินผลกระทบสิ่งแวดล้อม (EIA) แสดงผลการพิจารณาเห็นชอบรายงานฯ จากหน่วยงานที่เกี่ยวข้อง",
    "environmental_impact_iee": "มติคณะกรรมการผู้ชำนาญการรายงานการวิเคราะห์ผลกระทบสิ่งแวดล้อมเบื้องต้น (IEE)",
    "officer_document_requested": "เอกสารที่เจ้าหน้าที่ขอเพิ่มเติม",
    "applicant_signature": "ลายเซ็นผู้ยื่นคำขอ",
    # form artifacts / กระบวนการผลิต (outside attachment.* but with real files)
    "factory_maps_image": "ภาพแผนที่จาก Google Maps",
    "factory_maps_polygon_image": "ภาพ Polygon แผนที่",
    "factory_maps_attchment": "อัปโหลดแผนที่โดยสังเขป",
    "production_process_diagram": "แผนผังกระบวนการผลิต",
    # attachments outside attachment.* (field-map #24, #22) — real files too
    "power_of_attorney": "แนบเอกสารมอบอำนาจ กรณีผู้กรอกเอกสารเป็นตัวแทนผู้ประกอบการ",
    "factory_eia_attachment": "เอกสาร IEE/EIA/EHIA (ถ้ามี)",
}


def num(v):
    """Coerce a value to string as dataString stores it (number/str as-is)."""
    if v is None:
        return ""
    return str(v)


def parse_json_col(v):
    """attachment/process columns are JSON-stringified arrays (or arrays already)
    → return a list or None."""
    if v is None:
        return None
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            try:
                return json.loads(s.replace('\\"', '"'))
            except Exception:
                return None
    return None


# --- master-data resolver --------------------------------------------------
class GeoResolver:
    """Loads the 3 small master-data JSONs (province/district/subdistrict) and
    resolves numeric codes to Thai names. Missing master-data → falls back to
    the raw code string (never raises)."""

    def __init__(self, master_dir):
        self.prov = {}
        self.dist = {}
        self.subd = {}
        try:
            self.prov = {o["code"]: o["name_th"]
                         for o in json.load(open(os.path.join(master_dir, "master-data_province.json"), encoding="utf-8"))["result"]}
            self.dist = {(o["province_code"], o["code"]): o["name_th"]
                         for o in json.load(open(os.path.join(master_dir, "master-data_district.json"), encoding="utf-8"))["result"]}
            self.subd = {(o["province_code"], o["district_code"], o["code"]): o["name_th"]
                         for o in json.load(open(os.path.join(master_dir, "master-data_subdistrict.json"), encoding="utf-8"))["result"]}
        except Exception as e:
            # master-data is best-effort; the modal still renders with raw codes.
            print(f"[warn] โหลด master-data ไม่ได้: {e}")

    def province(self, code):
        return self.prov.get(str(code), str(code) if code is not None else "")

    def district(self, prov_code, code):
        return self.dist.get((str(prov_code), str(code)), str(code) if code is not None else "")

    def subdistrict(self, prov_code, dist_code, code):
        return self.subd.get((str(prov_code), str(dist_code), str(code)),
                             str(code) if code is not None else "")


def build_datastring(data, geo):
    """Build the 6 Thai-labeled dataString blocks from result.data.* (ผู้ยื่นเอกสาร /
    กิจการโรงงาน / การผลิต / เครื่องจักร / กระบวนการผลิต / ระยะเวลาดำเนินการ)."""
    p = data["personal"]
    f = data["factory"]
    m = data["manufacture"]
    mac = data["machine"]
    pr = data["process"]
    op = data["operation_period"]

    prov_p = p.get("province")
    # ข้อมูลผู้ยื่นเอกสาร
    personal = {
        "ชื่อ-นามสกุล": p.get("full_name", ""),
        "อายุ": p.get("age", ""),
        "สัญชาติย่อ": p.get("nationality", ""),
        "เบอร์มือถือ": p.get("telephone", ""),
        "อีเมล": p.get("email", ""),
        "ที่อยู่": p.get("address_no", ""),
        "หมู่": p.get("moo", ""),
        "ซอย": p.get("soi", ""),
        "ถนน": p.get("street", ""),
        "จังหวัด": geo.province(prov_p),
        "อำเภอ": geo.district(prov_p, p.get("district")),
        "ตำบล": geo.subdistrict(prov_p, p.get("district"), p.get("sub_district")),
        "รหัสไปรษณีย์": p.get("postal_code", ""),
        "ติดต่อที่อยู่": p.get("contact_address_no", ""),
        "ติดต่ออำเภอ": geo.district(p.get("contact_province"), p.get("contact_district")),
        "ติดต่อหมู่": p.get("contact_moo", ""),
        "ติดต่อจังหวัด": geo.province(p.get("contact_province")),
        "ติดต่อซอย": p.get("contact_soi", ""),
        "ติดต่อถนน": p.get("contact_street", ""),
        "ติดต่อตำบล": geo.subdistrict(p.get("contact_province"), p.get("contact_district"), p.get("contact_sub_district")),
        "ติดต่อไปรษณีย์": p.get("contact_postal_code", ""),
        "ใช้ที่อยู่เดียวกันกับทะเบียนบ้าน": "true" if p.get("same_address") else "false",
        "สัญชาติ": p.get("nationality_name", ""),
    }

    # ข้อมูลกิจการโรงงาน (เลียนแบบลำดับ EX_1)
    main_business = parse_json_col(f.get("factory_main_business"))
    if isinstance(main_business, list) and main_business:
        main_business_out = main_business
    else:
        main_business_out = []

    maps_polygon = parse_json_col(f.get("factory_maps_polygon"))
    if not isinstance(maps_polygon, list):
        maps_polygon = []

    factory = {
        "ชื่อผู้ประกอบการ": f.get("applicant_name_license", ""),
        "อายุผู้ประกอบการ": f.get("applicant_age", ""),
        "สัญชาติผู้ประกอบการ": f.get("applicant_nationality", ""),
        "เบอร์ติดต่อผู้ประกอบการ": f.get("applicant_telephone", ""),
        "อีเมลผู้ประกอบการ": f.get("applicant_email", ""),
        "เลขที่ที่อยู่ของผู้ประกอบการ": f.get("applicant_address_no", ""),
        "หมู่ที่อยู่ของผู้ประกอบการ": f.get("applicant_address_moo", ""),
        "ซอยที่อยู่ของผู้ประกอบการ": f.get("applicant_address_soi", ""),
        "ถนนที่อยู่ของผู้ประกอบการ": f.get("applicant_address_street", ""),
        "ลำคลอง": f.get("applicant_address_canel", ""),
        "แม่น้ำ": f.get("applicant_address_river", ""),
        "จังหวัดที่อยู่ของผู้ประกอบการ": geo.province(f.get("applicant_address_province")),
        "อำเภอที่อยู่ของผู้ประกอบการ": geo.district(f.get("applicant_address_province"), f.get("applicant_address_district")),
        "ตำบลที่อยู่ของผู้ประกอบการ": geo.subdistrict(f.get("applicant_address_province"), f.get("applicant_address_district"), f.get("applicant_address_sub_district")),
        "รหัสไปรษณีย์": f.get("applicant_address_postal_code", ""),
        "ประเภทผู้ประกอบการ": f.get("applicant_type", ""),
        "เว็บไซต์": f.get("website", ""),
        "factory_is_eia": "true" if f.get("factory_is_eia") in (True, "true", "True") else ("false" if f.get("factory_is_eia") in (False, "false", "False") else str(f.get("factory_is_eia", "") or "")),
        "เลขที่โครงการ IEE/EIA/EHIA": f.get("factory_eia_no", "") or "",
        "มีการแนบไฟล์ eia": f.get("factory_eia_attchment", "") or "",
        "ชื่อโรงงาน": f.get("factory_name", ""),
        "ประเภทหรือชนิดของโรงงาน": main_business_out,
        "ประเภทกิจการรอง": f.get("factory_additional_business", ""),
        "กำลังเครื่องจักร (แรงม้า)": f.get("factory_machine_horsepower", ""),
        "ทุนจดทะเบียน": f.get("factory_authorised_capital", ""),
        "เบอร์โทรโรงงาน": f.get("factory_telephone", ""),
        "โรงงานอีเมล": f.get("factory_email", ""),
        "ที่อยู่โรงงาน": f.get("factory_address_no", ""),
        "โรงงานหมุ่ที่": f.get("factory_moo", ""),
        "ซอยโรงงาน": f.get("factory_soi", ""),
        "ถนนโรงงาน": f.get("factory_street", ""),
        "จังหวัดโรงงาน": f.get("factory_province", ""),
        "อำเภอที่ตั้งโรงงาน": f.get("factory_district", ""),
        "ตำบลของโรงงาน": f.get("factory_sub_district", ""),
        "รหัสไปรษณีย์โรงงาน": f.get("factory_postal_code", ""),
        "อยู่นอก/ในเขตเทศบาล": f.get("factory_municipality", ""),
        "เทศบาลภายใต้": f.get("factory_municipality_id", "") or "",
        "แผนที่": [{"lat": str(pt.get("lat")), "lng": str(pt.get("lng"))} for pt in maps_polygon],
        "รายละเอียดโรงงาน": f.get("factory_description", ""),
        "ตามที่อยู่บริษัท": f.get("same_address_type", ""),
        "เจ้าของที่": f.get("size_landholder", ""),
        "พื้นที่สิ่งปลูกสร้าง": f.get("size_building_area", ""),
        "พื้นที่โรงงาน": f.get("size_factory_area", ""),
        "อาคารโรงงานมีอยู่เดิม": f.get("size_is_new_construction", ""),
        "ประเภทโรงงาน": f.get("size_building_type", ""),
        "หลังตาโรงงาน": f.get("size_roof_material", ""),
        "สถานที่ใกล้เคียงกับ": f.get("size_nearby_location", ""),
        "ราคาที่ดิน": f.get("investment_land_amount", ""),
        "ราคาอาคารและสิ่งปลูกสร้าง": f.get("investment_building_amount", ""),
        "เครื่องจักรอุปกรณ์และค่าติดตั้ง": f.get("investment_machine_amount", ""),
        "เงินทุนหมุนเวียน": f.get("investment_working_capital_amount", ""),
        "ยอดรวมลงทุน": f.get("investment_total_amount", ""),
        "ช่างฝีมือแรงงานชาย": f.get("worker_male_handiwork", ""),
        "ช่างฝีมือแรงงานหญิง": f.get("worker_female_handiwork", ""),
        "แรงงานชาย": f.get("worker_male_none_handiwork", ""),
        "แรงงานหญิง": f.get("worker_female_none_handiwork", ""),
        "เจ้าหน้าที่และวิชาการ": f.get("worker_executive_officer", ""),
        "ช่างเทคนิคและช่างฝีมือจากต่างประเทศ": f.get("worker_foreigner_handiwork", ""),
        "ผู้ชำนาญการจากต่างประเทศ": f.get("worker_foreigner_specialist", ""),
        "ตั้งแต่เวลา": f.get("office_hours_start", ""),
        "ถึงเวลา": f.get("office_hours_end", ""),
        "ทำงานวันละ ชม": f.get("office_hours_total_working_hours_per_day", ""),
        "จำนวนกะ": f.get("office_hours_shift_qty", ""),
        "วันหยุดงาน": f.get("office_hours_holiday", ""),
        "ทำงานปีละ": f.get("office_hours_total_working_days_per_year", ""),
        "เปิดตลอด 24 ชม": "true" if f.get("office_is_24_open") in (True, "true", "True") else ("" if f.get("office_is_24_open") in ("", None) else str(f.get("office_is_24_open"))),
    }

    # ข้อมูลการผลิต
    raw = parse_json_col(m.get("raw_material")) or []
    outgrowth = parse_json_col(m.get("outgrowth_product")) or []
    specified = parse_json_col(m.get("specified_product")) or []
    manufacture = {
        "ผลิตภัณท์": [
            {"รายการ": it.get("name", ""), "จำนวน": it.get("quantity", ""),
             "หน่วย": it.get("unit", ""), "จาก": it.get("from", "")}
            for it in specified
        ],
        "วัตถุพลอยได้": [
            {"รายการ": it.get("name", ""), "จำนวน": it.get("quantity", ""),
             "หน่วย": it.get("unit", ""), "จาก": it.get("from", ""),
             "สถานะ": it.get("status", "")}
            for it in outgrowth
        ],
        "วัตถุดิบ": [
            {"รายการ": it.get("name", ""), "จำนวน": it.get("quantity", ""),
             "หน่วย": it.get("unit", ""), "จาก": it.get("from", "")}
            for it in raw
        ],
    }

    # ข้อมูลเครื่องจักร
    items = parse_json_col(mac.get("machine_items")) or []
    machine = {
        "ตารางเครื่องจักรในการผลิต": [
            {
                "ชื่อเครื่องจักร": it.get("machine_name", ""),
                "หมายเลขทะเบียน": it.get("machine_no", ""),
                "ขนาดของเครื่องจักร (ตร.ม.)": it.get("machine_dimension", "") or "-",
                "จำนวนเครื่องจักร": it.get("machine_qty", ""),
                "งานที่ใช้": it.get("type_of_use", ""),
                "แรงม้า": it.get("horsepower", ""),
                "แรงม้าเปรียบเทียบ": it.get("comparative_horsepower", ""),
                "รวมกำลังเครื่องจักร": it.get("total_horsepower", ""),
                "บริษัทผู้ผลิต": it.get("product_by", "") or "",
                "ประเทศผู้ผลิต": it.get("made_in", "") or "",
            }
            for it in items
        ]
    }

    # ข้อมูลกระบวนการผลิต
    diagram = parse_json_col(pr.get("production_process_diagram")) or []
    process = {
        "ตารางเอกสารแผนผังกระบวนการผลิต": [
            {"หัวข้อแผนผังการผลิต": it.get("title", ""), "ชื่อไฟล์": it.get("file_name", "")}
            for it in diagram if isinstance(it, dict)
        ]
    }

    # ข้อมูลระยะเวลาดำเนินการ
    operation = {
        "ขั้นที่ 1: จะทำการก่อสร้างอาคารโรงงาน": op.get("first_step", ""),
        "จะแล้วเส็จภายในกี่วัน 1": op.get("first_step_description", ""),
        "ขั้นที่ 2: จะทำการติดตั้งเครื่องจักร": op.get("second_step", ""),
        "จะแล้วเส็จภายในกี่วัน 2": op.get("second_step_description", ""),
        "ขั้นที่ 3: จะทำการทดลองดเครื่องจักร": op.get("third_step", ""),
        "จะแล้วเส็จภายในกี่วัน 3": op.get("third_step_description", ""),
    }

    return {
        "ข้อมูลผู้ยื่นเอกสาร": personal,
        "ข้อมูลกิจการโรงงาน": factory,
        "ข้อมูลการผลิต": manufacture,
        "ข้อมูลเครื่องจักรในการผลิต": machine,
        "ข้อมูลกระบวนการผลิต": process,
        "ข้อมูลระยะเวลาดำเนินการ": operation,
    }


def local_path_for_url(url, doc_dir):
    """Resolve an S3 URL to a local path (take the last segment, URL-decode it).
    Returns (path, key) — path is None when the file is absent from disk. The
    ``key`` is the local filename (== our files.filename) used to join to the
    files table. Never touches the network."""
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None, None
    key = url.split("/")[-1]
    key_dec = unquote(key)
    # keep both encoded + decoded (some filenames have Thai/parens)
    for cand in (key_dec, key):
        p = os.path.join(doc_dir, cand)
        if os.path.isfile(p):
            return p, cand
    return None, key_dec


def build_attachments(data, doc_dir):
    """Build PDF + image attachment lists from data.attachment (+ process + factory
    maps). Maps every category the template references; a file absent from disk →
    an empty (MISSING) entry. The AWS network fallback in the original module is
    DISABLED here (offline app, no S3) — ``local_path_for_url`` is the only resolver.

    Returns (pdfs, imgs, manifest). Each FOUND manifest row carries ``local_path``
    + ``key`` (== files.filename) so callers can join to the files table for the
    👁️ preview route."""
    att = data["attachment"]
    pdfs, imgs, manifest = [], [], []

    def is_image_key(key_or_name):
        k = key_or_name.lower()
        return k.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"))

    def add(category, url, display_name):
        filetype = ATTACH_FILETYPE.get(category, category)
        path, key = local_path_for_url(url, doc_dir)
        entry_name = display_name or key
        if path:
            sz = os.path.getsize(path)
            if is_image_key(key or ""):
                imgs.append({"filename": entry_name, "filetype": filetype})
                manifest.append({"category": category, "filetype": filetype,
                                 "filename": entry_name, "key": key,
                                 "status": "FOUND", "size": sz, "bucket": "image",
                                 "local_path": path})
            else:
                pdfs.append({"filename": entry_name, "filetype": filetype})
                manifest.append({"category": category, "filetype": filetype,
                                 "filename": entry_name, "key": key,
                                 "status": "FOUND", "size": sz, "bucket": "pdf",
                                 "local_path": path})
        else:
            # No network fallback (intentional for an offline app): missing file →
            # an empty entry so every mapped category still appears in the modal.
            note = "MISSING (not in doc root)"
            if is_image_key(key or ""):
                imgs.append({"filename": "", "filetype": filetype})
                manifest.append({"category": category, "filetype": filetype,
                                 "filename": entry_name, "key": key, "status": "MISSING",
                                 "size": 0, "bucket": "image", "note": note})
            else:
                pdfs.append({"filename": "", "filetype": filetype})
                manifest.append({"category": category, "filetype": filetype,
                                 "filename": entry_name, "key": key, "status": "MISSING",
                                 "size": 0, "bucket": "pdf", "note": note})

    # 1) attachment.* — map every type in ATTACH_ORDER (#1–#26). Each category
    #    gets >=1 entry: real file → entry, empty/missing → empty entry.
    for col in ATTACH_ORDER:
        if col not in att:
            add(col, None, None)
            continue
        val = att[col]
        arr = parse_json_col(val)
        if isinstance(arr, list):
            has_valid = False
            for it in arr:
                if not isinstance(it, dict):
                    continue
                if it.get("file_path"):
                    add(col, it["file_path"], it.get("file_name"))
                    has_valid = True
                elif "message" in it:
                    # error entry (e.g. juristic "Request failed with status code 500")
                    pass
            if not has_valid:
                add(col, None, None)
        elif isinstance(arr, str) and arr.startswith("http"):
            # bare URL e.g. applicant_signature
            add(col, arr, None)
        elif isinstance(val, str) and val.startswith("http"):
            add(col, val, None)
        else:
            add(col, None, None)

    # 2) process.production_process_diagram (production process PDF)
    ppd = parse_json_col(data["process"].get("production_process_diagram"))
    if isinstance(ppd, list):
        for it in ppd:
            if isinstance(it, dict) and it.get("file_path"):
                add("production_process_diagram", it["file_path"], it.get("file_name") or it.get("title"))

    # 3) form-artifact map images (factory_maps_image / polygon_image / maps_attchment)
    fac = data["factory"]
    for fld, cat in (("factory_maps_image", "factory_maps_image"),
                     ("factory_maps_polygon_image", "factory_maps_polygon_image")):
        url = fac.get(fld)
        if isinstance(url, str) and url.startswith("http"):
            add(cat, url, fld)
    fma = parse_json_col(fac.get("factory_maps_attchment"))
    if isinstance(fma, list):
        for it in fma:
            if isinstance(it, dict):
                ad = it.get("actionData") or {}
                view = ad.get("view")
                name = (it.get("data") or [None])[0] if it.get("data") else None
                if view:
                    add("factory_maps_attchment", view, name)

    # 4) personal.attchment — power of attorney (field-map #24): [{name,size,type,path:URL}]
    pa = parse_json_col(data["personal"].get("attchment"))
    if isinstance(pa, list):
        for it in pa:
            if isinstance(it, dict) and it.get("path"):
                add("power_of_attorney", it["path"], it.get("name"))

    # 5) factory.factory_eia_attchment — IEE/EIA/EHIA (field-map #22)
    eia = parse_json_col(fac.get("factory_eia_attchment"))
    if isinstance(eia, list):
        for it in eia:
            if isinstance(it, dict):
                ad = it.get("actionData") or {}
                view = ad.get("view")
                name = (it.get("data") or [None])[0] if it.get("data") else None
                if view:
                    add("factory_eia_attachment", view, name)

    return pdfs, imgs, manifest
