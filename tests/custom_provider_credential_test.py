# -*- coding: utf-8 -*-
"""The custom-provider credential: schema surface, factory wiring, and
the per-credential model listing that turns its declared model names
into selectable cards."""
import tempfile
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase

import fakeredis.aioredis
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import (
    CredentialFactory,
    CustomProviderCredential,
)
from agentscope.model import OpenAIChatModel

HEADERS = {"X-User-ID": "alice"}


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


class CustomProviderSchemaTest(TestCase):
    """The type is registered, first, with the expected surface."""

    def test_factory_registers_it_first(self) -> None:
        self.assertIs(
            CredentialFactory.get_credential_class("custom_credential"),
            CustomProviderCredential,
        )
        schemas = CredentialFactory.list_schemas()
        self.assertEqual(
            schemas[0]["title"],
            "Custom Provider (OpenAI-compatible)",
        )

    def test_round_trip_and_model_class(self) -> None:
        cred = CustomProviderCredential.model_validate(
            {
                "api_key": "sk-anything",
                "base_url": "http://localhost:8000/v1",
                "models": ["my-llama-8b", "my-qwen-72b"],
            },
        )
        restored = CredentialFactory.from_dict(cred.model_dump())
        self.assertIsInstance(restored, CustomProviderCredential)
        self.assertEqual(restored.models, ["my-llama-8b", "my-qwen-72b"])
        self.assertIs(
            restored.get_chat_model_class(),
            OpenAIChatModel,
        )

    def test_models_field_requires_one(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            CustomProviderCredential.model_validate(
                {"api_key": "k", "base_url": "u", "models": []},
            )

class CustomProviderModelResolutionTest(IsolatedAsyncioTestCase):
    """get_model must build a runnable client from the credential."""

    async def test_get_model_builds_a_runnable_openai_client(self) -> None:
        """The load-bearing integration: a custom credential must flow
        through ``get_model`` into a runnable OpenAIChatModel."""
        from agentscope.app._service._model import get_model
        from agentscope.app._service import ResourceAccessService
        from agentscope.app.access import DenyAllResourceAccessPolicy
        from agentscope.app.storage import ChatModelConfig

        storage, _ = _fake_backends()
        async with storage:
            cred = CustomProviderCredential.model_validate(
                {
                    "api_key": "sk-anything",
                    "base_url": "http://localhost:8000/v1",
                    "models": ["my-llama-8b"],
                },
            )
            await storage.upsert_credential("alice", cred)
            # DenyAll still resolves OWNED credentials — the owner
            # read path needs no grants (mirror the endpoint test).
            access = ResourceAccessService(
                storage=storage,
                policy=DenyAllResourceAccessPolicy(),
            )
            model = await get_model(
                "alice",
                ChatModelConfig(
                    type="custom_credential",
                    credential_id=cred.id,
                    model="my-llama-8b",
                    parameters={},
                ),
                access,
            )
            from agentscope.model import OpenAIChatModel

            self.assertIsInstance(model, OpenAIChatModel)
            self.assertEqual(model.model, "my-llama-8b")
            self.assertEqual(
                model.credential.base_url,
                "http://localhost:8000/v1",
            )




class CustomProviderRouterTest(IsolatedAsyncioTestCase):
    """Create via HTTP, then list models per credential."""

    def setUp(self) -> None:
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

    def _create(self) -> str:
        created = self._client.post(
            "/credential/",
            json={
                "data": {
                    "type": "custom_credential",
                    "name": "My vLLM",
                    "api_key": "sk-anything",
                    "base_url": "http://localhost:8000/v1",
                    "models": ["my-llama-8b", "my-qwen-72b"],
                },
            },
            headers=HEADERS,
        )
        self.assertEqual(created.status_code, 201)
        return created.json()["credential_id"]

    def test_create_then_models_list_the_declared_names(self) -> None:
        credential_id = self._create()
        listed = self._client.get(
            "/model/",
            params={
                "provider": "custom_credential",
                "credential_id": credential_id,
            },
            headers=HEADERS,
        )
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            [m["name"] for m in body["models"]],
            ["my-llama-8b", "my-qwen-72b"],
        )
        # The cards carry a renderable parameter schema.
        self.assertIn(
            "properties",
            body["models"][0]["parameter_schema"],
        )

    def test_without_credential_id_the_custom_type_lists_nothing(
        self,
    ) -> None:
        self._create()
        listed = self._client.get(
            "/model/",
            params={"provider": "custom_credential"},
            headers=HEADERS,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["total"], 0)

    def test_other_users_credential_is_404(self) -> None:
        credential_id = self._create()
        listed = self._client.get(
            "/model/",
            params={
                "provider": "custom_credential",
                "credential_id": credential_id,
            },
            headers={"X-User-ID": "bob"},
        )
        self.assertEqual(listed.status_code, 404)

    def test_unknown_credential_id_is_404(self) -> None:
        listed = self._client.get(
            "/model/",
            params={
                "provider": "custom_credential",
                "credential_id": "cred-missing",
            },
            headers=HEADERS,
        )
        self.assertEqual(listed.status_code, 404)
