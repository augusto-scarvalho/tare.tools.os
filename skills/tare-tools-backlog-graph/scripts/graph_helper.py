#!/usr/bin/env python3
"""Self-contained Graph Backlog Helper for ChatGPT Skills.

Zero external dependencies. Works directly with work-graph.json.
"""
import sys
import json
from pathlib import Path
from collections import defaultdict

def find_graph_path():
    for p in ["work-graph.json", "../work-graph.json", "/mnt/data/work-graph.json", "C:/projects/tare-tools-relay/work-graph.json"]:
        if Path(p).exists():
            return Path(p)
    return Path("work-graph.json")

class GraphHelper:
    def __init__(self, path=None):
        self.path = path or find_graph_path()
        if not self.path.exists():
            self.raw = {"meta": {}, "sources": {}, "nodes": [], "edges": []}
        else:
            with open(self.path, "r", encoding="utf-8") as f:
                self.raw = json.load(f)
        
        self.nodes = {n["id"]: n for n in self.raw.get("nodes", []) if isinstance(n, dict) and "id" in n}
        self.edges = self.raw.get("edges", [])
        
        self.block_in = defaultdict(list)
        self.block_out = defaultdict(list)
        for e in self.edges:
            src, dst = e.get("from"), e.get("to")
            rel = e.get("type", "UNLOCKS")
            if rel == "UNLOCKS" and src and dst:
                self.block_in[dst].append(src)
                self.block_out[src].append(dst)

    def is_done(self, node_id):
        n = self.nodes.get(node_id)
        return bool(n and n.get("completion", {}).get("status") == "DONE")

    def next_tasks(self, limit=5):
        ready = []
        for nid, n in self.nodes.items():
            if n.get("completion", {}).get("status") == "DONE":
                continue
            prereqs = self.block_in.get(nid, [])
            if all(self.is_done(p) for p in prereqs):
                pri = n.get("priority", "P2")
                base = 100 if pri == "P0" else (50 if pri == "P1" else 20)
                unlock_score = len(self.block_out.get(nid, [])) * 10
                ready.append({
                    "id": nid,
                    "title": n.get("title"),
                    "priority": pri,
                    "cluster": n.get("cluster"),
                    "score": base + unlock_score,
                    "unblocks_count": len(self.block_out.get(nid, []))
                })
        return sorted(ready, key=lambda x: x["score"], reverse=True)[:limit]

    def packet(self, node_id):
        n = self.nodes.get(node_id)
        if not n:
            return {"error": f"Task '{node_id}' not found in work graph."}
        prereqs = [{"id": p, "title": self.nodes.get(p, {}).get("title"), "done": self.is_done(p)} for p in self.block_in.get(node_id, [])]
        downstream = [{"id": d, "title": self.nodes.get(d, {}).get("title")} for d in self.block_out.get(node_id, [])]
        return {
            "id": node_id,
            "title": n.get("title"),
            "priority": n.get("priority"),
            "cluster": n.get("cluster"),
            "summary": n.get("summary"),
            "exit_criteria": n.get("exit_criteria", []),
            "prerequisites": prereqs,
            "unblocks": downstream,
            "ready": all(p["done"] for p in prereqs),
            "status": n.get("completion", {}).get("status")
        }

    def why(self, node_id):
        n = self.nodes.get(node_id)
        if not n:
            return {"error": f"Task '{node_id}' not found."}
        prereqs = self.block_in.get(node_id, [])
        unsatisfied = [p for p in prereqs if not self.is_done(p)]
        return {
            "id": node_id,
            "title": n.get("title"),
            "status": n.get("completion", {}).get("status"),
            "ready": len(unsatisfied) == 0,
            "unsatisfied_blockers": [{"id": p, "title": self.nodes.get(p, {}).get("title"), "status": self.nodes.get(p, {}).get("completion", {}).get("status")} for p in unsatisfied]
        }

    def close_task(self, node_id, evidence=""):
        n = self.nodes.get(node_id)
        if not n:
            return {"error": f"Task '{node_id}' not found."}
        n.setdefault("completion", {})["status"] = "DONE"
        n["completion"]["dod_satisfied"] = True
        if evidence:
            n.setdefault("notes", []).append(f"CLOSED_EVIDENCE: {evidence}")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.raw, f, indent=2, ensure_ascii=False)
        return {"success": True, "id": node_id, "status": "DONE"}

def main():
    if len(sys.argv) < 2:
        print("Usage: graph_helper.py [next|packet <id>|why <id>|close <id> [--evidence <ev>]]")
        return
    
    cmd = sys.argv[1]
    helper = GraphHelper()
    
    if cmd == "next":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        print(json.dumps(helper.next_tasks(limit), indent=2))
    elif cmd == "packet" and len(sys.argv) > 2:
        print(json.dumps(helper.packet(sys.argv[2]), indent=2))
    elif cmd == "why" and len(sys.argv) > 2:
        print(json.dumps(helper.why(sys.argv[2]), indent=2))
    elif cmd == "close" and len(sys.argv) > 2:
        ev = sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] == "--evidence" else ""
        print(json.dumps(helper.close_task(sys.argv[2], ev), indent=2))
    else:
        print(json.dumps({"error": f"Unknown command or missing argument: {cmd}"}, indent=2))

if __name__ == "__main__":
    main()
