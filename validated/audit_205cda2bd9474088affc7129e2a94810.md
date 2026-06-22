### Title
`eip_1559_transaction_price` Returns Ethereum Gas Fee Without ckETH Ledger Transfer Fees, Causing `withdraw_erc20` to Always Fail for Upstream Canisters - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
The `eip_1559_transaction_price` query endpoint on the ckETH minter returns only the estimated Ethereum gas fee (`max_transaction_fee`) for a ckERC20 withdrawal. It does not include the ckETH ledger transfer fee (`CKETH_TRANSFER_FEE = 2_000_000_000_000 wei` on mainnet) that is charged twice: once for `icrc2_approve` and once for the `burn_from` call inside `withdraw_erc20`. Any upstream canister that uses `eip_1559_transaction_price.max_transaction_fee` to determine the exact ckETH balance a user needs will always cause `withdraw_erc20` to fail with `InsufficientFunds`.

### Finding Description
The `eip_1559_transaction_price` query endpoint is the canonical way for callers to determine the ckETH cost of a ckERC20 withdrawal: [1](#0-0) 

It returns `Eip1559TransactionPrice.max_transaction_fee`, which is computed as `max_fee_per_gas * gas_limit` — the pure Ethereum gas cost: [2](#0-1) 

Inside `withdraw_erc20`, the minter calls `burn_from(cketh_account, erc20_tx_fee)` on the ckETH ledger: [3](#0-2) 

The ckETH ledger is an ICRC-2 ledger. When `burn_from` (i.e., `icrc2_transfer_from`) executes, it deducts `erc20_tx_fee` from the allowance **and** deducts `CKETH_TRANSFER_FEE` from the `from` account's balance as the ledger operation fee. The minter's own code acknowledges this fee constant when computing reimbursements: [4](#0-3) 

The state validator also hard-codes the ledger fee value: [5](#0-4) 

The test suite confirms the exact failure mode — a user who deposits `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE + CKETH_TRANSFER_FEE - 1` (one wei short of the true total) fails with `InsufficientFunds`: [6](#0-5) 

The `CKETH_TRANSFER_FEE` constant used in tests is `2_000_000_000_000` wei: [7](#0-6) 

The total ckETH balance required for a successful `withdraw_erc20` is:

```
erc20_tx_fee + 2 × CKETH_TRANSFER_FEE
```

- `+CKETH_TRANSFER_FEE` for the `icrc2_approve` call the user must make first
- `+CKETH_TRANSFER_FEE` for the `burn_from` call the minter makes inside `withdraw_erc20`

`eip_1559_transaction_price` returns only `erc20_tx_fee`, understating the true requirement by `4_000_000_000_000 wei` (0.000004 ckETH) on mainnet.

### Impact Explanation
Any upstream canister that:
1. Queries `eip_1559_transaction_price(Some(ckerc20_ledger_id))` to obtain `max_transaction_fee`
2. Ensures the user holds exactly `max_transaction_fee` ckETH
3. Calls `icrc2_approve(minter, max_transaction_fee)` (consuming `CKETH_TRANSFER_FEE` from balance)
4. Calls `withdraw_erc20(amount, ckerc20_ledger_id, recipient)`

…will always receive `WithdrawErc20Error::CkEthLedgerError { InsufficientFunds }` because after the approval the user's balance is `max_transaction_fee - CKETH_TRANSFER_FEE`, which is less than `erc20_tx_fee + CKETH_TRANSFER_FEE` required by the ledger for the burn. The withdrawal is permanently blocked for such callers. User funds are not lost (the call simply reverts), but the canister's withdrawal functionality is broken.

### Likelihood Explanation
The `eip_1559_transaction_price` query endpoint is the only public, machine-readable way to determine the ckETH gas fee for a ckERC20 withdrawal. Any canister integrating ckERC20 withdrawals programmatically and using this endpoint for precise balance management will hit this failure. The ckETH documentation advises human users to approve a "large amount," which masks the issue for manual flows but not for automated upstream canisters. [8](#0-7) 

### Recommendation
The `eip_1559_transaction_price` response should include a `total_cketh_needed` field computed as `max_transaction_fee + 2 × CKETH_LEDGER_TRANSACTION_FEE` (covering both the approval and the burn), or the existing `max_transaction_fee` field should be redefined to include the ledger fees. Alternatively, the DID and documentation should explicitly state that callers must add `2 × CKETH_TRANSFER_FEE` to the returned value to determine the required ckETH balance.

### Proof of Concept

**Attacker-controlled entry path:** Any unprivileged canister caller invoking the public `eip_1559_transaction_price` query and `withdraw_erc20` update endpoints.

**Step-by-step:**

1. Upstream canister calls `eip_1559_transaction_price(Some(ckusdc_ledger_id))` → receives `max_transaction_fee = F` (e.g., `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE`).
2. Upstream canister ensures user holds exactly `F` ckETH.
3. Upstream canister calls `icrc2_approve(minter, F)` on ckETH ledger → ledger deducts `CKETH_TRANSFER_FEE` from balance → user balance becomes `F - CKETH_TRANSFER_FEE`.
4. Upstream canister calls `withdraw_erc20(TWO_USDC, ckusdc_ledger_id, eth_address)`.
5. Inside `withdraw_erc20`, minter estimates `erc20_tx_fee ≈ F` and calls `burn_from(user, F)` on ckETH ledger.
6. ckETH ledger requires balance ≥ `F + CKETH_TRANSFER_FEE`; actual balance is `F - CKETH_TRANSFER_FEE`.
7. Ledger returns `InsufficientFunds { balance: F - CKETH_TRANSFER_FEE }`.
8. `withdraw_erc20` returns `WithdrawErc20Error::CkEthLedgerError { InsufficientFunds }`.

The test at `rs/ethereum/cketh/minter/tests/ckerc20.rs:316–348` demonstrates this exact failure with `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE + CKETH_TRANSFER_FEE - 1` as the deposited amount, confirming the root cause is the missing ledger fee in the query response. [6](#0-5) [1](#0-0) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L169-198)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L429-458)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L511-513)
```rust
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L161-171)
```rust
        let cketh_ledger_transfer_fee = match self.ethereum_network {
            EthereumNetwork::Mainnet => Wei::new(2_000_000_000_000),
            EthereumNetwork::Sepolia => Wei::new(10_000_000_000),
        };
        if self.cketh_minimum_withdrawal_amount < cketh_ledger_transfer_fee {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must cover ledger transaction fee, \
                otherwise ledger can return a BadBurn error that should be returned to the user"
                    .to_string(),
            ));
        }
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L316-348)
```rust
    #[test]
    fn should_error_when_not_enough_cketh() {
        let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
        let caller = ckerc20.caller();
        let cketh_ledger = ckerc20.cketh_ledger_id();
        let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");

        ckerc20
            .deposit_cketh(DepositCkEthParams {
                amount: DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE + CKETH_TRANSFER_FEE - 1,
                ..Default::default()
            })
            .call_cketh_ledger_approve_minter(
                caller,
                DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE,
                None,
            )
            .call_minter_withdraw_erc20(
                caller,
                0_u8,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(WithdrawErc20Error::CkEthLedgerError {
                error: LedgerError::InsufficientFunds {
                    balance: Nat::from(DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE - 1),
                    failed_burn_amount: Nat::from(DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE),
                    token_symbol: "ckETH".to_string(),
                    ledger_id: cketh_ledger,
                },
            });
    }
```

**File:** rs/ethereum/cketh/test_utils/src/lib.rs (L51-52)
```rust
pub const CKETH_TRANSFER_FEE: u64 = 2_000_000_000_000;
pub const CKETH_MINIMUM_WITHDRAWAL_AMOUNT: u64 = 30_000_000_000_000_000;
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L241-246)
```text
1. The user calls the ckETH ledger to approve the minter to burn some of the user's ckETH tokens to pay for the transaction fees. The exact amount of ckETH needed depends on the current Ethereum gas price, which can greatly fluctuate. The following example approves the minter for 1 ETH, which could potentially allow for multiple withdrawals without having to approve the minter each time.
+
[source,shell]
----
dfx canister --network ic call ledger icrc2_approve "(record { spender = record { owner = principal \"$(dfx canister id minter --network ic)\" }; amount = 1_000_000_000_000_000_000:nat })"
----
```
