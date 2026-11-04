from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tare_node.qualified_fleet import (  # noqa: E402
    CATALOG,
    FleetConfigError,
    compose,
    owner_fleet,
    public_card,
    build_backend_command,
    host_mode,
    load_registry,
    ninfer_chat_request,
    recommend,
    resolve_model,
    validate_registry,
)


def test_catalog_home_paths_resolve_without_changing_qualification(tmp_path, monkeypatch):
    import json
    from tare_node.qualified_fleet import first_fleet
    source = CATALOG.read_text(encoding="utf-8")
    original = json.loads(source)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    registry = load_registry(catalog_path)
    card = registry["models"]["qwen38-gsq"]
    artifact = tmp_path / original["models"]["qwen38-gsq"]["artifact"]["path"][2:]
    artifact.parent.mkdir(parents=True)
    artifact.touch()
    assert card["artifact"]["path"] == artifact.as_posix()
    assert first_fleet(registry)["models"] == ["qwen38-gsq"]
    assert build_backend_command("qwen38-gsq", card, host="127.0.0.1", port=18080)[1] == artifact.as_posix()
    for model_id, before in original["models"].items():
        after = registry["models"][model_id]
        assert {k: v for k, v in after.items() if k not in ("artifact", "runtime")} == {
            k: v for k, v in before.items() if k not in ("artifact", "runtime")}
        assert {k: v for k, v in after["artifact"].items() if k != "path"} == {
            k: v for k, v in before["artifact"].items() if k != "path"}
    vision = registry["models"]["gemma-vision"]["runtime"]
    projector = vision["args"][vision["args"].index("--mmproj") + 1]
    assert projector == (tmp_path / "models/gemma-4-12b-vision/mmproj-gemma-4-12B-it-Q8_0.gguf").as_posix()
    assert vision["environment"]["LD_LIBRARY_PATH"].startswith(tmp_path.as_posix() + "/opt/")
    assert catalog_path.read_text(encoding="utf-8") == source


def test_qwen38_chat_template_ships_with_the_package_byte_for_byte(tmp_path):
    import hashlib
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(CATALOG.read_text(encoding="utf-8"), encoding="utf-8")  # a copied catalog still finds it
    args = load_registry(catalog_path)["models"]["qwen38"]["runtime"]["args"]
    template = Path(args[args.index("--chat-template-file") + 1])
    assert template.is_file() and template.parent == CATALOG.parent
    # Exactly the qwen38 bring-up template (tare.tools.local-labs ops/qwen38-bringup/), the file the catalog named.
    assert hashlib.sha256(template.read_bytes()).hexdigest() == (
        "bf6686a32ad4e999feafb1031e8f84191541953be0ca6363657831e227966efe")


class QualifiedFleetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_registry(CATALOG)

    def test_the_catalog_holds_the_models_we_qualified(self) -> None:
        self.assertEqual(
            set(self.registry["models"]),
            {"qwen38", "qwen38-ninfer", "qwen38-gsq", "swift27b", "swift-next", "qwen36-moe", "fable-tc", "hauhaucs",
             "gemma-vision", "muse-vision"},
        )
        self.assertTrue(all(
            card["qualification"] in {"promoted", "qualified_role"}
            for card in self.registry["models"].values()
        ))

    def test_aliases_and_recommendations_are_deterministic(self) -> None:
        self.assertEqual(resolve_model(self.registry, "coding")[0], "hauhaucs")
        self.assertEqual(resolve_model(self.registry, "vision-hard")[0], "muse-vision")
        self.assertEqual(resolve_model(self.registry, "throughput")[0], "qwen36-moe")
        self.assertEqual(recommend(self.registry, "math")[0], "fable-tc")
        self.assertEqual(recommend(self.registry, "agent-tools")[0], "qwen38-gsq")

    def test_one_resident_model_is_fail_closed(self) -> None:
        invalid = copy.deepcopy(self.registry)
        invalid["fleet"]["max_resident_models"] = 2
        with self.assertRaisesRegex(FleetConfigError, "max_resident_models=1"):
            validate_registry(invalid)

    def test_the_owner_decides_what_enters_the_fleet(self) -> None:
        mine = copy.deepcopy(self.registry)
        mine["models"]["my-model"] = {"artifact": {"path": "/data/models/mine.gguf"},
                                      "runtime": {"binary": "/opt/llama/llama-server", "args": ["-c", "8192"]}}
        mine["models"]["hauhaucs"]["qualification"] = "hold"
        validate_registry(mine)
        card = public_card("my-model", mine["models"]["my-model"])
        self.assertEqual((card["display_name"], card["qualification"], card["modalities"]),
                         ("my-model", "owner_choice", ["text"]))
        mine["models"]["my-model"]["artifact"]["path"] = "models/mine.gguf"
        with self.assertRaisesRegex(FleetConfigError, "absolute"):
            validate_registry(mine)

    def test_the_owners_fleet_picks_from_the_catalog_and_the_catalog_keeps_its_cards_current(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".config" / "tare-node" / "fleet.json"
            first = owner_fleet(self.registry, path)  # the first start: no weights here, so the catalog default
            self.assertEqual(first, {"default": "qwen38-gsq", "models": ["qwen38-gsq"], "custom": {},
                                     "off": []})
            path.write_text('{"default": "hauhaucs", "models": ["hauhaucs", "retired-one"]}', encoding="utf-8")
            self.assertEqual(owner_fleet(self.registry, path)["default"], "hauhaucs")  # never overwritten
        served, missing = compose(self.registry, {"default": "hauhaucs", "models": ["hauhaucs", "retired-one"]})
        self.assertEqual((list(served["models"]), missing), (["hauhaucs"], ["retired-one"]))
        self.assertIs(served["models"]["hauhaucs"], self.registry["models"]["hauhaucs"])  # the catalog's card
        self.assertEqual(served["aliases"]["coding"], "hauhaucs")
        self.assertNotIn("general", served["aliases"])  # its model is not served
        self.assertNotIn("host_modes", served["fleet"])
        with self.assertRaisesRegex(FleetConfigError, "retired-one"):
            compose(self.registry, {"default": "hauhaucs", "models": ["hauhaucs", "retired-one"]}, strict=True)
        with self.assertRaisesRegex(FleetConfigError, "own names"):
            compose(self.registry, {"models": [], "custom": {"hauhaucs": {"artifact": {"path": "/m/x"}}}})

    def test_an_own_model_needs_only_its_weights_and_may_be_the_default(self) -> None:
        served, _ = compose(self.registry, {"default": "mine", "models": ["qwen38-gsq"],
                                            "custom": {"mine": {"artifact": {"path": "/models/mine.gguf"}}}})
        self.assertEqual(list(served["models"]), ["qwen38-gsq", "mine"])  # ours first
        self.assertEqual(resolve_model(served, None)[0], "mine")
        command = build_backend_command("mine", served["models"]["mine"], host="127.0.0.1", port=18080)
        self.assertTrue(command[0].endswith("llama-server") and command[2] == "/models/mine.gguf")
    def test_gateway_owns_identity_and_network_flags(self) -> None:
        card = self.registry["models"]["hauhaucs"]
        command = build_backend_command("hauhaucs", card, host="127.0.0.1", port=18080)
        self.assertEqual(command[0], card["runtime"]["binary"])
        self.assertEqual(command[command.index("--alias") + 1], "hauhaucs")
        self.assertEqual(command[command.index("--port") + 1], "18080")
        self.assertEqual(command[command.index("-m") + 1], card["artifact"]["path"])

    def _ninfer_card(self) -> dict:
        card = copy.deepcopy(self.registry["models"]["hauhaucs"])
        card["runtime"] = {"kind": "ninfer", "binary": "/home/operator/opt/ninfer/v0.6.1-rtx3090/ninfer-serve",
                           "environment": {}, "args": ["--max-context", "32768", "--spec", "mtp"]}
        card["artifact"]["path"] = "/home/operator/models/qwen38-27b/ninfer/qwen3_8_27b.ninfer"
        return card

    def test_ninfer_backend_takes_artifact_positionally_and_gateway_owns_its_identity(self) -> None:
        registry = copy.deepcopy(self.registry)
        registry["models"]["qwen38-ninfer"] = self._ninfer_card()
        validate_registry(registry)
        command = build_backend_command("qwen38-ninfer", registry["models"]["qwen38-ninfer"],
                                        host="127.0.0.1", port=18080)
        self.assertEqual(command[1], "/home/operator/models/qwen38-27b/ninfer/qwen3_8_27b.ninfer")
        self.assertEqual(command[command.index("--model-id") + 1], "qwen38-ninfer")
        self.assertNotIn("-m", command)
        wrong = copy.deepcopy(registry)
        wrong["models"]["qwen38-ninfer"]["runtime"]["binary"] = "/home/operator/opt/slop/bin/llama-server"
        with self.assertRaisesRegex(FleetConfigError, "ninfer-serve"):
            validate_registry(wrong)
        owned = copy.deepcopy(registry)
        owned["models"]["qwen38-ninfer"]["runtime"]["args"] += ["--model-id", "other"]
        with self.assertRaisesRegex(FleetConfigError, "gateway-owned"):
            validate_registry(owned)

    def test_strata_backend_is_started_from_its_config_on_the_gateway_port(self) -> None:
        registry = copy.deepcopy(self.registry)
        card = copy.deepcopy(self.registry["models"]["hauhaucs"])
        card["artifact"]["path"] = "/mnt/wsl/models/flash-next/swift-IQ3_XXS/shard-1.gguf"
        card["runtime"] = {"kind": "strata", "binary": "/mnt/wsl/models/strata/.venv/bin/python",
                           "server": "/mnt/wsl/models/strata/serve/server.py",
                           "config": "/mnt/wsl/models/strata/strata-swift-iq3_xxs.json",
                           "model_name": "swift-1.5-iq3_xxs", "environment": {}, "args": [],
                           "idle_unload_seconds": 900}
        registry["models"]["swift-next"] = card
        validate_registry(registry)
        command = build_backend_command("swift-next", card, host="127.0.0.1", port=18080)
        self.assertEqual(command[1:3], ["/mnt/wsl/models/strata/serve/server.py", "--engine"])
        self.assertEqual(command[command.index("--port") + 1], "18080")
        self.assertEqual(command[command.index("--config") + 1], card["runtime"]["config"])
        for broken in ({"model_name": ""}, {"idle_unload_seconds": 10}, {"args": ["--port", "1"]}):
            bad = copy.deepcopy(registry)
            bad["models"]["swift-next"]["runtime"].update(broken)
            with self.assertRaises(FleetConfigError):
                validate_registry(bad)

    def test_solo_model_only_when_its_host_ram_is_free_now(self) -> None:
        modes = self.registry["fleet"]["host_modes"]
        now = 1_000_000
        needed = modes["solo_host_ram_gb"] + modes["ram_margin_gb"]
        fits = {"ts": now - 60, "windows_free_gb": needed, "desktop_idle_seconds": 5}  # someone at the desk is fine
        self.assertEqual(host_mode(self.registry, fits, now)["model"], "swift-next")
        # A resident solo model already holds its RAM: it keeps fitting instead of flapping.
        held = {**fits, "windows_free_gb": needed - modes["solo_host_ram_gb"]}
        self.assertEqual(host_mode(self.registry, held, now, "swift-next")["mode"], "solo")
        cases = {
            "host_ram_short": {**fits, "windows_free_gb": needed - 0.5},
            "host_status_stale": {**fits, "ts": now - modes["max_status_age_seconds"] - 1},
            "host_status_missing": None,
        }
        for reason, status in cases.items():
            result = host_mode(self.registry, status, now)
            self.assertEqual((result["mode"], result["model"], result["reason"]), ("shared", "qwen38-gsq", reason))
        broken = copy.deepcopy(self.registry)
        broken["fleet"]["host_modes"]["solo"] = "unknown"
        with self.assertRaisesRegex(FleetConfigError, "host_modes"):
            validate_registry(broken)

    def test_ninfer_request_moves_thinking_controls_to_top_level(self) -> None:
        body = {"model": "m", "temperature": 1.0, "reasoning_effort": "high",
                "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "low", "other": 1}}
        out = ninfer_chat_request(body)
        self.assertEqual(out["enable_thinking"], True)
        self.assertEqual(out["reasoning_effort"], "high")  # an explicit top-level value wins
        self.assertEqual(out["chat_template_kwargs"], {"other": 1})
        self.assertEqual(ninfer_chat_request({"chat_template_kwargs": {"enable_thinking": False}}),
                         {"enable_thinking": False})
        self.assertIn("chat_template_kwargs", body)  # input is not mutated


if __name__ == "__main__":
    unittest.main()
