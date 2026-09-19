"""Image ownership, atomic forgetting and real SDK serialization without live inference."""

import copy
import json
from dataclasses import asdict

import httpx2 as httpx
import pytest
from openai import OpenAI

from forgetting_agent.context import Archive, ContextState
from forgetting_agent.costs import estimate
from forgetting_agent.loop import LoopConfig, run_episode
from forgetting_agent.media import ToolImage, input_metrics, project_messages
from forgetting_agent.provider import OpenAIModel
from forgetting_agent.scripted import ScriptedModel
from forgetting_agent.tools import ToolResult
from forgetting_agent.trace import Trace

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8WQAAAAASUVORK5CYII="
IMAGE = ToolImage(PNG, "image/png")


class Screens:
    names = ("screen",)
    schemas = {
        "screen": {
            "description": "Read the current screen.",
            "parameters": {"type": "object", "properties": {}},
        }
    }

    def call(self, name, arguments):
        return ToolResult("Screen evidence", images=(IMAGE,))


def image_parts(messages):
    return [
        p
        for m in messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p["type"] == "image_url"
    ]


@pytest.mark.parametrize("arm", ["method", "retained"])
def test_sdk_image_roundtrip_and_cost_usage(tmp_path, arm):
    steps = [[("screen", {}), ("screen", {})]]
    if arm == "method":
        steps += [
            [
                (
                    "context_apply",
                    {
                        "targets": [
                            {"id": "r1", "note": "Button checked"},
                            {"id": "r2", "note": "Duplicate screen"},
                        ]
                    },
                )
            ],
            [("context_recover", {"ref": "ic-0001"})],
            [("context_apply", {"targets": [{"id": "r3", "note": "Reviewed again"}]})],
        ]
    steps += [[("submit_answer", {"answer": "done"})]]
    fixture = ScriptedModel(steps)
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        response = fixture.complete(body)
        response["usage"] = {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}
        return httpx.Response(200, json=response)

    model = OpenAIModel(
        "fake", "https://fake.invalid/v1", "vision-fixture", supports_images=True, streaming=False
    )
    model._client.close()
    trace = Trace()
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model._client = OpenAI(
            api_key="fake", base_url="https://fake.invalid/v1", http_client=client, max_retries=0
        )
        result = run_episode(
            "Inspect the screen.", Screens(), model, LoopConfig(arm=arm, max_calls=8), trace
        )
    assert result.termination == "submitted"
    assert [len(image_parts(b["messages"])) for b in bodies] == (
        [0, 2, 0, 1, 0] if arm == "method" else [0, 2]
    )
    for body in bodies:
        pending = set()
        for m in body["messages"]:
            assert "images" not in m  # internal attachments never leak onto the wire
            if m["role"] == "assistant":
                assert not pending
                pending = {t["id"] for t in m.get("tool_calls", [])}
            elif m["role"] == "tool":
                pending.remove(m["tool_call_id"])
                assert isinstance(m["content"], str) and PNG not in m["content"]
            elif m["role"] == "user":
                assert not pending  # carriers occur only after the complete tool batch
        for part in image_parts(body["messages"]):
            assert part == IMAGE.part()
    assert [m for m in result.state.messages if m["role"] == "user"] == [
        {"role": "user", "content": "Inspect the screen."}
    ]
    if arm == "method":
        assert result.state.archive.get("ic-0001").original_images == (IMAGE,)
        assert result.state.archive.get("ic-0003").original_images == (IMAGE,)
        assert not result.state.result_images
    responses = [e for e in trace.events if e["event"] == "response"]
    assert sum(e["usage"]["prompt_tokens"] for e in responses) == len(steps) * 1000
    metrics = [e["input_metrics"] for e in trace.events if e["event"] == "request"]
    assert metrics[1]["estimated_total_tokens"] is None
    assert metrics[1]["estimated_image_tokens"] is None
    cost = estimate(trace.events)
    assert cost["tokens_reported"]["input_tokens"] == len(steps) * 1000
    assert cost["image_accounting"]["separate_image_tokens"] is None
    assert cost["image_accounting"]["logical_request_image_occurrences"] == (
        3 if arm == "method" else 2
    )


def test_atomic_archive_images_and_chronological_clear(monkeypatch):
    state = ContextState("system", "task")
    state.append_tool_result("c", "screen", {}, "caption", images=(IMAGE,))
    before = copy.deepcopy(state.__dict__)
    assert not state.apply([{"id": "r1", "note": "keep"}, {"id": "r2", "note": "bad"}]).ok
    assert state.__dict__ == before

    def fail(*args):
        raise OSError("storage unavailable")

    with monkeypatch.context() as m:
        m.setattr(Archive, "with_entries", fail)
        assert not state.apply([{"id": "r1", "note": "keep"}]).ok
    assert state.__dict__ == before
    state.clear_oldest_result()
    assert not image_parts(project_messages(state.messages))
    assert not state.result_images


def test_image_size_is_not_a_text_token_estimate():
    small = {"role": "tool", "tool_call_id": "c", "content": "caption", "images": [asdict(IMAGE)]}
    large = copy.deepcopy(small)
    large["images"][0]["data"] = "YWJj" * 100000
    a = input_metrics(project_messages([small]), [])
    b = input_metrics(project_messages([large]), [])
    assert a == b and a["estimated_image_tokens"] is None


def test_text_only_provider_refuses_image_before_http():
    model = OpenAIModel("fake", "https://fake.invalid/v1", "text-model")
    try:
        with pytest.raises(ValueError, match="not enabled"):
            model.complete({"messages": [{"role": "user", "content": [IMAGE.part()]}]})
        assert not model.last_attempts
    finally:
        model._client.close()
