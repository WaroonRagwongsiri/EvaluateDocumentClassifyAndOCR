#!/usr/bin/env python3
"""DeQA-Doc quality-scorer service — persistent JSONL stdin/stdout subprocess.

Loads the fully fine-tuned mPLUG-Owl2-7B checkpoint DeQA-Doc-Overall
(HuggingFace mapo80/DeQA-Doc-Overall, merged weights — no LoRA adapter, so
--model-base is not used) via the DeQA-Score repo's own modeling code
(src/model/builder.py). Scoring is the DeQA trick: forward
"USER: How would you rate the quality of this image?\\n<|image|>\\nASSISTANT:
The quality of the image is", read last-token logits at the 5 level words,
softmax over just those, expected score = sum(prob * weight), weights 5..1
(the repo's src/evaluate/scorer.py, adapted to a long-lived service).

Protocol (one JSON per line):
  stdin : {"id": <any>, "image": "<abs path to page PNG>"}
  stdout: {"id": ..., "score": 3.42, "level": "fair", "probs": {...}}
  startup: one {"ready": true, "load_s": ...} line first
  per-image errors emit {"id": ..., "error": "..."} and the service lives on

MUST be launched with CUDA_VISIBLE_DEVICES=4 (GPU 4 reserved for this service).

Usage:
  CUDA_VISIBLE_DEVICES=4 .venv/bin/python scorer_service.py --model <model dir>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# src.* imports (repo layout) — resolve relative to this file, not CWD.
# The DeQA-Score code lives under QualityScore/ (a git submodule).
sys.path.insert(0, str(Path(__file__).resolve().parent / "QualityScore" / "DeQA-Score"))

LEVEL_NAMES = ("excellent", "good", "fair", "poor", "bad")
WEIGHTS = (5.0, 4.0, 3.0, 2.0, 1.0)
_LEVEL_BANDS = (("excellent", 4.5), ("good", 3.5), ("fair", 2.5), ("poor", 1.5))


def level_for(score: float) -> str:
    for name, lo in _LEVEL_BANDS:
        if score >= lo:
            return name
    return "bad"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True,
                    help="DeQA-Doc full model dir (merged weights)")
    ap.add_argument("--model-base", default=None,
                    help="unused for merged checkpoints; kept for LoRA dirs")
    ap.add_argument("--device", default="cuda:0")
    args = ap.parse_args()

    import torch
    from PIL import Image
    from src.model.builder import load_pretrained_model
    from src.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN
    from src.mm_utils import tokenizer_image_token

    # the builder prints progress to stdout, which must stay pure JSONL —
    # swap stdout to stderr for the whole load, restore before the ready line
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    t0 = time.time()
    tokenizer, model, image_processor, _ctx_len = load_pretrained_model(
        args.model, args.model_base, "deqa-mplug_owl2", device=args.device)
    model.eval()

    # level token ids, computed exactly like the repo's Scorer:
    # tokenize each level word, drop the leading BOS the tokenizer adds.
    ids_ = [ids[1:] or [tokenizer.unk_token_id]
            for ids in tokenizer(LEVEL_NAMES)["input_ids"]]
    preferential_ids = [ids[0] for ids in ids_]
    level_ids_t = torch.tensor(preferential_ids, device=model.device)
    weights_t = torch.tensor(WEIGHTS, device=model.device, dtype=torch.float32)

    prompt = ("USER: How would you rate the quality of this image?\n"
              f"{DEFAULT_IMAGE_TOKEN}\nASSISTANT: The quality of the image is")
    input_ids = tokenizer_image_token(
        prompt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt"
    ).unsqueeze(0).to(model.device)

    def expand2square(img, background_color):
        w, h = img.size
        if w == h:
            return img
        side = max(w, h)
        result = Image.new(img.mode, (side, side), background_color)
        result.paste(img, ((side - w) // 2, (side - h) // 2))
        return result

    sys.stdout = _real_stdout
    print(json.dumps({"ready": True, "load_s": round(time.time() - t0, 1),
                      "model": args.model}), flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = {}
        try:
            req = json.loads(line)
            img = Image.open(req["image"]).convert("RGB")
            img = expand2square(img, tuple(int(x * 255) for x in
                                           image_processor.image_mean))
            with torch.inference_mode():
                tensor = image_processor.preprocess(
                    img, return_tensors="pt")["pixel_values"].half().to(model.device)
                # input_ids MUST be keyword: forward's first positional param
                # is input_type (None→forward_single, else ValueError).
                logits = model(input_ids=input_ids.repeat(tensor.shape[0], 1),
                               images=tensor)["logits"][:, -1, :]
                five = logits[0, level_ids_t]
                probs = torch.softmax(five.float(), dim=-1)
                score = float((probs * weights_t).sum())
            resp = {"id": req.get("id"), "score": round(score, 3),
                    "level": level_for(score),
                    "probs": {lv: round(float(p), 4)
                              for lv, p in zip(LEVEL_NAMES, probs)}}
        except Exception as exc:
            import traceback; traceback.print_exc()
            resp = {"id": req.get("id") if isinstance(req, dict) else None,
                    "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(resp), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
