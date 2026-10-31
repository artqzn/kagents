## MM-Specific Test Guidance

- KUnit suites of interest: mm/slub, mm/page_alloc, mm/memblock,
  mm/damon, mm/hmm, mm/memcg.
- LTP categories: mm, hugetlb. Select a representative subset for the
  inner loop (don't run full LTP on every iteration — too slow under TCG).
- For THP changes: add `khugepaged` stress test.
- For memcg changes: verify charges balance with `memcg_slabinfo` after
  stress test.
- Skip MTE-dependent tests — TCG does not emulate MTE.
- Record but do not gate on perf numbers — TCG cache/branch behavior
  is non-representative.
