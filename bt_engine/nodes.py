"""Custom py_trees node types for the BT workflow engine.

Node types:
- LLMResponseNode: Generate natural language using a focused prompt
- LLMExtractNode: Extract structured data from user message using LLM
- ToolActionNode: Call a CRM/common tool directly (no LLM)
- ConditionNode: Evaluate a Python predicate against blackboard state
- UserInputNode: Wait for user input (returns RUNNING until input arrives)
- BlackboardWriteNode: Write data to blackboard
- LogNode: Write audit trail entry
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable

import py_trees

from config import get_genai_client, get_model_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: safe blackboard get with default
# ---------------------------------------------------------------------------

def _bb_get(client: py_trees.blackboard.Client, key: str, default=None):
    """Get a value from a py_trees blackboard Client with a default.

    py_trees v2.4 Client.get() does not accept a default parameter.
    """
    try:
        return client.get(key)
    except (KeyError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Helper: run async function from synchronous py_trees update()
# ---------------------------------------------------------------------------

def _run_coro_in_new_loop(coro):
    """Run a coroutine in a brand-new event loop (for use in a worker thread).

    This avoids 'Future attached to a different loop' errors that occur when
    an async client (e.g. google.genai aio) reuses objects bound to another loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_async(coro):
    """Run an async coroutine from synchronous context.

    py_trees' update() is synchronous. We bridge to async tool/LLM calls
    by running them in the current event loop if one exists, or creating one.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context (e.g. FastAPI) — run in a thread
        # with a fresh event loop to avoid "Future attached to a different loop"
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(_run_coro_in_new_loop, coro).result()
    else:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# LLMResponseNode
# ---------------------------------------------------------------------------

class LLMResponseNode(py_trees.behaviour.Behaviour):
    """Generate a natural language response using a focused LLM prompt.

    The prompt_template can contain {placeholders} that are filled from
    blackboard values. The generated response is written to
    bb.agent_response (appended if multiple nodes generate text in one tick).
    """

    def __init__(self, name: str, prompt_template: str, **kwargs):
        super().__init__(name, **kwargs)
        self.prompt_template = prompt_template
        self.bb = None  # set in setup or initialise

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="user_message", access=py_trees.common.Access.READ)
        self.bb.register_key(key="agent_response", access=py_trees.common.Access.WRITE)
        self.bb.register_key(key="conversation_history", access=py_trees.common.Access.READ)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)

    def update(self) -> py_trees.common.Status:
        try:
            bb_dict = _bb_get(self.bb, "bb_dict", {})

            # Skip-on-resume: if this node already ran in a prior session, skip
            completed_steps = bb_dict.get("_completed_steps", set())
            if self.name in completed_steps:
                logger.info(f"[{self.name}] Skipped (resume)")
                return py_trees.common.Status.SUCCESS

            user_message = _bb_get(self.bb, "user_message", "")

            # Build prompt from template + blackboard context
            try:
                prompt = self.prompt_template.format(**bb_dict)
            except KeyError:
                prompt = self.prompt_template

            # Include relevant context
            context_parts = []
            order_data = bb_dict.get("order_data")
            if order_data:
                context_parts.append(f"Order info: {json.dumps(order_data, default=str)}")
            customer_data = bb_dict.get("customer_data")
            if customer_data:
                context_parts.append(f"Customer info: {json.dumps(customer_data, default=str)}")
            refund_data = bb_dict.get("refund_data")
            if refund_data:
                context_parts.append(f"Refund info: {json.dumps(refund_data, default=str)}")
            escalation_data = bb_dict.get("escalation_data")
            if escalation_data:
                context_parts.append(f"Escalation info: {json.dumps(escalation_data, default=str)}")
            # Cross-session memories
            memories = bb_dict.get("customer_memories")
            if memories:
                mem_lines = [f"- {m['summary']}" for m in memories[:5]]
                context_parts.append(f"Past interactions:\n" + "\n".join(mem_lines))

            context_str = "\n".join(context_parts)

            full_prompt = f"""You are a friendly customer service agent having a live chat conversation.

