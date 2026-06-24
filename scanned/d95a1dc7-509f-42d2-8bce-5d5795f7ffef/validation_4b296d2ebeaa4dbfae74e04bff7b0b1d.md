### Title
Incorrect Cycles-to-Mint Calculation in `ensure_balance` Allows Minting Rate Limit Bypass - (`rs/nns/cmc/src/main.rs`)

---

### Summary

The `ensure_balance` function in the Cycles Minting Canister (CMC) computes the number of cycles to mint as `cycles_to_mint = cycles - current_balance`, where `current_balance` is the CMC's live balance at call time. This value — not the full requested `cycles` amount — is what gets charged to the minting rate limiter (`base_limiter`). An unprivileged canister can deposit cycles into the CMC before a `notify_top_up` / `notify_create_canister` / `notify_mint_cycles` call, artificially inflating `current_balance`, shrinking `cycles_to_mint`, and causing the limiter to be charged less than the true amount of ICP-backed value being converted. This allows more ICP to be burned into cycles per hour than the protocol's `base_cycles_limit` is intended to permit.

---

### Finding Description

`ensure_balance` is the single synchronous gate that enforces the hourly minting cap:

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128()); // ← snapshot of live balance
    let cycles_to_mint = cycles - current_balance;                           // ← can be reduced by attacker

    with_state_mut(|state| {
        limiter_to_use.check_and_add_cycles(state, now, cycles_to_mint)?;   // ← limiter charged cycles_to_mint, not cycles
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    assert!(ic_cdk::api::canister_balance128() >= cycles.get());
    Ok(())
}
``` [1](#0-0) 

The limiter's `check_and_add_cycles` enforces `count + cycles_to_mint <= limit`:

```rust
pub fn check_and_add_cycles(&mut self, now: SystemTime, cycles_to_mint: Cycles, limit: Cycles) -> Result<(), String> {
    self.purge_old(now);
    let count = self.get_count();
    if count + cycles_to_mint > limit {
        return Err(format!("More than {} cycles have been minted in the last {} seconds ...", ...));
    }
    self.add(now, cycles_to_mint);
    Ok(())
}
``` [2](#0-1) 

The state stores `base_cycles_limit` (150 × 10¹⁵ cycles/hour) and `base_limiter`: [3](#0-2) 

`ensure_balance` is called from all three public minting paths:

- `deposit_cycles` → called by `process_top_up` → called by `notify_top_up` [4](#0-3) 
- `do_mint_cycles` → called by `process_mint_cycles` → called by `notify_mint_cycles` [5](#0-4) 
- `do_create_canister` → called by `process_create_canister` → called by `notify_create_canister` [6](#0-5) 

**Root cause**: The limiter is charged `cycles - current_balance` (newly minted cycles) rather than `cycles` (the full ICP-backed value being converted). Because any canister can call `deposit_cycles` on the management canister to increase the CMC's balance, an attacker can pre-fund the CMC to reduce `cycles_to_mint` to an arbitrarily small value, making the limiter check trivially pass even when the total ICP-to-cycles conversion far exceeds `base_cycles_limit`.

Additionally, if `current_balance >= cycles`, the subtraction `cycles - current_balance` underflows. In release-mode Rust (wrapping arithmetic on `u128`), `cycles_to_mint` wraps to a value near `u128::MAX`. The subsequent `count + cycles_to_mint` addition also wraps, producing an unpredictable result that may spuriously pass or fail the limiter check — a secondary correctness hazard.

---

### Impact Explanation

The `base_cycles_limit` (150 × 10¹⁵ cycles/hour ≈ 1,500 ICP/hour at typical rates) is the protocol's primary rate-control on ICP burning. By pre-funding the CMC with `X` cycles before a `notify_top_up` call requesting `Z` cycles (where `Z > base_cycles_limit`), an attacker causes the limiter to be charged only `Z − X` instead of `Z`. If `Z − X ≤ base_cycles_limit`, the call succeeds. The attacker effectively converts `Z` ICP-worth of value into cycles while only consuming `Z − X` of the hourly budget. Repeating this across hours allows sustained above-limit ICP burning, distorting the ICP/cycles exchange rate and undermining the tokenomic controls the limit is designed to enforce.

---

### Likelihood Explanation

Any canister on any subnet can call the management canister's `deposit_cycles` to increase the CMC's balance. No privileged role, governance vote, or threshold key is required. The attacker only needs to hold cycles (obtainable by minting at the normal rate over prior hours). The attack is fully deterministic and requires no timing precision because `ensure_balance` is synchronous — the balance read and the mint occur in the same execution slice, so pre-funding before the `notify_top_up` ingress message is sufficient.

---

### Recommendation

Charge the limiter based on the full `cycles` parameter (the ICP-backed value being converted), not on `cycles_to_mint` (the incremental mint). This mirrors the recommendation in the original report: base the accounting on the input parameters of the operation rather than on a balance difference that can be manipulated externally.

```rust
fn ensure_balance(cycles: Cycles, limiter_to_use: CyclesMintingLimiterSelector) -> Result<(), String> {
    let now = now_system_time();
    let current_balance = Cycles::from(ic_cdk::api::canister_balance128());
    if current_balance >= cycles {
        return Ok(()); // already funded, nothing to mint
    }
    let cycles_to_mint = cycles - current_balance;

    with_state_mut(|state| {
        // Charge the limiter for the full ICP-backed amount, not just the incremental mint
        limiter_to_use.check_and_add_cycles(state, now, cycles)?;  // ← use `cycles`, not `cycles_to_mint`
        state.total_cycles_minted += cycles_to_mint;
        Ok::<_, String>(())
    })?;

    let _minted_cycles = ic0_mint_cycles128(cycles_to_mint);
    Ok(())
}
```

The explicit `current_balance >= cycles` guard also eliminates the underflow hazard.

---

### Proof of Concept

Assume `base_cycles_limit` = 150 × 10¹⁵ cycles/hour and the ICP/cycles rate is 1 ICP = 10¹⁴ cycles.

1. **Accumulate cycles**: Over 10 prior hours, attacker mints at the limit, accumulating 1,500 × 10¹⁴ = 150 × 10¹⁵ cycles.
2. **Pre-fund CMC**: Attacker calls `deposit_cycles` on the management canister targeting the CMC, depositing 150 × 10¹⁵ cycles. CMC's `current_balance` is now 150 × 10¹⁵.
3. **Submit large top-up**: Attacker transfers 3,000 ICP (worth 300 × 10¹⁵ cycles) to the CMC's subaccount and calls `notify_top_up`.
4. **`ensure_balance` executes**: `cycles_to_mint = 300×10¹⁵ − 150×10¹⁵ = 150×10¹⁵`. Limiter check: `0 + 150×10¹⁵ ≤ 150×10¹⁵` → **passes**.
5. **Result**: CMC mints 150 × 10¹⁵ new cycles, combines with the 150 × 10¹⁵ pre-funded cycles, and deposits 300 × 10¹⁵ cycles to the target canister. The limiter records only 150 × 10¹⁵ minted this hour, even though 300 × 10¹⁵ ICP-backed value was converted — **2× the intended hourly limit**.

### Citations

**File:** rs/nns/cmc/src/main.rs (L232-243)
```rust
    /// How many cycles are allowed to be minted in an hour.
    pub base_cycles_limit: Cycles,

    /// How many cycles are allowed to be minted by the Subnet Rental Canister in a month.
    pub subnet_rental_cycles_limit: Cycles,

    /// Maintain a count of how many cycles have been minted in the last hour.
    pub base_limiter: limiter::Limiter,

    /// Maintain a count of how many cycles have been minted by the Subnet Rental Canister
    /// in the last month.
    pub subnet_rental_canister_limiter: limiter::Limiter,
```

**File:** rs/nns/cmc/src/main.rs (L2110-2118)
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
```

**File:** rs/nns/cmc/src/main.rs (L2149-2151)
```rust
    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;
```

**File:** rs/nns/cmc/src/main.rs (L2247-2249)
```rust
    // Always use base cycles limit for minting cycles, since the Subnet Rental Canister
    // doesn't call endpoints using this function.
    ensure_balance(cycles, CyclesMintingLimiterSelector::BaseLimit)?;
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
