## MM-Specific Review Checklist

In addition to general kernel review, for MM patches verify:

- **folio vs page confusion**: New code using `struct page *` where
  `struct folio *` is the correct API. Most page-cache and allocator
  operations should use folio.
- **GFP flag correctness**: `GFP_KERNEL` in atomic context (spinlock
  held, RCU read lock, softirq) is a blocker. Use `GFP_NOWAIT` or defer.
- **Lock ordering**: zone->lock > lruvec->lru_lock > pgdat->lru_lock.
  Document any deviation.
- **Page reference balancing**: Every `get_page`/`folio_get` must have
  a matching `put_page`/`folio_put` on all paths including error paths.
- **NUMA assumptions**: Hardcoded node IDs (`nid = 0`) are bugs. Use
  `numa_node_id()` or the page's node.
- **THP split handling**: Code touching THP must handle
  `split_huge_page` failure gracefully.
- **memcg charge balance**: Every `mem_cgroup_charge` must pair with
  `mem_cgroup_uncharge` on all paths.
- **PFN arithmetic**: Use `pfn_t` / `unsigned long` consistently. Mixing
  `int` and `unsigned long` for PFNs is a bug.
- **printk in atomic**: `printk` can deadlock in some contexts. Use
  `printk_deferred` or `pr_warn_ratelimited` in OOM paths.

Subsystem-specific commit subject prefixes:
- `mm:` for general MM
- `mm/slub:`, `mm/page_alloc:`, `mm/hugetlb:`, `mm/memcg:` for
  subsystem-specific