Tone rules:
- Sound like a real human, not a corporate bot
- Be warm and conversational — use contractions, natural phrasing
- Keep responses short (2-4 sentences when possible, max 6)
- Never use letter formatting (no "Sincerely", "Dear", "Best regards")
- Never output internal labels like "Case Status Update" or system metadata
- Don't repeat information the customer already knows unless confirming something new
- Use the customer's name sparingly (once at most)

{f"Context:{chr(10)}{context_str}" if context_str else ""}

Customer said: {user_message}

Your task: {prompt}"""

            response_text = _run_async(self._call_llm(full_prompt))

            # Append to agent_response (multiple nodes may contribute)
            existing = _bb_get(self.bb, "agent_response", "")
            if existing:
                self.bb.set("agent_response", existing + "\n\n" + response_text)
            else:
                self.bb.set("agent_response", response_text)

            # Track completion for resume
            completed_steps = bb_dict.get("_completed_steps", set())
            completed_steps.add(self.name)
            bb_dict["_completed_steps"] = completed_steps
            self.bb.set("bb_dict", bb_dict)

            logger.info(f"[{self.name}] LLM response generated ({len(response_text)} chars)")
            return py_trees.common.Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] LLM call failed: {e}")
            return py_trees.common.Status.FAILURE

    async def _call_llm(self, prompt: str) -> str:
        # Create a fresh client so its aiohttp session belongs to the
        # current event loop (avoids "Future attached to a different loop").
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=get_model_name(),
            contents=prompt,
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# LLMExtractNode
# ---------------------------------------------------------------------------

class LLMExtractNode(py_trees.behaviour.Behaviour):
    """Extract structured data from user message using LLM.

    The extraction schema defines what fields to extract. Results are
    written to the blackboard as individual keys.
    """

    def __init__(self, name: str, prompt_template: str, extract_keys: list[str] | None = None, **kwargs):
        super().__init__(name, **kwargs)
        self.prompt_template = prompt_template
        self.extract_keys = extract_keys or ["order_id", "merchant_name", "amount", "date"]
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="user_message", access=py_trees.common.Access.READ)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)

    def update(self) -> py_trees.common.Status:
        try:
            user_message = _bb_get(self.bb, "user_message", "")
            if not user_message:
                return py_trees.common.Status.FAILURE

            keys_str = ", ".join(self.extract_keys)
            prompt = f"""{self.prompt_template}

User message: "{user_message}"

Extract the following fields if present: {keys_str}

