I have enough code to make a definitive assessment. Let me trace the exact logic.

**Key code paths found:**

`IngressHistoryState::insert` (metadata_state.rs lines 1572-1618): [1](#0-0) 

`forget_terminal_statuses` (metadata_state.rs lines 1679-1742): [2](#0-1) 

`next_terminal_time` reset guard (lines 1588-1592): [3](#0-2) 

---

**Tracing the attack path precisely:**

1. Victim's message completes at time T1 → pruning_time = T1 + MAX_INGRESS_TTL. `next_terminal_time` is set to this value.
2. Attacker's messages complete at T2 > T1 → pruning_time = T2 + MAX_INGRESS_TTL (later). The reset guard at line 1590 (`if self.next_terminal_time > timeout`) is **false** because attacker's timeout is later, so `next_terminal_time` stays at T1 + MAX_INGRESS_TTL.
3. Attacker's large-payload insertions push `memory_usage > ingress_memory_capacity`, triggering `forget_terminal_statuses`.
4. The loop at line 1693 iterates `pruning_times` from `next_terminal_time` (= T1 + MAX_INGRESS_TTL) upward — **victim's entry is first**.
5. The memory check at line 1699 (`if self.memory_usage <= target_size { break; }`) fires **before** processing each bucket, but since memory is still over capacity, it does NOT break before processing the victim's bucket.
6. Victim's `Completed` status is transitioned to `Done` (lines 1717-1729), losing the reply payload.
7. The loop may then break after processing the victim's bucket if memory drops below target.

**There is no guard that protects a recently-completed entry from being evicted by another user's memory pressure.** The eviction order is purely chronological (oldest pruning time first), and there is no per-user isolation or minimum retention guarantee.

---

### Title
Attacker-Controlled Memory Pressure Causes Premature Eviction of Victim's `Completed` Ingress Status to `Done` — (`rs/replicated_state/src/metadata_state.rs`)

### Summary
An unprivileged attacker who can submit ingress messages to a canister returning large reply payloads can deliberately exceed `ingress_memory_capacity` in the subnet's ingress history, causing `forget_terminal_statuses` to evict the victim's `Completed` status (with reply payload) to `Done` before the victim polls `read_state`, permanently destroying the reply data.

### Finding Description
`IngressHistoryState::insert` (metadata_state.rs line 1604) triggers `forget_terminal_statuses` whenever `memory_usage > ingress_memory_capacity`. That function iterates `pruning_times` from `next_terminal_time` (the oldest unprocessed terminal entry) upward, transitioning `Completed`/`Failed` entries to `Done` until memory drops below the target. Because pruning times are assigned as `completion_time + MAX_INGRESS_TTL`, a victim whose message completed slightly earlier than the attacker's messages will have an earlier pruning time and will be evicted first. The `next_terminal_time` reset guard (line 1590) only resets to an earlier time if the new entry's timeout is earlier than the current `next_terminal_time` — attacker entries with later timeouts do not trigger this reset, so the victim's entry remains the scan start point.

There is no per-user memory quota, no minimum retention window for recently-completed entries, and no protection preventing one user's insertions from evicting another user's `Completed` status.

### Impact Explanation
The victim's `Completed` ingress status is irreversibly transitioned to `Done`. The reply payload (return value of the canister call) is permanently lost. The victim calling `read_state` after eviction receives `Done` with no data, with no way to recover the response. The canister's state change is committed, but the user cannot observe the result. This constitutes a targeted denial-of-response attack.

### Likelihood Explanation
The attacker needs: (a) a canister (their own or any canister returning large responses) that returns large reply payloads, (b) enough cycles to submit the required number of ingress messages, and (c) timing such that their messages complete after the victim's. The number of messages required depends on `ingress_memory_capacity` divided by max reply size (~2 MB). If `ingress_memory_capacity` is in the low tens of MB, only tens of messages are needed. The attack is repeatable and can be automated. No privileged access, governance majority, or subnet compromise is required.

### Recommendation
- Introduce per-user or per-message minimum retention guarantees: a `Completed` entry should not be evicted until it has been resident for at least a minimum duration (e.g., a few seconds or a configurable floor).
- Alternatively, account memory pressure per-originating-user so that one user's large payloads cannot evict another user's entries.
- Consider evicting the largest entries first (within the same pruning bucket) rather than strictly oldest-first, reducing the ability to target specific victims.
- Enforce per-user caps on total ingress history memory to prevent a single attacker from consuming the shared budget.

### Proof of Concept
State-machine test outline:
1. Insert victim's `Completed` status with a small payload at time T.
2. Insert N attacker `Completed` statuses with large payloads (each near max reply size) at time T + 1ns, where N × payload_size > `ingress_memory_capacity`.
3. Assert that `ingress_history.get(victim_message_id)` returns `Done` (evicted) rather than `Completed`.
4. Assert that at least some attacker entries remain `Completed` (they were inserted later and thus have later pruning times, so they are processed after the victim's entry).

This directly demonstrates that the victim's reply is lost due to attacker-controlled memory pressure, with no privileged access required.

### Citations

**File:** rs/replicated_state/src/metadata_state.rs (L1583-1610)
```rust
        if let IngressStatus::Known { state, .. } = &status
            && state.is_terminal()
        {
            let timeout = time + MAX_INGRESS_TTL;

            // Reset `self.next_terminal_time` in case it is after the current timeout
            // and the entry is completed or failed.
            if self.next_terminal_time > timeout && state.is_terminal_with_payload() {
                self.next_terminal_time = timeout;
            }
            Arc::make_mut(&mut self.pruning_times)
                .entry(timeout)
                .or_default()
                .insert(message_id.clone());
        }
        self.memory_usage += status.payload_bytes();
        let old_status = Arc::make_mut(&mut self.statuses).insert(message_id, Arc::new(status));
        if let Some(old) = &old_status {
            self.memory_usage -= old.payload_bytes();
        }

        if self.memory_usage > ingress_memory_capacity.get() as usize {
            self.forget_terminal_statuses(
                ingress_memory_capacity,
                time,
                observe_time_in_terminal_state,
            );
        }
```

**File:** rs/replicated_state/src/metadata_state.rs (L1693-1733)
```rust
        for (time, ids) in self
            .pruning_times
            .range((Included(self.next_terminal_time), Unbounded))
        {
            self.next_terminal_time = *time;

            if self.memory_usage <= target_size {
                break;
            }

            // We keep track of entries by how much they are evicted before their "pruning_time".
            let time_until_pruning = time.saturating_duration_since(now);
            let time_in_ingress_history_secs =
                MAX_INGRESS_TTL.saturating_sub(time_until_pruning).as_secs();

            for id in ids.iter() {
                observe_time_in_terminal_state(time_in_ingress_history_secs);
                match statuses.get(id).map(Arc::as_ref) {
                    Some(IngressStatus::Known {
                        receiver,
                        user_id,
                        time,
                        state,
                    }) if state.is_terminal_with_payload() => {
                        let done_status = Arc::new(IngressStatus::Known {
                            receiver: *receiver,
                            user_id: *user_id,
                            time: *time,
                            state: IngressState::Done,
                        });
                        self.memory_usage += done_status.payload_bytes();

                        // We can safely unwrap here because we know there must be an
                        // ingress status with the given `id` in `statuses` in this
                        // branch.
                        let old_status = statuses.insert(id.clone(), done_status).unwrap();
                        self.memory_usage -= old_status.payload_bytes();
                    }
                    _ => continue,
                }
            }
```
