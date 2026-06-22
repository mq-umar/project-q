"""Pure workflow DAG validation and scheduling state transitions."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
import heapq
from types import MappingProxyType
from typing import Any


class DependencyPolicy(StrEnum):
    """Controls which terminal dependency outcomes permit a node to run."""

    ALL_SUCCESS = "all_success"
    ALL_DONE = "all_done"


class FailurePolicy(StrEnum):
    """Controls how a node failure affects the remaining workflow."""

    FAIL = "fail"
    SKIP = "skip"
    CONTINUE = "continue"


class NodeStatus(StrEnum):
    """Execution status used by immutable workflow state snapshots."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class WorkflowNode:
    """An immutable node definition within a workflow DAG."""

    key: str
    dependencies: tuple[str, ...] = ()
    dependency_policy: DependencyPolicy = DependencyPolicy.ALL_SUCCESS
    failure_policy: FailurePolicy = FailurePolicy.FAIL

    def __post_init__(self) -> None:
        key = self.key.strip() if isinstance(self.key, str) else ""
        if not key:
            raise ValueError("node key must be a non-empty string")
        if isinstance(self.dependencies, str):
            raise ValueError(f"node {key!r} dependencies must be an iterable of strings")
        dependencies = tuple(
            dependency.strip() if isinstance(dependency, str) else dependency
            for dependency in self.dependencies
        )
        if any(not isinstance(dependency, str) or not dependency for dependency in dependencies):
            raise ValueError(f"node {key!r} dependencies must be non-empty strings")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"node {key!r} contains duplicate dependencies")
        try:
            dependency_policy = DependencyPolicy(self.dependency_policy)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid dependency policy for node {key!r}") from exc
        try:
            failure_policy = FailurePolicy(self.failure_policy)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid failure policy for node {key!r}") from exc

        object.__setattr__(self, "key", key)
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "dependency_policy", dependency_policy)
        object.__setattr__(self, "failure_policy", failure_policy)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WorkflowNode:
        if not isinstance(value, Mapping):
            raise TypeError("workflow node definition must be a mapping")
        if "key" not in value:
            raise ValueError("workflow node definition requires key")
        return cls(
            key=value["key"],
            dependencies=value.get("dependencies", ()),
            dependency_policy=value.get(
                "dependency_policy", DependencyPolicy.ALL_SUCCESS
            ),
            failure_policy=value.get("failure_policy", FailurePolicy.FAIL),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "dependencies": list(self.dependencies),
            "dependency_policy": self.dependency_policy.value,
            "failure_policy": self.failure_policy.value,
        }