Return ONLY a JSON object with the extracted fields. Use null for fields not found.
Example: {{"order_id": "ORD-123", "merchant_name": "TechMart", "amount": 80.0, "date": null}}"""

            response_text = _run_async(self._call_llm(prompt))

            # Parse JSON from response
            extracted = self._parse_json(response_text)

            # Write extracted values to blackboard
            bb_dict = _bb_get(self.bb, "bb_dict", {})
            for key in self.extract_keys:
                value = extracted.get(key)
                if value is not None:
                    bb_dict[key] = value
            self.bb.set("bb_dict", bb_dict)

            logger.info(f"[{self.name}] Extracted: {extracted}")
            return py_trees.common.Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] Extraction failed: {e}")
            return py_trees.common.Status.FAILURE

    def _parse_json(self, text: str) -> dict:
        """Extract JSON from LLM response, handling markdown code blocks."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.startswith("```") and not in_block:
                    in_block = True
                    continue
                elif line.startswith("```") and in_block:
                    break
                elif in_block:
                    json_lines.append(line)
            text = "\n".join(json_lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in the text
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
            return {}

    async def _call_llm(self, prompt: str) -> str:
        # Create a fresh client so its aiohttp session belongs to the
        # current event loop (avoids "Future attached to a different loop").
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=get_model_name(),
            contents=prompt,
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# ToolActionNode
# ---------------------------------------------------------------------------

class ToolActionNode(py_trees.behaviour.Behaviour):
    """Call a tool function directly (no LLM involved).

    The tool_func must be an async function. Args are pulled from the
    blackboard using arg_keys mapping: {param_name: blackboard_key}.
    Results are optionally stored back in the blackboard.
    """

    def __init__(
        self,
        name: str,
        tool_func: Callable,
        arg_keys: dict[str, str] | None = None,
        fixed_args: dict[str, Any] | None = None,
        result_key: str | None = None,
        **kwargs,
    ):
        super().__init__(name, **kwargs)
        self.tool_func = tool_func
        self.arg_keys = arg_keys or {}
        self.fixed_args = fixed_args or {}
        self.result_key = result_key
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)

    def update(self) -> py_trees.common.Status:
        try:
            bb_dict = _bb_get(self.bb, "bb_dict", {})

            # Skip-on-resume: if this tool already ran successfully, skip
            completed_steps = bb_dict.get("_completed_steps", set())
            if self.name in completed_steps:
                logger.info(f"[{self.name}] Skipped (resume)")
                return py_trees.common.Status.SUCCESS

            # Build args from blackboard (skip None values — the tool function's
            # own defaults handle optional params; missing required params will
            # raise TypeError, caught by the except block below)
            kwargs = {}
            for param_name, bb_key in self.arg_keys.items():
                value = bb_dict.get(bb_key)
                if value is not None:
                    kwargs[param_name] = value

            # Add fixed args
            kwargs.update(self.fixed_args)

            # All tool functions accept bb dict as last arg
            result = _run_async(self.tool_func(**kwargs, bb=bb_dict))

            # Update blackboard with mutated bb_dict
            self.bb.set("bb_dict", bb_dict)

            # Optionally store the result under a specific key
            if self.result_key:
                bb_dict[self.result_key] = result
                self.bb.set("bb_dict", bb_dict)

            # Check if the tool indicated failure
            if isinstance(result, dict) and result.get("found") is False:
                logger.info(f"[{self.name}] Tool returned not found")
                return py_trees.common.Status.FAILURE

            # Track completion for resume
            completed_steps = bb_dict.get("_completed_steps", set())
            completed_steps.add(self.name)
            bb_dict["_completed_steps"] = completed_steps
            self.bb.set("bb_dict", bb_dict)

            logger.info(f"[{self.name}] Tool call succeeded")
            return py_trees.common.Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] Tool call failed: {e}")
            return py_trees.common.Status.FAILURE


# ---------------------------------------------------------------------------
# ConditionNode
# ---------------------------------------------------------------------------

class ConditionNode(py_trees.behaviour.Behaviour):
    """Evaluate a Python predicate against blackboard state.

    The predicate is a callable that takes the bb_dict and returns bool.
    Returns SUCCESS if True, FAILURE if False.
    """

    def __init__(self, name: str, predicate: Callable[[dict], bool], **kwargs):
        super().__init__(name, **kwargs)
        self.predicate = predicate
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.READ)

    def update(self) -> py_trees.common.Status:
        try:
            bb_dict = _bb_get(self.bb, "bb_dict", {})
            result = self.predicate(bb_dict)
            status = py_trees.common.Status.SUCCESS if result else py_trees.common.Status.FAILURE
            logger.info(f"[{self.name}] Condition evaluated: {result}")
            return status
        except Exception as e:
            logger.error(f"[{self.name}] Condition evaluation failed: {e}")
            return py_trees.common.Status.FAILURE


# ---------------------------------------------------------------------------
# UserInputNode
# ---------------------------------------------------------------------------

class UserInputNode(py_trees.behaviour.Behaviour):
    """Wait for user input. Returns RUNNING until input arrives.

    When this node is ticked and awaiting_input is False, it sets
    awaiting_input=True and returns RUNNING, signaling the runner to
    pause and wait for the next user message. On the next tick (after
    the runner feeds in a new message), it returns SUCCESS.
    """

    def __init__(self, name: str, **kwargs):
        super().__init__(name, **kwargs)
        self.bb = None
        self._waiting = False

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="awaiting_input", access=py_trees.common.Access.WRITE)
        self.bb.register_key(key="user_message", access=py_trees.common.Access.READ)

    def update(self) -> py_trees.common.Status:
        if not self._waiting:
            # First tick: signal that we need input
            self.bb.set("awaiting_input", True)
            self._waiting = True
            logger.info(f"[{self.name}] Waiting for user input")
            return py_trees.common.Status.RUNNING
        else:
            # Second tick: input has been provided by the runner
            self._waiting = False
            logger.info(f"[{self.name}] User input received")
            return py_trees.common.Status.SUCCESS

    def terminate(self, new_status: py_trees.common.Status):
        if new_status == py_trees.common.Status.INVALID:
            self._waiting = False


