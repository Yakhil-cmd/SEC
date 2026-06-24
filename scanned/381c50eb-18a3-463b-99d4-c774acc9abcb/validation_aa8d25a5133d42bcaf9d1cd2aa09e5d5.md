### Title
Existing Cycles Balance in CMC Skews Rate-Limiter and `total_cycles_minted` Accounting — (File: rs/nns/cmc/src/main.rs)

### Summary
The `ensure_balance` function in the Cycles Minting Canister (CMC) reads `ic_cdk::api::canister_balance128()` — the canister's *total* live cycles balance — to decide how many new cycles to mint. Because `Cycles` subtraction is saturating, any cycles donated to the CMC by an unprivileged caller reduce `cycles_to_mint` to zero, silently bypassing the rate limiter and understating `total_cycles_minted`, while ICP is still burned.

### Finding Description

In `rs/nns/cmc/src/main.rs`, `ensure_balance` is called for every top-up, canister-creation, and cycles-ledger-deposit operation:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128()); // ← includes ALL cycles, donated or minted
    let cycles_to_mint = cycles - current_balance;                           // ← saturating_sub → 0 when balance ≥ cycles

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;   // ← charged 0 → rate limit not consumed
        state.total_cycles_minted += cycles_to_mint;                         // ← counter not incremented
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);                 // ← mints 0 new cycles
    Ok(())
}
```

`ic_cdk::api::canister_balance128()` returns the CMC's *entire* live balance, which includes cycles received via the management canister's `deposit_cycles` endpoint (callable by any canister on any subnet). When `current_balance ≥ cycles`, `cycles_to_mint` saturates to zero, so:

1. The rate limiter (`check_and_add_cycles`) is charged **zero** — the per-period cap is not consumed.
2. `state.total_cycles_minted` is incremented by **zero** — the public audit counter is understated.
3. No new cycles are minted via `ic0_mint_cycles128`.
4. The CMC still sends `cycles` worth of cycles to the target canister from its existing balance.
5. The ICP is still burned by `burn_and_log` after the call returns.

This is structurally identical to the Omnipool bug: `totalUnderlying_` (≡ `current_balance`) includes both the "allocated" portion (cycles minted from ICP) and the "unallocated" portion (donated cycles), inflating the base used to compute how much work remains, and causing the guard (rate limiter / allocation check) to be skipped. [1](#0-0) [2](#0-1) 

### Impact Explanation

**Rate-limiter bypass:** The rate limiter is the sole on-chain mechanism preventing a burst of ICP-to-cycles conversions. An attacker who pre-funds the CMC with donated cycles can allow an arbitrary amount of ICP to be burned in a single period without the limiter firing. Because cycles can be obtained from chain-fusion (ckETH/ckBTC bridging) or from any canister that has accumulated cycles, the attacker does not need to have previously been subject to the same limiter.

**`total_cycles_minted` understatement:** The public query `total_cycles_minted` is used by governance and monitoring tooling to audit the ICP-to-cycles conversion rate. When the CMC's balance is inflated by donations, this counter silently diverges from the true amount of cycles distributed, breaking the conservation invariant that `total_cycles_minted ≈ Σ(ICP burned × rate)`. [3](#0-2) [4](#0-3) 

### Likelihood Explanation

Any canister on any subnet can call `management_canister.deposit_cycles` targeting the CMC's canister ID, attaching cycles as payment. This is a standard, unprivileged inter-canister call. Cycles can be sourced from chain-fusion bridges (ckETH, ckBTC) without having previously been subject to the CMC rate limiter. The attack requires no privileged access, no governance majority, and no threshold-crypto compromise. The cost to the attacker is the donated cycles themselves, but the benefit (bypassing the rate limiter for a large ICP holder) can outweigh that cost. [5](#0-4) 

### Recommendation

Track the CMC's "minted-from-ICP" balance separately from its total live balance. Introduce a `cycles_held_from_minting: Cycles` field in `State` that is incremented by `cycles_to_mint` after each successful `ic0_mint_cycles128` call and decremented when cycles are sent out. Use this field — not `canister_balance128()` — to compute `cycles_to_mint`:

```rust
let cycles_to_mint = cycles.saturating_sub(state.cycles_held_from_minting);
```

This mirrors the Omnipool fix: pass `beforeTotalUnderlying` (only the allocated/tracked portion) instead of `beforeAllocatedBalance` (which includes the untracked contract balance).

### Proof of Concept

1. Attacker's canister calls `ic_cdk::api::call::call_with_payment128(management_canister, "deposit_cycles", (CMC_CANISTER_ID,), LARGE_CYCLES)` — donating `LARGE_CYCLES` to the CMC.
2. CMC's `canister_balance128()` is now `LARGE_CYCLES + prior_balance`.
3. Victim calls `notify_top_up` for `N` ICP (≡ `X` cycles, where `X ≤ LARGE_CYCLES`).
4. `ensure_balance(X)` runs: `cycles_to_mint = X - (LARGE_CYCLES + prior_balance) = 0` (saturating).
5. `check_and_add_cycles(state, now, 0)` — rate limiter not charged.
6. `state.total_cycles_minted += 0` — counter not updated.
7. CMC sends `X` cycles to victim's canister from its existing balance.
8. `burn_and_log` burns `N` ICP.

Repeat steps 3–8 for any number of users until `LARGE_CYCLES` is exhausted; the rate limiter is never triggered regardless of how much ICP is burned. [1](#0-0) [6](#0-5)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1985-2012)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

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

**File:** rs/nns/cmc/src/main.rs (L2303-2325)
```rust
/// Ensure the Cycles Minting canister has at least `cycles` balance of cycles, otherwise, mint more
/// so that the balance of this canister is at least `cycles`.  If the `check_minting_limit` is true,
/// the minting limit is checked and enforced before minting, otherwise, the minting limit is ignored.
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

**File:** rs/nns/cmc/src/main.rs (L2327-2330)
```rust
#[query(hidden = true)]
fn total_cycles_minted() -> Nat {
    with_state(|state| state.total_cycles_minted.get().into())
}
```
