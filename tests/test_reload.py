"""POST /agents/{name}/reload re-reads config/agents.yaml (fork item B19)."""
import asyncio

import yaml


def _models(h):
    async def go():
        resp = await h.client.get("/agents", headers=h._headers())
        return {a["name"]: a["model"] for a in (await resp.json())["agents"]}
    return go()


def _edit(h, fn):
    path = h.workspace / "config" / "agents.yaml"
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_reload_picks_up_a_changed_model(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            assert (await _models(h))["a"] == "fake-model"
            _edit(h, lambda d: d["agents"]["a"].update(model="haiku"))
            resp = await h.client.post("/agents/a/reload", headers=h._headers())
            assert resp.status == 200, await resp.text()
            return await _models(h)

    assert asyncio.run(scenario())["a"] == "haiku"


def test_reload_rebuilds_the_discord_token_map(harness, monkeypatch):
    h = harness(agents=["a", "b"])
    monkeypatch.setenv("TOKEN_FOR_B", "tok-b")

    async def scenario():
        async with h:
            assert "b" not in h.module.AGENT_TOKENS
            _edit(h, lambda d: d["agents"]["b"].update(discord={"token_env": "TOKEN_FOR_B"}))
            resp = await h.client.post("/agents/a/reload", headers=h._headers())
            assert resp.status == 200
            return dict(h.module.AGENT_TOKENS)

    assert asyncio.run(scenario()).get("b") == "tok-b"


def test_invalid_edit_keeps_old_config_and_returns_400(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            _edit(h, lambda d: d["agents"]["a"].update(model="haiku", effort="bogus"))
            resp = await h.client.post("/agents/a/reload", headers=h._headers())
            body = await resp.json()
            return resp.status, body, await _models(h)

    status, body, models = asyncio.run(scenario())
    assert status == 400
    assert any("effort" in p for p in body["problems"])
    assert models["a"] == "fake-model"


def test_two_agents_boot_from_yaml(harness):
    h = harness(agents=["a", "b"])

    async def scenario():
        async with h:
            return await _models(h)

    models = asyncio.run(scenario())
    assert {"a", "b"} <= set(models)
    assert not (h.workspace / "config" / "agents.json").exists()
