# -*- coding: utf-8 -*-
"""The credential test endpoint over a real app and a fake model.

The chat path is faked at the credential layer, mirroring
``app_workflow_run_router_test``: a credential registered with
``CredentialFactory`` whose model class records what it was built with
and answers instantly (or fails / hangs / streams, on cue). The
endpoint therefore exercises router, access resolution and the model
service with no network.
"""
import asyncio
import tempfile
from typing import Any, AsyncGenerator, Literal, Type
from unittest import IsolatedAsyncioTestCase
from unittest import mock

import fakeredis.aioredis
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentscope.app import create_app
from agentscope.app._router import _credential as _credential_router_module
from agentscope.app._service import ResourceAccessService
from agentscope.app.access import DenyAllResourceAccessPolicy
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import CredentialBase, CredentialFactory
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.tool import ToolChoice

HEADERS = {"X-User-ID": "alice"}


class _TestEndpointCredential(CredentialBase):
    """A credential whose chat model records every build and call."""

    type: Literal["fake_test_endpoint_credential"] = (
        "fake_test_endpoint_credential"
    )
    """The credential type discriminator."""

    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        """Return the fake model class this credential resolves to."""
        return _RecordingModel


class _RecordingModel(ChatModelBase):
    """A model that records how it was built and called.

    The model name scripts the behaviour, so one class covers the ok,
    failing, hanging and streaming paths:

    - ``ok-`` / anything else: reply instantly
    - ``fail-``: raise on every call
    - ``hang-``: sleep well past any test timeout
    - ``stream-``: reply as a chunked async generator
    """

    class Parameters(BaseModel):
        """The parameters this fake model accepts."""

        temperature: float = 0.7

    builds: list[dict] = []
    """One (model, parameters) entry per construction."""

    calls: list[str] = []
    """The model name seen by each ``_call_api`` invocation."""

    def __init__(
        self,
        credential: CredentialBase,
        model: str,
        parameters: BaseModel | None = None,
        stream: bool = False,
        max_retries: int = 0,
        retry_delay: float = 0.0,
        context_size: int = 32768,
    ) -> None:
        """Initialize like a provider model, minus the HTTP client."""
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters or self.Parameters(),
            stream=stream,
            max_retries=max_retries,
            retry_delay=retry_delay,
            context_size=context_size,
        )
        # ``get_model`` touches the formatter only when a model card
        # matches, and this class lists none; mirror real providers
        # anyway so a future card lookup cannot AttributeError.
        self.formatter = OpenAIChatFormatter()
        type(self).builds.append(
            {
                "model": model,
                "parameters": (
                    parameters.model_dump() if parameters else None
                ),
            },
        )

    @classmethod
    def list_models(cls, *args: Any, **kwargs: Any) -> list:
        """No model cards: keeps resolution hermetic to this test."""
        return []

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """Answer, or misbehave when the model name says to."""
        del messages, tools, tool_choice, kwargs
        type(self).calls.append(model_name)
        if model_name.startswith("fail-"):
            raise RuntimeError("fake provider rejected the request")
        if model_name.startswith("hang-"):
            await asyncio.sleep(30)
        if model_name.startswith("stream-"):

            async def _chunks() -> AsyncGenerator[ChatResponse, None]:
                yield ChatResponse(
                    content=[TextBlock(text="OK")],
                    is_last=True,
                )

            return _chunks()
        return ChatResponse(
            content=[TextBlock(text="OK")],
            is_last=True,
        )


CredentialFactory.register_credential(_TestEndpointCredential)


