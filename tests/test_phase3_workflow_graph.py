from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from project_q.services.workflow_graph import (
    DependencyPolicy,
    FailurePolicy,
    NodeStatus,
    WorkflowGraph,
    WorkflowNode,
    WorkflowState,
)


class WorkflowGraphValidationTests(unittest.TestCase):
    def test_rejects_duplicate_node_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate node key"):
            WorkflowGraph(
                [
                    WorkflowNode("build"),
                    WorkflowNode("build"),
                ]
            )

    def test_rejects_unknown_dependencies(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown dependency"):
            WorkflowGraph([WorkflowNode("deploy", dependencies=("build",))])

    def test_rejects_cycles_with_the_cycle_path(self) -> None:
        with self.assertRaisesRegex(ValueError, r"cycle.*a.*c.*b.*a"):
            WorkflowGraph(
                [
                    WorkflowNode("a", dependencies=("c",)),
                    WorkflowNode("b", dependencies=("a",)),
                    WorkflowNode("c", dependencies=("b",)),
                ]
            )

    def test_enforces_node_and_parallelism_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 to 50 nodes"):
            WorkflowGraph([])
        with self.assertRaisesRegex(ValueError, "1 to 50 nodes"):
            WorkflowGraph([WorkflowNode(f"node-{index}") for index in range(51)])
        with self.assertRaisesRegex(ValueError, "parallelism must be between 1 and 8"):
            WorkflowGraph([WorkflowNode("only")], parallelism=0)
        with self.assertRaisesRegex(ValueError, "parallelism must be between 1 and 8"):
            WorkflowGraph([WorkflowNode("only")], parallelism=9)

    def test_topological_order_is_deterministic_by_key(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("join", dependencies=("beta", "alpha")),
                WorkflowNode("beta"),
                WorkflowNode("alpha"),
                WorkflowNode("finish", dependencies=("join",)),
            ]
        )

        self.assertEqual(graph.topological_order, ("alpha", "beta", "join", "finish"))
        self.assertEqual(tuple(node.key for node in graph), graph.topological_order)

    def test_nodes_normalize_serialized_values_and_round_trip_to_dicts(self) -> None:
        node = WorkflowNode.from_mapping(
            {
                "key": " publish ",
                "dependencies": [" build "],
                "dependency_policy": "all_done",
                "failure_policy": "continue",
            }
        )

        self.assertEqual(node.key, "publish")
        self.assertEqual(node.dependencies, ("build",))
        self.assertIs(node.dependency_policy, DependencyPolicy.ALL_DONE)
        self.assertIs(node.failure_policy, FailurePolicy.CONTINUE)
        self.assertEqual(
            node.to_dict(),
            {
                "key": "publish",
                "dependencies": ["build"],
                "dependency_policy": "all_done",
                "failure_policy": "continue",
            },
        )
        with self.assertRaises(FrozenInstanceError):
            node.key = "changed"  # type: ignore[misc]

    def test_graph_accepts_serialized_nodes_without_mutating_them(self) -> None:
        source = [
            {"key": "build"},
            {"key": "test", "dependencies": ["build"]},
        ]

        graph = WorkflowGraph(source, parallelism=2)

        self.assertEqual(graph.topological_order, ("build", "test"))
        self.assertEqual(source[1]["dependencies"], ["build"])

    def test_graph_round_trips_a_canonical_immutable_definition(self) -> None:
        graph = WorkflowGraph.from_mapping(
            {
                "parallelism": 2,
                "nodes": [
                    {"key": "test", "dependencies": ["build"]},
                    {"key": "build"},
                ],
            }
        )

        self.assertEqual(
            graph.to_dict(),
            {
                "parallelism": 2,
                "nodes": [
                    {
                        "key": "build",
                        "dependencies": [],
                        "dependency_policy": "all_success",
                        "failure_policy": "fail",
                    },
                    {
                        "key": "test",
                        "dependencies": ["build"],
                        "dependency_policy": "all_success",
                        "failure_policy": "fail",
                    },
                ],
            },
        )
        with self.assertRaises(AttributeError):
            graph.parallelism = 4  # type: ignore[misc]


