import json
import re

from langchain_core.tools import BaseTool
from langgraph.graph.graph import CompiledGraph
from langgraph.graph.message import BaseMessage
from langchain_core.messages import SystemMessage, HumanMessage

from ra_aid.agents.ciayn_agent import CiaynAgent
from ra_aid.config import (
    DEFAULT_MAX_TOOL_FAILURES,
    FALLBACK_TOOL_MODEL_LIMIT,
    RETRY_FALLBACK_COUNT,
)
from ra_aid.console.output import cpm
from ra_aid.exceptions import ToolExecutionError
from ra_aid.llm import initialize_llm, validate_provider_env
from ra_aid.logging_config import get_logger
from ra_aid.tool_configs import get_all_tools
from ra_aid.tool_leaderboard import supported_top_tool_models

logger = get_logger(__name__)


class FallbackHandler:
    """
    FallbackHandler manages fallback logic when tool execution fails.

    It loads fallback models from configuration and validated provider settings,
    maintains failure counts, and triggers appropriate fallback methods for both
    prompt-based and function-calling tool invocations. It also resets internal
    counters when a tool call succeeds.
    """

    def __init__(self, config, tools):
        """
        Initialize the FallbackHandler with the given configuration and tools.

        Args:
            config (dict): Configuration dictionary that may include fallback settings.
            tools (list): List of available tools.
        """
        self.config = config
        self.tools: list[BaseTool] = tools
        self.fallback_enabled = config.get("fallback_tool_enabled", True)
        self.fallback_tool_models = self._load_fallback_tool_models(config)
        self.max_failures = config.get("max_tool_failures", DEFAULT_MAX_TOOL_FAILURES)
        self.tool_failure_consecutive_failures = 0
        self.failed_messages: list[BaseMessage] = []
        self.tool_failure_used_fallbacks = set()
        self.current_failing_tool_name = ""
        self.current_tool_to_bind = None

    def _load_fallback_tool_models(self, config):
        """
        Load and return fallback tool models based on the provided configuration.

        If the config specifies 'fallback_tool_models', those are used (assuming comma-separated names).
        Otherwise, this method filters the supported_top_tool_models based on provider environment validation,
        selecting up to FALLBACK_TOOL_MODEL_LIMIT models.

        Args:
            config (dict): Configuration dictionary.

        Returns:
            list of dict: Each dictionary contains keys 'model' and 'type' representing a fallback model.
        """
        supported = []
        skipped = []
        for item in supported_top_tool_models:
            provider = item.get("provider")
            model_name = item.get("model")
            if validate_provider_env(provider):
                supported.append(item)
                if len(supported) == FALLBACK_TOOL_MODEL_LIMIT:
                    break
            else:
                skipped.append(model_name)
        final_models = []
        for item in supported:
            if "type" not in item:
                item["type"] = "prompt"
            item["model"] = item["model"].lower()
            final_models.append(item)
        message = "Fallback models selected: " + ", ".join(
            [m["model"] for m in final_models]
        )
        if skipped:
            message += (
                "\nSkipped top tool calling models due to missing provider ENV API keys: "
                + ", ".join(skipped)
            )
        cpm(message, title="Fallback Models")
        return final_models

    def handle_failure(
        self, error: ToolExecutionError, agent: CiaynAgent | CompiledGraph
    ):
        """
        Handle a tool failure by incrementing the failure counter and triggering fallback if thresholds are exceeded.

        Args:
            error (Exception): The exception raised during execution. If the exception has a 'base_message' attribute, that message is recorded.
            agent: The agent instance on which fallback may be executed.
        """
        if not self.fallback_enabled:
            return None

        failed_tool_call_name = self.extract_failed_tool_name(error)
        self._reset_on_new_failure(failed_tool_call_name)

        logger.debug(
            f"_handle_tool_failure: tool failure encountered for code '{failed_tool_call_name}' with error: {error}"
        )

        self.current_failing_tool_name = failed_tool_call_name
        self.current_tool_to_bind = self._find_tool_to_bind(
            agent, failed_tool_call_name
        )

        if hasattr(error, "base_message") and error.base_message:
            self.failed_messages.append(error.base_message)

        self.tool_failure_consecutive_failures += 1
        logger.debug(
            f"_handle_tool_failure: failure count {self.tool_failure_consecutive_failures}, max_failures {self.max_failures}"
        )

        if (
            self.tool_failure_consecutive_failures >= self.max_failures
            and self.fallback_tool_models
        ):
            logger.debug(
                "_handle_tool_failure: threshold reached, invoking fallback mechanism."
            )
            return self.attempt_fallback()

    def attempt_fallback(self):
        """
        Initiate the fallback process by iterating over all fallback models and triggering the appropriate fallback method.

        Returns:
            The response from a fallback model if any, otherwise None.
        """
        logger.error(
            f"Tool call failed {self.tool_failure_consecutive_failures} times. Attempting fallback for tool: {self.current_failing_tool_name}"
        )
        cpm(
            f"**Tool fallback activated**: Attempting fallback for tool {self.current_failing_tool_name}.",
            title="Fallback Notification",
        )
        for fallback_model in self.fallback_tool_models:
            if fallback_model.get("type", "prompt").lower() == "fc":
                response = self.attempt_fallback_function(fallback_model)
            else:
                response = self.attempt_fallback_prompt(fallback_model)
            if response:
                return response
        cpm("All fallback models have failed", title="Fallback Failed")
        return None

    def reset_fallback_handler(self):
        """
        Reset the fallback handler's internal failure counters and clear the record of used fallback models.
        """
        self.tool_failure_consecutive_failures = 0
        self.failed_messages.clear()
        self.tool_failure_used_fallbacks.clear()
        self.fallback_tool_models = self._load_fallback_tool_models(self.config)

    def _reset_on_new_failure(self, failed_tool_call_name):
        if (
            self.current_failing_tool_name
            and failed_tool_call_name != self.current_failing_tool_name
        ):
            logger.debug(
                "New failing tool name identified. Resetting consecutive tool failures."
            )
            self.reset_fallback_handler()

    def extract_failed_tool_name(self, error: ToolExecutionError):
        if error.tool_name:
            failed_tool_call_name = error.tool_name
        else:
            msg = str(error)
            logger.debug("Error message: %s", msg)
            match = re.search(r"name=['\"](\w+)['\"]", msg)
            if match:
                failed_tool_call_name = str(match.group(1))
                logger.debug(
                    "Extracted failed_tool_call_name using regex: %s",
                    failed_tool_call_name,
                )
            else:
                failed_tool_call_name = "Tool execution error"
                raise Exception("Fallback failed: Could not extract failed tool name.")

        return failed_tool_call_name

    def _find_tool_to_bind(self, agent, failed_tool_call_name):
        logger.debug(f"failed_tool_call_name={failed_tool_call_name}")
        tool_to_bind = None
        if hasattr(agent, "tools"):
            tool_to_bind = next(
                (t for t in agent.tools if t.func.__name__ == failed_tool_call_name),
                None,
            )
        if tool_to_bind is None:
            all_tools = get_all_tools()
            tool_to_bind = next(
                (t for t in all_tools if t.func.__name__ == failed_tool_call_name),
                None,
            )
        if tool_to_bind is None:
            # TODO: Would be nice to try fuzzy match or levenstein str match to find closest correspond tool name
            raise Exception(
                f"Fallback failed: {failed_tool_call_name} not found in all tools."
            )
        return tool_to_bind

    def attempt_fallback_prompt(self, fallback_model):
        """
        Attempt a prompt-based fallback by invoking the current failing tool with the given fallback model.

        Args:
            fallback_model (dict): The fallback model to use.

        Returns:
            The response from the fallback model invocation, or None if failed.
        """
        try:
            logger.debug(f"Trying fallback model: {fallback_model['model']}")
            simple_model = initialize_llm(
                fallback_model["provider"], fallback_model["model"]
            )
            binded_model = simple_model.bind_tools(
                [self.current_tool_to_bind],
                tool_choice=self.current_failing_tool_name,
            )
            retry_model = binded_model.with_retry(
                stop_after_attempt=RETRY_FALLBACK_COUNT
            )
            # msg_list = []
            msg_list = self.construct_prompt_msg_list()
            # response = retry_model.invoke(self.current_failing_tool_name)
            response = retry_model.invoke(msg_list)

            cpm(f"response={response}")
            self.tool_failure_used_fallbacks.add(fallback_model["model"])
            tool_call = self.base_message_to_tool_call_dict(response)
            if tool_call:
                result = self.invoke_prompt_tool_call(tool_call)
                cpm(f"result={result}")
                logger.debug(
                    "Prompt-based fallback executed successfully with model: "
                    + fallback_model["model"]
                )
                self.reset_fallback_handler()
                return result
            else:
                cpm(
                    response.content if hasattr(response, "content") else response,
                    title="Fallback Model Response: " + fallback_model["model"],
                )
                return response
        except Exception as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            logger.error(
                f"Prompt-based fallback with model {fallback_model['model']} failed: {e}"
            )
            return None

    def attempt_fallback_function(self, fallback_model):
        """
        Attempt a function-calling fallback by invoking the current failing tool with the given fallback model.

        Args:
            fallback_model (dict): The fallback model to use.

        Returns:
            The response from the fallback model invocation, or None if failed.
        """
        try:
            logger.debug(f"Trying fallback model: {fallback_model['model']}")
            simple_model = initialize_llm(
                fallback_model["provider"], fallback_model["model"]
            )
            binded_model = simple_model.bind_tools(
                [self.current_tool_to_bind],
                tool_choice=self.current_failing_tool_name,
            )
            retry_model = binded_model.with_retry(
                stop_after_attempt=RETRY_FALLBACK_COUNT
            )
            response = retry_model.invoke(self.current_failing_tool_name)
            cpm(f"response={response}")
            self.tool_failure_used_fallbacks.add(fallback_model["model"])
            logger.debug(
                "Function-calling fallback executed successfully with model: "
                + fallback_model["model"]
            )
            cpm(
                response.content if hasattr(response, "content") else response,
                title="Fallback Model Response: " + fallback_model["model"],
            )
            return response
        except Exception as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            logger.error(
                f"Function-calling fallback with model {fallback_model['model']} failed: {e}"
            )
            return None

    def construct_prompt_msg_list(self):
        """
        Construct a list of chat messages for the fallback prompt.
        The initial message instructs the assistant that it is a fallback tool caller.
        Then includes the failed tool call messages from self.failed_messages.
        Finally, it appends a human message asking it to retry calling the tool with correct valid arguments.

        Returns:
            list: A list of chat messages.
        """
        msg_list: list[BaseMessage] = []
        msg_list.append(
            SystemMessage(
                content="You are a fallback tool caller. Your only responsibility is to figure out what the previous failed tool call was trying to do and to call that tool with the correct format and arguments, using the provided failure messages."
            )
        )
        if self.failed_messages:
            # Convert to system messages to avoid API errors asking for correct msg structure
            msg_list.extend([SystemMessage(str(msg)) for msg in self.failed_messages])

        msg_list.append(
            HumanMessage(
                content=f"Retry using the tool '{self.current_failing_tool_name}' with improved arguments."
            )
        )
        return msg_list

    def invoke_prompt_tool_call(self, tool_call_request: dict):
        """
        Invoke a tool call from a prompt-based fallback response.

        Args:
            tool_call_request (dict): The tool call request containing keys 'type', 'name', and 'arguments'.

        Returns:
            The result of invoking the tool.
        """
        tool_name_to_tool = {tool.func.__name__: tool for tool in self.tools}
        name = tool_call_request["name"]
        arguments = tool_call_request["arguments"]
        return tool_name_to_tool[name].invoke(arguments)

    def base_message_to_tool_call_dict(self, response: BaseMessage):
        """
        Extracts a tool call dictionary from a BaseMessage.

        Args:
            response: The response object containing tool call data.

        Returns:
            A tool call dictionary with keys 'id', 'type', 'name', and 'arguments' if a tool call is found,
            otherwise None.
        """
        tool_calls = self.get_tool_calls(response)
        if tool_calls:
            if len(tool_calls) > 1:
                logger.warning("Multiple tool calls detected, using the first one")
            tool_call = tool_calls[0]
            return {
                "id": tool_call["id"],
                "type": tool_call["type"],
                "name": tool_call["function"]["name"],
                "arguments": self._parse_tool_arguments(
                    tool_call["function"]["arguments"]
                ),
            }
        return None

    def _parse_tool_arguments(self, tool_arguments):
        """
        Helper method to parse tool call arguments.
        If tool_arguments is a string, it returns the JSON-parsed dictionary.
        Otherwise, returns tool_arguments as is.
        """
        if isinstance(tool_arguments, str):
            return json.loads(tool_arguments)
        return tool_arguments

    def get_tool_calls(self, response: BaseMessage):
        """
        Extracts tool calls list from a fallback response.

        Args:
            response: The response object containing tool call data.

        Returns:
            The tool calls list if present, otherwise None.
        """
        tool_calls = None
        if hasattr(response, "additional_kwargs") and response.additional_kwargs.get(
            "tool_calls"
        ):
            tool_calls = response.additional_kwargs.get("tool_calls")
        elif hasattr(response, "tool_calls"):
            tool_calls = response.tool_calls
        elif isinstance(response, dict) and response.get("additional_kwargs", {}).get(
            "tool_calls"
        ):
            tool_calls = response.get("additional_kwargs").get("tool_calls")
        return tool_calls
