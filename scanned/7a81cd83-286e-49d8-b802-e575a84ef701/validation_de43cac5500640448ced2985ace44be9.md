### Title
ICP Burned Without Cycles Credited When Conversion Yields Sub-Fee Amount and Refund Fails - (File: rs/nns/cmc/src/lib.rs, rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using integer division that can produce a cycles value below the cycles ledger's minimum deposit fee. When this happens, the cycles ledger rejects the deposit. The CMC then attempts to refund the ICP, but the refund itself requires `MINT_CYCLES_REFUND_FEE` (2× the ICP transfer fee = 20,000 e8s). If the deposited ICP amount is less than this refund fee, the refund silently fails and the user's ICP is permanently lost with no cycles credited.

### Finding Description
`TokensToCycles::to_cycles` performs integer division that truncates toward zero:

```rust
pub fn to_cycles(&self, icpts: Tokens) -> Cycles {
    Cycles::new(
        icpts.get_e8s() as u128
            * self.xdr_permyriad_per_icp as u128
            * self.cycles_per_xdr.get()
            / (icp_ledger::TOKEN_SUBDIVIDABLE_BY as u128 * 10_000),
    )
}
``` [1](#0-0) 

The denominator is `10^8 × 10^4 = 10^12`. For small ICP amounts, the resulting cycles value can be below the cycles ledger's deposit fee. There is no zero-or-minimum guard on the result before proceeding.

In `process_mint_cycles` (and analogously `process_top_up`, `process_create_canister`), the flow is:

1. `tokens_to_cycles(amount)` is called — no check that the result is above the cycles ledger fee.
2. `do_mint_cycles(to_account, cycles, ...)` is called with the (potentially sub-fee) cycles value.
3. The cycles ledger rejects the deposit.
4. `refund_icp(sub, from, amount, MINT_CYCLES_REFUND_FEE)` is called — but if `amount < MINT_CYCLES_REFUND_FEE` (20,000 e8s), the refund itself fails silently.
5. The user's ICP is burned with no cycles credited and no refund. [2](#0-1) 

The `tokens_to_cycles` wrapper also has no guard: [3](#0-2) 

This behavior is confirmed by an integration test that explicitly shows `block_index: None` (no refund) when the amount is too small: [4](#0-3) 

### Impact Explanation
A user who sends a small ICP amount (between 1 and 19,999 e8s) to the CMC subaccount and calls `notify_mint_cycles`, `notify_top_up`, or `notify_create_canister` will have their ICP permanently burned. They receive zero cycles and zero refund. The ICP is irrecoverably lost. This is the direct IC analog of M-33: assets are consumed, the resulting "shares" (cycles) are below the minimum usable threshold, and the user receives nothing in return.

### Likelihood Explanation
The scenario is reachable by any unprivileged ingress sender. A user must transfer ICP to the CMC's subaccount (paying the 10,000 e8s ICP ledger fee) and then call a notify endpoint. With typical exchange rates (~5 XDR/ICP, 1T cycles/XDR), 1 e8 of ICP yields ~50,000 cycles — well below the cycles ledger fee — triggering the failure path. The maximum ICP loss at the CMC is bounded by `MINT_CYCLES_REFUND_FEE - 1 = 19,999 e8s` (~0.0002 ICP). While the absolute amount is small, the loss is total (100% of the deposited amount) and permanent, matching the M-33 pattern exactly.

### Recommendation
Add a pre-flight check in `tokens_to_cycles` or at the call sites (`process_mint_cycles`, `process_top_up`, `process_create_canister`) that rejects the operation — returning an error before any ICP is burned — when the computed cycles value is below the cycles ledger's minimum deposit fee. Concretely:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    let cycles = /* existing conversion */;
    if cycles < CYCLES_LEDGER_MIN_DEPOSIT_FEE {
        return Err(NotifyError::Other {
            error_code: ...,
            error_message: "Amount too small to cover cycles ledger fee".to_string(),
        });
    }
    Ok(cycles)
}
```

This mirrors the recommended mitigation from M-33: revert (reject) the transaction when the resulting token/cycles amount would be zero or below the minimum usable threshold, before any assets are consumed.

### Proof of Concept
1. Alice transfers 1 e8 of ICP to the CMC subaccount for her principal (paying 10,000 e8s in ICP ledger fees).
2. Alice calls `notify_mint_cycles` on the CMC.
3. CMC calls `tokens_to_cycles(Tokens::from_e8s(1))`:
   - `1 × 50_000 × 10^12 / 10^12 = 50_000` cycles — below the cycles ledger fee.
4. CMC calls `do_mint_cycles(alice_account, Cycles::new(50_000), ...)`.
5. Cycles ledger rejects: `"The requested amount 50000 to be deposited is less than the cycles ledger fee"`.
6. CMC calls `refund_icp(sub, alice, Tokens::from_e8s(1), MINT_CYCLES_REFUND_FEE)`.
7. Refund fails: `1 e8 < 20_000 e8s` (MINT_CYCLES_REFUND_FEE). `block_index = None`.
8. Alice has lost her 1 e8 of ICP. She received 0 cycles. [5](#0-4) [6](#0-5) [4](#0-3)

### Citations

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

**File:** rs/nns/integration_tests/src/cycles_minting_canister.rs (L1337-1350)
```rust
    // insufficient amount
    let notify_mint_result =
        notify_mint_cycles(&state_machine, Tokens::new(0, 1).unwrap(), None, None).unwrap_err();
    let NotifyError::Refunded {
        reason,
        block_index,
    } = notify_mint_result
    else {
        panic!("Not refunded.")
    };
    assert!(reason.contains(
        "The requested amount 1000000 to be deposited is less than the cycles ledger fee"
    ));
    assert_eq!(block_index, None); // Amount too small to refund
```
