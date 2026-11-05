# Complete bounded-path search with neighborhood tools

Treat querying and assembling paths as different work. Cache queried canonical IDs and all returned edges; reuse a node response when both search fronts reach it. A query returns one node's neighborhood, not one edge. Stop only after covering the necessary search region, not after finding a witness.

For a hop bound H, one sufficient strategy is to expand neighborhoods from the start to radius floor(H/2) and from the target to radius ceil(H/2). Query nodes at depth strictly less than each radius; boundary nodes need not be queried for that strategy. For H=4, query the two endpoints and their direct neighbors, reusing overlaps. Every path of length<=H has a split covered by these two endpoint regions. Keep depth and side separately even when caching a response across sides. Alternate shallow frontier work or prioritize the less branching side. This is a sufficient strategy, not a minimum-query guarantee.

get_code_subgraph retrieves edges among already-known IDs, not an exhaustive neighborhood of each ID; absence of other vertices is not a proof of completeness. You can use it when those additional edges are useful.

Assemble an undirected adjacency from observed edges, collapse duplicates and preserve canonical IDs. Enumerate paths with a per-path visited-node set and the hop bound. A global visited-node set would discard alternative paths. Stop extending an individual path when it reaches the target. Deduplicate entire ordered node sequences. You may write this logic with execute_python; no solver code is supplied for you.

Only claim complete after all neighborhoods needed by your chosen strategy returned fully and your enumeration of the observed graph is exhaustive. If any required response is clipped, failed or unqueried, distinguish known paths from proven completeness. Review node-table indices, path endpoints, repeated nodes, maximum hops and duplicate paths before your single submission.
