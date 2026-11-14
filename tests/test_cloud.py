"""Cloud provider adapters: Tripo V3 and Tencent Hunyuan 3D Pro."""

from __future__ import annotations

import httpx
import pytest
import respx
from tests.stl_helpers import binary_stl_bytes

from xprint.cloud import (
    AdapterUnknown,
    CloudTask,
    HunyuanCloud,
    TripoCloud,
    build_provider,
    download_model,
    to_stl,
)
from xprint.errors import AdapterUnavailable, InvalidArgument
from xprint.preview import load_stl

TRIPO = "https://openapi.tripo3d.com/v3"


def _image_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_tripo_image_roundtrip() -> None:
    with respx.mock(assert_all_called=False) as mock:
        upload = mock.post(f"{TRIPO}/files").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"file_token": "file_a"}})
        )
        submit = mock.post(f"{TRIPO}/generation/image-to-model").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"task_id": "task-1"}})
        )
        poll = mock.get(f"{TRIPO}/tasks/task-1").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {"status": "running", "progress": 40, "output": {}},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "status": "success",
                            "progress": 100,
                            "output": {
                                "model_url": "https://cdn.test/model.glb",
                                "rendered_image_url": "https://cdn.test/preview.png",
                            },
                        },
                    },
                ),
            ]
        )

        client = TripoCloud("key", base_url=TRIPO)
        task_id = client.submit(image=_image_bytes())
        assert task_id == "task-1"
        assert upload.calls
        assert submit.calls

        first = client.poll(task_id)
        assert isinstance(first, CloudTask)
        assert first.status == "running" and first.progress == 40
        assert first.terminal is False

        done = client.poll(task_id)
        assert done.status == "success"
        assert done.model_url == "https://cdn.test/model.glb"
        assert done.preview_url == "https://cdn.test/preview.png"
        assert poll.call_count == 2
        client.close()


def test_tripo_text_submit_sends_prompt() -> None:
    with respx.mock(assert_all_called=False) as mock:
        submit = mock.post(f"{TRIPO}/generation/text-to-model").mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {"task_id": "task-2"}})
        )
        client = TripoCloud("key", base_url=TRIPO, model="v3.1-20260211")
        assert client.submit(prompt="a bracket") == "task-2"
        body = submit.calls[0].request.content
        assert b"a bracket" in body
        assert b"v3.1-20260211" in body
        assert submit.calls[0].request.headers["Authorization"] == "Bearer key"
        client.close()


def test_tripo_rejects_bad_key() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{TRIPO}/generation/text-to-model").mock(
            return_value=httpx.Response(401, json={"code": 401})
        )
        client = TripoCloud("bad", base_url=TRIPO)
        with pytest.raises(AdapterUnavailable, match="rejected the API key"):
            client.submit(prompt="x")
        client.close()


def test_tripo_failed_task_reports_provider_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{TRIPO}/tasks/task-3").mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "status": "failed",
                        "progress": 60,
                        "error_code": 1004,
                        "error_message": "invalid input image",
                    },
                },
            )
        )
        client = TripoCloud("key", base_url=TRIPO)
        task = client.poll("task-3")
        assert task.status == "failed"
        assert task.error == "invalid input image"
        assert task.terminal is True
        client.close()


def test_hunyuan_submit_and_poll() -> None:
    with respx.mock(assert_all_called=False) as mock:
        submit = mock.post("https://ai3d.tencentcloudapi.com").mock(
            side_effect=[
                httpx.Response(200, json={"Response": {"JobId": "job-9"}}),
                httpx.Response(
                    200,
                    json={
                        "Response": {
                            "Status": "DONE",
                            "ResultFile3Ds": [
                                {"Type": "STL", "Url": "https://cos.test/model.stl"},
                            ],
                        }
                    },
                ),
            ]
        )
        client = HunyuanCloud("sid", "skey", region="ap-guangzhou")
        job_id = client.submit(prompt="a bracket")
        assert job_id == "job-9"
        task = client.poll(job_id)
        assert task.status == "success"
        assert task.model_url == "https://cos.test/model.stl"
        assert submit.call_count == 2
        auth = submit.calls[0].request.headers["Authorization"]
        assert auth.startswith("TC3-HMAC-SHA256 Credential=sid/")
        assert submit.calls[0].request.headers["X-TC-Action"] == "SubmitHunyuanTo3DProJob"
        assert submit.calls[1].request.headers["X-TC-Action"] == "QueryHunyuanTo3DProJob"
        client.close()


def test_hunyuan_error_envelope() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://ai3d.tencentcloudapi.com").mock(
            return_value=httpx.Response(
                200,
                json={"Response": {"Error": {"Code": "AuthFailure", "Message": "bad secret"}}},
            )
        )
        client = HunyuanCloud("sid", "skey")
        with pytest.raises(AdapterUnavailable, match="bad secret"):
            client.submit(prompt="x")
        client.close()


def test_build_provider_rejects_unknown_adapter() -> None:
    with pytest.raises(AdapterUnknown):
        build_provider("nope", {"adapter": "nope"})


def test_to_stl_converts_glb_and_scales() -> None:
    import trimesh

    box = trimesh.creation.box(extents=[0.02, 0.02, 0.02])
    glb = trimesh.exchange.gltf.export_glb(box)
    stl = to_stl(glb, ".glb", scale=1000)
    triangles = load_stl_bytes(stl)
    lo, hi = triangles.reshape(-1, 3).min(axis=0), triangles.reshape(-1, 3).max(axis=0)
    assert (hi - lo) == pytest.approx([20.0, 20.0, 20.0], abs=0.2)


def test_to_stl_passthrough_and_bad_scale() -> None:
    data = binary_stl_bytes()
    assert to_stl(data, ".stl") == data
    with pytest.raises(InvalidArgument):
        to_stl(data, ".stl", scale=0)


def test_download_model_rejects_empty_body() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://cdn.test/empty.stl").mock(return_value=httpx.Response(200, content=b""))
        with pytest.raises(AdapterUnavailable):
            download_model("https://cdn.test/empty.stl")


def load_stl_bytes(data: bytes):  # noqa: ANN001, ANN201 - test helper
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "model.stl"
        path.write_bytes(data)
        return load_stl(path)