# ---------------------------------------------------------------------------
# BlackboardWriteNode
# ---------------------------------------------------------------------------

class BlackboardWriteNode(py_trees.behaviour.Behaviour):
    """Write one or more values to the blackboard.

    values_func is a callable that takes the current bb_dict and returns
    a dict of key-value pairs to merge into bb_dict.
    """

    def __init__(self, name: str, values_func: Callable[[dict], dict], **kwargs):
        super().__init__(name, **kwargs)
        self.values_func = values_func
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)

    def update(self) -> py_trees.common.Status:
        try:
            bb_dict = _bb_get(self.bb, "bb_dict", {})
            new_values = self.values_func(bb_dict)
            bb_dict.update(new_values)
            self.bb.set("bb_dict", bb_dict)
            logger.info(f"[{self.name}] Wrote keys: {list(new_values.keys())}")
            return py_trees.common.Status.SUCCESS
        except Exception as e:
            logger.error(f"[{self.name}] BlackboardWrite failed: {e}")
            return py_trees.common.Status.FAILURE


# ---------------------------------------------------------------------------
# LogNode
# ---------------------------------------------------------------------------

class LogNode(py_trees.behaviour.Behaviour):
    """Write an audit trail entry. Always returns SUCCESS."""

    def __init__(self, name: str, message: str = "", **kwargs):
        super().__init__(name, **kwargs)
        self.message = message
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="audit_trail", access=py_trees.common.Access.WRITE)

    def update(self) -> py_trees.common.Status:
        trail = _bb_get(self.bb, "audit_trail", [])
        entry = {
            "timestamp": datetime.now().isoformat(),
            "node": self.name,
            "message": self.message or f"Step '{self.name}' completed",
        }
        trail.append(entry)
        self.bb.set("audit_trail", trail)
        logger.info(f"[LOG] {entry['message']}")
        return py_trees.common.Status.SUCCESS


# ---------------------------------------------------------------------------
# LLMClassifyNode
# ---------------------------------------------------------------------------

class LLMClassifyNode(py_trees.behaviour.Behaviour):
    """Classify user input into a category using LLM.

    Uses constrained enum decoding (text/x.enum) when available for
    guaranteed valid classification. Falls back to free-text matching
    if constrained decoding fails.

    Writes the classification result to bb_dict[result_key].
    """

    def __init__(
        self,
        name: str,
        prompt_template: str,
        categories: list[str],
        result_key: str,
        **kwargs,
    ):
        super().__init__(name, **kwargs)
        self.prompt_template = prompt_template
        self.categories = categories
        self.result_key = result_key
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="user_message", access=py_trees.common.Access.READ)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.WRITE)

    def update(self) -> py_trees.common.Status:
        try:
            user_message = _bb_get(self.bb, "user_message", "")
            categories_str = ", ".join(self.categories)

            prompt = f"""{self.prompt_template}

User message: "{user_message}"

Classify into exactly ONE of these categories: {categories_str}"""

            # Try constrained enum decoding first
            classification = _run_async(self._classify_constrained(prompt))

            bb_dict = _bb_get(self.bb, "bb_dict", {})
            bb_dict[self.result_key] = classification
            self.bb.set("bb_dict", bb_dict)

            logger.info(f"[{self.name}] Classified as: {classification}")
            return py_trees.common.Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] Classification failed: {e}")
            return py_trees.common.Status.FAILURE

    async def _classify_constrained(self, prompt: str) -> str:
        """Classify using constrained enum decoding, with free-text fallback."""
        try:
            from bt_engine.compiler.llm_utils import classify_enum, make_dynamic_enum
            CategoryEnum = make_dynamic_enum("Category", self.categories)
            result = await classify_enum(prompt, CategoryEnum)
            # Validate result is actually one of our categories
            if result in self.categories:
                return result
            # Fuzzy match if constrained output doesn't exactly match
            for cat in self.categories:
                if cat.lower() in result.lower():
                    return cat
            return result
        except Exception:
            logger.debug(f"[{self.name}] Constrained decoding failed, falling back to free-text")
            return await self._classify_freetext(prompt)

    async def _classify_freetext(self, prompt: str) -> str:
        """Fallback: free-text classification with string matching."""
        client = get_genai_client()
        full_prompt = prompt + "\n\nReturn ONLY the category name, nothing else."
        response = await client.aio.models.generate_content(
            model=get_model_name(),
            contents=full_prompt,
        )
        response_text = (response.text or "").strip().lower().replace('"', '').replace("'", "")
        for cat in self.categories:
            if cat.lower() in response_text:
                return cat
        return response_text


