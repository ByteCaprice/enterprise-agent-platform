"""Non-streaming Agent-turn parser on the same pinned HTTPS egress as completion."""

import asyncio
import re

from domain.agent_turn import (
    AgentAssistantMessage,
    AgentModelTurn,
    AgentToolCall,
    AgentToolMessage,
    AgentTurnRequest,
    AgentTurnResponse,
    strict_json,
)
from domain.platform_model import ModelTransportFailure, ModelUsage
from domain.task import canonical_json
from infra.model_http import OpenAICompatibleTransport, response_usage


def provider_messages(conversation):
    messages = []
    for message in conversation.messages:
        item = {"role": message.role, "content": message.content}
        if isinstance(message, AgentAssistantMessage):
            item["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                "name": call.alias, "arguments": canonical_json(call.arguments),
            }} for call in message.tool_calls]
        elif isinstance(message, AgentToolMessage):
            item["tool_call_id"] = message.tool_call_id
        messages.append(item)
    return messages


def parse_turn_response(body, status, request_id):
    actual_model = None
    usage = ModelUsage()
    code = "MODEL_RESPONSE_INVALID"
    try:
        data = strict_json(body)
        model = data.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,199}", model):
            actual_model = model
        usage = response_usage(data.get("usage"))
        if actual_model is None or not isinstance(data.get("choices"), list) or len(data["choices"]) != 1:
            raise ValueError
        choice = data["choices"][0]
        message = choice["message"]
        if message.get("role") != "assistant" or message.get("audio") or message.get("function_call"):
            raise ValueError
        if message.get("refusal"):
            code = "MODEL_RESPONSE_REFUSED"
            raise ValueError
        if any(message.get(key) for key in ("reasoning_content", "reasoning", "thinking")):
            code = "MODEL_REASONING_UNSUPPORTED"
            raise ValueError
        content = message.get("content")
        calls = message.get("tool_calls")
        if choice.get("finish_reason") == "tool_calls":
            if not isinstance(calls, list) or not calls or len(calls) > 512 or content is not None and not isinstance(content, str):
                raise ValueError
            normalized = []
            for call in calls:
                if call["type"] != "function" or not isinstance(call["function"]["arguments"], str):
                    raise ValueError
                normalized.append(AgentToolCall(id=call["id"], alias=call["function"]["name"],
                                                arguments=strict_json(call["function"]["arguments"])))
            turn = AgentModelTurn(kind="tools", text=content or "", tool_calls=tuple(normalized))
        elif choice.get("finish_reason") == "stop" and not calls and isinstance(content, str):
            turn = AgentModelTurn(kind="final", text=content)
        else:
            raise ValueError
        return AgentTurnResponse(turn=turn, actual_model=actual_model, usage=usage, http_status=status, request_id=request_id)
    except Exception:
        return AgentTurnResponse(error_code=code, actual_model=actual_model, usage=usage,
                                 http_status=status, request_id=request_id)


class AgentTurnHTTPTransport(OpenAICompatibleTransport):
    async def generate_turn(self, request: AgentTurnRequest) -> AgentTurnResponse:
        try:
            async with asyncio.timeout(request.timeout_seconds):
                payload = {"model": request.model, "messages": provider_messages(request.conversation),
                           "max_tokens": request.max_tokens, "stream": False, "n": 1}
                if request.request_profile == "openrouter_non_reasoning":
                    payload["reasoning"] = {"enabled": False, "exclude": True}
                elif request.request_profile != "openai_chat":
                    raise ModelTransportFailure("MODEL_REQUEST_PROFILE_UNSUPPORTED")
                if request.temperature is not None:
                    payload["temperature"] = request.temperature
                if request.tools:
                    payload.update({"tools": [{"type": "function", "function": {
                        "name": tool.alias, "description": tool.description, "parameters": tool.input_schema,
                    }} for tool in request.tools], "tool_choice": "auto", "parallel_tool_calls": False})
                body, status, request_id = await self._post_json(request.base_url, request.secret, payload)
                return parse_turn_response(body, status, request_id)
        except (ModelTransportFailure, asyncio.CancelledError):
            raise
        except TimeoutError:
            raise ModelTransportFailure("MODEL_UPSTREAM_TIMEOUT") from None
        except Exception:
            raise ModelTransportFailure("MODEL_REQUEST_REJECTED") from None
