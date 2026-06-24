### Title
`eip_1559_transaction_price` Returns ckETH Gas Limit When Passed the ckETH Ledger ID as a ckERC20 Token - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `eip_1559_transaction_price` query on the ckETH minter canister accepts an optional `ckerc20_ledger_id` parameter to distinguish between ckETH and ckERC20 withdrawal fee estimates. When the caller passes the ckETH ledger's own principal ID as the `ckerc20_ledger_id`, the function silently returns the **ckETH gas limit** (`21,000`) instead of the ckERC20 gas limit (`65,000`). This causes the returned `max_transaction_fee` to be **~3× too low** for a ckERC20 withdrawal. A user who relies on this underestimated fee to approve the minter will have their ckETH burned but the ckERC20 withdrawal will fail due to insufficient fee, resulting in a net loss of ckETH (minus the penalty fee).

---

### Finding Description

In `rs/ethereum/cketh/minter/src/main.rs`, the `eip_1559_transaction_price` query function selects the gas limit based on the supplied `ckerc20_ledger_id`:

```rust
let gas_limit = match token {
    None => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,                    // 21_000
    Some(Eip1559TransactionPriceArg { ckerc20_ledger_id }) => {
        match read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id)) {
            Some(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,       // 65_000
            None => {
                if ckerc20_ledger_id == read_state(|s| s.cketh_ledger_id) {
                    CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT             // 21_000 ← wrong branch
                } else {
                    ic_cdk::trap(...)
                }
            }
        }
    }
};
```

The intent of the `None` inner branch is to allow callers to pass the ckETH ledger ID to get the ckETH price estimate. However, the `Eip1559TransactionPriceArg` struct is documented as:

> *"When specified, it is to lookup transaction price for a ckERC20 token withdrawal."*

A user following the ckERC20 withdrawal flow is instructed to call `eip_1559_transaction_price` with their ckERC20 ledger ID to determine how much ckETH to approve. If they accidentally (or through a buggy client) pass the ckETH ledger ID instead of the ckERC20 ledger ID, the function returns a `max_transaction_fee` computed with `gas_limit = 21_000` instead of `65_000`. The returned `max_transaction_fee` is then used by the user to call `icrc2_approve` on the ckETH ledger. When `withdraw_erc20` is subsequently called, the minter internally calls `estimate_erc20_transaction_fee()` which always uses `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`:

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

The minter will attempt to burn `erc20_tx_fee` (computed with `65_000` gas) from the user's ckETH allowance, but the user only approved ~`21_000/65_000 ≈ 32%` of the required amount. The ckETH burn fails with `InsufficientAllowance`, and the withdrawal is rejected — but the user has already paid the ckETH ledger transaction fee for the failed `icrc2_approve` call.

The analog to the original report is direct: just as `VeryFastRouter.swap()` could mix ERC20 token amounts into an ETH accumulator when `isETHSell` is set to the wrong value, here the `eip_1559_transaction_price` query mixes the ckETH gas limit into a response that the caller intends to use for a ckERC20 withdrawal, because the token-type discriminator (`ckerc20_ledger_id`) is not validated to be a *ckERC20* ledger — it silently accepts the ckETH ledger ID and returns the wrong gas limit.

---

### Impact Explanation

A user who calls `eip_1559_transaction_price` with the ckETH ledger ID (instead of a ckERC20 ledger ID) receives a `max_transaction_fee` that is approximately **3× too low** (21,000 vs 65,000 gas units). If the user uses this value to approve the minter and then calls `withdraw_erc20`, the minter's internal fee estimate will exceed the user's allowance. The ckETH burn will fail, the withdrawal will be rejected, and the user loses the ckETH ledger `icrc2_approve` transaction fee. Additionally, if the user had already approved a large amount and the minter's fee estimate happens to fit within the allowance (e.g., during low-gas periods), the minter may successfully burn ckETH but then fail to construct a valid ERC-20 transaction due to the fee mismatch, leading to a `FailedErc20WithdrawalRequest` reimbursement event that deducts `CKETH_LEDGER_TRANSACTION_FEE` as a penalty.

**Severity: Low-Medium** — direct financial loss is bounded to ledger fees and the penalty deduction, but the incorrect fee estimate is a chain-fusion accounting bug reachable by any unprivileged ingress caller.

---

### Likelihood Explanation

Any user or wallet application that calls `eip_1559_transaction_price` with the wrong ledger ID — including the ckETH ledger ID itself (which is a documented valid input per the code at line 179) — will receive a silently incorrect fee estimate. The ckETH ledger ID is publicly known and easily discoverable via `get_minter_info`. A confused user or a buggy client library could trivially trigger this path. The existing test at `rs/ethereum/cketh/minter/tests/cketh.rs:216-218` explicitly demonstrates that passing the ckETH ledger ID is a supported and tested code path, increasing the likelihood that real users will rely on it for ckERC20 fee estimation.

---

### Recommendation

The `eip_1559_transaction_price` function should reject the call (via `ic_cdk::trap`) when the supplied `ckerc20_ledger_id` matches the ckETH ledger ID and the caller's intent is to estimate a ckERC20 withdrawal fee. Alternatively, the function should be split into two separate endpoints — one for ckETH and one for ckERC20 — to eliminate the ambiguity. At minimum, the documentation and the fallback branch should be updated to make clear that passing the ckETH ledger ID as a `ckerc20_ledger_id` is not a valid way to estimate ckERC20 withdrawal fees.

---

### Proof of Concept

1. Attacker/user calls the `eip_1559_transaction_price` query on the ckETH minter canister, passing `ckerc20_ledger_id = <ckETH ledger principal>`.
2. The function hits the branch at line 179: `if ckerc20_ledger_id == read_state(|s| s.cketh_ledger_id)` → `true`.
3. `gas_limit` is set to `CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 21_000`.
4. The returned `max_transaction_fee = max_fee_per_gas × 21_000` — approximately 3× lower than the correct `65_000`-gas ckERC20 fee.
5. User calls `icrc2_approve` on the ckETH ledger with this underestimated amount.
6. User calls `withdraw_erc20` on the minter.
7. Minter calls `estimate_erc20_transaction_fee()` internally, which uses `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`, producing a fee ~3× larger than the user's allowance.
8. The ckETH `burn_from` fails with `InsufficientAllowance`; the withdrawal is rejected; the user loses the ckETH ledger fee paid for the `icrc2_approve` call.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L170-198)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L429-432)
```rust
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```
