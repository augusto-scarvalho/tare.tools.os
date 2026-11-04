"""The owner's fleet over the network: GET /v1/fleet shows our catalog and the owner's pick, PUT changes what the
gateway serves at once, with the node token."""
import json
from urllib import request

from test_gateway_gpu_coordination import gateway, serving
from test_gateway_reservation import call
from tare_node.qualified_fleet import CATALOG, compose, load_registry, save_fleet


def get(endpoint, path):
    with request.urlopen(endpoint + path, timeout=5) as response:
        return json.load(response)


def test_the_owner_picks_the_fleet_and_the_gateway_serves_it_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "GPU_API_TOKEN", "node-token")
    catalog = load_registry(CATALOG)
    with serving() as rt:
        rt.catalog, rt.fleet_path, rt.fleet_missing = catalog, tmp_path / "fleet.json", []
        save_fleet({"default": "qwen38-gsq", "models": ["qwen38-gsq"], "custom": {}}, rt.fleet_path)
        rt.config = compose(catalog, json.loads(rt.fleet_path.read_text()))[0]
        rt.model_id = "qwen38-gsq"
        view = get(rt.endpoint, "/v1/fleet")
        assert view["served"] == ["qwen38-gsq"] and view["default"] == "qwen38-gsq"
        assert [card["id"] for card in view["catalog"]] == list(catalog["models"])
        assert all(card["on_node"] in (True, False) for card in view["catalog"])

        mine = {"default": "mine", "models": ["hauhaucs", "fable-tc"],
                "custom": {"mine": {"artifact": {"path": "/models/mine.gguf"}}}}
        assert call(rt.endpoint, "/v1/fleet", mine, method="PUT")[0] == 401
        code, view, _ = call(rt.endpoint, "/v1/fleet", mine, method="PUT", token="node-token")
        assert code == 200 and view["served"] == ["hauhaucs", "fable-tc", "mine"] and view["default"] == "mine"
        assert json.loads(rt.fleet_path.read_text()) == {**mine, "off": ["qwen38-gsq"]}  # taken out: stays off
        assert rt.model_id is None  # the resident model left the fleet: stopped
        assert [m["id"] for m in get(rt.endpoint, "/v1/models")["data"]] == ["hauhaucs", "fable-tc", "mine"]

        typo = {**mine, "models": ["hauhacus"]}
        code, refused, _ = call(rt.endpoint, "/v1/fleet", typo, method="PUT", token="node-token")
        assert code == 400 and "hauhacus" in refused["error"]["message"]
        assert json.loads(rt.fleet_path.read_text())["models"] == mine["models"]


def test_a_qualified_model_downloaded_to_the_node_joins_unless_the_owner_turned_it_off(tmp_path, monkeypatch):
    import copy
    monkeypatch.setattr(gateway, "GPU_API_TOKEN", "node-token")
    catalog = copy.deepcopy(load_registry(CATALOG))
    for model_id in ("hauhaucs", "fable-tc", "gemma-vision"):
        catalog["models"][model_id]["artifact"]["path"] = str(tmp_path / (model_id + ".gguf"))
    with serving() as rt:
        rt.catalog, rt.fleet_path, rt.fleet_missing = catalog, tmp_path / "fleet.json", []
        save_fleet({"default": "qwen38-gsq", "models": ["qwen38-gsq", "fable-tc"], "custom": {}}, rt.fleet_path)
        rt.config = compose(catalog, json.loads(rt.fleet_path.read_text()))[0]
        assert gateway.join_arrivals() == []  # nothing downloaded yet
        mine = {"default": "qwen38-gsq", "models": ["qwen38-gsq"], "custom": {}}  # the owner takes fable-tc out
        assert call(rt.endpoint, "/v1/fleet", mine, method="PUT", token="node-token")[0] == 200
        assert json.loads(rt.fleet_path.read_text())["off"] == ["fable-tc"]
        for model_id in ("hauhaucs", "fable-tc"):
            (tmp_path / (model_id + ".gguf")).write_bytes(b"weights")
        assert gateway.join_arrivals() == ["hauhaucs"]  # downloaded; fable-tc stays off
        assert get(rt.endpoint, "/v1/fleet")["served"] == ["qwen38-gsq", "hauhaucs"]
        assert gateway.join_arrivals() == []
        back = {"default": "qwen38-gsq", "models": ["qwen38-gsq", "hauhaucs", "fable-tc"], "custom": {}}
        assert call(rt.endpoint, "/v1/fleet", back, method="PUT", token="node-token")[0] == 200
        assert json.loads(rt.fleet_path.read_text())["off"] == []  # served again: no longer off
