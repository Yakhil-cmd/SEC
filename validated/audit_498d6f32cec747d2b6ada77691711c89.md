Audit Report

## Title
Ingress History Oldest-First Eviction Allows Unprivileged Attacker to Prematurely Transition Victim's `Completed` Status to `Done` — (`rs/replicated_state/src/metadata_state.rs`)

## Summary
`IngressHistoryState::forget_terminal_statuses` evicts `Completed`/`Failed` ingress statuses to `Done` (dropping reply payloads) in strict oldest-first order when `memory_usage` exceeds `ingress_memory_capacity` (4 GiB). An unprivileged attacker who controls a canister returning large reply payloads can flood the ingress history with `Completed` entries timestamped after a victim's entry, causing the victim's `Completed` status to be evicted before the victim polls `read_state`, permanently destroying the reply payload.

## Finding Description
In `rs/replicated_state/src/metadata_state.rs`, `IngressHistoryState::insert` (L1604–1610) calls `forget_terminal_statuses` whenever `memory_usage > ingress_memory_capacity`. The eviction loop (L1693–1733) iterates `pruning_times` from `next_terminal_time` upward. Crucially, the memory guard `if self.memory_usage <= target_size { break; }` (L1699) is evaluated **after** advancing `next_terminal_time` (L1697) but **before** evicting the current bucket. This means the oldest bucket is unconditionally evicted whenever memory is above target at the start of that iteration — there is no per-user fairness or isolation.

Pruning times are keyed by `time + MAX_INGRESS_TTL` (L1586), where `time` is the current batch time when the terminal status is inserted. If a victim's `Completed` entry was inserted at batch time T1 and the attacker's entries are inserted at T2 > T1, the victim's bucket (T1 + MAX_INGRESS_TTL) is older and is reached first. The loop evicts the victim's entry, memory drops below target, and the loop breaks — leaving the attacker's entries at T2 + MAX_INGRESS_TTL intact as `Completed`.

`payload_bytes()` (L108–117 of `rs/types/types/src/ingress.rs`) returns 0 for `Done`, so evicting to `Done` reduces `memory_usage` by the full reply payload size. The `Done` state carries no payload; the reply is permanently gone from replicated state.

The `INGRESS_HISTORY_MEMORY_CAPACITY` is 4 GiB (L68 of `rs/config/src/execution_environment.rs`). With a maximum reply size of ~2 MB, an attacker needs on the order of ~2,048 messages to fill the history from empty. On a subnet with existing ingress history load, the threshold is lower. The attack requires no privileged access — only a canister under the attacker's control and sufficient cycles.

## Impact Explanation
A legitimate user permanently loses their canister reply. The `Done` state carries no payload and there is no retry path — the data is gone from replicated state. The user submitted a valid ingress message, paid fees, the canister executed correctly, but the user never receives the result. This constitutes a targeted, permanent denial-of-response against specific users, matching the allowed impact: **Application/platform-level DoS with concrete user harm** (High, $2,000–$10,000), or at minimum Medium given the resource cost required.

## Likelihood Explanation
The attacker requires: (1) a canister under their control returning large reply payloads (trivially deployable on mainnet); (2) enough cycles to submit ~2,048 messages with ~2 MB replies to fill 4 GiB from empty — fewer if the subnet's ingress history is already partially loaded; (3) the victim's message to have completed at an earlier batch time than the attacker's flood, which is easy to arrange by submitting the flood after observing the victim's message enter `Completed` state. No consensus corruption, threshold attack, or privileged access is required. The attack is repeatable and can be sustained by continuously replenishing the ingress history.

## Recommendation
1. **Per-user/per-canister memory accounting**: Track ingress history memory per `user_id` or `receiver`. When eviction is needed, evict from the largest contributor first rather than globally oldest-first.
2. **Minimum retention window**: Guarantee that a `Completed` entry is never evicted to `Done` within a minimum window (e.g., a configurable fraction of `MAX_INGRESS_TTL`) after insertion, giving the originating user time to poll.
3. **Cap per-user ingress history size**: Reject or immediately evict new `Completed` entries from a user/canister that already holds a disproportionate share of ingress history memory, preventing a single party from monopolizing the budget.
4. **Evict inserting party first**: When a new insertion triggers eviction, prefer evicting entries from the same `user_id`/`receiver` before touching other users' entries.

## Proof of Concept
State-machine unit test outline (safe, local, deterministic):

```rust
use ic_replicated_state::metadata_state::IngressHistoryState;
use ic_types::{ingress::{IngressState, IngressStatus, WasmResult}, NumBytes, Time};

let capacity = NumBytes::from(10 * 2_000_000_u64); // fits ~10 x 2MB replies
let mut history = IngressHistoryState::new();

// 1. Insert victim's Completed status at batch time T1
let t1 = Time::from_nanos_since_unix_epoch(1_000_000_000);
history.insert(victim_msg_id(), completed_2mb(t1), t1, NumBytes::from(u64::MAX), |_| {});
assert!(matches!(history.get(&victim_msg_id()), Some(IngressStatus::Known { state: IngressState::Completed(_), .. })));

// 2. Flood with attacker's Completed statuses at T2 > T1 with 2MB payloads
let t2 = Time::from_nanos_since_unix_epoch(2_000_000_000);
for i in 0..11 {
    history.insert(attacker_msg_id(i), completed_2mb(t2), t2, capacity, |_| {});
}

// 3. Victim's entry is now Done — payload permanently lost
assert!(matches!(history.get(&victim_msg_id()), Some(IngressStatus::Known { state: IngressState::Done, .. })));
// Attacker's most recent entry remains Completed
assert!(matches!(history.get(&attacker_msg_id(10)), Some(IngressStatus::Known { state: IngressState::Completed(_), .. })));
```

The existing test `ingress_history_insert_beyond_limit_will_succeed` (L1362–1437 of `rs/replicated_state/src/metadata_state/tests.rs`) already confirms that the most recently inserted entry survives while older entries are transitioned to `Done`, directly validating the eviction ordering exploited by this attack.