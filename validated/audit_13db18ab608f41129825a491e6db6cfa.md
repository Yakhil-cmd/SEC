I have all the code needed to trace this vulnerability precisely. Let me analyze the exact execution path.

## Execution Flow Trace

**`notify_top_up` → `process_top_up` → `deposit_cycles` → `ensure_balance`** [1](#0-0) [2](#0-1) [3](#0-2) 

The ordering inside `deposit_cycles` is:
1. `ensure_balance(cycles, limiter_to_use)?` — charges the rate limiter **and** calls `ic0_mint_cycles128` (cycles land in CMC's own balance)
2. `call_with_payment128(..., METHOD_DEPOSIT_CYCLES, ..., cycles)` — sends those cycles to the target canister via the management canister

If step 2 fails, `deposit_cycles` returns `Err`, `process_top_up` refunds the ICP, but **step 1 is never rolled back**.

Inside `ensure_balance`:

```rust
let cycles_to_mint = cycles - current_balance;

with_state_mut(|state| {
    limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;  // limiter charged HERE
    state.total_cycles_minted += cycles_to_mint;
    Ok::<_, String>(())
})?;

let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);  // cycles minted HERE
``` [4](#0-3) 

`check_and_add_cycles` in the limiter calls `self.add(now, cycles_to_mint)` which permanently increments `total_count` — there is no rollback path. [5](#0-4) 

---

### Title
Rate-Limit Consumed Before Cycle Delivery Confirmed, Enabling Global Minting DoS — (`rs/nns/cmc/src/main.rs`, `rs/nns/cmc/src/limiter.rs`)

### Summary
`ensure_balance` charges the global per-hour minting rate limiter and calls `ic0_mint_cycles128` before the management canister `deposit_cycles` call is made. If that call fails, the rate-limit increment is permanent while the ICP is refunded. An unprivileged attacker with ~1500 ICP can exhaust the 150P-cycles/hour limit in a single transaction, get their ICP back (minus a small fee), and block all legitimate cycle minting for up to one hour.

### Finding Description

The invariant violation is in `deposit_cycles` (line 2117):

```rust
if mint_cycles {
    ensure_balance(cycles, limiter_to_use)?;   // (A) limiter charged, cycles minted into CMC
}

let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
    candid::Principal::management_canister(),
    METHOD_DEPOSIT_CYCLES,
    ...
    u128::from(cycles),
).await;                                        // (B) may fail

res.map_err(...)?;                              // (C) returns Err if (B) failed
``` [6](#0-5) 

Step (A) is unconditionally committed to state before the `await` at step (B). On the IC, `await` is a yield point; state mutations before it are durable. If (B) returns an error (e.g., target canister does not exist, is stopped, or is frozen), (C) propagates the error upward. `process_top_up` then calls `refund_icp`, returning the ICP to the attacker. [7](#0-6) 

The `base_limiter.total_count` is never decremented. The `DEFAULT_CYCLES_LIMIT` is 150 × 10¹⁵ cycles per hour. [8](#0-7) 

**CMC balance accumulation note:** After a failed call, the cycles attached to `call_with_payment128` are returned to the CMC by the IC runtime, so the CMC's balance rises to `cycles`. On a second call with the same amount, `cycles_to_mint = cycles − current_balance = 0`, so the limiter is not charged again. However, this does not protect against the attack: a single call with 1500 ICP (≈150P cycles) exhausts the full hourly limit in one shot, assuming the CMC's balance starts below that threshold (which is the normal operating state, since the CMC mints on demand and immediately forwards cycles).

### Impact Explanation

- Global DoS on all cycle minting (`notify_top_up`, `notify_mint_cycles`, `notify_create_canister`) for up to one hour per attack cycle.
- All users attempting to top up canisters or create new canisters via ICP→cycles conversion are blocked.
- The `base_limiter` is shared across all callers. [9](#0-8) 

### Likelihood Explanation

- Attacker entry point is the public `notify_top_up` update call — no privilege required.
- The ICP transfer to a non-existent canister's subaccount is valid on the ledger.
- The management canister's `deposit_cycles` reliably fails for non-existent canister IDs.
- Net cost to attacker: only `TOP_UP_CANISTER_REFUND_FEE` per attack cycle (ICP is otherwise refunded).
- Attack is repeatable every hour.

### Recommendation

Move `check_and_add_cycles` (and `ic0_mint_cycles128`) to **after** the management canister call succeeds, or implement a compensating rollback: if `call_with_payment128` returns an error, subtract the previously added cycles from the limiter before returning. The simplest fix is to restructure `deposit_cycles` so that `ensure_balance` is only called on the success path, or to pass the minted cycles as an already-held balance rather than minting speculatively.

### Proof of Concept

State-machine test sketch:
1. Configure CMC with a mock management canister that always rejects `deposit_cycles`.
2. Fund attacker with 1500 ICP; attacker sends ICP to CMC subaccount of a non-existent canister.
3. Attacker calls `notify_top_up { block_index, canister_id: non_existent }`.
4. Assert: `notify_top_up` returns `Err(NotifyError::Refunded { ... })` and attacker ICP is restored.
5. Assert: `state.base_limiter.get_count()` equals 150P cycles — rate limit exhausted.
6. Assert: a subsequent legitimate `notify_top_up` from a different user returns `Err` with the "More than N cycles have been minted" message. [10](#0-9)

### Citations

**File:** rs/nns/cmc/src/main.rs (L83-83)
```rust
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;
```

**File:** rs/nns/cmc/src/main.rs (L239-239)
```rust
    pub base_limiter: limiter::Limiter,
```

**File:** rs/nns/cmc/src/main.rs (L1999-2011)
```rust
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L2110-2138)
```rust
async fn deposit_cycles(
    canister_id: CanisterId,
    cycles: Cycles,
    mint_cycles: bool,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    if mint_cycles {
        ensure_balance(cycles, limiter_to_use)?;
    }

    let res: CallResult<()> = ic_cdk::api::call::call_with_payment128(
        candid::Principal::management_canister(),
        METHOD_DEPOSIT_CYCLES,
        (CanisterIdRecord {
            canister_id: canister_id.get().0,
        },),
        u128::from(cycles),
    )
    .await;

    res.map_err(|(code, msg)| {
        format!(
            "Depositing cycles failed with code {}: {:?}",
            code as i32, msg
        )
    })?;

    Ok(())
}
```

**File:** rs/nns/cmc/src/main.rs (L2306-2325)
```rust
fn ensure_balance(
    cycles: Cycles,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<(), String> {
    let now = now_system_time();

    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    // unused because of check above
    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
```

**File:** rs/nns/cmc/src/limiter.rs (L34-56)
```rust
    pub fn check_and_add_cycles(
        &mut self,
        now: SystemTime,
        cycles_to_mint: Cycles,
        limit: Cycles,
    ) -> Result<(), String> {
        self.purge_old(now);
        let count = self.get_count();

        if count + cycles_to_mint > limit {
            LIMITER_REJECT_COUNT.with(|count| {
                count.set(count.get().saturating_add(1));
            });

            return Err(format!(
                "More than {} cycles have been minted in the last {} seconds, please try again later.",
                limit,
                self.get_max_age().as_secs(),
            ));
        }
        self.add(now, cycles_to_mint);
        Ok(())
    }
```
