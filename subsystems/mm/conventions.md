## Memory Management Coding Conventions

- Use the `struct folio` API for new code where applicable. Avoid raw
  `struct page` manipulation unless working at the physical page level.
- Patch series, not single commits. Each patch must build and boot
  independently.
- Use `READ_ONCE` / `WRITE_ONCE` for shared state. Document any deviation
  from standard lock ordering.
- Lock ordering: zone->lock > lruvec->lru_lock > pgdat->lru_lock.
  Document any deviation.
- GFP flags: never allocate with GFP_KERNEL while holding a spinlock.
  Use GFP_NOWAIT or defer allocation.
- Reference counting: prefer folio_try_get / folio_put over get_page /
  put_page for new code.
- Error paths: use `goto cleanup_label` for unwinding, not nested ifs.
- Numerical: `unsigned long` for PFNs, `pgoff_t` for page indices,
  `nid_t` for node IDs. Never mix.
- Commit subject prefix: "mm:" or "mm/<subsystem>:" (e.g. "mm/slub:")
