### Title
Missing Minimum ERC20 Amount Check in `withdraw_erc20` Before ckETH Burn - (`rs/ethereum/cketh/minter/src/main.rs`)

### Summary

The ckETH minter exposes two withdrawal endpoints: `withdraw_eth` and `withdraw_erc20`. `withdraw_eth` enforces a `cketh_minimum_withdrawal_amount` guard before any state change. `withdraw_erc20` has no analogous minimum-amount guard for the ERC20 token amount before it burns ckETH to pay for gas fees. Any unprivileged caller can submit a dust (zero or sub-fee) ERC20 withdrawal, causing the minter to irreversibly burn the caller's ckETH for gas, fail the ERC20 burn, and reimburse only `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`, permanently destroying `CKETH_LEDGER_TRANSACTION_FEE` wei per call.

### Finding Description

`withdraw_eth` performs an explicit minimum-amount check before any ledger interaction:

```rust
let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
if amount < minimum_withdrawal_amount {
    return Err(WithdrawalError::AmountTooLow { ... });
}
``` [1](#0-0) 

`withdraw_erc20` performs no equivalent check on `ckerc20_withdrawal_amount`. After converting the caller-supplied `amount` to `Erc20Value`, the function immediately proceeds to estimate the gas fee and burn ckETH:

```rust
let ckerc20_withdrawal_amount =
    Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");
// ... no minimum check ...
let erc20_tx_fee = estimate_erc20_transaction_fee().await...;
// ckETH burned here, before ERC20 amount is validated
match cketh_ledger.burn_from(cketh_account, erc20_tx_fee, ...).await { ... }
``` [2](#0-1) 

When the subsequent ERC20 burn fails with `AmountTooLow`, the reimbursement path deducts a penalty:

```rust
LedgerBurnError::AmountTooLow { .. } => erc20_tx_fee
    .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
    .unwrap_or(Wei::ZERO),
``` [3](#0-2) 

The `WithdrawErc20Error` type has no top-level `AmountTooLow` variant (unlike `WithdrawalError` for `withdraw_eth`), so there is no structural path to reject a dust ERC20 amount before the ckETH burn occurs. [4](#0-3) 

The test `should_error_when_ckerc20_withdrawal_amount_too_small` confirms this flow is reachable and results in ckETH loss: [5](#0-4) 

### Impact Explanation

Every call to `withdraw_erc20` with a dust ERC20 amount (e.g., `amount = 0` or `amount < ckerc20_ledger_fee`) causes:

1. An async call to the EVM RPC canister to estimate gas fees (minter cycles consumed).
2. An irreversible ckETH burn on the ledger for `erc20_tx_fee`.
3. A failed ERC20 burn returning `AmountTooLow`.
4. A reimbursement of `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`, permanently destroying `CKETH_LEDGER_TRANSACTION_FEE` (2 000 000 000 000 wei on mainnet) of the caller's ckETH per call.
5. A `FailedErc20WithdrawalRequest` event written to the minter's append-only event log, polluting it indefinitely.

The minter's per-principal guard (`retrieve_withdraw_guard`) serializes requests per principal but does not prevent repeated calls across time or from multiple principals. An attacker holding minimal ckETH can repeatedly trigger this path, consuming minter cycles and polluting the event log at a cost of one `CKETH_LEDGER_TRANSACTION_FEE` per call.

### Likelihood Explanation

The `withdraw_erc20` endpoint is publicly callable by any non-anonymous principal with a non-zero ckETH allowance. No privileged role is required. The cost to trigger the bug is `CKETH_LEDGER_TRANSACTION_FEE` (≈ $0.004 at current prices), making repeated exploitation economically feasible. The missing check is a straightforward omission visible by comparing the two withdrawal functions side-by-side.

### Recommendation

Add a minimum ERC20 amount check in `withdraw_erc20` before the ckETH burn, analogous to the check in `withdraw_eth`. Since ERC20 tokens have per-token ledger fees, the minter should either:

1. Query the ERC20 ledger fee at call time and reject if `ckerc20_withdrawal_amount <= ckerc20_ledger_fee`, returning a new top-level `AmountTooLow` variant in `WithdrawErc20Error`; or
2. Store a per-token `minimum_withdrawal_amount` in minter state (mirroring `cketh_minimum_withdrawal_amount`) and enforce it before any async call.

This mirrors the fix suggested in the original report: add the guard at the entry point of the code path that currently lacks it.

### Proof of Concept

```
1. Caller approves ckETH minter for erc20_tx_fee on the ckETH ledger.
2. Caller approves ckETH minter for 1 wei on the ckUSDC ledger.
3. Caller calls withdraw_erc20(amount=1, ckerc20_ledger_id=ckUSDC, recipient=valid_eth_addr).
4. Minter burns erc20_tx_fee ckETH from caller (succeeds).
5. Minter attempts to burn 1 wei ckUSDC (fails: AmountTooLow, min = CKERC20_TRANSFER_FEE).
6. Minter reimburses erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE ckETH.
7. Caller has permanently lost CKETH_LEDGER_TRANSACTION_FEE (2_000_000_000_000 wei).
8. Minter event log contains a FailedErc20WithdrawalRequest entry.
9. Repeat from step 1 to amplify cycle drain and log pollution.
```

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-296)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L415-460)
```rust
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");

    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
    {
        Ok(cketh_ledger_burn_index) => {
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L509-513)
```rust
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
```

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L30-46)
```rust
#[derive(Clone, PartialEq, Debug, CandidType, Deserialize)]
pub enum WithdrawErc20Error {
    TokenNotSupported {
        supported_tokens: Vec<crate::endpoints::CkErc20Token>,
    },
    RecipientAddressBlocked {
        address: String,
    },
    CkEthLedgerError {
        error: LedgerError,
    },
    CkErc20LedgerError {
        cketh_block_index: Nat,
        error: LedgerError,
    },
    TemporarilyUnavailable(String),
}
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L497-530)
```rust
    #[test]
    fn should_error_when_ckerc20_withdrawal_amount_too_small() {
        let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
        let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
        let caller = ckerc20.caller();
        let ckerc20_tx_fee = CKETH_MINIMUM_WITHDRAWAL_AMOUNT;

        ckerc20
            .deposit_cketh_and_ckerc20(
                EXPECTED_BALANCE,
                TWO_USDC + CKERC20_TRANSFER_FEE,
                ckusdc.clone(),
                caller,
            )
            .expect_mint()
            .call_cketh_ledger_approve_minter(caller, ckerc20_tx_fee, None)
            .call_ckerc20_ledger_approve_minter(ckusdc.ledger_canister_id, caller, TWO_USDC, None)
            .call_minter_withdraw_erc20(
                caller,
                CKERC20_TRANSFER_FEE - 1,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(WithdrawErc20Error::CkErc20LedgerError {
                cketh_block_index: 2_u8.into(),
                error: LedgerError::AmountTooLow {
                    minimum_burn_amount: CKERC20_TRANSFER_FEE.into(),
                    failed_burn_amount: Nat::from(CKERC20_TRANSFER_FEE - 1),
                    token_symbol: "ckUSDC".to_string(),
                    ledger_id: ckusdc.ledger_canister_id,
                },
            });
    }
```
