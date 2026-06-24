### Title
`withdraw_erc20` Lacks Caller-Side Maximum Fee Bound for ckETH Gas Burn - (`File: rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `withdraw_erc20` endpoint on the ckETH minter computes and immediately burns an Ethereum gas fee (`erc20_tx_fee`) from the caller's ckETH balance based on the current live gas price estimate, without accepting any caller-provided maximum fee bound. Because Ethereum gas prices are volatile, a caller cannot protect themselves from having more ckETH burned than they anticipated.

---

### Finding Description

In `withdraw_erc20()`, the minter internally calls `estimate_erc20_transaction_fee()`, which fetches the current gas fee estimate and computes `gas_fee_estimate.to_price(CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT).max_transaction_fee()`. This value is then immediately burned from the caller's ckETH account via `cketh_ledger.burn_from(cketh_account, erc20_tx_fee, ...)`: [1](#0-0) 

The `erc20_tx_fee` is computed as: [2](#0-1) 

The `WithdrawErc20Arg` struct accepted by the endpoint contains no `max_fee` or `max_transaction_fee` field: [3](#0-2) 

Confirmed in the Candid interface: [4](#0-3) 

The minter documentation explicitly acknowledges the fee is variable:

> "The exact amount of ckETH needed depends on the current Ethereum gas price, which can greatly fluctuate." [5](#0-4) 

By contrast, for `withdraw_eth`, the user burns a fixed `amount` of ckETH upfront and the fee is deducted from that amount — the user knows exactly how much ckETH leaves their account. For `withdraw_erc20`, the ckETH gas fee is a separate, unbounded burn computed at execution time.

The gas fee estimate is derived from `base_fee_per_gas` of the last finalized Ethereum block, scaled as `2 * base_fee_per_gas + max_priority_fee_per_gas`: [6](#0-5) 

---

### Impact Explanation

A user who queries `eip_1559_transaction_price` to estimate the fee before calling `withdraw_erc20` may have significantly more ckETH burned than expected if Ethereum gas prices spike between the query and execution. The user has no mechanism to abort the call if the fee exceeds their tolerance. The ICRC-2 allowance provides a hard ceiling (the burn fails if allowance is insufficient), but the user must pre-approve a large allowance (the documentation recommends 1 ETH) to avoid repeated approvals, meaning the allowance ceiling provides no practical per-call protection. This results in unexpected ckETH burns from user accounts.

---

### Likelihood Explanation

Ethereum gas prices are well-known to be volatile, with spikes of 2–10× within minutes during network congestion. The IC consensus round introduces a non-trivial delay between when a user queries the price and when `withdraw_erc20` executes. Any unprivileged user calling `withdraw_erc20` is exposed to this risk on every call. The entry path is a standard ingress call to the ckETH minter canister, requiring no special privileges.

---

### Recommendation

Add an optional `max_transaction_fee : opt nat` field to `WithdrawErc20Arg`. If provided, the minter should reject the call (returning a new `WithdrawErc20Error::FeeTooHigh` variant) if `erc20_tx_fee > max_transaction_fee`, before any ckETH burn is attempted. This mirrors the existing `Eip1559TransactionPrice.max_transaction_fee` field already exposed via the query endpoint, giving callers a natural way to enforce a bound. [7](#0-6) 

---

### Proof of Concept

1. User queries `eip_1559_transaction_price` (for the ckUSDC ledger) and observes `max_transaction_fee = X` wei.
2. User calls `icrc2_approve` on the ckETH ledger, approving the minter for `X + buffer`.
3. Ethereum gas prices spike 3× before the `withdraw_erc20` ingress message is processed.
4. `estimate_erc20_transaction_fee()` returns `3X`.
5. The minter burns `3X` ckETH from the user's account — 3× more than the user anticipated.
6. The user has no recourse; the burn is irreversible (overcharged transaction fees are not reimbursed per the documented behavior). [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-451)
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

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L6-13)
```rust
#[derive(CandidType, Deserialize)]
pub struct WithdrawErc20Arg {
    pub amount: Nat,
    pub ckerc20_ledger_id: Principal,
    pub recipient: String,
    pub from_cketh_subaccount: Option<Subaccount>,
    pub from_ckerc20_subaccount: Option<Subaccount>,
}
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L161-180)
```text
type Eip1559TransactionPrice = record {
    // Maximum amount of gas transaction is authorized to consume.
    gas_limit : nat;

    // Maximum amount of Wei per gas unit that the transaction is willing to pay in total.
    // This covers the base fee determined by the network and the `max_priority_fee_per_gas`.
    max_fee_per_gas : nat;

    // Maximum amount of Wei per gas unit that the transaction gives to miners
    // to incentivize them to include their transaction (priority fee).
    max_priority_fee_per_gas : nat;

    // Maximum amount of Wei that can be charged for the transaction,
    // computed as `max_fee_per_gas * gas_limit`
    max_transaction_fee : nat;

    // Timestamp of when the price was estimated.
    // Nanoseconds since the UNIX epoch.
    timestamp : opt nat64;
};
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L384-401)
```text
type WithdrawErc20Arg = record {
    // Amount of tokens to withdraw.
    // The amount is in the smallest unit of the token, e.g.,
    // ckUSDC uses 6 decimals and so to withdraw 1 ckUSDC, the amount should be 1_000_000.
    amount : nat;

    // The ledger ID for that ckERC20 token.
    ckerc20_ledger_id : principal;

    // Ethereum address to withdraw to.
    recipient : text;

    // The subaccount to burn ckETH from to pay for the transaction fee.
    from_cketh_subaccount : opt Subaccount;

    // The subaccount to burn ckERC20 from.
    from_ckerc20_subaccount : opt Subaccount;
};
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L241-241)
```text
1. The user calls the ckETH ledger to approve the minter to burn some of the user's ckETH tokens to pay for the transaction fees. The exact amount of ckETH needed depends on the current Ethereum gas price, which can greatly fluctuate. The following example approves the minter for 1 ETH, which could potentially allow for multiple withdrawals without having to approve the minter each time.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/tx.rs (L516-536)
```rust
impl GasFeeEstimate {
    pub fn checked_estimate_max_fee_per_gas(&self) -> Option<WeiPerGas> {
        self.base_fee_per_gas
            .checked_mul(2_u8)
            .and_then(|base_fee_estimate| {
                base_fee_estimate.checked_add(self.max_priority_fee_per_gas)
            })
    }

    pub fn estimate_max_fee_per_gas(&self) -> WeiPerGas {
        self.checked_estimate_max_fee_per_gas()
            .unwrap_or(WeiPerGas::MAX)
    }

    pub fn to_price(self, gas_limit: GasAmount) -> TransactionPrice {
        TransactionPrice {
            gas_limit,
            max_fee_per_gas: self.estimate_max_fee_per_gas(),
            max_priority_fee_per_gas: self.max_priority_fee_per_gas,
        }
    }
```
