# Connected-component traversal

Topology membership is built in expected O(V + E) time, with O(V + E) storage.
The sorted agent ID catalogue chooses component roots in canonical order.
Each vertex is scheduled once; bulk set intersections discover unvisited peers.
Traversal order cannot affect frozenset memberships or component root order.

This removes repeated min(remaining) scans, which cost O(V^2) for isolated
vertices, and redundant scheduling and neighbor sorting on dense graphs.
The complete graph update still evaluates every pair in O(V^2); this change
optimizes the component-building stage, not radio-pair enumeration.

Tests exhaust all 1,024 graphs on five nonconsecutive IDs and cover isolated
and dense graphs. Existing scenario timing and protocol metrics remain covered
by the complete regression suite. Synthetic method-only measurements use five
runs and exclude graph construction; they are not flight benchmarks.
