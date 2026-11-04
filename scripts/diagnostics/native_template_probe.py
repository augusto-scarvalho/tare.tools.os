"""Probe an explicitly selected llama-server renderer; never request generation."""
import argparse
import hashlib
import json
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Backend base URL exposing /apply-template")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--argument-format", choices=("tagged", "json"), default="tagged")
    args = parser.parse_args()
    started = time.perf_counter()
    rows = []
    prompts = []
    for value in (None, "null", "control-different"):
        payload = {
            "messages": [
                {"role": "user", "content": "Probe argument representation."},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "probe-1", "type": "function", "function": {
                        "name": "probe", "arguments": {"value": value}}}
                ]},
                # Complete the exchange: this endpoint treats a final assistant
                # message as a generation prefill and may omit its tool calls.
                {"role": "tool", "tool_call_id": "probe-1", "content": "done"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "probe", "parameters": {"type": "object", "properties": {
                    "value": {"type": ["string", "null"]}}, "required": ["value"]}}}],
            "add_generation_prompt": False,
        }
        request = urllib.request.Request(
            args.url.rstrip("/") + "/apply-template",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            rendered = json.load(response)["prompt"]
        if not isinstance(rendered, str) or not rendered:
            raise ValueError("renderer returned no prompt")
        prompts.append(rendered)
        marker = "<function=probe>"
        call = rendered.partition(marker)[2].partition("</function>")[0]
        argument = call.partition("<parameter=value>")[2].partition("</parameter>")[0].strip()
        observed = bool(call) and "<parameter=value>" in call
        if args.argument_format == "json":
            try:
                values = json.loads(call)
                observed = isinstance(values, dict) and "value" in values
                argument = values.get("value") if observed else None
            except ValueError:
                observed = False
                argument = None
        rows.append({"input": value, "prompt_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
                     "argument_observed": observed,
                     "rendered_argument": argument})
    result = {
        "cases": rows,
        "argument_format": args.argument_format,
        "null_string_distinguished": prompts[0] != prompts[1],
        "control_distinguished": prompts[1] != prompts[2],
        "observation_valid": all(row["argument_observed"] for row in rows)
        and rows[2]["rendered_argument"] == "control-different",
        "wall_ms": (time.perf_counter() - started) * 1000,
        "http_requests": 3,
        "inference_calls": 0,
        "scope": "Live template rendering only; no parser, generation, or lifecycle",
    }
    print(json.dumps(result, indent=2))
    if not result["observation_valid"] or not result["control_distinguished"]:
        return 2  # Invalid observation must not masquerade as a type collision.
    return 0 if result["null_string_distinguished"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
