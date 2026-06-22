### Title
Integer Overflow in `get_blocks_pb` Bounds Check Bypasses Range Validation in ICP Archive and Ledger Canisters - (File: `rs/ledger_suite/icp/archive/src/main.rs`, `rs/ledger_suite/icp/ledger/src/main.rs`)

---

### Summary

The `get_blocks_pb` query endpoint in both the ICP archive canister and the ICP ledger canister constructs the requested block range as `start..start + length` without overflow protection. When `start` is near `u64::MAX`, the addition wraps to a small value, producing an inverted (empty) range. The subsequent `is_subrange` bounds check passes on this empty range, silently bypassing the intended out-of-bounds rejection. Depending on the archive node's `from_offset`, this either returns a misleading empty-success response or causes a panic/trap via a second wrapping subtraction, creating a DoS on the query endpoint.

---

### Finding Description

**Root cause — archive canister:** [1](#0-0) 

```rust
let length = length
    .min(usize::MAX as u64)
    .min(icp_ledger::max_blocks_per_request(...) as u64);
let local_blocks_range = from_offset..from_offset + blocks_len();
let requested_range = start..start + length;   // ← unchecked u64 addition
```

**Root cause — ledger canister:** [2](#0-1) 

```rust
let length = std::cmp::min(args.length, max_blocks_per_request(...) as u64);
let local_blocks_range = blockchain.num_archived_blocks..blockchain.chain_length();
let requested_range = args.start..args.start + length;  // ← unchecked u64 addition
```

**Bounds check that is bypassed:** [3](#0-2) 

```rust
pub fn is_subrange(l: &Range<u64>, r: &Range<u64>) -> bool {
    r.start <= l.start && l.end <= r.end
}
```

`is_subrange` does not account for the case where `l` is an inverted (empty) range produced by overflow. When `start + length` wraps to a value smaller than `start`, the resulting range `start..wrapped_end` satisfies both conditions trivially:
- `r.start <= l.start` → `from_offset <= u64::MAX - k` — always true for any realistic `from_offset`
- `l.end <= r.end` → `wrapped_end <= from_offset + blocks_len()` — true for any small wrapped value

**Second overflow in the execution path (archive only):** [4](#0-3) 

```rust
let offset_requested_range =
    requested_range.start - from_offset..requested_range.end - from_offset;
for index in offset_requested_range {
    blocks.push(get_block_stable(index).unwrap());
```

When `from_offset > 0`, `requested_range.end - from_offset` underflows (wraps), producing a non-empty range near `u64::MAX`. The loop then calls `get_block_stable` with indices that do not exist, causing `.unwrap()` to panic and the canister to trap.

---

### Impact Explanation

Two distinct impacts depending on the archive node's `from_offset`:

| Scenario | `from_offset` | Effect |
|---|---|---|
| First archive node | `0` | `offset_requested_range` is also inverted/empty; loop skips; returns `Ok([])` instead of an error — bounds check silently bypassed |
| Subsequent archive nodes | `> 0` | Second wrapping subtraction produces indices near `u64::MAX`; `get_block_stable(...).unwrap()` panics; canister traps — **DoS on the query endpoint** |

For the ledger canister, the same overflow on line 1045 causes `get_blocks` to return `Ok([])` for crafted inputs instead of the expected out-of-range error, silently misreporting block availability.

---

### Likelihood Explanation

The `get_blocks_pb` endpoint is a public `canister_query` callable by any unprivileged user or canister. No special role, key, or governance action is required. The attacker only needs to supply `start` near `u64::MAX` (e.g., `u64::MAX - max_blocks_per_request + 1`) and any non-zero `length`. The `length` is clamped to `max_blocks_per_request` (a small constant), so the overflow is achievable with a single crafted call. This is trivially constructable.

---

### Recommendation

Replace the unchecked additions with overflow-safe alternatives at both sites:

**Archive (`rs/ledger_suite/icp/archive/src/main.rs` line 337):**
```rust
let end = match start.checked_add(length) {
    Some(e) => e,
    None => {
        // return out-of-range error
    }
};
let requested_range = start..end;
```

**Ledger (`rs/ledger_suite/icp/ledger/src/main.rs` line 1045):**
```rust
let end = match args.start.checked_add(length) {
    Some(e) => e,
    None => {
        // return out-of-range error
    }
};
let requested_range = args.start..end;
```

Alternatively, use `range_utils::make_range` which already uses `saturating_add`: [5](#0-4) 

However, `saturating_add` silently truncates rather than errors; a `checked_add` with an explicit error return is preferable for a bounds-enforcing path.

---

### Proof of Concept

Call `get_blocks_pb` on any ICP archive node with:
```
start  = 18446744073709551610   // u64::MAX - 5
length = 6                      // any value ≤ max_blocks_per_request
```

**Expected behavior:** Out-of-range error response.

**Actual behavior (first archive node, `from_offset = 0`):**
- `start + length` wraps to `0`
- `requested_range = (u64::MAX - 5)..0` — empty, inverted
- `is_subrange` passes
- `offset_requested_range = (u64::MAX - 5)..0` — empty
- Returns `Ok([])` — no error, no blocks

**Actual behavior (second+ archive node, e.g., `from_offset = 1000`):**
- `start + length` wraps to `0`
- `is_subrange` passes
- `offset_requested_range = (u64::MAX - 1005)..(u64::MAX - 999)` — 6 elements near `u64::MAX`
- `get_block_stable(u64::MAX - 1005).unwrap()` → `None.unwrap()` → **panic → canister trap**
- Query returns a reject; archive query endpoint is DoS'd for the duration of the call

### Citations

**File:** rs/ledger_suite/icp/archive/src/main.rs (L333-337)
```rust
        let length = length
            .min(usize::MAX as u64)
            .min(icp_ledger::max_blocks_per_request(&PrincipalId::from(msg_caller())) as u64);
        let local_blocks_range = from_offset..from_offset + blocks_len();
        let requested_range = start..start + length;
```

**File:** rs/ledger_suite/icp/archive/src/main.rs (L351-354)
```rust
        let offset_requested_range =
            requested_range.start - from_offset..requested_range.end - from_offset;
        for index in offset_requested_range {
            blocks.push(get_block_stable(index).unwrap());
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1039-1045)
```rust
        let length = std::cmp::min(
            args.length,
            max_blocks_per_request(&PrincipalId::from(caller())) as u64,
        );
        let blockchain = &LEDGER.read().unwrap().blockchain;
        let local_blocks_range = blockchain.num_archived_blocks..blockchain.chain_length();
        let requested_range = args.start..args.start + length;
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/range_utils.rs (L6-11)
```rust
pub fn make_range(start: u64, len: usize) -> Range<u64> {
    Range {
        start,
        end: start.saturating_add(len as u64),
    }
}
```

**File:** rs/ledger_suite/common/ledger_canister_core/src/range_utils.rs (L38-40)
```rust
pub fn is_subrange(l: &Range<u64>, r: &Range<u64>) -> bool {
    r.start <= l.start && l.end <= r.end
}
```
