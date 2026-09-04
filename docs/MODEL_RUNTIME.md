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

Each online Worker selects one `mvp-evaluation-v2` contract from a
deployment-owned challenge registry. The pinned 9B tokenizer evidence at the
model revision above is:

| Artifact | SHA-256 |
| --- | --- |
| `tokenizer_config.json` | `316230d6a809701f4db5ea8f8fc862bc3a6f3229c937c174e674ff3ca0a64ac8` |
| `tokenizer.json` | `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42` |
| UTF-8 chat-template value | `a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715` |

The worker must compute these hashes from the local snapshot and use
`apply_chat_template(..., tokenize=True, add_generation_prompt=True,
enable_thinking=False)` before consuming jobs. A served model alias alone is not
runtime attestation because it does not prove the model commit or tokenizer
artifacts.

## Online worker attestation

At startup the Worker loads the complete registry, selects the required challenge
ID, and accepts only an `mvp-evaluation-v2` contract whose public description also
matches the explicitly configured challenge artifacts. It then loads the
tokenizer from a deployment-owned local model directory with Transformers in
offline mode, rehashes the two tokenizer files and template, and verifies the
local vLLM `/v1/models` response before it claims a job. The directory name is not
treated as revision evidence; the pinned tokenizer file hashes are. The Worker
accepts only a loopback hostname:
`127.0.0.1`, `::1`, or `localhost`. An SSH tunnel can also bind to a loopback
address and cannot be distinguished by the HTTP client, so deployment network
policy must prohibit tunnels and remote port forwarding for online evaluation.

This check protects the Worker from accidental configuration mismatch. It is not
a cryptographic proof of a running vLLM process or model weights: the
OpenAI-compatible `/v1/models` API exposes a model alias, not a revision hash or
process identity. Production therefore trusts the deployment boundary that owns
the Worker command, local snapshot, launch-evidence file, loopback network
namespace, and vLLM process. Do not run the Worker with a snapshot, evidence
file, or localhost endpoint controlled by an untrusted tenant.

The vLLM launcher must write an operator-controlled JSON evidence file before
starting the Worker. On POSIX systems, the Worker rejects a symlink, non-regular,
group-writable, or world-writable evidence file. It has this exact schema:

```json
{
  "schema_version": "linguistic-oj-vllm-launch-v1",
  "model_snapshot_path": "/absolute/path/to/c202236235762e1c871ad0ccb60c8ee5ba337b9a",
  "runtime_version": "0.27.1+cu129",
  "max_model_len": 4096,
  "max_num_seqs": 1,
  "language_model_only": true
}
```

The Worker derives its attestation from this file, the local snapshot, and the
local service. It does not accept a caller-supplied identity assertion. Every
chat-completions request explicitly sends `add_generation_prompt: true` and
`enable_thinking: false`, matching local token preflight.

Install the Worker with `pip install '.[qwen-worker]'`. Run it with a
deployment-owned registry and one selected challenge, for example:

```bash
python -m linguistic_oj.qwen_worker \
  --root /srv/linguistic-oj \
  --challenge-registry config/deployment-challenges.json \
  --challenge-id en-example-upos-v1 \
  --postgres-database-url-file /run/credentials/postgres-url \
  --redis-url-file /run/credentials/redis-url \
  --public-challenge challenges/public/en-example-upos-v1.json \
  --private-challenge runtime/private/challenges/en-example-upos-v1.json \
  --dataset runtime/private/datasets/english.jsonl \
  --vllm-base-url http://127.0.0.1:8000/v1 \
  --tokenizer-snapshot /srv/models/qwen3.5-9b \
  --launch-evidence /run/linguistic-oj/qwen-launch.json
```

Run these commands from the deployment root when using relative artifact paths,
or pass absolute paths for the public challenge, private challenge, dataset,
tokenizer snapshot, and launch evidence.

Use `--once` for a single operational smoke delivery; otherwise it polls the
selected contract's Redis Stream continuously.

Run the paired API with the same registry:

```bash
python -m linguistic_oj.qwen_api \
  --root /srv/linguistic-oj \
  --challenge-registry config/deployment-challenges.json \
  --postgres-database-url-file /run/credentials/postgres-url \
  --redis-url-file /run/credentials/redis-url \
  --authenticate package.module:callback
```

The authentication callback receives a FastAPI request and must return
`linguistic_oj.api.Principal`; it is deployment-owned rather than a built-in
header-based fallback. The API creates one contract-snapshot Redis route per
executable registry entry. A Worker selects one of those same snapshots by
challenge ID. Development smoke tests may opt in to draft submissions;
production cannot.

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

The later two-language UPOS comparison observed a higher 9B peak of about
22.5 GiB during long generations. See [`UPOS_BENCHMARK.md`](UPOS_BENCHMARK.md)
for its accuracy, validity, completion-reason, and latency results.
