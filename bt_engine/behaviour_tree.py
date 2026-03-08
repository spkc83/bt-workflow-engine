"""Purpose-built async behaviour tree engine for enterprise workflow orchestration.

Designed for banking operations (fraud triage, customer care, disputes) where:
- Steps involve async I/O (LLM calls, API calls, database queries)
- Workflows pause for human input and resume across sessions
- Regulatory audit trails are mandatory
- Independent evidence-gathering steps can run concurrently
- External system calls need retry resilience

Composites:
  Sequence  - run children left-to-right, stop on FAILURE or RUNNING
  Selector  - run children left-to-right, stop on SUCCESS or RUNNING
  Parallel  - run children concurrently (asyncio.gather)

Decorators:
  Retry     - retry a child on failure with backoff
  Inverter  - flip SUCCESS <-> FAILURE

All node tick() methods are async. A single await tree.tick(bb) call
processes the entire tree until a pause point (RUNNING) or completion.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class Status(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RUNNING = "RUNNING"


# ---------------------------------------------------------------------------
# Base Node
# ---------------------------------------------------------------------------

class Node:
    """Base class for all behaviour tree nodes."""

    def __init__(self, name: str):
        self.name = name
        self.status: Status | None = None
        self.children: list[Node] = []

    async def tick(self, bb: dict) -> Status:
        """Execute this node and record an audit trail entry."""
        status = await self._do_tick(bb)
        self.status = status
        # Structured audit event
        bb.setdefault("_audit_trail", []).append({
            "tick": bb.get("_tick_count", 0),
            "timestamp": datetime.now().isoformat(),
            "node_name": self.name,
            "node_type": type(self).__name__,
            "status": status.value,
        })
        return status

    async def _do_tick(self, bb: dict) -> Status:
        raise NotImplementedError

    def reset(self):
        """Reset this node and all descendants for re-execution."""
        self.status = None
        for child in self.children:
            child.reset()

    def iterate(self):
        """Yield all nodes in the subtree (depth-first pre-order)."""
        yield self
        for child in self.children:
            yield from child.iterate()

    def __repr__(self):
        return f"{type(self).__name__}({self.name!r})"


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------

class Sequence(Node):
    """Execute children left-to-right. Stop on first FAILURE or RUNNING.

    memory=True (default): On re-tick after RUNNING, resume from the child
    that returned RUNNING — already-succeeded children are not re-executed.
    memory=False: Always start from the first child.

    Banking use cases:
    - Linear workflow steps: collect info -> lookup -> validate -> process
    - Ensuring all preconditions pass before action
    """

    def __init__(self, name: str, memory: bool = True, children: list[Node] | None = None):
        super().__init__(name)
        self.children = list(children or [])
        self.memory = memory
        self._index = 0

    def add_children(self, children: list[Node]):
        self.children.extend(children)

    async def _do_tick(self, bb: dict) -> Status:
        if not self.memory:
            self._index = 0

        while self._index < len(self.children):
            child = self.children[self._index]
            status = await child.tick(bb)

            if status == Status.RUNNING:
                return Status.RUNNING
            elif status == Status.FAILURE:
                self._index = 0
                return Status.FAILURE

            # SUCCESS — advance to next child
            self._index += 1

        # All children succeeded
        self._index = 0
        return Status.SUCCESS

    def reset(self):
        super().reset()
        self._index = 0


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------

class Selector(Node):
    """Execute children left-to-right. Stop on first SUCCESS or RUNNING.

    memory=False (default): Always evaluate from the first child —
    standard "try alternatives until one works" pattern.
    memory=True: Resume from the child that returned RUNNING.

    Banking use cases:
    - Routing by severity: high -> medium -> low paths
    - Fallback strategies: exact lookup -> fuzzy search -> manual entry
    - Condition-guarded branches: eligible -> outside window -> cancelled
    """

    def __init__(self, name: str, memory: bool = False, children: list[Node] | None = None):
        super().__init__(name)
        self.children = list(children or [])
        self.memory = memory
        self._index = 0

    def add_children(self, children: list[Node]):
        self.children.extend(children)

    async def _do_tick(self, bb: dict) -> Status:
        if not self.memory:
            self._index = 0

        while self._index < len(self.children):
            child = self.children[self._index]
            status = await child.tick(bb)

            if status == Status.RUNNING:
                return Status.RUNNING
            elif status == Status.SUCCESS:
                self._index = 0
                return Status.SUCCESS

            # FAILURE — try next child
            self._index += 1

        # All children failed
        self._index = 0
        return Status.FAILURE

    def reset(self):
        super().reset()
        self._index = 0


# ---------------------------------------------------------------------------
# Parallel
# ---------------------------------------------------------------------------

class Parallel(Node):
    """Execute children concurrently using asyncio.gather.

    policy="all" (default): SUCCESS only if every child succeeds.
        If any child fails, the overall result is FAILURE.
    policy="any": SUCCESS if at least one child succeeds.

    Children must not return RUNNING — Parallel is designed for
    concurrent fire-and-forget operations (tool calls, LLM calls),
    not interactive nodes.

    Banking use cases:
    - Fraud investigation: gather transactions + device data + login history
    - KYC checks: verify identity + check watchlists + validate documents
    - Dispute evidence: pull merchant records + bank statements + prior cases
    """

    def __init__(self, name: str, policy: str = "all", children: list[Node] | None = None):
        super().__init__(name)
        self.children = list(children or [])
        self.policy = policy

    def add_children(self, children: list[Node]):
        self.children.extend(children)

    async def _do_tick(self, bb: dict) -> Status:
        if not self.children:
            return Status.SUCCESS

        results = await asyncio.gather(
            *[child.tick(bb) for child in self.children],
            return_exceptions=True,
        )

        statuses = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"[{self.name}] Child '{self.children[i].name}' raised: {result}")
                statuses.append(Status.FAILURE)
            else:
                statuses.append(result)

        if self.policy == "all":
            return Status.SUCCESS if all(s == Status.SUCCESS for s in statuses) else Status.FAILURE
        else:  # "any"
            return Status.SUCCESS if any(s == Status.SUCCESS for s in statuses) else Status.FAILURE

    def reset(self):
        super().reset()


# ---------------------------------------------------------------------------
# Retry (decorator)
# ---------------------------------------------------------------------------

class Retry(Node):
    """Retry a child node on FAILURE with configurable attempts and delay.

    Wraps a single child. On FAILURE, resets the child and retries up to
    max_attempts times with an optional delay between attempts.
    RUNNING and SUCCESS are returned immediately.

    Banking use cases:
    - Resilient API calls to core banking systems
    - Transient failure handling for fraud detection services
    - Network-unreliable external integrations (KYC providers, watchlists)
    """

    def __init__(self, name: str, child: Node, max_attempts: int = 3, delay_seconds: float = 1.0):
        super().__init__(name)
        self.children = [child]
        self.max_attempts = max_attempts
        self.delay_seconds = delay_seconds

    @property
    def child(self) -> Node:
        return self.children[0]

    async def _do_tick(self, bb: dict) -> Status:
        for attempt in range(self.max_attempts):
            status = await self.child.tick(bb)
            if status != Status.FAILURE:
                return status
            if attempt < self.max_attempts - 1:
                logger.info(
                    f"[{self.name}] Attempt {attempt + 1}/{self.max_attempts} failed, "
                    f"retrying in {self.delay_seconds}s"
                )
                await asyncio.sleep(self.delay_seconds)
                self.child.reset()
        logger.warning(f"[{self.name}] All {self.max_attempts} attempts failed")
        return Status.FAILURE


# ---------------------------------------------------------------------------
# Inverter (decorator)
# ---------------------------------------------------------------------------

class Inverter(Node):
    """Flip SUCCESS <-> FAILURE. RUNNING passes through unchanged.

    Banking use cases:
    - "If NOT eligible" branching
    - Negating condition checks for fallback paths
    """

    def __init__(self, name: str, child: Node):
        super().__init__(name)
        self.children = [child]

    @property
    def child(self) -> Node:
        return self.children[0]

    async def _do_tick(self, bb: dict) -> Status:
        status = await self.child.tick(bb)
        if status == Status.SUCCESS:
            return Status.FAILURE
        elif status == Status.FAILURE:
            return Status.SUCCESS
        return Status.RUNNING


# ---------------------------------------------------------------------------
# BehaviourTree
# ---------------------------------------------------------------------------

class BehaviourTree:
    """Top-level wrapper around a root node."""

    def __init__(self, root: Node):
        self.root = root

    async def tick(self, bb: dict) -> Status:
        """Tick the tree. One call processes until RUNNING or completion."""
        return await self.root.tick(bb)

    def reset(self):
        self.root.reset()