class WorkflowReadinessTests(unittest.TestCase):
    def test_fan_out_respects_parallelism_and_fan_in_waits_for_every_dependency(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("root"),
                WorkflowNode("alpha", dependencies=("root",)),
                WorkflowNode("beta", dependencies=("root",)),
                WorkflowNode("gamma", dependencies=("root",)),
                WorkflowNode("join", dependencies=("alpha", "beta", "gamma")),
            ],
            parallelism=2,
        )
        state = WorkflowState.initial(graph)

        self.assertEqual(state.ready_keys, ("root",))
        state = state.start("root").succeed("root")
        self.assertEqual(state.ready_keys, ("alpha", "beta"))

        state = state.start("alpha")
        self.assertEqual(state.ready_keys, ("beta",))
        state = state.start("beta")
        self.assertEqual(state.ready_keys, ())
        state = state.succeed("alpha")
        self.assertEqual(state.ready_keys, ("gamma",))
        state = state.start("gamma").succeed("gamma")
        self.assertEqual(state.ready_keys, ())
        state = state.succeed("beta")
        self.assertEqual(state.ready_keys, ("join",))

    def test_all_success_skips_a_blocked_branch_but_all_done_becomes_ready(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("prepare", failure_policy=FailurePolicy.SKIP),
                WorkflowNode("strict", dependencies=("prepare",)),
                WorkflowNode(
                    "cleanup",
                    dependencies=("prepare",),
                    dependency_policy=DependencyPolicy.ALL_DONE,
                ),
            ],
            parallelism=3,
        )

        state = WorkflowState.initial(graph).start("prepare").fail("prepare")

        self.assertEqual(state.status("prepare"), NodeStatus.SKIPPED)
        self.assertEqual(state.status("strict"), NodeStatus.SKIPPED)
        self.assertEqual(state.ready_keys, ("cleanup",))

    def test_continue_failure_satisfies_all_success_dependencies(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("optional", failure_policy=FailurePolicy.CONTINUE),
                WorkflowNode("publish", dependencies=("optional",)),
            ]
        )

        state = WorkflowState.initial(graph).start("optional").fail("optional")

        self.assertEqual(state.status("optional"), NodeStatus.FAILED)
        self.assertFalse(state.halted)
        self.assertEqual(state.ready_keys, ("publish",))

    def test_fail_policy_halts_new_work_and_skips_pending_nodes(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("first"),
                WorkflowNode("independent"),
                WorkflowNode("downstream", dependencies=("first",)),
            ],
            parallelism=2,
        )
        state = WorkflowState.initial(graph).start("first").start("independent")

        state = state.fail("first")

        self.assertTrue(state.halted)
        self.assertEqual(state.status("first"), NodeStatus.FAILED)
        self.assertEqual(state.status("downstream"), NodeStatus.SKIPPED)
        self.assertEqual(state.status("independent"), NodeStatus.RUNNING)
        self.assertEqual(state.ready_keys, ())

    def test_all_done_waits_until_dependencies_are_terminal(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("alpha", failure_policy=FailurePolicy.CONTINUE),
                WorkflowNode("beta"),
                WorkflowNode(
                    "join",
                    dependencies=("alpha", "beta"),
                    dependency_policy=DependencyPolicy.ALL_DONE,
                ),
            ],
            parallelism=2,
        )
        state = WorkflowState.initial(graph).start("alpha").start("beta")

        state = state.fail("alpha")
        self.assertEqual(state.ready_keys, ())
        state = state.succeed("beta")
        self.assertEqual(state.ready_keys, ("join",))

    def test_blocked_all_success_nodes_are_skipped_recursively(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("source", failure_policy=FailurePolicy.SKIP),
                WorkflowNode("middle", dependencies=("source",)),
                WorkflowNode("leaf", dependencies=("middle",)),
                WorkflowNode(
                    "cleanup",
                    dependencies=("leaf",),
                    dependency_policy=DependencyPolicy.ALL_DONE,
                ),
            ]
        )

        state = WorkflowState.initial(graph).start("source").fail("source")

        self.assertEqual(state.status("middle"), NodeStatus.SKIPPED)
        self.assertEqual(state.status("leaf"), NodeStatus.SKIPPED)
        self.assertEqual(state.ready_keys, ("cleanup",))

    def test_direct_readiness_queries_accept_serialized_statuses_and_limits(self) -> None:
        graph = WorkflowGraph(
            [WorkflowNode("charlie"), WorkflowNode("alpha"), WorkflowNode("beta")],
            parallelism=3,
        )

        self.assertEqual(
            graph.ready_keys({"alpha": "running"}, limit=1),
            ("beta",),
        )
        self.assertEqual(
            tuple(node.key for node in graph.ready_nodes({"alpha": "succeeded"})),
            ("beta", "charlie"),
        )

    def test_state_snapshots_are_immutable_and_reject_illegal_transitions(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("first"),
                WorkflowNode("second", dependencies=("first",)),
            ]
        )
        state = WorkflowState.initial(graph)

        with self.assertRaises(TypeError):
            state.statuses["first"] = NodeStatus.SUCCEEDED  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "not ready"):
            state.start("second")
        with self.assertRaisesRegex(ValueError, "not running"):
            state.succeed("first")

        running = state.start("first")
        completed = running.succeed("first").start("second").succeed("second")
        self.assertEqual(state.status("first"), NodeStatus.PENDING)
        self.assertEqual(running.status("first"), NodeStatus.RUNNING)
        self.assertTrue(completed.is_complete)

    def test_restored_fail_fast_state_cannot_schedule_pending_work(self) -> None:
        graph = WorkflowGraph(
            [
                WorkflowNode("failed"),
                WorkflowNode("pending"),
            ],
            parallelism=2,
        )

        state = WorkflowState.restore(graph, {"failed": "failed"})

        self.assertTrue(state.halted)
        self.assertEqual(state.status("pending"), NodeStatus.SKIPPED)
        self.assertEqual(graph.ready_keys({"failed": "failed"}), ())


if __name__ == "__main__":
    unittest.main()
