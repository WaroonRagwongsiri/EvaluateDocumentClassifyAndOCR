"""Mapping + client bridge to the real per-filetype production extractor.

Production runs a per-filetype extractor (``service.document_validator.
ocr_json_extractor.extractByfileType.process_<slug>_files``) that returns
structured per-page JSON (``is_X`` + data dicts for 15 slugs, ``is_X`` +
``ocr_text`` for 6 fulltext slugs) — NOT a generic fulltext OCR. This module lets
the eval harness call those real extractors so the review page grades
production truth (per evaluate_plan.md: "reuse, don't rewrite… call the real
extractor").

Mapping chain: ``declared_category`` → ``slug`` (DECLARED_TO_SLUG) → ``fn``
(SLUG_TO_FN, imported directly from the 21 production extractor modules) +
``fileType`` Thai string (build_dataset.ATTACH_FILETYPE, cosmetic — echoed into
the result only; the extractor routes by the fn we pick, and detects PDF vs
image from magic bytes / the filename suffix, never from fileType).

Import safety: the ``service.*`` package chain uses namespace packages
(``ocr_json_extractor`` / ``extractByfileType`` have no ``__init__.py``); the
extractor modules use relative imports (``....llm.chatcompletions_service`` →
``service.llm…``) that resolve once the elicense repo root is on ``sys.path``.
The three transitive modules (``chatcompletions_service`` → aiohttp,
``central_ocr_client`` → httpx, ``image_utils`` → pymupdf+pillow) have no
module-level side effects, so importing the 21 fns directly is safe.
``pipeline.py`` is NOT imported (it pulls 19 question modules + common).

Security invariant (still in force): the eval app never hits S3/network for
file fetch. The worker reads ``files.local_path`` under DOC_ROOT itself and
base64-encodes the bytes; no file path crosses into the extractor — the
extractor reads bytes only from ``file_info["base64"]`` (image_utils:231-258).
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Optional

from .. import config

# --- put the elicense repo root on sys.path (once, guarded) ----------------
# The production extractor modules live under ``service.*`` in that repo. They
# use relative imports that resolve only when the repo root is importable, so
# prepend it before the ``from service...`` imports below.
_ELICENSE_ROOT = str(config.ELICENSE_REPO_PATH)
if _ELICENSE_ROOT and _ELICENSE_ROOT not in sys.path:
    sys.path.insert(0, _ELICENSE_ROOT)

# --- production clients (built explicitly from eval config) ------------------
# Production env names differ from the eval's, so we construct both clients
# with explicit args rather than relying on their env reads. ChatCompletions
# hits the same Qwen endpoint the eval already uses (config.LLM_ENDPOINT,
# default http://localhost:4000); CentralOCRClient hits the central OCR service
# (config.CENTRAL_OCR_BASE_URL, default http://localhost:8123). temperature /
# max_tokens are left to the production defaults (0.05 / 4000).
from service.llm.chatcompletions_service import ChatCompletionsService  # noqa: E402
from service.document_validator.ocr.central_ocr_client import CentralOCRClient  # noqa: E402

# --- the 21 extractor fns, imported directly from the production modules -----
# One fn per unique slug (factory_location_map covers 3 Thai strings → 1 fn).
# Cross-checked against FILETYPE_EXTRACTORS (pipeline.py:127-245) — every
# (slug, fn) here is a (slug, fn) there.
from service.document_validator.ocr_json_extractor.extractByfileType.juristic_person_certificate import (  # noqa: E402
    process_juristic_person_certificate_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.land_map_diagram import (  # noqa: E402
    process_land_map_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.factory_location_map import (  # noqa: E402
    process_factory_location_map_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.power_of_attorney_with_revenue_stamp import (  # noqa: E402
    process_power_of_attorney_with_revenue_stamp_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.attchment import (  # noqa: E402
    process_attchment_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.name_change_certificate import (  # noqa: E402
    process_name_change_certificate_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.production_process_diagram import (  # noqa: E402
    process_production_process_diagram_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.factory_building_diagram import (  # noqa: E402
    process_factory_building_diagram_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.machine_installation_diagram import (  # noqa: E402
    process_machine_installation_diagram_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.factory_document_of_right import (  # noqa: E402
    process_factory_document_of_right_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.consent_document_to_set_up_factory import (  # noqa: E402
    process_consent_document_to_set_up_factory_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.copy_of_house_registration_factory_location import (  # noqa: E402
    process_copy_of_house_registration_factory_location_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.copy_of_professional_engineering_license import (  # noqa: E402
    process_copy_of_professional_engineering_license_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.factory_safety_certificate import (  # noqa: E402
    process_factory_safety_certificate_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.factory_building_plan import (  # noqa: E402
    process_factory_building_plan_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.waste_document import (  # noqa: E402
    process_waste_document_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.emissions_document import (  # noqa: E402
    process_emissions_document_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.factory_operation_risk import (  # noqa: E402
    process_factory_operation_risk_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.environmental_risk import (  # noqa: E402
    process_environmental_risk_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.environmental_impact_eia import (  # noqa: E402
    process_environmental_impact_eia_files,
)
from service.document_validator.ocr_json_extractor.extractByfileType.environmental_impact_iee import (  # noqa: E402
    process_environmental_impact_iee_files,
)

# --- slug → extractor fn (21 unique slugs) -----------------------------------
SLUG_TO_FN: dict[str, Callable] = {
    "juristic": process_juristic_person_certificate_files,
    "land_map": process_land_map_files,
    "factory_location_map": process_factory_location_map_files,
    "poa_revenue_stamp": process_power_of_attorney_with_revenue_stamp_files,
    "attchment": process_attchment_files,
    "name_change": process_name_change_certificate_files,
    "production_diagram": process_production_process_diagram_files,
    "building_diagram": process_factory_building_diagram_files,
    "machine_diagram": process_machine_installation_diagram_files,
    "land_doc": process_factory_document_of_right_files,
    "consent": process_consent_document_to_set_up_factory_files,
    "house_registration": process_copy_of_house_registration_factory_location_files,
    "engineer_license": process_copy_of_professional_engineering_license_files,
    "safety_cert": process_factory_safety_certificate_files,
    "building_plan": process_factory_building_plan_files,
    "waste": process_waste_document_files,
    "emissions": process_emissions_document_files,
    "factory_operation_risk": process_factory_operation_risk_files,
    "environmental_risk": process_environmental_risk_files,
    "eia": process_environmental_impact_eia_files,
    "iee": process_environmental_impact_iee_files,
}

# 6 fulltext slugs: the per-page result dict carries ``ocr_text`` (transcribed
# by the central OCR service) in addition to the ``is_X`` classification flag.
# The 15 JSON-only slugs carry only ``is_X`` + structured data (Vision-LLM only;
# rotation is optional and fails safe to 0). Used by the review page to label
# the result box.
FULLTEXT_SLUGS: frozenset[str] = frozenset(
    {"waste", "emissions", "factory_operation_risk", "environmental_risk", "eia", "iee"}
)

# --- declared_category → slug ------------------------------------------------
# Built by matching each declared_category's Thai filetype string
# (build_dataset.ATTACH_FILETYPE) against the FILETYPE_EXTRACTORS filetype
# strings (pipeline.py:127-245). A declared category with no matching Thai
# string has no extractor → omitted here → extract_fn_for returns None.
#
# 21 extractable declared categories (18 in-vocab + 3 OOV). 9 no-extractor:
#   power_of_attorney_competent_authority (in-vocab, but its Thai string is not
#     in FILETYPE_EXTRACTORS), the 4 *_certifier contexts, another_document,
#   officer_document_requested, applicant_signature, factory_eia_attchment.
# (The plan's prose counted "8"; power_of_attorney_competent_authority is the
# 9th — it correctly returns None. OOV ≠ no-extractor: attchment,
#   production_process_diagram, factory_maps_attchment are OOV for the 19-way
#   classifier but DO have extractors.)
DECLARED_TO_SLUG: dict[str, str] = {
    # --- 18 in-vocab declared categories that map to an extractor slug ---
    "juristic_person_certificate": "juristic",
    "land_map_diagram": "land_map",
    "factory_building_plan": "building_plan",
    "factory_safety_certificate": "safety_cert",
    "factory_building_diagram": "building_diagram",
    "machine_installation_diagram": "machine_diagram",
    "waste_document": "waste",
    "emissions_document": "emissions",
    "name_change_certificate": "name_change",
    "power_of_attorney_with_revenue_stamp": "poa_revenue_stamp",
    "factory_document_of_right": "land_doc",
    "consent_document_to_set_up_factory": "consent",
    "copy_of_house_registration_factory_location": "house_registration",
    "copy_of_professional_engineering_license": "engineer_license",
    "factory_operation_risk": "factory_operation_risk",
    "environmental_risk": "environmental_risk",
    "environmental_impact_eia": "eia",
    "environmental_impact_iee": "iee",
    # --- 3 OOV declared categories that DO have an extractor ---
    "attchment": "attchment",
    "production_process_diagram": "production_diagram",
    "factory_maps_attchment": "factory_location_map",
}


def extract_fn_for(declared: str | None) -> Callable | None:
    """The production extractor fn for a declared_category, or None when the
    declared context has no extractor (no file_extracts rows are created for it;
    the review page shows the classification block only)."""
    if not declared:
        return None
    return SLUG_TO_FN.get(DECLARED_TO_SLUG.get(declared))


def slug_for(declared: str | None) -> str | None:
    """The slug for a declared_category, or None (debug/labeling helper)."""
    if not declared:
        return None
    return DECLARED_TO_SLUG.get(declared)


def file_type_string(declared: str | None) -> str | None:
    """The Thai filetype label for a declared_category (the cosmetic
    ``file_info["fileType"]`` field, echoed into the extractor result only).
    Sourced from build_dataset.ATTACH_FILETYPE so the label matches what the
    production frontend sends."""
    if not declared:
        return None
    from .build_dataset import ATTACH_FILETYPE
    return ATTACH_FILETYPE.get(declared)


def make_clients() -> tuple[ChatCompletionsService, Optional[CentralOCRClient]]:
    """Build the LLM + (optional) OCR clients explicitly from eval config.

    Returns ``(llm, ocr_client)``. ``ocr_client`` is None when
    ``CENTRAL_OCR_BASE_URL`` is empty → the extractor skips rotation (JSON-only
    slugs still succeed, fails-safe to 0; fulltext slugs that need the central
    OCR service will create a default client and error on the OCR call —
    expected when the OCR service is down). Construct once per worker run.
    """
    llm = ChatCompletionsService(
        base_url=config.LLM_ENDPOINT,
        api_key=config.MODEL_API_KEY,
        model=config.MODEL_NAME,
        timeout=config.LLM_TIMEOUT,
    )
    if config.CENTRAL_OCR_BASE_URL:
        ocr_client: Optional[CentralOCRClient] = CentralOCRClient(
            base_url=config.CENTRAL_OCR_BASE_URL
        )
    else:
        ocr_client = None
    return llm, ocr_client


async def run_extractor(
    fn: Callable,
    file_info: dict[str, Any],
    ocr_client: Optional[CentralOCRClient],
    llm: ChatCompletionsService,
) -> list[dict]:
    """Call one production extractor fn for one file. Returns its per-page
    result list (``result[0]["pages"]``). Raises on empty/missing result so the
    worker's per-context handler can mark the context's pages as 'error'.

    Keeps the async boundary in one place — the (sync) worker drives this via
    ``asyncio.run(run_extractor(...))`` per context.
    """
    result = await fn([file_info], ocr_client=ocr_client, llm=llm)
    if not result or not isinstance(result, list):
        raise RuntimeError("extractor returned no result")
    first = result[0]
    if not isinstance(first, dict) or "pages" not in first:
        raise RuntimeError("extractor result missing 'pages'")
    pages = first.get("pages") or []
    if not isinstance(pages, list):
        raise RuntimeError("extractor 'pages' is not a list")
    return pages


def _self_check() -> dict:
    """Cross-check the maps for the verification step. Returns a summary dict:
    slug count, extractable declared count, no-extractor declared list, and a
    few spot checks."""
    from .build_dataset import ATTACH_FILETYPE

    # every declared category in the schema's declared_category_t enum:
    declared_all = [
        "juristic_person_certificate", "land_map_diagram", "factory_building_plan",
        "factory_safety_certificate", "factory_building_diagram",
        "machine_installation_diagram", "waste_document", "emissions_document",
        "name_change_certificate", "power_of_attorney_with_revenue_stamp",
        "factory_document_of_right", "consent_document_to_set_up_factory",
        "power_of_attorney_competent_authority",
        "copy_of_house_registration_factory_location",
        "copy_of_professional_engineering_license", "factory_operation_risk",
        "environmental_risk", "environmental_impact_eia", "environmental_impact_iee",
        "factory_building_plan_certifier", "machine_installation_diagram_certifier",
        "waste_document_certifier", "emissions_document_certifier", "another_document",
        "officer_document_requested", "applicant_signature", "attchment",
        "production_process_diagram", "factory_maps_attchment", "factory_eia_attchment",
    ]
    extractable = [d for d in declared_all if extract_fn_for(d) is not None]
    no_extractor = [d for d in declared_all if extract_fn_for(d) is None]
    # every extractable declared must have a Thai filetype label (cosmetic, but
    # file_type_string should not return None for a context we actually extract)
    missing_ft = [d for d in extractable if ATTACH_FILETYPE.get(d) is None]
    return {
        "n_slugs": len(SLUG_TO_FN),
        "n_extractable_declared": len(extractable),
        "n_no_extractor_declared": len(no_extractor),
        "no_extractor": no_extractor,
        "extractable_missing_filetype": missing_ft,
        "land_map_callable": callable(extract_fn_for("land_map_diagram")),
        "another_document_none": extract_fn_for("another_document") is None,
        "poa_competent_authority_none": extract_fn_for(
            "power_of_attorney_competent_authority") is None,
        "factory_maps_attchment_slug": slug_for("factory_maps_attchment"),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(_self_check(), indent=2, ensure_ascii=False))
    print("DECLARED_TO_SLUG:", json.dumps(DECLARED_TO_SLUG, indent=2, ensure_ascii=False))
