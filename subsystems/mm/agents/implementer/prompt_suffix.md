## MM-Specific Implementation Notes

- For folio migrations: use `page_folio()` to convert `struct page *` to
  `struct folio *` first, then call folio APIs.
- For allocator changes: update both `__alloc_pages` and `alloc_pages`
  call sites consistently.
- For memcg: balance every `mem_cgroup_charge` with `mem_cgroup_uncharge`
  on all paths including error paths.
- For THP: handle `split_huge_page` failure gracefully.
- Use `pr_warn_ratelimited` in OOM paths — `printk` can deadlock.