# ---------------------------------------------------------------------------
# MemoryWriteNode
# ---------------------------------------------------------------------------

class MemoryWriteNode(py_trees.behaviour.Behaviour):
    """Save a customer memory to the database for cross-session context.

    Summarizes the current interaction using LLM and persists it to the
    customer_memories table. Used at workflow completion when save_memory=True.
    """

    def __init__(self, name: str, memory_type: str = "interaction", **kwargs):
        super().__init__(name, **kwargs)
        self.memory_type = memory_type
        self.bb = None

    def initialise(self):
        self.bb = py_trees.blackboard.Client(name=self.name)
        self.bb.register_key(key="bb_dict", access=py_trees.common.Access.READ)
        self.bb.register_key(key="conversation_history", access=py_trees.common.Access.READ)

    def update(self) -> py_trees.common.Status:
        try:
            bb_dict = _bb_get(self.bb, "bb_dict", {})
            history = _bb_get(self.bb, "conversation_history", [])
            customer_id = bb_dict.get("customer_id")

            if not customer_id or not history:
                logger.info(f"[{self.name}] No customer_id or history — skipping memory save")
                return py_trees.common.Status.SUCCESS

            # Build a summary from blackboard state
            summary_parts = []
            if bb_dict.get("order_data"):
                order = bb_dict["order_data"]
                summary_parts.append(
                    f"Order {order.get('order_id', 'unknown')}: "
                    f"{order.get('merchant_name', '')} ${order.get('total', '')}"
                )
            if bb_dict.get("refund_data") or bb_dict.get("refund_result"):
                summary_parts.append("Refund processed")
            if bb_dict.get("escalation_data") or bb_dict.get("escalation_result"):
                summary_parts.append("Case escalated to supervisor")
            if bb_dict.get("store_credit_result"):
                summary_parts.append("Store credit issued")

            if not summary_parts:
                summary_parts.append("Customer service interaction")

            summary = "; ".join(summary_parts)

            # Persist to database
            memory_data = {
                "order_id": bb_dict.get("order_id"),
                "case_id": bb_dict.get("case_id"),
                "resolution": summary,
            }
            _run_async(self._save_memory(customer_id, summary, memory_data))

            logger.info(f"[{self.name}] Memory saved for customer {customer_id}: {summary}")
            return py_trees.common.Status.SUCCESS

        except Exception as e:
            # Memory save failure should not block the workflow
            logger.error(f"[{self.name}] Memory save failed (non-blocking): {e}")
            return py_trees.common.Status.SUCCESS

    async def _save_memory(self, customer_id: str, summary: str, data: dict):
        """Persist memory to the customer_memories table."""
        import uuid
        from database.db import execute

        memory_id = f"MEM-{uuid.uuid4().hex[:8]}"
        await execute(
            """INSERT INTO customer_memories (memory_id, customer_id, memory_type, summary, data, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (memory_id, customer_id, self.memory_type, summary, json.dumps(data, default=str),
             datetime.now().isoformat()),
        )
