## MM-Specific Planning Guidance

When planning MM patches:
- Identify whether the change touches the buddy allocator, slab/slub,
  page cache, memcg, THP, hugetlb, or reclaim — different subsystems
  have different maintainers and review queues.
- Decompose refactors (e.g. folio migration) into per-file patches.
- Each patch in a series must boot independently.
- For tests, prefer KUnit suites matching the kunit_filter ("mm").
  Add LTP mm category tests for integration coverage.
- Reference prior art via commit hash and LKML link in the RFC body.
