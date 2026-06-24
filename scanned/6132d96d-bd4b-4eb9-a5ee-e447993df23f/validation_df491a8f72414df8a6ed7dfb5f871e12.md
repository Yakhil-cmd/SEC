### Title
`eip_1559_transaction_price` Omits ckETH Ledger Transfer Fee, Causing Mismatch with Actual `withdraw_erc20` Cost - (`rs/ethereum/cketh/minter/src/main.rs`)

### Summary

The `eip_1559_transaction_price` query endpoint in the ckETH minter returns only the Ethereum gas fee estimate. However, when `withdraw_erc20` is executed, the minter's `burn_from` call on the ckETH ledger charges the user's ckETH balance for both the gas fee **and** the ckETH ledger transfer fee (`CKETH_LEDGER_TRANSACTION_FEE = 2_000_000_000_000 wei`). This fee is never reflected in the preview endpoint, creating a systematic mismatch between the estimated cost and the actual cost deducted from the user's ckETH balance.

### Finding Description

The `eip_1559_transaction_price` query endpoint reads the cached `last_transaction_price_estimate` and returns only Ethereum gas components: [1](#0-0) 

The returned `Eip1559TransactionPrice` struct contains only `gas_limit`, `max_fee_per_gas`, `max_priority_fee_per_gas`, and `max_transaction_fee` — all Ethereum-side values: [2](#0-1) 

When `withdraw_erc20` is called, the minter first calls `estimate_erc20_transaction_fee()` to get the gas fee, then calls `burn_from` on the ckETH ledger for that amount: [3](#0-2) 

Under ICRC-2 semantics, `burn_from` (i.e., `icrc2_transfer_from`) charges `erc20_tx_fee + cketh_ledger_transfer_fee` from the user's ckETH **balance**, while only consuming `erc20_tx_fee` from the allowance. The ledger fee is a fixed constant: [4](#0-3) 

The code itself acknowledges this fee exists — it is explicitly deducted from reimbursements when the subsequent ckERC20 burn fails: [5](#0-4) 

The `eip_1559_transaction_price` endpoint is the only public query endpoint for estimating the ckETH cost of a withdrawal: [6](#0-5) 

### Impact Explanation

Any user or integrator who calls `eip_1559_transaction_price` to determine the exact ckETH balance required for `withdraw_erc20` will compute a value that is `2_000_000_000_000 wei` (0.000002 ckETH) short of what is actually needed. If the user's ckETH balance equals exactly the returned `max_transaction_fee`, the `burn_from` call inside `withdraw_erc20` will fail with `InsufficientFunds`. The user's ckERC20 tokens are not at risk, but the withdrawal fails and the user must retry with a higher balance. This is a direct analog to the EIP-4626 preview/actual fee mismatch: the preview function omits a fee that the actual operation charges.

### Likelihood Explanation

The entry path is fully unprivileged: any user can call `eip_1559_transaction_price` (query) and then `withdraw_erc20` (update). Integrators building wallets or DeFi protocols on top of ckERC20 withdrawals are the primary affected party. The `eip_1559_transaction_price` endpoint is explicitly documented as the mechanism to estimate the ckETH cost of a withdrawal, making it the natural source of truth for approval and balance checks.

### Recommendation

The `eip_1559_transaction_price` response should include the ckETH ledger transfer fee in the total cost when queried for an ERC20 withdrawal context. Concretely, when `Eip1559TransactionPriceArg` is provided (ERC20 case), the returned `max_transaction_fee` should be `gas_fee.to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT).max_transaction_fee() + CKETH_LEDGER_TRANSACTION_FEE`. Alternatively, a separate field (e.g., `cketh_ledger_fee`) should be added to `Eip1559TransactionPrice` so callers can compute the true total ckETH balance requirement.

### Proof of Concept

1. User calls `eip_1559_transaction_price(Some({ckerc20_ledger_id}))` → receives `max_transaction_fee = X` wei.
2. User ensures their ckETH balance is exactly `X` wei and approves `X` wei to the minter via `icrc2_approve`.
3. User calls `withdraw_erc20(amount, ckerc20_ledger_id, recipient, ...)`.
4. Inside `withdraw_erc20`, `estimate_erc20_transaction_fee()` returns `X` (same cached estimate).
5. Minter calls `cketh_ledger.burn_from(cketh_account, X, ...)`.
6. The ckETH ledger (ICRC-2) deducts `X + 2_000_000_000_000` from the user's balance.
7. The call fails with `InsufficientFunds` because the user only has `X` wei — the `2_000_000_000_000 wei` ledger fee was never surfaced by the preview endpoint. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L59-59)
```rust
pub const CKETH_LEDGER_TRANSACTION_FEE: Wei = Wei::new(2_000_000_000_000_u128);
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L167-198)
```rust
/// Estimate price of EIP-1559 transaction based on the
/// `base_fee_per_gas` included in the last finalized block.
#[query]
async fn eip_1559_transaction_price(
    token: Option<Eip1559TransactionPriceArg>,
) -> Eip1559TransactionPrice {
    let gas_limit = match token {
        None => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        Some(Eip1559TransactionPriceArg { ckerc20_ledger_id }) => {
            match read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id)) {
                Some(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
                None => {
                    if ckerc20_ledger_id == read_state(|s| s.cketh_ledger_id) {
                        CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT
                    } else {
                        ic_cdk::trap(format!(
                            "ERROR: Unsupported ckERC20 token ledger {ckerc20_ledger_id}"
                        ))
                    }
                }
            }
        }
    };
    match read_state(|s| s.last_transaction_price_estimate.clone()) {
        Some((ts, estimate)) => {
            let mut result = Eip1559TransactionPrice::from(estimate.to_price(gas_limit));
            result.timestamp = Some(ts);
            result
        }
        None => ic_cdk::trap("ERROR: last transaction price estimate is not available"),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-458)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L511-513)
```rust
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L545-553)
```rust
async fn estimate_erc20_transaction_fee() -> Option<Wei> {
    lazy_refresh_gas_fee_estimate()
        .await
        .map(|gas_fee_estimate| {
            gas_fee_estimate
                .to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT)
                .max_transaction_fee()
        })
}
```

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L22-29)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct Eip1559TransactionPrice {
    pub gas_limit: Nat,
    pub max_fee_per_gas: Nat,
    pub max_priority_fee_per_gas: Nat,
    pub max_transaction_fee: Nat,
    pub timestamp: Option<u64>,
}
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L712-713)
```text
    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;
```
