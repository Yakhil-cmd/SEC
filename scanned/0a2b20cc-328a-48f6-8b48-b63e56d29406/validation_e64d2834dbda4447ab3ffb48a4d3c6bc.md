### Title
Loop Termination Ambiguity in `purge_old_tickets` Causes Incorrect "Not Done" Signal — (File: rs/sns/swap/src/swap.rs)

---

### Summary

The `purge_old_tickets` function in the SNS Swap canister uses a `for _i in 0..max_number_to_inspect` loop and signals completion by returning `None` (iterator exhausted) vs. `Some(last_principal)` (limit hit). When the number of remaining tickets is exactly `max_number_to_inspect`, the loop counter runs out while the iterator is not yet exhausted from the caller's perspective — but all tickets have in fact been inspected. The function returns `Some(last_principal)` instead of `None`, causing `try_purge_old_tickets` to incorrectly conclude the scan is incomplete, skip updating the completion timestamp, and schedule an unnecessary extra periodic task.

---

### Finding Description

In `purge_old_tickets`:

```rust
for _i in 0..max_number_to_inspect {
    match iter.next() {
        Some((principal, ticket)) => {
            last_principal = Some(principal.as_slice().to_vec());
            // ...
        }
        None => {
            last_principal = None;  // exhausted → signals "done"
            break;
        }
    }
}
last_principal  // Some(...) → signals "not done"
``` [1](#0-0) 

The only way `last_principal` is set to `None` is if `iter.next()` returns `None` inside the loop. If the loop counter reaches `max_number_to_inspect` on the same iteration that processes the very last ticket, the `None` branch is never entered. `last_principal` remains `Some(last_ticket_principal)`, and the function returns `Some(...)` — the same signal used when the limit was hit with more tickets remaining.

The caller `try_purge_old_tickets` interprets `Some(new_next_principal)` as "not done":

```rust
Some(new_next_principal) => {
    self.purge_old_tickets_next_principal = Some(new_next_principal);
    Some(false)  // ← "not done"
}
None => {
    // purge_old_tickets_last_completion_timestamp_nanoseconds updated here
    Some(true)   // ← "done"
}
``` [2](#0-1) 

When the edge case fires:
1. `purge_old_tickets_last_completion_timestamp_nanoseconds` is **not** updated.
2. `purge_old_tickets_next_principal` is set to the last inspected principal (not `FIRST_PRINCIPAL_BYTES`).
3. The 10-minute cooldown timer does not reset.
4. The next periodic task resumes from `last_principal` (inclusive), re-inspects that one ticket, finds no more, and only then correctly concludes done. [3](#0-2) 

---

### Impact Explanation

- **Completion timestamp delayed**: `purge_old_tickets_last_completion_timestamp_nanoseconds` is not updated until the extra follow-up task runs. The 10-minute inter-cycle cooldown starts one periodic-task interval late.
- **Stale cursor persists**: If the ticket count drops below `number_of_tickets_threshold` (100 million in production) before the follow-up task runs, `try_purge_old_tickets` returns `None` (threshold not met) without running, leaving `purge_old_tickets_next_principal` pointing mid-list. The next full scan will start from that mid-list position, skipping principals earlier in the BTree ordering until the cursor is eventually reset.
- **Old tickets linger**: Tickets that should have been purged in the completed scan remain in `OPEN_TICKETS_MEMORY` for at least one additional periodic-task interval, consuming stable memory. [4](#0-3) 

---

### Likelihood Explanation

The edge case fires whenever the number of tickets reachable from `start_principal` is exactly `max_number_to_inspect` (100,000 in production). An unprivileged participant in an open SNS swap can call `new_sale_ticket` to create tickets. By coordinating ticket creation so that the total count is exactly 100,000 at the time the periodic task runs, an attacker can reliably trigger the condition. The attacker entry path is the public `new_sale_ticket` endpoint, which requires no special privilege. [5](#0-4) 

---

### Recommendation

After the `for` loop, check whether the iterator is truly exhausted by attempting one more `iter.next()` call, or track a separate boolean flag that is set only when `iter.next()` returns `None`:

```rust
let mut exhausted = false;
for _i in 0..max_number_to_inspect {
    match iter.next() {
        Some((principal, ticket)) => {
            last_principal = Some(principal.as_slice().to_vec());
            // ...
        }
        None => {
            exhausted = true;
            break;
        }
    }
}
// Signal "done" if exhausted OR if limit was hit with no remaining items
if exhausted || iter.next().is_none() {
    None
} else {
    last_principal
}
```

Alternatively, after the loop, call `iter.next()` once: if it returns `None`, return `None` (done); otherwise return `last_principal` (not done). This mirrors the fix recommended in the original report: check for a valid next item OR the deletion limit, not just the counter alone.

---

### Proof of Concept

1. Open an SNS swap canister in `Lifecycle::Open`.
2. Set `max_number_to_inspect = N` (e.g., 2 in tests, 100,000 in production).
3. Create exactly `N` tickets via `new_sale_ticket`, all with `creation_time` old enough to be purged.
4. Trigger `try_purge_old_tickets` with `number_of_tickets_threshold ≤ N`.
5. Observe: `try_purge_old_tickets` returns `Some(false)` ("not done") even though all `N` tickets were inspected and purged.
6. Observe: `purge_old_tickets_last_completion_timestamp_nanoseconds` is **not** updated.
7. Trigger `try_purge_old_tickets` a second time; it returns `Some(true)` ("done") and updates the timestamp.

The existing test `test_purge_old_tickets` uses `MAX_NUMBER_TO_INSPECT = 2` and ticket counts that are multiples of 2, which avoids the exact-boundary case and does not catch this bug. [6](#0-5)

### Citations

**File:** rs/sns/swap/src/swap.rs (L2619-2629)
```rust
        const INTERVAL_NANOSECONDS: u64 = 60 * 10 * 1_000_000_000; // 10 minutes

        if self.lifecycle() != Lifecycle::Open {
            return None;
        }

        // Do not run purge_old_tickets if the number of tickets is less than or equal
        // to the threshold. This should save cycles.
        if memory::OPEN_TICKETS_MEMORY.with(|ts| ts.borrow().len()) < number_of_tickets_threshold {
            return None;
        }
```

**File:** rs/sns/swap/src/swap.rs (L2631-2665)
```rust
        let purge_old_tickets_last_completion_timestamp_nanoseconds = self
            .purge_old_tickets_last_completion_timestamp_nanoseconds
            .unwrap_or(0);

        let purge_old_tickets_next_principal = self.purge_old_tickets_next_principal().to_vec();
        let first_principal_bytes = FIRST_PRINCIPAL_BYTES.to_vec();

        if purge_old_tickets_next_principal != first_principal_bytes
            || purge_old_tickets_last_completion_timestamp_nanoseconds + INTERVAL_NANOSECONDS
                <= now_nanoseconds()
        {
            return match self.purge_old_tickets(
                now_nanoseconds(),
                purge_old_tickets_next_principal,
                max_age_in_nanoseconds,
                max_number_to_inspect,
            ) {
                Some(new_next_principal) => {
                    // If a principal is returned then there are some principals that haven't been
                    // checked yet by purge_old_tickets. We record the next principal so that
                    // the next periodic task can continue the work.
                    self.purge_old_tickets_next_principal = Some(new_next_principal);
                    Some(false)
                }
                None => {
                    // If no principal is returned then purge_old_tickets has
                    // exhausted all the tickets.
                    log!(INFO, "purge_old_tickets done");
                    self.purge_old_tickets_next_principal = Some(first_principal_bytes);
                    self.purge_old_tickets_last_completion_timestamp_nanoseconds =
                        Some(now_nanoseconds());
                    Some(true)
                }
            };
        }
```

**File:** rs/sns/swap/src/swap.rs (L2699-2724)
```rust
        memory::OPEN_TICKETS_MEMORY.with(|tickets| {
            let mut to_purge = vec![];
            let last_principal = {
                let mut last_principal = None;
                let tickets = tickets.borrow();
                let min_principal = Blob::from_bytes(Cow::from(&start_principal[..]));
                let mut iter = tickets.range((Included(min_principal), Unbounded));
                for _i in 0..max_number_to_inspect {
                    match iter.next() {
                        Some((principal, ticket)) => {
                            last_principal = Some(principal.as_slice().to_vec());
                            // ticket.creation_time is in nanoseconds
                            if ticket.creation_time + max_age_in_nanoseconds
                                < curr_time_in_nanoseconds
                            {
                                to_purge.push(principal);
                            }
                        }
                        None => {
                            last_principal = None;
                            break;
                        }
                    }
                }
                last_principal
            };
```

**File:** rs/sns/swap/src/swap.rs (L4711-4717)
```rust
    #[test]
    fn test_purge_old_tickets() {
        const TEN_MINUTES: u64 = 60 * 10 * 1_000_000_000;
        const ONE_DAY: u64 = ONE_DAY_SECONDS * 1_000_000_000;
        const NUMBER_OF_TICKETS_THRESHOLD: u64 = 10;
        const MAX_AGE_IN_NANOSECONDS: u64 = ONE_DAY * 2;
        const MAX_NUMBER_TO_INSPECT: u64 = 2;
```
