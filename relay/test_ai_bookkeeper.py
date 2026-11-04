#!/usr/bin/env python3
import subprocess
import json
from pathlib import Path

def test_gemini():
    prompt = (
        "You are the tare.tools Senior Graph Backlog Curator & Bookkeeper AI (Gemini 3.7).\n"
        "Given the landed evidence and technical debts, extract candidate backlog tasks.\n"
        "Return a JSON object in this format:\n"
        "{\n"
        '  "status": "SUCCESS",\n'
        '  "debts_extracted": 1,\n'
        '  "candidate_nodes": [\n'
        "    {\n"
        '      "id": "INTAKE-DEBT-EXAMPLE-01",\n'
        '      "title": "Example Debt Resolution",\n'
        '      "cluster": "07 Assurance / task alignment",\n'
        '      "priority": "P1",\n'
        '      "horizon": "H1",\n'
        '      "exit_criteria": ["Verify and complete debt resolution."]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Return ONLY the JSON string."
    )
    cmd = ["agy", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
    print("Running agy Gemini curation...")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("Return code:", proc.returncode)
    try:
        data = json.loads(proc.stdout)
        print("Parsed JSON response:", json.dumps(data, indent=2)[:500])
    except Exception as e:
        print("Raw output:", proc.stdout[:500])
        print("JSON parse error:", e)

if __name__ == "__main__":
    test_gemini()
