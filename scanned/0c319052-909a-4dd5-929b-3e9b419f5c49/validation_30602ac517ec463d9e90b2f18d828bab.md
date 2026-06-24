### Title
Notification Map Flood Permanently Locks Victim ICP via `purge_old_notifications` Advancing `last_purged_notification` Past Victim's Block Index — (`rs/nns/cmc/src/main.rs`)

---

### Summary

An unprivileged attacker who can afford ~100+ ICP in ledger fees can permanently lock a victim's ICP in a CMC subaccount by flooding `blocks_notified` with 1,000,000 real ledger entries at block indices higher than the victim's, triggering `purge_old_notifications` to advance `last_purged_notification` past the victim's block index, causing every subsequent `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` call for that block to return `TransactionTooOld` with no recovery path.

---

### Finding Description

**Constants:** [1](#0-0) 

```
MAX_NOTIFY_HISTORY = 1_000_000
MAX_NOTIFY_PURGE   =   100_000
```

**Purge logic** removes the `MAX_NOTIFY_PURGE` smallest block indices from `blocks_notified` whenever the map exceeds `MAX_NOTIFY_HISTORY`, then monotonically advances `last_purged_notification` to the highest evicted index: [2](#0-1) 

**Every notify entry point** calls `purge_old_notifications` first, then hard-rejects any block index ≤ `last_purged_notification`: [3](#0-2) 

The same pattern is repeated identically in `notify_mint_cycles` and `notify_create_canister`. [4](#0-3) [5](#0-4) 

**Attack sequence:**

1. Victim sends ICP to a CMC subaccount at ledger block index **N** but has not yet called `notify_top_up`.
2. Attacker makes **1,000,001** real ICP transfers to CMC subaccounts at block indices **N+1 … N+1,000,001** (each transfer costs only the ledger fee; the attacker can immediately call notify on each to recover cycles, so net cost ≈ fees only, ~100 ICP).
3. Attacker calls `notify_top_up` for each of those blocks, inserting 1,000,001 entries into `blocks_notified`.
4. On the 1,000,001st call, `purge_old_notifications` fires: it removes the 100,000 smallest entries (blocks N+1 … N+100,000) and sets `last_purged_notification = N+100,000`.
5. Victim now calls `notify_top_up(block_index = N)`. The check `N ≤ N+100,000` is **true** → `TransactionTooOld` is returned.
6. Victim's ICP remains in the CMC subaccount permanently; there is no automatic refund path for this error.

Note that `fetch_transaction` (the async ledger call) executes **before** the purge check, so the victim's block is verified to exist on-chain before being rejected — the rejection is purely a map-state artifact. [6](#0-5) 

The state struct itself acknowledges the ambiguity but understates the impact: [7](#0-6) 

---

### Impact Explanation

The victim's ICP is permanently locked in the CMC subaccount. `TransactionTooOld` is a non-retriable terminal error (the block index will never become "new" again). There is no admin refund endpoint, no heartbeat-driven recovery for meaningful-memo transactions, and no way for the victim to re-submit the notification. The ICP is effectively burned from the victim's perspective.

---

### Likelihood Explanation

**Moderate.** The attack requires ~100 ICP in fees (1,000,001 transfers × 0.0001 ICP fee), which is economically non-trivial but feasible for a targeted attack against a victim holding significant ICP. The victim's block index is publicly visible on the ledger. The attacker only needs to submit transfers after the victim's transfer and before the victim calls notify. No privileged access, no key material, and no consensus corruption is required — only ledger write access, which any ICP holder has.

---

### Recommendation

1. **Decouple `last_purged_notification` from block indices in the map.** Instead of advancing `last_purged_notification` to the highest purged block index, advance it only to `min(purged_block_indices) - 1`, or use a time-based cutoff rather than a map-size-based one.
2. **Reject `TransactionTooOld` only for blocks older than a fixed time window** (e.g., 24 hours), not based on map eviction state.
3. **Add a refund path** for blocks rejected as `TransactionTooOld` so victims can recover their ICP.
4. **Rate-limit notify calls per principal** to make flooding economically impractical.

---

### Proof of Concept

```rust
// State-machine test sketch
let mut state = State::default();

// Fill blocks_notified with MAX_NOTIFY_HISTORY entries at indices 1..=1_000_000
// (simulating attacker's completed notifications at blocks > victim's block 0)
for i in 1u64..=1_000_000 {
    state.blocks_notified.insert(i, NotificationStatus::NotifiedTopUp(Ok(Cycles::zero())));
}

// Victim's block index is 0 (or any index < the attacker's lowest block)
let victim_block: BlockIndex = 0;

// Trigger purge by adding one more entry (simulating the 1_000_001st notify call)
state.blocks_notified.insert(1_000_001, NotificationStatus::Processing);
state.purge_old_notifications(MAX_NOTIFY_HISTORY);

// last_purged_notification is now 100_000 (the 100_000th smallest entry purged)
assert!(state.last_purged_notification >= victim_block);

// Victim's notify call would now hit:
// if block_index <= state.last_purged_notification → TransactionTooOld
assert!(victim_block <= state.last_purged_notification);
```

### Citations

**File:** rs/nns/cmc/src/main.rs (L69-72)
```rust
/// The maximum number of notification statuses to store.
const MAX_NOTIFY_HISTORY: usize = 1_000_000;
/// The maximum number of old notification statuses we purge in one go.
const MAX_NOTIFY_PURGE: usize = 100_000;
```

**File:** rs/nns/cmc/src/main.rs (L258-262)
```rust
    pub blocks_notified: BTreeMap<BlockIndex, NotificationStatus>,
    // The status of blocks not new than this is ambiguous. This is because we
    // must bound how much memory we use; in particular, blocks_notified must
    // not grow without bound.
    pub last_purged_notification: BlockIndex,
```

**File:** rs/nns/cmc/src/main.rs (L339-353)
```rust
    fn purge_old_notifications(&mut self, max_history: usize) {
        let mut last_purged = 0;
        let mut cnt = 0;
        // Remove elements from the beginning of self.blocks_notified until either
        // it is small enough, or MAX_NOTIFY_PURGE entries have been removed.
        while self.blocks_notified.len() > max_history && cnt < MAX_NOTIFY_PURGE {
            // pop_first is nightly only
            let block_height = *self.blocks_notified.iter().next().unwrap().0;
            self.blocks_notified.remove(&block_height);
            last_purged = block_height;
            cnt += 1;
        }
        // make sure this grows monotonically (a delayed callback might have added older status)
        self.last_purged_notification = last_purged.max(self.last_purged_notification);
    }
```

**File:** rs/nns/cmc/src/main.rs (L1157-1162)
```rust
    let (amount, from) = fetch_transaction(
        block_index,
        Subaccount::from(&canister_id),
        MEMO_TOP_UP_CANISTER,
    )
    .await?;
```

**File:** rs/nns/cmc/src/main.rs (L1172-1179)
```rust
    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }
```

**File:** rs/nns/cmc/src/main.rs (L1264-1271)
```rust
    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }
```

**File:** rs/nns/cmc/src/main.rs (L1373-1380)
```rust
    let maybe_early_result = with_state_mut(|state| {
        state.purge_old_notifications(MAX_NOTIFY_HISTORY);

        if block_index <= state.last_purged_notification {
            return Some(Err(NotifyError::TransactionTooOld(
                state.last_purged_notification + 1,
            )));
        }
```
