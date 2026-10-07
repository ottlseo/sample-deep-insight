"""The runtime reports the trace and final job status on every exit path.

Run from managed-agentcore/ with the project venv: .venv/bin/python -m pytest tests/
"""
import asyncio
import json

import pytest

import agentcore_runtime as runtime


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        with open(path, encoding="utf-8") as f:
            self.objects[key] = f.read()


class FakeGraph:
    def __init__(self, events, error=None):
        self.events, self.error = events, error

    async def stream_async(self, graph_input):
        for event in self.events:
            yield event
            await asyncio.sleep(0)
        if self.error:
            raise self.error
        yield {"type": "workflow_complete"}


TEXT = {"type": "agent_text_stream", "event_type": "text_chunk", "agent_name": "planner", "data": "plan"}
TOOL = {"type": "agent_tool_stream", "event_type": "tool_use", "agent_name": "coder",
        "tool_name": "custom_interpreter_python_tool", "tool_id": "t1", "tool_input": '{"code": "1+1"}'}


@pytest.fixture
def s3(monkeypatch, tmp_path):
    fake = FakeS3()
    monkeypatch.setenv("S3_BUCKET_NAME", "bucket")
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    monkeypatch.setattr(runtime, "_setup_execution", lambda: None)
    monkeypatch.setattr(runtime, "_cleanup_request_session", lambda request_id: None)
    monkeypatch.setattr(runtime, "_get_output_session_id", lambda request_id: "sess-1")
    monkeypatch.setattr(runtime, "_print_conversation_history", lambda: None)
    monkeypatch.setattr(runtime, "_print_token_usage_summary", lambda: None)
    monkeypatch.setattr("src.utils.event_log.tempfile.gettempdir", lambda: str(tmp_path))
    return fake


def _run(monkeypatch, graph, consume=None):
    monkeypatch.setattr(runtime, "build_graph", lambda: graph)
    payload = {"prompt": "analyze sales", "data_directory": "s3://bucket/uploads/job-1/", "job_id": "job-1"}

    async def main():
        gen = runtime.agentcore_streaming_execution(payload, None)
        if consume:
            await consume(gen)
        else:
            return [e async for e in gen]

    return asyncio.run(main())


def _status(s3):
    return json.loads(s3.objects["deep-insight/fargate_sessions/sess-1/output/job_status.json"])


def _trace(s3):
    text = s3.objects["deep-insight/fargate_sessions/sess-1/output/events.jsonl"]
    return [json.loads(line) for line in text.splitlines()]


def test_success_reports_trace_with_tool_events_and_success(s3, monkeypatch):
    streamed = _run(monkeypatch, FakeGraph([TEXT, TOOL]))
    # tool events stay out of the response stream but are in the trace
    assert [e["type"] for e in streamed] == ["agent_text_stream", "workflow_complete"]
    assert [r["kind"] for r in _trace(s3)] == ["input", "text", "tool_use"]
    assert _trace(s3)[0]["job_id"] == "job-1"
    status = _status(s3)
    assert status["status"] == "Success" and status["job_id"] == "job-1" and status["session_id"] == "sess-1"


def test_exception_mid_run_reports_failed(s3, monkeypatch):
    with pytest.raises(RuntimeError):
        _run(monkeypatch, FakeGraph([TEXT], error=RuntimeError("bedrock throttled")))
    status = _status(s3)
    assert status["status"] == "Failed" and "bedrock throttled" in status["error"]
    assert [r["kind"] for r in _trace(s3)] == ["input", "text"]


def test_client_disconnect_reports_failed(s3, monkeypatch):
    async def read_one_then_disconnect(gen):
        await gen.__anext__()
        await gen.aclose()

    _run(monkeypatch, FakeGraph([TEXT, TEXT, TEXT]), consume=read_one_then_disconnect)
    status = _status(s3)
    assert status["status"] == "Failed" and "client disconnected" in status["error"]


def test_disconnect_after_success_keeps_success(s3, monkeypatch):
    async def read_until_final_then_disconnect(gen):
        async for event in gen:
            if event["type"] == "workflow_complete":
                await gen.aclose()
                return

    _run(monkeypatch, FakeGraph([TEXT]), consume=read_until_final_then_disconnect)
    assert _status(s3)["status"] == "Success"
