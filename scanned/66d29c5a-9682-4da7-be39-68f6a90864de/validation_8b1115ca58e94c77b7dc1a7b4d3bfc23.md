### Title
Integer Division Truncation Causes Incorrect Merge Candidate Ordering - (`rs/state_manager/src/tip.rs`)

---

### Summary

The `merge` function in `rs/state_manager/src/tip.rs` sorts PageMap merge candidates by their efficiency ratio `(bytes_saved / write_size)` using integer division with a fixed-point multiplier of 1000. Due to truncation, two candidates with strictly different actual ratios can receive the same sort key, or — worse — a less efficient candidate can receive a more negative (higher-priority) key than a more efficient one. This causes the replica to select suboptimal merges when reducing storage overhead, potentially writing more data to disk than necessary or failing to reduce overhead below the 2.5× threshold within the available budget.

---

### Finding Description

Inside `merge()`, after the file-count-driven merges are scheduled, the remaining candidates are re-sorted to greedily reduce storage overhead:

```rust
// Sort by ratio of saved bytes to write size.
merge_candidates.sort_by_key(|m| {
    if m.write_size_bytes() != 0 {
        // Fixed point to compute overhead ratio for sort.
        -1000_i64 * (m.storage_size_bytes_before() as i64 - m.storage_size_bytes_after() as i64)
            / m.write_size_bytes() as i64
    } else {
        0
    }
});
``` [1](#0-0) 

The key is computed as `floor(-1000 × saved / write)`. Rust integer division truncates toward zero, so the fractional part of the true ratio is discarded. This produces the same vulnerability class as M-03: two candidates whose true ratios are distinct can collapse to the same integer key, and — critically — a candidate with a *lower* true ratio can receive a *more negative* key than a candidate with a *higher* true ratio, reversing their priority.

**Concrete reversal example (all values in bytes):**

| Candidate | `saved` | `write` | True ratio | Key (`-1000×saved/write`) |
|-----------|---------|---------|------------|--------------------------|
| A | 1 000 | 1 001 | 0.999 001… | `−1 000 000 / 1 001 = −998` |
| B | 999 | 1 000 | 0.999 000 | `−999 000 / 1 000 = −999` |

`sort_by_key` sorts ascending, so B (key −999) is placed before A (key −998). B is treated as higher priority. Yet A has the strictly better ratio. The same arithmetic scales linearly: replace every value with its GiB equivalent and the result is identical.

The correct comparison is cross-multiplication: `saved_A × write_B` vs `saved_B × write_A`, which is exactly what the `PrioritizedStash` ordering in the IDKG pre-signature builder already does correctly:

```rust
// Compare the fill level by cross-multiplying to avoid floating-point arithmetic
let self_level = self.count * other.max;
let other_level = other.count * self.max;
``` [2](#0-1) 

---

### Impact Explanation

The sorted list is consumed greedily until `storage_saved >= storage_to_save`:

```rust
for m in merge_candidates.into_iter() {
    if storage_saved >= storage_to_save {
        break;
    }
    storage_saved += m.storage_size_bytes_before() as i64 - m.storage_size_bytes_after() as i64;
    ...
    scheduled_merges.push(m);
}
``` [3](#0-2) 

When the ordering is wrong, a less efficient merge (lower `saved/write`) is executed before a more efficient one. Two concrete consequences:

1. **Excess disk I/O**: To save the same number of bytes, the replica writes more data than the optimal selection would require, consuming more of the `MERGE_SOFT_BUDGET_BYTES` (250 GiB) budget.
2. **Failure to meet the 2.5× overhead threshold**: If the budget is exhausted before `storage_saved >= storage_to_save` because suboptimal merges were chosen first, the replica's on-disk storage remains above the `max_storage = 2.5 × mem_size` ceiling, degrading storage health over successive checkpoints. [4](#0-3) 

Because the merge function is deterministic and runs identically on every node, there is no consensus divergence — all replicas make the same suboptimal choice. The impact is operational: increased write amplification and potential long-term storage overhead accumulation.

---

### Likelihood Explanation

The condition is triggered whenever two merge candidates have ratios that differ only in the sub-`1/1000` fractional part. Given that PageMap shard sizes are measured in bytes and can be arbitrary values, such near-equal ratios arise naturally during normal operation without any adversarial input. A canister that writes specific amounts of data to stable memory can also craft shard sizes that reliably trigger the reversal, though the impact remains operational rather than directly financial.

---

### Recommendation

Replace the integer-division key with a cross-multiplication comparator, mirroring the correct pattern already used in `PrioritizedStash`:

```rust
merge_candidates.sort_by(|a, b| {
    // Compare saved_a / write_a vs saved_b / write_b
    // by cross-multiplying to avoid truncation:
    // saved_a * write_b  vs  saved_b * write_a
    let saved_a = a.storage_size_bytes_before().saturating_sub(a.storage_size_bytes_after()) as u128;
    let saved_b = b.storage_size_bytes_before().saturating_sub(b.storage_size_bytes_after()) as u128;
    let write_a = a.write_size_bytes() as u128;
    let write_b = b.write_size_bytes() as u128;
    // Higher ratio = higher priority = sort first (descending)
    (saved_b * write_a).cmp(&(saved_a * write_b))
});
```

Using `u128` prevents overflow for realistic storage sizes (up to ~18 EiB per dimension before overflow). This is the exact mitigation recommended in M-03 (`quote1 * base2 >= quote2 * base1`) applied to the storage domain.

---

### Proof of Concept

Given two merge candidates with:
- **A**: `storage_before=2000`, `storage_after=1000`, `write=1001` → `saved=1000`, ratio≈0.999001
- **B**: `storage_before=1999`, `storage_after=1000`, `write=1000` → `saved=999`, ratio=0.999000

Current code:
- Key A: `−1000 × 1000 / 1001 = −1000000 / 1001 = −998` (Rust truncates toward zero)
- Key B: `−1000 × 999 / 1000 = −999000 / 1000 = −999`

`sort_by_key` ascending → B (−999) before A (−998). B is selected first despite being less efficient.

With cross-multiplication:
- `saved_A × write_B = 1000 × 1000 = 1_000_000`
- `saved_B × write_A = 999 × 1001 = 999_999`

`1_000_000 > 999_999` → A correctly sorts first. [5](#0-4)

### Citations

**File:** rs/state_manager/src/tip.rs (L942-943)
```rust
    // Max 2.5 overhead
    let max_storage = storage_info.mem_size * 2 + storage_info.mem_size / 2;
```

**File:** rs/state_manager/src/tip.rs (L970-979)
```rust
    // Sort by ratio of saved bytes to write size.
    merge_candidates.sort_by_key(|m| {
        if m.write_size_bytes() != 0 {
            // Fixed point to compute overhead ratio for sort.
            -1000_i64 * (m.storage_size_bytes_before() as i64 - m.storage_size_bytes_after() as i64)
                / m.write_size_bytes() as i64
        } else {
            0
        }
    });
```

**File:** rs/state_manager/src/tip.rs (L988-999)
```rust
    for m in merge_candidates.into_iter() {
        if storage_saved >= storage_to_save {
            break;
        }

        storage_saved += m.storage_size_bytes_before() as i64 - m.storage_size_bytes_after() as i64;
        merges_by_storage += 1;
        // Only full merges reduce overhead, and there should be enough of them to reach
        // `storage_to_save` before tapping into partial merges.
        debug_assert!(m.is_full_merge());
        scheduled_merges.push(m);
    }
```

**File:** rs/consensus/idkg/src/payload_builder/pre_signatures.rs (L294-299)
```rust
        // Compare the fill level by cross-multiplying to avoid floating-point arithmetic
        let self_level = self.count * other.max;
        let other_level = other.count * self.max;

        // Reverse the order to make the emptiest stash the greatest priority
        let res = other_level.cmp(&self_level);
```
