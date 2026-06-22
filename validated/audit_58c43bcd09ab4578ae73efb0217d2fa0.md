### Title
CMC ICP-to-Cycles Conversion Operations Lack Minimum Output (Slippage) Protection - (File: rs/nns/cmc/src/main.rs)

### Summary

The Cycles Minting Canister (CMC) exposes a two-step ICP-to-cycles conversion flow (`notify_top_up`, `notify_mint_cycles`, `notify_create_canister`). In step 1 the user irrevocably commits ICP to a CMC subaccount; in step 2 the user calls the notification endpoint. The conversion rate applied in step 2 is whatever `icp_xdr_conversion_rate` is stored in CMC state at that moment. None of the notification argument types accept a `minimum_cycles_expected` (or equivalent) parameter, so the user has no on-chain way to bound the minimum output they will receive.

### Finding Description

The two-step flow is:

1. User transfers ICP to a CMC subaccount keyed by their principal or target canister ID.
2. User calls `notify_top_up` / `notify_mint_cycles` / `notify_create_canister` referencing the ledger block index.

The notification argument types carry no slippage guard:

```
// rs/nns/cmc/cmc.did
type NotifyTopUpArg = record {
  block_index : BlockIndex;
  canister_id : principal;
};

type NotifyMintCyclesArg = record {
  block_index : BlockIndex;
  to_subaccount : Subaccount;
  deposit_memo : Memo;
};
``` [1](#0-0) [2](#0-1) 

The conversion itself reads the current rate from state with no floor check:

```rust
// rs/nns/cmc/src/main.rs
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
``` [3](#0-2) 

`process_top_up`, `process_mint_cycles`, and `process_create_canister` all call `tokens_to_cycles` and immediately proceed to deposit/burn with no minimum-output guard: [4](#0-3) [5](#0-4) 

The `TokensToCycles::to_cycles` formula is a pure arithmetic conversion with no floor:

```rust
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    Cycles::new(
        icpts.get_e8s() as u128
            * self.xdr_permyriad_per_icp as u128
            * self.cycles_per_xdr.get()
            / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
    )
}
``` [6](#0-5) 

The `icp_xdr_conversion_rate` is updated by NNS governance whenever a new rate arrives from the exchange-rate canister, with no rate-of-change cap between consecutive updates beyond the requirement that the new timestamp is strictly greater: [7](#0-6) 

### Impact Explanation

Once a user sends ICP to the CMC subaccount (step 1), the ICP is locked. If the ICP/XDR rate falls between step 1 and step 2, the user receives proportionally fewer cycles with no recourse. There is no refund path for an unfavorable rate — the only refund paths are for operational failures (canister does not exist, cycles ledger error, etc.), not for rate-slippage. A user who sent ICP expecting, say, 100 T cycles at a rate of 10 XDR/ICP may receive only 50 T cycles if the rate halves before they call `notify_top_up`. The ICP is burned regardless.

**Vulnerability class:** cycles/resource accounting bug — missing minimum-output guard on an irrevocable two-step conversion.

### Likelihood Explanation

The ICP/XDR rate is updated daily from the exchange-rate canister and can move substantially in a short period (ICP is a volatile asset). The two-step flow has no enforced time bound between step 1 and step 2 beyond the `MAX_NOTIFY_HISTORY` window of 1,000,000 ledger blocks. Any unprivileged ingress sender who uses the CMC conversion flow is exposed. No privileged access is required to trigger the loss — the user simply calls `notify_top_up` or `notify_mint_cycles` after the rate has moved adversely. [8](#0-7) 

### Recommendation

Add an optional `minimum_cycles_expected: opt nat` field to `NotifyTopUpArg`, `NotifyMintCyclesArg`, and `NotifyCreateCanisterArg`. In `tokens_to_cycles` (or immediately after), compare the computed cycles against the caller-supplied minimum and return `NotifyError::Other` (without burning the ICP, allowing a retry after the rate recovers) if the minimum is not met. This mirrors the `acceptablePrice` / `minOutputAmount` pattern used in GMX and is the standard slippage-protection idiom for two-step conversion flows. [1](#0-0) [2](#0-1) 

### Proof of Concept

1. At time T₀, the ICP/XDR rate is 20 XDR/ICP (`xdr_permyriad_per_icp = 200_000`). User sends 10 ICP to their CMC subaccount, expecting ≈ 2,000 T cycles (at 1 T cycles/XDR).
2. At time T₁, the NNS governance updates the rate to 10 XDR/ICP (`xdr_permyriad_per_icp = 100_000`) — a 50 % drop, plausible during a market downturn.
3. User calls `notify_top_up`. `tokens_to_cycles` reads the new rate and computes ≈ 1,000 T cycles.
4. The ICP is burned, 1,000 T cycles are deposited — half of what the user expected. No minimum-output check exists to abort or refund.

The `TokensToCycles::to_cycles` formula confirms the linear dependence on `xdr_permyriad_per_icp`:

```
cycles = e8s * xdr_permyriad_per_icp * cycles_per_xdr / (1e8 * 10_000)
``` [9](#0-8)

### Citations

**File:** rs/nns/cmc/cmc.did (L27-33)
```text
type NotifyTopUpArg = record {
  // Index of the block on the ICP ledger that contains the payment.
  block_index : BlockIndex;

  // The canister to top up.
  canister_id : principal;
};
```

**File:** rs/nns/cmc/cmc.did (L200-204)
```text
type NotifyMintCyclesArg = record {
  block_index : BlockIndex;
  to_subaccount : Subaccount;
  deposit_memo : Memo;
};
```

**File:** rs/nns/cmc/src/main.rs (L69-72)
```rust
/// The maximum number of notification statuses to store.
const MAX_NOTIFY_HISTORY: usize = 1_000_000;
/// The maximum number of old notification statuses we purge in one go.
const MAX_NOTIFY_PURGE: usize = 100_000;
```

**File:** rs/nns/cmc/src/main.rs (L1009-1040)
```rust
fn do_set_icp_xdr_conversion_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    proposed_conversion_rate: IcpXdrConversionRate,
) -> Result<(), String> {
    print(format!(
        "[cycles] conversion rate update: {proposed_conversion_rate:?}"
    ));

    if proposed_conversion_rate.xdr_permyriad_per_icp == 0 {
        return Err("Proposed conversion rate must be greater than 0".to_string());
    }

    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }

        state.icp_xdr_conversion_rate = Some(proposed_conversion_rate.clone());
        update_recent_icp_xdr_rates(state, &proposed_conversion_rate);

        let witness_generator = convert_data_to_mixed_hash_tree(state);
        env.set_certified_data(&witness_generator.hash_tree().digest().0[..]);

        Ok(())
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L1899-1923)
```rust
// If conversion fails, log and return an error
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => {
                let error_message =
                    "No conversion rate found in CMC, notification aborted".to_string();
                print(&error_message);
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message,
                })
            }
        }
    })
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

**File:** rs/nns/cmc/src/lib.rs (L351-367)
```rust
pub struct TokensToCycles {
    /// Number of 1/10,000ths of XDR that 1 ICP is worth.
    pub xdr_permyriad_per_icp: u64,
    /// Number of cycles that 1 XDR is worth.
    pub cycles_per_xdr: Cycles,
}

impl TokensToCycles {
    pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
        Cycles::new(
            icpts.get_e8s() as u128
                * self.xdr_permyriad_per_icp as u128
                * self.cycles_per_xdr.get()
                / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
        )
    }
}
```
