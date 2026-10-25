"""Connected components preserve canonical output for every small topology."""

from itertools import combinations

from eudis_swarm.communication import CommunicationGraph


def test_all_five_vertex_topologies_match_reachability():
    agent_ids = (2, 5, 11, 19, 23)
    graph = CommunicationGraph(reversed(agent_ids), communication_range=1.0)
    pairs = list(combinations(agent_ids, 2))
    for mask in range(1 << len(pairs)):
        edges: set[tuple[int, int]] = {
            edge for index, edge in enumerate(pairs) if mask & (1 << index)
        }
        neighbors, components = graph._build_topology(edges)
        expected_neighbors = {
            vertex: frozenset(
                right if left == vertex else left
                for left, right in edges
                if vertex in (left, right)
            )
            for vertex in agent_ids
        }
        assert neighbors == expected_neighbors
        expected = []
        seen = set()
        for root in agent_ids:
            if root in seen:
                continue
            reached = {root}
            while True:
                expanded = reached | {
                    peer for vertex in reached for peer in expected_neighbors[vertex]
                }
                if expanded == reached:
                    break
                reached = expanded
            expected.append(frozenset(reached))
            seen.update(reached)
        assert components == tuple(expected)


def test_isolated_and_dense_topologies_keep_canonical_membership():
    agent_ids = tuple(range(1, 201))
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    _, isolated = graph._build_topology(set())
    assert isolated == tuple(frozenset({vertex}) for vertex in agent_ids)
    _, dense = graph._build_topology(set(combinations(agent_ids, 2)))
    assert dense == (frozenset(agent_ids),)
