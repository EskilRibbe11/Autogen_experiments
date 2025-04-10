import asyncio
import json
import logging
import warnings
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Sequence,
)

from autogen_core import CancellationToken, FunctionCall
from autogen_core.memory import Memory
from autogen_core.model_context import (
    ChatCompletionContext,
    UnboundedChatCompletionContext,
)
from autogen_core.models import (
    AssistantMessage,
    ChatCompletionClient,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool, Tool

from .. import EVENT_LOGGER_NAME
from ..base import Handoff as HandoffBase
from ..base import Response
from ..messages import (
    AgentEvent,
    ChatMessage,
    HandoffMessage,
    MemoryQueryEvent,
    MultiModalMessage,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
    ToolCallSummaryMessage,
)
from ..state import AssistantAgentState
from ._base_chat_agent import BaseChatAgent

event_logger = logging.getLogger(EVENT_LOGGER_NAME)


class AssistantAgent(BaseChatAgent):
    """An agent that provides assistance with tool use.

    The :meth:`on_messages` returns a :class:`~autogen_agentchat.base.Response`
    in which :attr:`~autogen_agentchat.base.Response.chat_message` is the final
    response message.

    The :meth:`on_messages_stream` creates an async generator that produces
    the inner messages as they are created, and the :class:`~autogen_agentchat.base.Response`
    object as the last item before closing the generator.

    .. note::

        The caller must only pass the new messages to the agent on each call
        to the :meth:`on_messages` or :meth:`on_messages_stream` method.
        The agent maintains its state between calls to these methods.
        Do not pass the entire conversation history to the agent on each call.

    .. note::
        The assistant agent is not thread-safe or coroutine-safe.
        It should not be shared between multiple tasks or coroutines, and it should
        not call its methods concurrently.

    Tool call behavior:

    * If the model returns no tool call, then the response is immediately returned as a :class:`~autogen_agentchat.messages.TextMessage` in :attr:`~autogen_agentchat.base.Response.chat_message`.
    * When the model returns tool calls, they will be executed right away:
        - When `reflect_on_tool_use` is False (default), the tool call results are returned as a :class:`~autogen_agentchat.messages.ToolCallSummaryMessage` in :attr:`~autogen_agentchat.base.Response.chat_message`. `tool_call_summary_format` can be used to customize the tool call summary.
        - When `reflect_on_tool_use` is True, the another model inference is made using the tool calls and results, and the text response is returned as a :class:`~autogen_agentchat.messages.TextMessage` in :attr:`~autogen_agentchat.base.Response.chat_message`.

    .. note::
        By default, the tool call results are returned as response when tool calls are made.
        So it is recommended to pay attention to the formatting of the tools return values,
        especially if another agent is expecting them in a specific format.
        Use `tool_call_summary_format` to customize the tool call summary, if needed.

    Hand off behavior:

    * If a handoff is triggered, a :class:`~autogen_agentchat.messages.HandoffMessage` will be returned in :attr:`~autogen_agentchat.base.Response.chat_message`.
    * If there are tool calls, they will also be executed right away before returning the handoff.


    .. note::
        If multiple handoffs are detected, only the first handoff is executed.


    Limit context size sent to the model:

    You can limit the number of messages sent to the model by setting
    the `model_context` parameter to a :class:`~autogen_core.model_context.BufferedChatCompletionContext`.
    This will limit the number of recent messages sent to the model and can be useful
    when the model has a limit on the number of tokens it can process.


    Args:
        name (str): The name of the agent.
        model_client (ChatCompletionClient): The model client to use for inference.
        tools (List[Tool | Callable[..., Any] | Callable[..., Awaitable[Any]]] | None, optional): The tools to register with the agent.
        handoffs (List[HandoffBase | str] | None, optional): The handoff configurations for the agent,
            allowing it to transfer to other agents by responding with a :class:`HandoffMessage`.
            The transfer is only executed when the team is in :class:`~autogen_agentchat.teams.Swarm`.
            If a handoff is a string, it should represent the target agent's name.
        model_context (ChatCompletionContext | None, optional): The model context for storing and retrieving :class:`~autogen_core.models.LLMMessage`. It can be preloaded with initial messages. The initial messages will be cleared when the agent is reset.
        description (str, optional): The description of the agent.
        system_message (str, optional): The system message for the model. If provided, it will be prepended to the messages in the model context when making an inference. Set to `None` to disable.
        reflect_on_tool_use (bool, optional): If `True`, the agent will make another model inference using the tool call and result
            to generate a response. If `False`, the tool call result will be returned as the response. Defaults to `False`.
        tool_call_summary_format (str, optional): The format string used to create a tool call summary for every tool call result.
            Defaults to "{result}".
            When `reflect_on_tool_use` is `False`, a concatenation of all the tool call summaries, separated by a new line character ('\\n')
            will be returned as the response.
            Available variables: `{tool_name}`, `{arguments}`, `{result}`.
            For example, `"{tool_name}: {result}"` will create a summary like `"tool_name: result"`.
        memory (Sequence[Memory] | None, optional): The memory store to use for the agent. Defaults to `None`.

    Raises:
        ValueError: If tool names are not unique.
        ValueError: If handoff names are not unique.
        ValueError: If handoff names are not unique from tool names.
        ValueError: If maximum number of tool iterations is less than 1.

    Examples:

        The following example demonstrates how to create an assistant agent with
        a model client and generate a response to a simple task.

        .. code-block:: python

            import asyncio
            from autogen_core import CancellationToken
            from autogen_ext.models.openai import OpenAIChatCompletionClient
            from autogen_agentchat.agents import AssistantAgent
            from autogen_agentchat.messages import TextMessage


            async def main() -> None:
                model_client = OpenAIChatCompletionClient(
                    model="gpt-4o",
                    # api_key = "your_openai_api_key"
                )
                agent = AssistantAgent(name="assistant", model_client=model_client)

                response = await agent.on_messages(
                    [TextMessage(content="What is the capital of France?", source="user")], CancellationToken()
                )
                print(response)


            asyncio.run(main())


        The following example demonstrates how to create an assistant agent with
        a model client and a tool, generate a stream of messages for a task, and
        print the messages to the console.

        .. code-block:: python

            import asyncio
            from autogen_ext.models.openai import OpenAIChatCompletionClient
            from autogen_agentchat.agents import AssistantAgent
            from autogen_agentchat.messages import TextMessage
            from autogen_agentchat.ui import Console
            from autogen_core import CancellationToken


            async def get_current_time() -> str:
                return "The current time is 12:00 PM."


            async def main() -> None:
                model_client = OpenAIChatCompletionClient(
                    model="gpt-4o",
                    # api_key = "your_openai_api_key"
                )
                agent = AssistantAgent(name="assistant", model_client=model_client, tools=[get_current_time])

                await Console(
                    agent.on_messages_stream(
                        [TextMessage(content="What is the current time?", source="user")], CancellationToken()
                    )
                )


            asyncio.run(main())


        The following example shows how to use `o1-mini` model with the assistant agent.

        .. code-block:: python

            import asyncio
            from autogen_core import CancellationToken
            from autogen_ext.models.openai import OpenAIChatCompletionClient
            from autogen_agentchat.agents import AssistantAgent
            from autogen_agentchat.messages import TextMessage


            async def main() -> None:
                model_client = OpenAIChatCompletionClient(
                    model="o1-mini",
                    # api_key = "your_openai_api_key"
                )
                # The system message is not supported by the o1 series model.
                agent = AssistantAgent(name="assistant", model_client=model_client, system_message=None)

                response = await agent.on_messages(
                    [TextMessage(content="What is the capital of France?", source="user")], CancellationToken()
                )
                print(response)


            asyncio.run(main())

        .. note::

            The `o1-preview` and `o1-mini` models do not support system message and function calling.
            So the `system_message` should be set to `None` and the `tools` and `handoffs` should not be set.
            See `o1 beta limitations <https://platform.openai.com/docs/guides/reasoning#beta-limitations>`_ for more details.
    """

    def __init__(
        self,
        name: str,
        model_client: ChatCompletionClient,
        *,
        tools: List[Tool | Callable[..., Any] | Callable[..., Awaitable[Any]]] | None = None,
        handoffs: List[HandoffBase | str] | None = None,
        model_context: ChatCompletionContext | None = None,
        description: str = "An agent that provides assistance with ability to use tools.",
        system_message: (
            str | None
        ) = "You are a helpful AI assistant. Solve tasks using your tools. Reply with TERMINATE when the task has been completed.",
        reflect_on_tool_use: bool = False,
        tool_call_summary_format: str = "{result}",
        memory: Sequence[Memory] | None = None,
    ):
        super().__init__(name=name, description=description)
        self._model_client = model_client
        self._memory = None
        if memory is not None:
            if isinstance(memory, list):
                self._memory = memory
            else:
                raise TypeError(f"Expected Memory, List[Memory], or None, got {type(memory)}")

        self._system_messages: List[
            SystemMessage | UserMessage | AssistantMessage | FunctionExecutionResultMessage
        ] = []
        if system_message is None:
            self._system_messages = []
        else:
            self._system_messages = [SystemMessage(content=system_message)]
        self._tools: List[Tool] = []
        if tools is not None:
            if model_client.model_info["function_calling"] is False:
                raise ValueError("The model does not support function calling.")
            for tool in tools:
                if isinstance(tool, Tool):
                    self._tools.append(tool)
                elif callable(tool):
                    if hasattr(tool, "__doc__") and tool.__doc__ is not None:
                        description = tool.__doc__
                    else:
                        description = ""
                    self._tools.append(FunctionTool(tool, description=description))
                else:
                    raise ValueError(f"Unsupported tool type: {type(tool)}")
        # Check if tool names are unique.
        tool_names = [tool.name for tool in self._tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError(f"Tool names must be unique: {tool_names}")
        # Handoff tools.
        self._handoff_tools: List[Tool] = []
        self._handoffs: Dict[str, HandoffBase] = {}
        if handoffs is not None:
            if model_client.model_info["function_calling"] is False:
                raise ValueError("The model does not support function calling, which is needed for handoffs.")
            for handoff in handoffs:
                if isinstance(handoff, str):
                    handoff = HandoffBase(target=handoff)
                if isinstance(handoff, HandoffBase):
                    self._handoff_tools.append(handoff.handoff_tool)
                    self._handoffs[handoff.name] = handoff
                else:
                    raise ValueError(f"Unsupported handoff type: {type(handoff)}")
        # Check if handoff tool names are unique.
        handoff_tool_names = [tool.name for tool in self._handoff_tools]
        if len(handoff_tool_names) != len(set(handoff_tool_names)):
            raise ValueError(f"Handoff names must be unique: {handoff_tool_names}")
        # Check if handoff tool names not in tool names.
        if any(name in tool_names for name in handoff_tool_names):
            raise ValueError(
                f"Handoff names must be unique from tool names. Handoff names: {handoff_tool_names}; tool names: {tool_names}"
            )
        if model_context is not None:
            self._model_context = model_context
        else:
            self._model_context = UnboundedChatCompletionContext()
        self._reflect_on_tool_use = reflect_on_tool_use
        self._tool_call_summary_format = tool_call_summary_format
        self._is_running = False

    @property
    def produced_message_types(self) -> Sequence[type[ChatMessage]]:
        """The types of messages that the assistant agent produces."""
        message_types: List[type[ChatMessage]] = [TextMessage]
        if self._handoffs:
            message_types.append(HandoffMessage)
        if self._tools:
            message_types.append(ToolCallSummaryMessage)
        return tuple(message_types)

    async def on_messages(self, messages: Sequence[ChatMessage], cancellation_token: CancellationToken) -> Response:
        async for message in self.on_messages_stream(messages, cancellation_token):
            if isinstance(message, Response):
                return message
        raise AssertionError("The stream should have returned the final result.")

    async def on_messages_stream(
        self, messages: Sequence[ChatMessage], cancellation_token: CancellationToken
    ) -> AsyncGenerator[AgentEvent | ChatMessage | Response, None]:
        # Add messages to the model context.
        for msg in messages:
            if isinstance(msg, MultiModalMessage) and self._model_client.model_info["vision"] is False:
                raise ValueError("The model does not support vision.")
            await self._model_context.add_message(UserMessage(content=msg.content, source=msg.source))

        # Inner messages.
        inner_messages: List[AgentEvent | ChatMessage] = []

        # Update the model context with memory content.
        if self._memory:
            for memory in self._memory:
                update_context_result = await memory.update_context(self._model_context)
                if update_context_result and len(update_context_result.memories.results) > 0:
                    memory_query_event_msg = MemoryQueryEvent(
                        content=update_context_result.memories.results, source=self.name
                    )
                    inner_messages.append(memory_query_event_msg)
                    yield memory_query_event_msg

        # Generate an inference result based on the current model context.
        llm_messages = self._system_messages + await self._model_context.get_messages()
        result = await self._model_client.create(
            llm_messages, tools=self._tools + self._handoff_tools, cancellation_token=cancellation_token
        )

        # Add the response to the model context.
        await self._model_context.add_message(AssistantMessage(content=result.content, source=self.name))

        # Check if the response is a string and return it.
        if isinstance(result.content, str):
            yield Response(
                chat_message=TextMessage(content=result.content, source=self.name, models_usage=result.usage),
                inner_messages=inner_messages,
            )
            return

        # Process tool calls.
        assert isinstance(result.content, list) and all(isinstance(item, FunctionCall) for item in result.content)
        tool_call_msg = ToolCallRequestEvent(content=result.content, source=self.name, models_usage=result.usage)
        event_logger.debug(tool_call_msg)
        # Add the tool call message to the output.
        inner_messages.append(tool_call_msg)
        yield tool_call_msg

        # Execute the tool calls.
        results = await asyncio.gather(*[self._execute_tool_call(call, cancellation_token) for call in result.content])
        tool_call_result_msg = ToolCallExecutionEvent(content=results, source=self.name)
        event_logger.debug(tool_call_result_msg)
        await self._model_context.add_message(FunctionExecutionResultMessage(content=results))
        inner_messages.append(tool_call_result_msg)
        yield tool_call_result_msg

        # Detect handoff requests.
        handoffs: List[HandoffBase] = []
        for call in result.content:
            if call.name in self._handoffs:
                handoffs.append(self._handoffs[call.name])
        if len(handoffs) > 0:
            if len(handoffs) > 1:
                # show warning if multiple handoffs detected
                warnings.warn(
                    f"Multiple handoffs detected only the first is executed: {[handoff.name for handoff in handoffs]}",
                    stacklevel=2,
                )
            # Return the output messages to signal the handoff.
            yield Response(
                chat_message=HandoffMessage(content=handoffs[0].message, target=handoffs[0].target, source=self.name),
                inner_messages=inner_messages,
            )
            return

        if self._reflect_on_tool_use:
            # Generate another inference result based on the tool call and result.
            llm_messages = self._system_messages + await self._model_context.get_messages()
            result = await self._model_client.create(llm_messages, cancellation_token=cancellation_token)
            assert isinstance(result.content, str)
            # Add the response to the model context.
            await self._model_context.add_message(AssistantMessage(content=result.content, source=self.name))
            # Yield the response.
            yield Response(
                chat_message=TextMessage(content=result.content, source=self.name, models_usage=result.usage),
                inner_messages=inner_messages,
            )
        else:
            # Return tool call result as the response.
            tool_call_summaries: List[str] = []
            for i in range(len(tool_call_msg.content)):
                tool_call_summaries.append(
                    self._tool_call_summary_format.format(
                        tool_name=tool_call_msg.content[i].name,
                        arguments=tool_call_msg.content[i].arguments,
                        result=tool_call_result_msg.content[i].content,
                    ),
                )
            tool_call_summary = "\n".join(tool_call_summaries)
            yield Response(
                chat_message=ToolCallSummaryMessage(content=tool_call_summary, source=self.name),
                inner_messages=inner_messages,
            )

    async def _execute_tool_call(
        self, tool_call: FunctionCall, cancellation_token: CancellationToken
    ) -> FunctionExecutionResult:
        """Execute a tool call and return the result."""
        try:
            if not self._tools + self._handoff_tools:
                raise ValueError("No tools are available.")
            tool = next((t for t in self._tools + self._handoff_tools if t.name == tool_call.name), None)
            if tool is None:
                raise ValueError(f"The tool '{tool_call.name}' is not available.")
            arguments = json.loads(tool_call.arguments)
            result = await tool.run_json(arguments, cancellation_token)
            result_as_str = tool.return_value_as_string(result)
            return FunctionExecutionResult(content=result_as_str, call_id=tool_call.id)
        except Exception as e:
            return FunctionExecutionResult(content=f"Error: {e}", call_id=tool_call.id)

    async def on_reset(self, cancellation_token: CancellationToken) -> None:
        """Reset the assistant agent to its initialization state."""
        await self._model_context.clear()

    async def save_state(self) -> Mapping[str, Any]:
        """Save the current state of the assistant agent."""
        model_context_state = await self._model_context.save_state()
        return AssistantAgentState(llm_context=model_context_state).model_dump()

    async def load_state(self, state: Mapping[str, Any]) -> None:
        """Load the state of the assistant agent"""
        assistant_agent_state = AssistantAgentState.model_validate(state)
        # Load the model context state.
        await self._model_context.load_state(assistant_agent_state.llm_context)