def _fake_backends() -> tuple:
    """Build a fakeredis-backed storage and message bus."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _Storage(RedisStorage):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    class _Bus(RedisMessageBus):
        async def __aenter__(self) -> Any:
            self._client = redis
            return self

        async def aclose(self) -> None:
            self._client = None

    return _Storage(), _Bus()


class CredentialTestEndpointTest(IsolatedAsyncioTestCase):
    """POST /credential/{id}/test over the full app."""

    def setUp(self) -> None:
        _RecordingModel.builds.clear()
        _RecordingModel.calls.clear()
        workdir = self.enterContext(tempfile.TemporaryDirectory())
        storage, bus = _fake_backends()
        self._client = self.enterContext(
            TestClient(
                create_app(
                    storage=storage,
                    message_bus=bus,
                    workspace_manager=LocalWorkspaceManager(workdir),
                    enable_index_worker=False,
                ),
            ),
        )
        created = self._client.post(
            "/credential/",
            json={
                "data": {
                    "type": "fake_test_endpoint_credential",
                    "name": "fake",
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        self._credential_id = created.json()["credential_id"]

    # ------------------------------------------------------------- helpers

    def _test(self, model: str, parameters: dict | None = None):
        """POST one credential test and return the response."""
        body = {"model": model}
        if parameters is not None:
            body["parameters"] = parameters
        return self._client.post(
            f"/credential/{self._credential_id}/test",
            json=body,
            headers=HEADERS,
        )

    # --------------------------------------------------------------- tests

    def test_ok_path_replies_success(self) -> None:
        """A working credential answers 200 ok=true, model name intact."""
        replied = self._test("mini-model")
        self.assertEqual(replied.status_code, 200)
        self.assertTrue(replied.json()["ok"])
        self.assertEqual(
            replied.json()["detail"],
            "Model replied successfully.",
        )
        self.assertEqual(_RecordingModel.calls, ["mini-model"])
        self.assertEqual(_RecordingModel.builds[0]["model"], "mini-model")

    def test_failing_model_is_data_not_500(self) -> None:
        """A provider error comes back 200 ok=false with the error."""
        replied = self._test("fail-bad-key")
        self.assertEqual(replied.status_code, 200)
        self.assertFalse(replied.json()["ok"])
        self.assertIn(
            "fake provider rejected the request",
            replied.json()["detail"],
        )

    def test_hanging_model_times_out(self) -> None:
        """A call past the deadline reports a timeout, not a hang."""
        with mock.patch.object(
            _credential_router_module,
            "_TEST_CALL_TIMEOUT_SECONDS",
            0.05,
        ):
            replied = self._test("hang-model")
        self.assertEqual(replied.status_code, 200)
        self.assertFalse(replied.json()["ok"])
        self.assertIn("timed out", replied.json()["detail"])

    async def test_handler_cancellation_settles_the_call(self) -> None:
        """Cancelling the handler mid-call still settles the child task.

        Drives the endpoint coroutine directly (a real cancellation
        cannot be pushed through TestClient) with a hanging model, then
        requires that no task from the exchange survives the handler.
        """
        storage, _ = _fake_backends()
        async with storage:
            credential_id = await storage.upsert_credential(
                "alice",
                CredentialFactory.from_dict(
                    {"type": "fake_test_endpoint_credential", "name": "f"},
                ),
            )
            access = ResourceAccessService(
                storage=storage,
                policy=DenyAllResourceAccessPolicy(),
            )
            handler = asyncio.create_task(
                _credential_router_module.test_credential(
                    credential_id,
                    _credential_router_module.TestCredentialRequest(
                        model="hang-model",
                    ),
                    user_id="alice",
                    access=access,
                ),
            )
            # Gate on the model call having started, so the cancel
            # lands while it is in flight, not before it exists.
            for _ in range(500):
                if _RecordingModel.calls:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(_RecordingModel.calls)

            handler.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await handler

            # A beat for the cancelled child to settle, then no task
            # from this exchange may still be running.
            await asyncio.sleep(0.05)
            lingering = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]
            self.assertEqual(lingering, [])

    def test_streaming_reply_drains_to_success(self) -> None:
        """A streamed reply is drained and still counts as a success."""
        replied = self._test("stream-model")
        self.assertEqual(replied.status_code, 200)
        self.assertTrue(replied.json()["ok"])
        self.assertEqual(_RecordingModel.calls, ["stream-model"])

    def test_unknown_credential_is_404(self) -> None:
        """Testing a missing credential id answers 404."""
        replied = self._client.post(
            "/credential/cred-missing/test",
            json={"model": "mini-model"},
            headers=HEADERS,
        )
        self.assertEqual(replied.status_code, 404)

    def test_other_users_credential_is_404(self) -> None:
        """A credential owned by someone else is invisible."""
        replied = self._client.post(
            f"/credential/{self._credential_id}/test",
            json={"model": "mini-model"},
            headers={"X-User-ID": "bob"},
        )
        self.assertEqual(replied.status_code, 404)

    def test_parameters_pass_through(self) -> None:
        """Request parameters reach the model's Parameters instance."""
        defaulted = self._test("mini-model")
        self.assertEqual(defaulted.status_code, 200)
        self.assertIsNone(_RecordingModel.builds[0]["parameters"])

        tuned = self._test("mini-model", parameters={"temperature": 0.1})
        self.assertEqual(tuned.status_code, 200)
        self.assertTrue(tuned.json()["ok"])
        self.assertEqual(
            _RecordingModel.builds[1]["parameters"],
            {"temperature": 0.1},
        )

    def test_openapi_lists_test_route(self) -> None:
        """The test route exists and keeps the CRUD routes company."""
        paths = self._client.get("/openapi.json").json()["paths"]
        self.assertIn("/credential/{credential_id}/test", paths)
        self.assertIn("post", paths["/credential/{credential_id}/test"])
        # The CRUD routes stay reachable in their own right.
        self.assertIn("/credential/", paths)
        self.assertIn("/credential/{credential_id}", paths)