class WorkflowGraph:
    """A validated, immutable DAG with deterministic readiness calculations."""

    __slots__ = ("_nodes", "_parallelism", "_topological_order")

    def __init__(
        self,
        nodes: Iterable[WorkflowNode | Mapping[str, Any]],
        *,
        parallelism: int = 1,
    ) -> None:
        node_list = tuple(
            node if isinstance(node, WorkflowNode) else WorkflowNode.from_mapping(node)
            for node in nodes
        )
        if not 1 <= len(node_list) <= 50:
            raise ValueError("workflow graph must contain 1 to 50 nodes")
        if (
            isinstance(parallelism, bool)
            or not isinstance(parallelism, int)
            or not 1 <= parallelism <= 8
        ):
            raise ValueError("parallelism must be between 1 and 8")

        node_by_key: dict[str, WorkflowNode] = {}
        for node in node_list:
            if node.key in node_by_key:
                raise ValueError(f"duplicate node key: {node.key!r}")
            node_by_key[node.key] = node

        for node in node_list:
            for dependency in node.dependencies:
                if dependency not in node_by_key:
                    raise ValueError(
                        f"node {node.key!r} references unknown dependency {dependency!r}"
                    )

        cycle = self._find_cycle(node_by_key)
        if cycle:
            raise ValueError(f"workflow graph contains a cycle: {' -> '.join(cycle)}")

        self._nodes = MappingProxyType(node_by_key)
        self._parallelism = parallelism
        self._topological_order = self._topological_sort(node_by_key)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WorkflowGraph:
        if not isinstance(value, Mapping):
            raise TypeError("workflow graph definition must be a mapping")
        if "nodes" not in value:
            raise ValueError("workflow graph definition requires nodes")
        nodes = value["nodes"]
        if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Iterable):
            raise ValueError("workflow graph nodes must be an iterable")
        return cls(nodes, parallelism=value.get("parallelism", 1))

    def __len__(self) -> int:
        return len(self._nodes)

    def __iter__(self) -> Iterator[WorkflowNode]:
        return (self._nodes[key] for key in self.topological_order)

    def __contains__(self, key: object) -> bool:
        return key in self._nodes

    def __getitem__(self, key: str) -> WorkflowNode:
        return self._nodes[key]

    @property
    def parallelism(self) -> int:
        return self._parallelism

    @property
    def topological_order(self) -> tuple[str, ...]:
        return self._topological_order

    @property
    def nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "parallelism": self.parallelism,
            "nodes": [node.to_dict() for node in self],
        }

    def ready_keys(
        self,
        statuses: Mapping[str, NodeStatus | str] | None = None,
        *,
        halted: bool = False,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        normalized = self._normalize_statuses(statuses or {})
        fail_fast = any(
            normalized[key] is NodeStatus.FAILED
            and node.failure_policy is FailurePolicy.FAIL
            for key, node in self._nodes.items()
        )
        if halted or fail_fast:
            return ()
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("ready node limit must be a non-negative integer")

        running = sum(status is NodeStatus.RUNNING for status in normalized.values())
        capacity = max(0, self.parallelism - running)
        if limit is not None:
            capacity = min(capacity, limit)
        if capacity == 0:
            return ()

        ready: list[str] = []
        for key in self.topological_order:
            if normalized[key] is not NodeStatus.PENDING:
                continue
            node = self._nodes[key]
            dependency_statuses = tuple(normalized[item] for item in node.dependencies)
            if node.dependency_policy is DependencyPolicy.ALL_DONE:
                satisfied = all(_is_terminal(status) for status in dependency_statuses)
            else:
                satisfied = all(
                    self._dependency_succeeded(dependency, normalized[dependency])
                    for dependency in node.dependencies
                )
            if satisfied:
                ready.append(key)
                if len(ready) == capacity:
                    break
        return tuple(ready)

    def ready_nodes(
        self,
        statuses: Mapping[str, NodeStatus | str] | None = None,
        *,
        halted: bool = False,
        limit: int | None = None,
    ) -> tuple[WorkflowNode, ...]:
        return tuple(
            self._nodes[key]
            for key in self.ready_keys(statuses, halted=halted, limit=limit)
        )

    def _normalize_statuses(
        self, statuses: Mapping[str, NodeStatus | str]
    ) -> dict[str, NodeStatus]:
        unknown = sorted(set(statuses) - set(self._nodes))
        if unknown:
            raise ValueError(f"statuses contain unknown node keys: {', '.join(unknown)}")
        normalized = {key: NodeStatus.PENDING for key in self._nodes}
        for key, status in statuses.items():
            try:
                normalized[key] = NodeStatus(status)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid status for node {key!r}: {status!r}") from exc
        return normalized

    def _dependency_succeeded(self, key: str, status: NodeStatus) -> bool:
        return status is NodeStatus.SUCCEEDED or (
            status is NodeStatus.FAILED
            and self._nodes[key].failure_policy is FailurePolicy.CONTINUE
        )

    @staticmethod
    def _find_cycle(nodes: Mapping[str, WorkflowNode]) -> tuple[str, ...]:
        visited: set[str] = set()
        active: set[str] = set()
        path: list[str] = []

        def visit(key: str) -> tuple[str, ...]:
            if key in active:
                start = path.index(key)
                return tuple(path[start:] + [key])
            if key in visited:
                return ()

            active.add(key)
            path.append(key)
            for dependency in sorted(nodes[key].dependencies):
                cycle = visit(dependency)
                if cycle:
                    return cycle
            path.pop()
            active.remove(key)
            visited.add(key)
            return ()

        for key in sorted(nodes):
            cycle = visit(key)
            if cycle:
                return cycle
        return ()

    @staticmethod
    def _topological_sort(nodes: Mapping[str, WorkflowNode]) -> tuple[str, ...]:
        dependents: dict[str, list[str]] = {key: [] for key in nodes}
        indegree = {key: len(node.dependencies) for key, node in nodes.items()}
        for node in nodes.values():
            for dependency in node.dependencies:
                dependents[dependency].append(node.key)

        available = [key for key, count in indegree.items() if count == 0]
        heapq.heapify(available)
        ordered: list[str] = []
        while available:
            key = heapq.heappop(available)
            ordered.append(key)
            for dependent in sorted(dependents[key]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(available, dependent)
        return tuple(ordered)


@dataclass(frozen=True, slots=True)
class WorkflowState:
    """An immutable execution snapshot for a validated workflow graph."""

    graph: WorkflowGraph
    statuses: Mapping[str, NodeStatus | str]
    halted: bool = False

    def __post_init__(self) -> None:
        normalized = self.graph._normalize_statuses(self.statuses)
        fail_fast = any(
            normalized[node.key] is NodeStatus.FAILED
            and node.failure_policy is FailurePolicy.FAIL
            for node in self.graph
        )
        halted = self.halted or fail_fast
        if halted:
            for key, status in normalized.items():
                if status is NodeStatus.PENDING:
                    normalized[key] = NodeStatus.SKIPPED
        object.__setattr__(self, "statuses", MappingProxyType(normalized))
        object.__setattr__(self, "halted", halted)

    @classmethod
    def initial(cls, graph: WorkflowGraph) -> WorkflowState:
        return cls(graph=graph, statuses={})

    @classmethod
    def restore(
        cls,
        graph: WorkflowGraph,
        statuses: Mapping[str, NodeStatus | str],
        *,
        halted: bool = False,
    ) -> WorkflowState:
        return cls(graph=graph, statuses=statuses, halted=halted)

    @property
    def ready_keys(self) -> tuple[str, ...]:
        return self.graph.ready_keys(self.statuses, halted=self.halted)

    @property
    def ready_nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(self.graph[key] for key in self.ready_keys)

    @property
    def is_complete(self) -> bool:
        return all(_is_terminal(status) for status in self.statuses.values())

    @property
    def has_failures(self) -> bool:
        return any(status is NodeStatus.FAILED for status in self.statuses.values())

    def status(self, key: str) -> NodeStatus:
        self._require_key(key)
        return self.statuses[key]

    def start(self, key: str) -> WorkflowState:
        self._require_key(key)
        if key not in self.ready_keys:
            raise ValueError(f"node {key!r} is not ready to start")
        return self._replace(key, NodeStatus.RUNNING)

    def succeed(self, key: str) -> WorkflowState:
        self._require_running(key)
        statuses = dict(self.statuses)
        statuses[key] = NodeStatus.SUCCEEDED
        self._skip_blocked_nodes(statuses)
        return WorkflowState(self.graph, statuses, halted=self.halted)

    def fail(self, key: str) -> WorkflowState:
        """Apply the node's fail-fast, skip, or continue failure policy."""

        self._require_running(key)
        statuses = dict(self.statuses)
        policy = self.graph[key].failure_policy
        statuses[key] = (
            NodeStatus.SKIPPED if policy is FailurePolicy.SKIP else NodeStatus.FAILED
        )

        halted = self.halted or policy is FailurePolicy.FAIL
        if halted:
            for node_key, status in statuses.items():
                if status is NodeStatus.PENDING:
                    statuses[node_key] = NodeStatus.SKIPPED
        else:
            self._skip_blocked_nodes(statuses)
        return WorkflowState(self.graph, statuses, halted=halted)

    def skip(self, key: str) -> WorkflowState:
        self._require_key(key)
        if self.statuses[key] not in {NodeStatus.PENDING, NodeStatus.RUNNING}:
            raise ValueError(f"node {key!r} cannot be skipped from {self.statuses[key].value}")
        statuses = dict(self.statuses)
        statuses[key] = NodeStatus.SKIPPED
        self._skip_blocked_nodes(statuses)
        return WorkflowState(self.graph, statuses, halted=self.halted)

    def _replace(self, key: str, status: NodeStatus) -> WorkflowState:
        statuses = dict(self.statuses)
        statuses[key] = status
        return WorkflowState(self.graph, statuses, halted=self.halted)

    def _skip_blocked_nodes(self, statuses: dict[str, NodeStatus]) -> None:
        changed = True
        while changed:
            changed = False
            for key in self.graph.topological_order:
                if statuses[key] is not NodeStatus.PENDING:
                    continue
                node = self.graph[key]
                if (
                    not node.dependencies
                    or node.dependency_policy is DependencyPolicy.ALL_DONE
                    or not all(_is_terminal(statuses[item]) for item in node.dependencies)
                ):
                    continue
                if all(
                    self.graph._dependency_succeeded(dependency, statuses[dependency])
                    for dependency in node.dependencies
                ):
                    continue
                statuses[key] = NodeStatus.SKIPPED
                changed = True

    def _require_key(self, key: str) -> None:
        if key not in self.graph:
            raise KeyError(key)

    def _require_running(self, key: str) -> None:
        self._require_key(key)
        if self.statuses[key] is not NodeStatus.RUNNING:
            raise ValueError(f"node {key!r} is not running")


def _is_terminal(status: NodeStatus) -> bool:
    return status in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED}
