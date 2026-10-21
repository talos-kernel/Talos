"""Defensive delivery guarantees around ambiguous Telegram acknowledgements.

No network is used.  The fake transport models the narrow failure mode where Telegram
has applied a mutation but the client loses the response before seeing its acknowledgement.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from talos.channel import DeliveryUncertain, StructuredMessage
from talos.telegram import (
    TelegramActivity, TelegramApiError, TelegramChannel, TelegramClient, TelegramReply,
)


CHAT_ID = 424242
CONVERSATION = f"telegram:{CHAT_ID}"


class AppliedThenResetClient:
    """Record provider-visible state, then raise as if the response connection reset."""

    def __init__(self) -> None:
        self.messages: dict[int, str] = {}
        self.next_id = 100
        self.reset_sends = False
        self.reset_edits = False

    def send_message(self, chat_id: int, text: str, **_kwargs: object) -> int:
        assert chat_id == CHAT_ID
        self.next_id += 1
        self.messages[self.next_id] = text
        if self.reset_sends:
            raise DeliveryUncertain("response reset after provider accepted send")
        return self.next_id

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **_kwargs: object
    ) -> None:
        assert chat_id == CHAT_ID
        self.messages[message_id] = text
        if self.reset_edits:
            raise DeliveryUncertain("response reset after provider accepted edit")


def test_plain_send_does_not_resend_after_ambiguous_acknowledgement() -> None:
    client = AppliedThenResetClient()
    client.reset_sends = True
    channel = TelegramChannel(client)

    with pytest.raises(DeliveryUncertain):
        channel.send(CONVERSATION, "Final answer")

    assert list(client.messages.values()) == ["Final answer"]


def test_stream_adoption_does_not_create_a_second_final_answer_after_ambiguous_edit() -> None:
    client = AppliedThenResetClient()
    reply = TelegramReply(client, CHAT_ID, min_edit_interval=0.0)
    reply.push("Draft")
    original_id = next(iter(client.messages))
    client.reset_edits = True

    with pytest.raises(DeliveryUncertain):
        reply.adopt("Final answer")

    final_ids = [message_id for message_id, text in client.messages.items() if text == "Final answer"]
    assert final_ids == [original_id]


def test_raw_connection_error_during_adoption_is_also_uncertain() -> None:
    client = AppliedThenResetClient()
    reply = TelegramReply(client, CHAT_ID, min_edit_interval=0.0)
    reply.push("Draft")

    def reset(*_args: object, **_kwargs: object) -> None:
        raise requests.ConnectionError("raw fake reset")

    client.edit_message_text = reset  # type: ignore[method-assign]

    with pytest.raises(DeliveryUncertain):
        reply.adopt("Final answer")

    assert len(client.messages) == 1


def test_initial_stream_send_uncertainty_is_latched_across_pushes_and_adopt() -> None:
    client = AppliedThenResetClient()
    client.reset_sends = True
    reply = TelegramReply(client, CHAT_ID, min_edit_interval=0.0)

    reply.push("First half ")
    reply.push("and final half")
    with pytest.raises(DeliveryUncertain):
        reply.adopt("First half and final half")

    assert len(client.messages) == 1


def _http_rejection(description: str, status: int = 400) -> TelegramApiError:
    return TelegramApiError(
        f"{status} response — {description}", status_code=status, description=description
    )


class ScriptedClient:
    def __init__(self, *effects: object) -> None:
        self.effects = list(effects)
        self.calls: list[tuple[str, dict]] = []

    def _apply(self, kind: str, kwargs: dict) -> int | None:
        self.calls.append((kind, kwargs))
        effect = self.effects.pop(0) if self.effects else None
        if isinstance(effect, Exception):
            raise effect
        return effect if isinstance(effect, int) else None

    def send_message(self, _chat_id: int, text: str, **kwargs: object) -> int:
        return self._apply("send", {"text": text, **kwargs}) or 900

    def edit_message_text(
        self, _chat_id: int, _message_id: int, text: str, **kwargs: object
    ) -> None:
        self._apply("edit", {"text": text, **kwargs})

    def delete_message(self, _chat_id: int, message_id: int) -> None:
        self._apply("delete", {"message_id": message_id})


def test_explicit_format_rejection_falls_back_to_plain_text() -> None:
    client = ScriptedClient(_http_rejection("Bad Request: can't parse entities"), 901)

    TelegramChannel(client).send(CONVERSATION, "Answer with *broken formatting")

    assert [kind for kind, _ in client.calls] == ["send", "send"]
    assert client.calls[0][1]["parse_mode"] == "HTML"
    assert "parse_mode" not in client.calls[1][1]


def test_structured_edit_reset_never_falls_back_to_fresh_message() -> None:
    client = ScriptedClient(DeliveryUncertain("edit response reset"))
    message = StructuredMessage("Decision recorded", edit_message_id=77)

    with pytest.raises(DeliveryUncertain):
        TelegramChannel(client).send_structured(CONVERSATION, message)

    assert [kind for kind, _ in client.calls] == ["edit"]


def test_structured_definite_stale_edit_falls_back_to_fresh_message() -> None:
    client = ScriptedClient(_http_rejection("Bad Request: message to edit not found"), 902)
    message = StructuredMessage("Decision recorded", edit_message_id=77)

    TelegramChannel(client).send_structured(CONVERSATION, message)

    assert [kind for kind, _ in client.calls] == ["edit", "send"]


def test_structured_unchanged_edit_is_already_delivered_without_fresh_send() -> None:
    client = ScriptedClient(_http_rejection("Bad Request: message is not modified"))
    message = StructuredMessage("Decision recorded", edit_message_id=77)

    TelegramChannel(client).send_structured(CONVERSATION, message)

    assert [kind for kind, _ in client.calls] == ["edit"]


def test_ambiguous_known_id_progress_edit_can_retry_same_id_and_adopt() -> None:
    client = ScriptedClient(501, DeliveryUncertain("edit ack lost"), None, None)
    reply = TelegramReply(client, CHAT_ID, min_edit_interval=0.0)

    reply.push("First ")
    reply.push("second ")
    reply.push("third")

    assert reply.adopt("First second third") is True
    assert [kind for kind, _ in client.calls].count("send") == 1
    assert [kind for kind, _ in client.calls].count("edit") == 3


def test_cleanup_never_deletes_message_after_ambiguous_final_adoption(
    monkeypatch,
) -> None:
    client = ScriptedClient(501, DeliveryUncertain("final edit ack lost"))
    reply = TelegramReply(client, CHAT_ID, min_edit_interval=0.0)
    reply.push("Draft")

    with pytest.raises(DeliveryUncertain):
        reply.adopt("Final answer")

    monkeypatch.setenv("TALOS_TIDY_WORK_TRAIL", "1")
    reply.cleanup()

    assert [kind for kind, _ in client.calls] == ["send", "edit"]


def test_client_transport_reset_is_redacted_and_typed_uncertain(monkeypatch) -> None:
    def post(url: str, **_kwargs: object):
        raise requests.ConnectionError(f"reset for {url}")

    monkeypatch.setattr("talos.telegram.requests.post", post)
    client = TelegramClient("private-token", 1)

    with pytest.raises(DeliveryUncertain) as caught:
        client.send_message(CHAT_ID, "Answer")

    assert "private-token" not in str(caught.value)


def test_client_http_rejection_keeps_only_safe_classification_metadata(monkeypatch) -> None:
    response = Mock(status_code=400)
    response.url = "https://api.telegram.org/botprivate-token/sendMessage"
    response.request.body = "token=private-token"
    response.json.return_value = {
        "ok": False,
        "description": "Bad Request: can't parse entities",
    }
    upstream = requests.HTTPError(
        "400 at https://api.telegram.org/botprivate-token/sendMessage", response=response
    )

    def post(_url: str, **_kwargs: object):
        raise upstream

    monkeypatch.setattr("talos.telegram.requests.post", post)
    client = TelegramClient("private-token", 1)

    with pytest.raises(requests.HTTPError) as caught:
        client.send_message(CHAT_ID, "Answer")

    assert caught.value.response is None
    assert getattr(caught.value, "status_code") == 400
    assert getattr(caught.value, "description") == "Bad Request: can't parse entities"
    assert "can't parse entities" in str(caught.value)
    assert "private-token" not in str(caught.value)
    assert "private-token" not in repr(caught.value)


def test_get_updates_keeps_ordinary_redacted_connection_error(monkeypatch) -> None:
    def get(url: str, **_kwargs: object):
        raise requests.ConnectionError(f"read reset at {url}")

    monkeypatch.setattr("talos.telegram.requests.get", get)
    client = TelegramClient("private-token", 1)

    with pytest.raises(requests.ConnectionError) as caught:
        client.get_updates(0)

    assert not isinstance(caught.value, DeliveryUncertain)
    assert "private-token" not in str(caught.value)
    assert "private-token" not in repr(caught.value)


def test_get_updates_http_error_keeps_plain_type_without_raw_response(monkeypatch) -> None:
    response = Mock(status_code=502)
    response.url = "https://api.telegram.org/botprivate-token/getUpdates"
    response.json.return_value = {"description": "Bad Gateway"}
    upstream = requests.HTTPError(f"502 at {response.url}", response=response)

    monkeypatch.setattr(
        "talos.telegram.requests.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(upstream),
    )

    with pytest.raises(requests.HTTPError) as caught:
        TelegramClient("private-token", 1).get_updates(0)

    assert type(caught.value) is requests.HTTPError
    assert caught.value.response is None
    assert "private-token" not in repr(caught.value)


def test_outbound_http_5xx_is_delivery_uncertain_without_raw_response(monkeypatch) -> None:
    response = Mock(status_code=502)
    response.url = "https://api.telegram.org/botprivate-token/sendMessage"
    response.json.return_value = {"ok": False, "description": "Bad Gateway"}
    upstream = requests.HTTPError(f"502 at {response.url}", response=response)

    monkeypatch.setattr(
        "talos.telegram.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(upstream),
    )

    with pytest.raises(DeliveryUncertain) as caught:
        TelegramClient("private-token", 1).send_message(CHAT_ID, "Answer")

    assert "private-token" not in str(caught.value)
    assert not hasattr(caught.value, "response")


def test_uncertain_activity_stops_heartbeat_without_creating_another_message() -> None:
    client = ScriptedClient(701, None)
    activity = TelegramActivity(client, CHAT_ID, heartbeat_s=0, min_edit_interval=0)
    assert activity._ensure_message() is True

    activity.uncertain()

    assert activity._stop.is_set()
    assert [kind for kind, _ in client.calls] == ["send", "edit"]
    assert "delivery unconfirmed" in client.calls[-1][1]["text"]
