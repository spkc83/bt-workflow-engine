"""Async leaf nodes for the BT workflow engine.

All nodes are async-native — no sync-to-async bridge needed.
Each node's tick() receives the blackboard dict directly.

Node types:
- LLMResponseNode: Generate natural language via LLM
- LLMExtractNode: Extract structured JSON from text via LLM
- LLMClassifyNode: Classify input into categories via LLM
- ToolActionNode: Call an async tool function
- ConditionNode: Evaluate a Python predicate
- UserInputNode: Pause for user input (RUNNING -> SUCCESS)
- BlackboardWriteNode: Write computed values to blackboard
- LogNode: Audit trail entry
- MemoryWriteNode: Persist interaction memory to DB
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable

from bt_engine.behaviour_tree import Node, Status
from config import get_genai_client, get_model_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLMResponseNode
# ---------------------------------------------------------------------------

class LLMResponseNode(Node):
    """Generate a natural language response using a focused LLM prompt.

    The prompt_template can contain {placeholders} filled from blackboard
    values. The generated response is written to bb["agent_response"]
    (appended if multiple nodes generate text in one tick).
    """

    def __init__(self, name: str, prompt_template: str, **kwargs):
        super().__init__(name)
        self.prompt_template = prompt_template

    async def _do_tick(self, bb: dict) -> Status:
        try:
            # Skip-on-resume: if this node already ran in a prior session, skip
            if self.name in bb.get("_completed_steps", set()):
                logger.info(f"[{self.name}] Skipped (resume)")
                return Status.SUCCESS

            user_message = bb.get("user_message", "")

            # Build prompt from template + blackboard context
            try:
                prompt = self.prompt_template.format(**bb)
            except KeyError:
                prompt = self.prompt_template

            # Include relevant context
            context_parts = []
            for key, label in [
                ("order_data", "Order info"),
                ("customer_data", "Customer info"),
                ("refund_data", "Refund info"),
                ("escalation_data", "Escalation info"),
            ]:
                data = bb.get(key)
                if data:
                    context_parts.append(f"{label}: {json.dumps(data, default=str)}")

            # Cross-session memories
            memories = bb.get("customer_memories")
            if memories:
                mem_lines = [f"- {m['summary']}" for m in memories[:5]]
                context_parts.append("Past interactions:\n" + "\n".join(mem_lines))

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
 - Never output tool call code, JSON blobs, or internal function traces
 - If tool results are relevant, summarize them in plain language

{f"Context:{chr(10)}{context_str}" if context_str else ""}

Customer said: {user_message}

Your task: {prompt}"""

            response_text = await self._call_llm(full_prompt)

            # Append to agent_response (multiple nodes may contribute)
            existing = bb.get("agent_response", "")
            if existing:
                bb["agent_response"] = existing + "\n\n" + response_text
            else:
                bb["agent_response"] = response_text

            # Track completion for resume
            bb.setdefault("_completed_steps", set()).add(self.name)

            logger.info(f"[{self.name}] LLM response generated ({len(response_text)} chars)")
            return Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] LLM call failed: {e}")
            return Status.FAILURE

    async def _call_llm(self, prompt: str) -> str:
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=get_model_name(),
            contents=prompt,
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# LLMExtractNode
# ---------------------------------------------------------------------------

