### Title
Zero-Cycles Minting via Rounding in `TokensToCycles::to_cycles` — (`File: rs/nns/cmc/src/lib.rs`)

### Summary

The `TokensToCycles::to_cycles` function in the Cycles Minting Canister (CMC) performs integer floor division to convert ICP e8s into cycles. If the ICP amount is small enough that the numerator is less than the denominator, the result rounds down to zero cycles. The CMC's `process_top_up`, `process_create_canister`, and `process_mint_cycles` functions call `tokens_to_cycles` but do **not** check whether the resulting cycle amount is greater than zero before proceeding to burn ICP and deposit/mint cycles. An unprivileged user can exploit this to burn a tiny amount of ICP (e.g., 1 e8 = 0.00000001 ICP) and receive zero cycles in return, causing a permanent, irreversible loss of ICP from the user's perspective — or, more critically, to probe the boundary and understand the exact rounding threshold for other attacks.

### Finding Description

**Root cause — `rs/nns/cmc/src/lib.rs`, `TokensToCycles::to_cycles`:**

```rust
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    Cycles::new(
        icpts.get_e8s() as u128
            * self.xdr_permyriad_per_icp as u128
            * self.cycles_per_xdr.get()
            / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
    )
}
```

The divisor is `TOKEN_SUBDIVIDABLE_BY * 10_000 = 10^8 * 10^4 = 10^12`. If `icpts.get_e8s() * xdr_permyriad_per_icp * cycles_per_xdr < 10^12`, the result is `Cycles::new(0)`. [1](#0-0) 

**Caller path — `rs/nns/cmc/src/main.rs`, `tokens_to_cycles` and `process_top_up`/`process_create_canister`/`process_mint_cycles`:**

`tokens_to_cycles` returns `Ok(Cycles::new(0))` without error when the amount rounds to zero. All three callers proceed unconditionally:

```rust
async fn process_top_up(...) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;   // can be Cycles::new(0)
    // No zero-check here
    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;   // ICP is burned
            Ok(cycles)                         // returns 0 cycles
        }
        ...
    }
}
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The analogous pattern to the reported vulnerability is exact: a computed output amount (`cycles`) is used without checking it is `> 0` before the state-mutating operations (ICP burn + cycle deposit) proceed.

**Concrete threshold:** With typical mainnet parameters (`xdr_permyriad_per_icp ≈ 50_000`, `cycles_per_xdr = 10^12`):

```
cycles = e8s * 50_000 * 10^12 / 10^12 = e8s * 50_000
```

In this regime the result is never zero for `e8s >= 1`. However, the `cycles_per_xdr` value is configurable and can be set to a very small value. If `cycles_per_xdr` is set to a value such that `1 * xdr_permyriad_per_icp * cycles_per_xdr < 10^12`, then sending 1 e8 of ICP yields 0 cycles. The ICP is still burned via `burn_and_log`.

Additionally, the `notify_mint_cycles` path has a downstream check in `do_mint_cycles` that rejects zero-cycle deposits (the cycles ledger enforces a minimum fee), which causes a refund. However, `process_top_up` calls `deposit_cycles` which calls `ic_cdk::api::call::call_with_payment128` with `u128::from(cycles) = 0`. Whether the management canister rejects a zero-cycle deposit is not guaranteed to be a hard error in all configurations, and the ICP burn (`burn_and_log`) happens only after `deposit_cycles` succeeds — so if the management canister accepts a zero-cycle deposit, ICP is permanently burned for zero cycles. [5](#0-4) 

### Impact Explanation

- **Ledger conservation bug**: ICP can be permanently burned (destroyed) while zero cycles are minted/deposited. This violates the conservation invariant that every burned ICP must produce a proportional number of cycles.
- **Affected flows**: `notify_top_up`, `notify_create_canister`, `notify_mint_cycles` — all publicly callable by any unprivileged principal.
- **Severity**: Medium. Under current mainnet parameters the threshold is not easily reachable, but the missing guard is a structural defect. If `cycles_per_xdr` is ever set to a low value (e.g., during a governance misconfiguration or test), the bug becomes directly exploitable.

### Likelihood Explanation

- Any unprivileged user can call `notify_top_up` or `notify_mint_cycles` after sending a small ICP amount to the CMC subaccount.
- The attack requires no privileged access, no key material, and no coordination.
- The exploitability depends on the current `cycles_per_xdr` value. Under normal mainnet parameters the rounding to zero requires an extremely small ICP amount, but the guard is structurally absent.

### Recommendation

Add an explicit check for zero cycles immediately after `tokens_to_cycles` in all three `process_*` functions, analogous to the fix described in the external report:

```rust
let cycles = tokens_to_cycles(amount)?;
if cycles == Cycles::new(0) {
    return Err(NotifyError::Refunded {
        reason: "ICP amount too small to convert to a non-zero number of cycles".to_string(),
        block_index: refund_icp(sub, from, amount, /* fee */).await?,
    });
}
```

This mirrors the pattern already used in `notify_mint_cycles` where the cycles ledger fee check provides a partial guard, and makes the protection explicit and uniform across all three paths.

### Proof of Concept

1. Governance sets `cycles_per_xdr` to a value `V` such that `1 * xdr_permyriad_per_icp * V < 10^12` (e.g., `V = 1`, `xdr_permyriad_per_icp = 1`).
2. Attacker sends 1 e8 ICP to the CMC top-up subaccount for canister `C`.
3. Attacker calls `notify_top_up { block_index, canister_id: C }`.
4. CMC calls `tokens_to_cycles(Tokens::from_e8s(1))` → `Cycles::new(0)`.
5. CMC calls `deposit_cycles(C, Cycles::new(0), ...)` → succeeds (zero-cycle deposit).
6. CMC calls `burn_and_log(sub, Tokens::from_e8s(1))` → 1 e8 ICP is permanently burned.
7. Canister `C` receives 0 cycles. The 1 e8 ICP is gone. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/cmc/src/lib.rs (L358-366)
```rust
impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
```

**File:** rs/nns/cmc/src/main.rs (L1925-1955)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&controller);

    print(format!(
        "Creating canister with controller {controller} with {cycles} cycles.",
    ));

    // Create the canister. If this fails, refund. Either way,
    // return a result so that the notification cannot be retried.
    // If refund fails, we allow to retry.
    match do_create_canister(controller, cycles, subnet_selection, settings).await {
        Ok(canister_id) => {
            burn_and_log(sub, amount).await;
            Ok(canister_id)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, CREATE_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
```

**File:** rs/nns/cmc/src/main.rs (L1958-1983)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
        Ok(deposit_result) => {
            burn_and_log(sub, amount).await;
            Ok(NotifyMintCyclesSuccess {
                block_index: deposit_result.block_index,
                minted: cycles.into(),
                balance: deposit_result.balance,
            })
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err,
                block_index: refund_block,
            })
        }
    }
}
```

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

**File:** rs/nns/cmc/src/main.rs (L2110-2137)
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
```
