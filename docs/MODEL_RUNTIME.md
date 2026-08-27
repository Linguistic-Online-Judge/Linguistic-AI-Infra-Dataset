# Qwen3.5 Model Runtime

## Verified baselines

Both real-model baselines use one RTX 3090 in text-only mode and share one
runtime:

| Component | Pinned value |
| --- | --- |
| Models | `Qwen/Qwen3.5-4B`, `Qwen/Qwen3.5-9B` |
| 4B revision | `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a` |
| 9B revision | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` |
| Python | `3.12.12` managed by uv |
| vLLM | `0.27.1+cu129` |
| PyTorch | `2.13.0+cu129` |
| TorchCodec | `0.16.0+cu129` |
| Prompt envelope | `1.0` |
| Context length | 4,096 tokens |
| Model mode | text-only, non-thinking |

The model files were downloaded from `https://hf-mirror.com` at the pinned
revisions. `hf cache verify` checked all 14 4B files and all 16 9B files
successfully. The model directories occupy about 8.8 GB and 19 GB respectively.

The server's system Python lacks both `ensurepip` and development headers. The
runtime therefore uses a user-owned uv binary and uv-managed Python under
`/mnt/local/babylm26_g2`; it does not modify system Python or require sudo.

The ordinary PyPI vLLM package resolves to a CUDA 13 stack, which is incompatible
with the node's driver. Use the official CUDA 12.9 release wheel instead. Also
install TorchCodec from PyTorch's CUDA 12.9 index; the general-index wheel loads
`libnvrtc.so.13` and prevents vLLM from starting.

## Server layout

```text
/mnt/local/babylm26_g2/
├─ bin/uv
├─ cache/
├─ logs/
├─ models/Qwen3.5-4B/
├─ models/Qwen3.5-9B/
├─ python/
└─ venvs/vllm/
```

## Start vLLM

Choose a free GPU in coordination with other users. This verified command used
GPU 0 and binds only to localhost:

```bash
PATH=/mnt/local/babylm26_g2/venvs/vllm/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
CUDA_VISIBLE_DEVICES=0 \
HF_HOME=/mnt/local/babylm26_g2/cache/huggingface \
VLLM_CACHE_ROOT=/mnt/local/babylm26_g2/cache/vllm \
/mnt/local/babylm26_g2/venvs/vllm/bin/vllm serve \
  /mnt/local/babylm26_g2/models/Qwen3.5-4B \
  --served-model-name Qwen/Qwen3.5-4B \
  --host 127.0.0.1 \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.80 \
  --max-num-seqs 1 \
  --language-model-only \
  --reasoning-parser qwen3 \
  --seed 2026
```

For 9B, replace both 4B model strings with their 9B equivalents and set
`--gpu-memory-utilization 0.95`. The 9B server used about 20.8 GiB on a 24 GiB
RTX 3090 with this single-sequence configuration.

The first startup takes two to four minutes after model loading while vLLM
warms up Qwen and FlashInfer kernels. Later starts reuse the compilation cache.

## Run the challenge

Forward the localhost service from the development machine:

```powershell
ssh -N -L 8000:127.0.0.1:8000 75
```

In a second terminal, run:

```powershell
.\.venv\Scripts\python.exe -m linguistic_oj.runner `
  --public "challenges\public\zh-gsdsimp-segmentation-v2.json" `
  --private "runtime\private\challenges\zh-gsdsimp-segmentation-v2.json" `
  --dataset "Standard_Dataset\by_language\Chinese_中文.jsonl" `
  --provider openai `
  --base-url "http://127.0.0.1:8000/v1" `
  --model "Qwen/Qwen3.5-4B" `
  --model-revision "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a" `
  --runtime-version "0.27.1+cu129" `
  --max-tokens 1024 `
  --timeout-seconds 120 `
  --prompt-file "runtime\private\prompts\segmentation.txt"
```

## Observed results

Two consecutive 50-sample runs for each model produced identical aggregate
JSON within the model:

| Measurement | 4B | 9B |
| --- | ---: | ---: |
| Valid responses | 50 / 50 | 49 / 50 |
| Invalid responses | 0 / 50 | 1 / 50 (`EMPTY_VALUE`) |
| Micro precision | 0.3848439821693908 | 0.6123727486296007 |
| Micro recall | 0.4245901639344262 | 0.6409836065573771 |
| Micro F1 | 0.40374123148869834 | 0.6263516219463356 |
| Observed GPU memory | about 19.0 GiB | about 20.8 GiB |

The mock Micro-F1 is 0.3949447077409162. The 9B model improves absolute
Micro-F1 by 0.222610390457637, or about 55.1% relative to 4B, despite the one
malformed response. This makes 9B the preferred candidate for broader task
evaluation; it does not yet establish a final model choice.

For each model, one smoke request plus both challenge runs produced 101
successful `stop` completions and no length, abort, repetition, or error
completions. The averages over each set of 101 sequential requests were:

| Runtime measurement | 4B | 9B |
| --- | ---: | ---: |
| End-to-end latency | 1.882 s/request | 1.326 s/request |
| Time to first token | 0.092 s/request | 0.101 s/request |
| Decode rate | 82.6 tokens/s | 49.4 tokens/s |
| Prompt tokens | 206.8/request | 206.7/request |
| Generation tokens | 148.8/request | 60.5/request |

These are smoke-test measurements, not a throughput benchmark. The server was
configured for one sequence and requests were issued sequentially.

The recorded aggregates are
`benchmarks/qwen3.5-4b-zh-gsdsimp-segmentation-v2.json` and
`benchmarks/qwen3.5-9b-zh-gsdsimp-segmentation-v2.json`. They contain no prompts,
model inputs, sample IDs, raw responses, or gold answers.