class LLMExtractNode(Node):
    """Extract structured data from user message using LLM.

    Results are written to the blackboard as individual keys.
    """

    def __init__(self, name: str, prompt_template: str, extract_keys: list[str] | None = None, **kwargs):
        super().__init__(name)
        self.prompt_template = prompt_template
        self.extract_keys = extract_keys or ["order_id", "merchant_name", "amount", "date"]

    async def _do_tick(self, bb: dict) -> Status:
        try:
            user_message = bb.get("user_message", "")
            if not user_message:
                return Status.FAILURE

            keys_str = ", ".join(self.extract_keys)
            prompt = f"""{self.prompt_template}

User message: "{user_message}"

Extract the following fields if present: {keys_str}

Return ONLY a JSON object with the extracted fields. Use null for fields not found.
Example: {{"order_id": "ORD-123", "merchant_name": "TechMart", "amount": 80.0, "date": null}}"""

            response_text = await self._call_llm(prompt)

            # Parse JSON from response
            extracted = self._parse_json(response_text)

            # Write extracted values to blackboard
            for key in self.extract_keys:
                value = extracted.get(key)
                if value is not None:
                    bb[key] = value

            logger.info(f"[{self.name}] Extracted: {extracted}")
            return Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] Extraction failed: {e}")
            return Status.FAILURE

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
        client = get_genai_client()
        response = await client.aio.models.generate_content(
            model=get_model_name(),
            contents=prompt,
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# LLMClassifyNode
# ---------------------------------------------------------------------------

class LLMClassifyNode(Node):
    """Classify user input into a category using LLM.

    Uses constrained enum decoding when available for guaranteed valid
    classification. Falls back to free-text matching if constrained
    decoding fails.

    Writes the classification result to bb[result_key].
    """

    def __init__(
        self,
        name: str,
        prompt_template: str,
        categories: list[str],
        result_key: str,
        **kwargs,
    ):
        super().__init__(name)
        self.prompt_template = prompt_template
        self.categories = categories
        self.result_key = result_key

    async def _do_tick(self, bb: dict) -> Status:
        try:
            user_message = bb.get("user_message", "")
            categories_str = ", ".join(self.categories)

            prompt = f"""{self.prompt_template}

User message: "{user_message}"

Classify into exactly ONE of these categories: {categories_str}"""

            classification = await self._classify_constrained(prompt)

            bb[self.result_key] = classification

            logger.info(f"[{self.name}] Classified as: {classification}")
            return Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] Classification failed: {e}")
            return Status.FAILURE

    async def _classify_constrained(self, prompt: str) -> str:
        """Classify using constrained enum decoding, with free-text fallback."""
        try:
            from bt_engine.compiler.llm_utils import classify_enum, make_dynamic_enum
            CategoryEnum = make_dynamic_enum("Category", self.categories)
            result = await classify_enum(prompt, CategoryEnum)
            if result in self.categories:
                return result
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
# ToolActionNode
# ---------------------------------------------------------------------------

