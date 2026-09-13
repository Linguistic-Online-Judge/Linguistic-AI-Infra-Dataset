"""Probe the existing school service over SSH, without changing services or scores."""

import argparse
import json
import subprocess
import sys

_REMOTE_CHECK = r'''
import json
import urllib.request

def request(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/v1/" + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as response:
        body = response.read(32769)
    if len(body) > 32768:
        raise ValueError("response exceeds probe limit")
    return json.loads(body)

model = "Qwen/Qwen3.5-9B"
models = request("models")
if model not in {entry["id"] for entry in models["data"]}:
    raise ValueError("the configured 9B alias is not served")
response = request("chat/completions", {
    "model": model,
    "messages": [{"role": "user", "content":
        'Assign one Universal Dependencies UPOS tag to each token: ["Cats", "sleep", "."]. '
        'Return only JSON with a tags array of exactly three tags. No explanation.'}],
    "temperature": 0, "top_p": 1, "seed": 2026, "max_tokens": 256,
    "stream": False, "add_generation_prompt": True,
    "chat_template_kwargs": {"enable_thinking": False}
})
prediction = json.loads(response["choices"][0]["message"]["content"])
tags = prediction["tags"]
inventory = set(("ADJ ADP ADV AUX CCONJ DET INTJ NOUN NUM PART PRON PROPN "
                 "PUNCT SCONJ SYM VERB X").split())
if set(prediction) != {"tags"} or not isinstance(tags, list) or len(tags) != 3:
    raise ValueError("synthetic probe response has the wrong shape")
if any(not isinstance(tag, str) or tag not in inventory for tag in tags):
    raise ValueError("synthetic probe contains invalid UPOS tags")
print(json.dumps({"model": model, "service_alias_verified": True,
                  "synthetic_generation_valid": True, "tags": tags,
                  "assurance": "alias-and-synthetic-request-only",
                  "platform_scores_written": False}, sort_keys=True))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="75", help="existing approved SSH host alias")
    args = parser.parse_args()
    if not args.ssh_host or args.ssh_host.startswith("-") or any(
        char.isspace() for char in args.ssh_host
    ):
        parser.error("--ssh-host must be one SSH host or alias")
    try:
        result = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             args.ssh_host, "python3", "-"],
            input=_REMOTE_CHECK, text=True, capture_output=True, timeout=65, check=False,
        )
        if result.returncode:
            raise RuntimeError("remote probe failed")
        report = json.loads(result.stdout)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        print("Qwen connection probe failed; check SSH and the existing service.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
