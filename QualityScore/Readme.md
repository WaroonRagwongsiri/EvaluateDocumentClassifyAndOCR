# QualityScore service

The page-image quality scorer used by the evaluation app is
`../scorer_service.py` (at the outer repo root — kept outside this submodule so
it's tracked by the outer repo). It's a long-lived JSONL stdin/stdout service
wrapping **DeQA-Doc-Overall** — HuggingFace
[`mapo80/DeQA-Doc-Overall`](https://huggingface.co/mapo80/DeQA-Doc-Overall),
the fully fine-tuned (merged) mPLUG-Owl2-7B checkpoint from the DeQA-Doc
project. This directory (a git submodule of the upstream DeQA-Doc repo) hosts
the DeQA-Score code, the venv, and the model weights. The model was downloaded
to `models/models/mapo80--DeQA-Doc-Overall` via:

```bash
.venv/bin/huggingface-cli download mapo80/DeQA-Doc-Overall \
  --local-dir models/models/mapo80--DeQA-Doc-Overall
```

(An earlier setup used the ModelScope `zhalala/DeQA-Doc-Mix` LoRA adapter +
`MAGAer13/mplug-owl2-llama2-7b` base; the merged Overall checkpoint replaces
it — no adapter/base combination needed.)

Run from the outer repo root:
```bash
CUDA_VISIBLE_DEVICES=4 QualityScore/.venv/bin/python scorer_service.py \
  --model QualityScore/models/models/mapo80--DeQA-Doc-Overall
```

---

# DeQA-Doc: Adapting DeQA-Score to Document Image Quality Assessment

Junjie Gao, Runze Liu, Yingzhe Peng, Shujian Yang, Jin Zhang, Kai Yang and Zhiyuan You

[![paper](https://img.shields.io/badge/arXiv-Paper-green.svg)](https://arxiv.org/abs/2507.12796)
[![GitHub Stars](https://img.shields.io/github/stars/Junjie-Gao19/DeQA-Doc?style=social)](https://github.com/Junjie-Gao19/DeQA-Doc)

This repository is the official implementation of "DeQA-Doc: Adapting DeQA-Score to Document Image Quality Assessment". 

Our DeQA-Doc wins the **Championship** 🏆 in the VQualA 2025 DIQA (Document Image Quality Assessment) Challenge.

## mPLUG-Owl2-7B Training 
### Installation
```bash
git clone https://github.com/Junjie-Gao19/DeQA-Doc.git
cd DeQA-Doc/DeQA-Score
pip install -e .
```
If you want to train, you need to install extra dependencies:
```bash
pip install -e .[train]
```
### Refer to the Readme of the DeQA repository
[DeQA](https://github.com/zhiyuanyou/DeQA-Score)

### Download pre-trained model
You can obtain the initial weight from [mPLUG-Owl2](https://huggingface.co/MAGAer13/mplug-owl2-llama2-7b)


[DIQA_model](https://www.modelscope.cn/models/zhalala/DeQA-Doc/summary) are different models trained separately in different dimensions

[DeQA-Mix](https://www.modelscope.cn/models/zhalala/DeQA-Doc-Mix/summary) is a separate model trained with multiple dimensions mixed

### Infer
```bash
sh scripts/infer.sh
```
When you finish infer, you need to use eval to transfer the result to the format of DIQA.
```bash
sh scripts/diqa_eval.sh
```
### Train
if you want to train your own model
```bash
sh scripts/train.sh 
or
sh scripts/train_lora.sh
```

## Qwen2.5-VL-7B Training
### Use Llamafactory framwork to train Qwen2.5-VL-7B model
#### Install Llamafactory
[Llamafactory](https://github.com/hiyouga/LLaMA-Factory)
#### Exchange src files
You need to exchange the files in Llamafactory with the files in this repository.
Their positions in Llama factory are consistent with those in this repository.
### Train
```bash
llamafactory-cli train examples/train_full/qwen2.5_vl_diqa_sft.yaml
```
### Infer
You should use the DeQA infer script to infer the result.
```bash
sh scripts/infer_qwen.sh
```

## Acknowledgements
This work is based on [DeQA-Score](https://github.com/zhiyuanyou/DeQA-Score). Sincerely thanks for this awesome work.

## Citation
If you find our work useful for your research and applications, please cite using the BibTeX:
```bash
@inproceedings{deqadoc,
  title={{DeQA-Doc}: Adapting {DeQA-Score} to Document Image Quality Assessment}, 
  author={Gao, Junjie and Liu, Runze and Peng, Yingzhe and Yang, Shujian and Zhang, Jin and Yang, Kai and You, Zhiyuan},
  booktitle={Proceedings of the IEEE/CVF International Conference on Computer Vision Workshop},
  year={2025},
}
```
