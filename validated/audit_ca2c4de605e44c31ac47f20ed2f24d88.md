### Title
Single Actor Can Exhaust Global CMC Cycles Minting Rate Limit, Blocking All Users from Minting Cycles - (File: rs/nns/cmc/src/main.rs)

---

### Summary

The Cycles Minting Canister (CMC) enforces a single global hourly rate limit (`base_limiter`) shared across all callers. A single unprivileged actor who holds sufficient ICP can mint up to the full `DEFAULT_CYCLES_LIMIT` (150 petacycles) in one transaction, exhausting the entire hourly budget and blocking every other user from minting cycles for up to one hour. Because the attacker receives cycles in exchange for their ICP, the net cost of the attack is essentially zero (only the ICP transaction fee).

---

### Finding Description

The CMC maintains a global `base_limiter` of type `Limiter` that accumulates all cycles minted across all callers within a rolling one-hour window. The limit is `DEFAULT_CYCLES_LIMIT = 150e15` cycles per hour. [1](#0-0) 

The `Limiter::check_and_add_cycles` function checks the global running total against the limit: [2](#0-1) 

The check is `count + cycles_to_mint > limit`, where `count` is the **global** total across all users. There is no per-caller sub-limit or per-caller quota. The `base_limiter` is stored in the shared CMC state: [3](#0-2) 

In `notify_top_up`, the limiter selector is chosen based on whether the caller is the `SUBNET_RENTAL_CANISTER_ID` topping up itself. All other callers share the `BaseLimit` bucket: [4](#0-3) 

The actual minting and limiter accounting happen in `ensure_balance`: [5](#0-4) 

Because the limiter is global and not per-caller, a single actor who mints the full 150P cycles in one call exhausts the entire hourly budget. All subsequent `notify_top_up`, `notify_create_canister`, and `notify_mint_cycles` calls from any user will be rejected with `"More than ... cycles have been minted in the last 3600 seconds, please try again later"` until the rolling window expires.

---

### Impact Explanation

- **All users** are blocked from minting cycles via the CMC for up to one hour per attack cycle.
- This affects canister creation via ICP (`notify_create_canister`), topping up existing canisters (`notify_top_up`), and minting to the cycles ledger (`notify_mint_cycles`).
- The attacker retains the cycles they minted (they paid ICP and received cycles of equivalent value), so the net financial cost is only the ICP ledger transaction fee (~0.0001 ICP).
- The attack can be repeated every hour indefinitely, effectively making the CMC permanently unavailable to all other users at negligible cost.
- This is a **cycles/resource accounting bug** with a **resource exhaustion / global-threshold DOS** impact.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged ingress sender or canister caller who holds ~1,500 ICP (at 100 XDR/ICP, 1T cycles/XDR) can trigger this. The ICP is not lost — it is converted to cycles — so the attacker's net cost is near zero.
- **No privileged role required**: The `notify_top_up` endpoint is open to any caller.
- **Repeatability**: The attack can be repeated every hour.
- **Motivation**: A competitor wishing to prevent other projects from topping up their canisters, or an actor wishing to disrupt the IC ecosystem, has a clear incentive.
- The existing test `cmc_notify_top_up_rate_limited` confirms the global limit is enforced and that a single large mint exhausts the budget for all subsequent callers: [6](#0-5) 

---

### Recommendation

1. **Per-caller sub-limits**: Track cycles minted per `PrincipalId` within the rolling window. Reject a request if the caller's individual share would exceed a per-caller cap (e.g., a fraction of the global limit), preventing any single actor from exhausting the global budget.
2. **Proportional allocation**: Alternatively, enforce that no single call can mint more than a configurable fraction (e.g., 10%) of the global hourly limit, regardless of the ICP amount sent.
3. **Graduated limits**: Apply a tiered limit where small mints are always allowed even when the global budget is near exhaustion, ensuring small users are not blocked by large actors.

---

### Proof of Concept

1. Attacker transfers 1,500 ICP to the CMC subaccount corresponding to their canister (at 100 XDR/ICP, this yields 150P cycles, exactly the hourly limit).
2. Attacker calls `notify_top_up` with the resulting block index and their canister ID.
3. CMC calls `ensure_balance`, which calls `check_and_add_cycles(now, 150_000_000_000_000_000, 150_000_000_000_000_000)`. The check passes (`0 + 150P == 150P`, not `>`), so the full limit is consumed. [7](#0-6) 

4. Attacker's canister receives 150P cycles. The `base_limiter` now holds `total_count = 150P`.
5. Any subsequent call from any user to `notify_top_up`, `notify_create_canister`, or `notify_mint_cycles` that would mint even 1 cycle fails:
   - `check_and_add_cycles` evaluates `150P + N > 150P` → `true` → returns `Err("More than 150000000000000000 cycles have been minted in the last 3600 seconds, please try again later.")`.
6. All users are blocked for up to 3,600 seconds (one hour).
7. Attacker repeats step 1–6 every hour at near-zero net cost. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/cmc/src/main.rs (L80-86)
```rust
/// Prior to 2024-12-10, we used 50e15, but legitimate users started running
/// into this. At that time, prices had recently gone up, so we resolved to
/// increase this by 3x.
const DEFAULT_CYCLES_LIMIT: u128 = 150e15 as u128;

/// The limit for the number of cycles that can be minted by the Subnet Rental Canister in a month.
const SUBNET_RENTAL_DEFAULT_CYCLES_LIMIT: u128 = 500e15 as u128;
```

**File:** rs/nns/cmc/src/main.rs (L232-244)
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

**File:** rs/nns/cmc/src/main.rs (L1148-1155)
```rust
    let src_canister_principal = SUBNET_RENTAL_CANISTER_ID.get();
    let limiter_to_use =
        if caller == src_canister_principal && canister_id.get() == src_canister_principal {
            // caller and destination needs to be src_canister_principal to get alternate limiter
            CyclesMintingLimiterSelector::SubnetRentalLimit
        } else {
            CyclesMintingLimiterSelector::BaseLimit
        };
```

**File:** rs/nns/cmc/src/main.rs (L2306-2324)
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

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L1644-1687)
```rust
#[test]
fn cmc_notify_top_up_rate_limited() {
    let state_machine = state_machine_builder_for_nns_tests().build();

    let account = AccountIdentifier::new(*TEST_USER1_PRINCIPAL, None);
    // The only requirement here is to have sufficient funds. Other than that,
    // the precise number here does not matter.
    let balance = Tokens::new(1e6 as u64, 0).unwrap();
    let nns_init_payloads = NnsInitPayloadsBuilder::new()
        .with_test_neurons()
        .with_ledger_account(account, balance)
        .build();
    setup_nns_canisters(&state_machine, nns_init_payloads);

    // First top-up should succeed since it's 90P - less than the 150P/hr limit.
    let cycles = notify_top_up(
        &state_machine,
        GOVERNANCE_CANISTER_ID,
        Tokens::new(900, 0).unwrap(),
    )
    .unwrap();
    assert_eq!(cycles, Cycles::new(90e15 as u128));

    // Second top-up should also succeed after 1 hour.
    state_machine.advance_time(Duration::from_secs(4000));
    let cycles = notify_top_up(
        &state_machine,
        GOVERNANCE_CANISTER_ID,
        Tokens::new(900, 0).unwrap(),
    )
    .unwrap();
    assert_eq!(cycles, Cycles::new(90e15 as u128));

    // Third top-up should fail since the rate limit is 150e15 cycles per hour,
    // and less than an hour has passed.
    state_machine.advance_time(Duration::from_secs(3000));
    let error = notify_top_up(
        &state_machine,
        GOVERNANCE_CANISTER_ID,
        Tokens::new(900, 0).unwrap(),
    )
    .unwrap_err();
    assert_matches!(error, NotifyError::Refunded { reason, .. } if reason.contains("try again later"));
}
```