class ToolActionNode(Node):
    """Call an async tool function directly (no LLM involved).

    Args are pulled from the blackboard using arg_keys mapping:
    {param_name: blackboard_key}. Results are optionally stored back.
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
        super().__init__(name)
        self.tool_func = tool_func
        self.arg_keys = arg_keys or {}
        self.fixed_args = fixed_args or {}
        self.result_key = result_key

    async def _do_tick(self, bb: dict) -> Status:
        try:
            # Skip-on-resume: if this tool already ran successfully, skip
            if self.name in bb.get("_completed_steps", set()):
                logger.info(f"[{self.name}] Skipped (resume)")
                return Status.SUCCESS

            # Build args from blackboard (skip None values)
            kwargs = {}
            for param_name, bb_key in self.arg_keys.items():
                value = bb.get(bb_key)
                if value is not None:
                    kwargs[param_name] = value
                else:
                    logger.debug(f"[{self.name}] Blackboard key '{bb_key}' is None, skipping")

            # Add fixed args
            kwargs.update(self.fixed_args)

            # All tool functions accept bb dict as last arg
            result = await self.tool_func(**kwargs, bb=bb)

            # Optionally store the result under a specific key
            if self.result_key:
                bb[self.result_key] = result

            # Check if the tool indicated failure
            if isinstance(result, dict) and result.get("found") is False:
                logger.info(f"[{self.name}] Tool returned not found")
                return Status.FAILURE

            # Track completion for resume
            bb.setdefault("_completed_steps", set()).add(self.name)

            logger.info(f"[{self.name}] Tool call succeeded")
            return Status.SUCCESS

        except Exception as e:
            logger.error(f"[{self.name}] Tool call failed: {e}")
            return Status.FAILURE


# ---------------------------------------------------------------------------
# ConditionNode
# ---------------------------------------------------------------------------

class ConditionNode(Node):
    """Evaluate a Python predicate against blackboard state.

    Returns SUCCESS if predicate returns True, FAILURE if False.
    The predicate receives the blackboard dict.
    """

    def __init__(self, name: str, predicate: Callable[[dict], bool], **kwargs):
        super().__init__(name)
        self.predicate = predicate

    async def _do_tick(self, bb: dict) -> Status:
        try:
            result = self.predicate(bb)
            status = Status.SUCCESS if result else Status.FAILURE
            logger.info(f"[{self.name}] Condition evaluated: {result}")
            return status
        except Exception as e:
            logger.error(f"[{self.name}] Condition evaluation failed: {e}")
            return Status.FAILURE


# ---------------------------------------------------------------------------
# UserInputNode
# ---------------------------------------------------------------------------

class UserInputNode(Node):
    """Wait for user input. Returns RUNNING until input arrives.

    First tick: sets awaiting_input=True, returns RUNNING (pauses tree).
    Second tick (after runner feeds new message): returns SUCCESS.
    """

    def __init__(self, name: str, **kwargs):
        super().__init__(name)
        self._waiting = False

    async def _do_tick(self, bb: dict) -> Status:
        if not self._waiting:
            bb["awaiting_input"] = True
            self._waiting = True
            logger.info(f"[{self.name}] Waiting for user input")
            return Status.RUNNING
        else:
            self._waiting = False
            logger.info(f"[{self.name}] User input received")
            return Status.SUCCESS

    def reset(self):
        super().reset()
        self._waiting = False


# ---------------------------------------------------------------------------
# BlackboardWriteNode
# ---------------------------------------------------------------------------

class BlackboardWriteNode(Node):
    """Write one or more computed values to the blackboard.

    values_func takes the current blackboard dict and returns a dict
    of key-value pairs to merge.
    """

    def __init__(self, name: str, values_func: Callable[[dict], dict], **kwargs):
        super().__init__(name)
        self.values_func = values_func

    async def _do_tick(self, bb: dict) -> Status:
        try:
            new_values = self.values_func(bb)
            bb.update(new_values)
            logger.info(f"[{self.name}] Wrote keys: {list(new_values.keys())}")
            return Status.SUCCESS
        except Exception as e:
            logger.error(f"[{self.name}] BlackboardWrite failed: {e}")
            return Status.FAILURE


# ---------------------------------------------------------------------------
# LogNode
# ---------------------------------------------------------------------------

class LogNode(Node):
    """Write an audit trail entry. Always returns SUCCESS."""

    def __init__(self, name: str, message: str = "", **kwargs):
        super().__init__(name)
        self.message = message

    async def _do_tick(self, bb: dict) -> Status:
        trail = bb.setdefault("audit_trail", [])
        entry = {
            "timestamp": datetime.now().isoformat(),
            "node": self.name,
            "message": self.message or f"Step '{self.name}' completed",
        }
        trail.append(entry)
        logger.info(f"[LOG] {entry['message']}")
        return Status.SUCCESS


# ---------------------------------------------------------------------------
# MemoryWriteNode
# ---------------------------------------------------------------------------

class MemoryWriteNode(Node):
    """Save a customer memory to the database for cross-session context.

    Summarizes the current interaction and persists it to the
    customer_memories table. Used at workflow completion when
    save_memory=True.
    """

    def __init__(self, name: str, memory_type: str = "interaction", **kwargs):
        super().__init__(name)
        self.memory_type = memory_type

    async def _do_tick(self, bb: dict) -> Status:
        try:
            customer_id = bb.get("customer_id")
            history = bb.get("conversation_history", [])

            if not customer_id or not history:
                logger.info(f"[{self.name}] No customer_id or history — skipping memory save")
                return Status.SUCCESS

            # Build a summary from blackboard state
            summary_parts = []
            if bb.get("order_data"):
                order = bb["order_data"]
                summary_parts.append(
                    f"Order {order.get('order_id', 'unknown')}: "
                    f"{order.get('merchant_name', '')} ${order.get('total', '')}"
                )
            if bb.get("refund_data") or bb.get("refund_result"):
                summary_parts.append("Refund processed")
            if bb.get("escalation_data") or bb.get("escalation_result"):
                summary_parts.append("Case escalated to supervisor")
            if bb.get("store_credit_result"):
                summary_parts.append("Store credit issued")

            if not summary_parts:
                summary_parts.append("Customer service interaction")

            summary = "; ".join(summary_parts)

            memory_data = {
                "order_id": bb.get("order_id"),
                "case_id": bb.get("case_id"),
                "resolution": summary,
            }
            await self._save_memory(customer_id, summary, memory_data)

            logger.info(f"[{self.name}] Memory saved for customer {customer_id}: {summary}")
            return Status.SUCCESS

        except Exception as e:
            # Memory save failure should not block the workflow
            logger.error(f"[{self.name}] Memory save failed (non-blocking): {e}")
            return Status.SUCCESS

    async def _save_memory(self, customer_id: str, summary: str, data: dict):
        import uuid
        from database.db import execute

        memory_id = f"MEM-{uuid.uuid4().hex[:8]}"
        await execute(
            """INSERT INTO customer_memories (memory_id, customer_id, memory_type, summary, data, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (memory_id, customer_id, self.memory_type, summary, json.dumps(data, default=str),
             datetime.now().isoformat()),
        )
