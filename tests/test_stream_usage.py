import json
from types import SimpleNamespace as NS

import pytest

from llm.base import LLMMessage, LLMToolSchema
from llm.openai_compat import OpenAICompatBackend


def backend_for(chunks, function_calling=True):
    backend = object.__new__(OpenAICompatBackend)
    backend._model = "recorded"
    backend._max_tokens = 8192
    backend._use_function_calling = function_calling
    calls = []
    backend._client = NS(chat=NS(completions=NS(create=lambda **kwargs: (calls.append(kwargs) or iter(chunks)))))
    return backend, calls


@pytest.mark.parametrize("function_calling", [True, False])
def test_usage_only_final_chunk_is_counted(function_calling):
    delta = NS(content="TASK_COMPLETE: done", reasoning_content="think", tool_calls=None)
    chunks = [NS(choices=[NS(finish_reason="stop", delta=delta)], usage=None), NS(choices=[], usage=NS(prompt_tokens=1234, completion_tokens=9876))]
    backend, calls = backend_for(chunks, function_calling)
    result = backend.stream([LLMMessage("user", "run")], [])
    assert (result.input_tokens, result.output_tokens) == (1234, 9876)
    assert result.usage_source == "provider"
    assert calls[0]["stream_options"] == {"include_usage": True}


def test_usage_fallback_includes_tool_arguments_reasoning_and_schemas():
    delta = NS(content=None, reasoning_content="thinking " * 4000, tool_calls=[NS(index=0, function=NS(name="file_edit", arguments=json.dumps({"path": "p", "new_string": "x" * 20000})))])
    chunks = [NS(choices=[NS(finish_reason="tool_calls", delta=delta)])]
    backend, _ = backend_for(chunks)
    result = backend.stream([LLMMessage("user", "run")], [LLMToolSchema("file_edit", "schema " * 1000, {"type": "object"})])
    assert result.usage_source == "estimated"
    assert result.input_tokens > 1000
    assert result.output_tokens > 10000
