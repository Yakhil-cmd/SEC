Based on my analysis of the code, this is a real vulnerability.

---

### Title
Global Base Limiter Exhaustion via Single Large `notify_top_up` Call - (`rs/nns/cmc/src/limiter.rs`, `rs/nns/cmc/src/main.rs`)

### Summary
The CMC's base cycles limiter is a single shared global counter with no per-user isolation. A single unprivileged caller holding sufficient ICP can exhaust the entire hourly minting capacity in one `notify_top_up` call, causing all subsequent minting operations from any user to fail for up to one hour.

### Finding Description

The `check_and_add_cycles` function in `limiter.rs` maintains a single global rolling-window counter: [1](#0-0) 

The check at line 43 is simply `count + cycles_to_mint > limit` — there is no per-caller cap, no per-transaction maximum, and no sub-limiter per user. If a single call adds exactly `limit` cycles, the check passes and the full capacity is consumed.

In `notify_top_up`, all non-subnet-rental callers are routed to `CyclesMintingLimiterSelector::BaseLimit`: [2](#0-1) 

This means every regular user shares the same global limiter. Once it is full, all calls to `notify_top_up`, `notify_create_canister`, `notify_mint_cycles`, and `create_canister` that use `BaseLimit` will return a rate-limit error until the rolling window expires (up to one hour).

### Impact Explanation
- **Platform-wide minting DoS**: All users are blocked from minting cycles for up to one hour.
- **Attack is economically free**: The attacker converts ICP → cycles (no net loss). They retain the cycles and can repeat the attack each hour.
- **No privileged access required**: Any principal with sufficient ICP can execute this via a standard ledger transfer + `notify_top_up` call.

### Likelihood Explanation
The attack requires holding ~1500 ICP (at a 100 XDR/ICP rate to mint 150T cycles), which is a non-trivial but realistic amount for a motivated attacker. Since the attacker receives cycles in return, the net cost is near zero (only opportunity cost). The attack is repeatable every hour.

### Recommendation
1. Add a **per-principal sub-limiter** alongside the global limiter, capping how many cycles any single caller can mint per hour.
2. Alternatively, enforce a **per-call maximum** on `cycles_to_mint` independent of the global limit.
3. Consider a tiered limit structure where large mints require governance approval or are subject to a separate high-value limiter.

### Proof of Concept
1. Attacker transfers ~1500 ICP to CMC subaccount for their canister.
2. Attacker calls `notify_top_up` — `check_and_add_cycles` accepts the full `base_cycles_limit` (e.g., 150T cycles) and records it in the global limiter.
3. Any subsequent call from any user to `notify_top_up` (or other minting endpoints using `BaseLimit`) hits the check at `limiter.rs:43`: `count + cycles_to_mint > limit` evaluates to `true`, returning a rate-limit error.
4. All minting is blocked for up to one hour until `purge_old` expires the window. [3](#0-2)

### Citations

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

**File:** rs/nns/cmc/src/main.rs (L1149-1155)
```rust
    let limiter_to_use =
        if caller == src_canister_principal && canister_id.get() == src_canister_principal {
            // caller and destination needs to be src_canister_principal to get alternate limiter
            CyclesMintingLimiterSelector::SubnetRentalLimit
        } else {
            CyclesMintingLimiterSelector::BaseLimit
        };
```
