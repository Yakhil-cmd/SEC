### Title
Fixed `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` Causes Permanent Withdrawal Failure for Non-Standard ERC-20 Tokens - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter hardcodes a single gas limit of `65_000` for every ckERC20 → ERC-20 withdrawal transaction, regardless of the actual gas requirements of the specific ERC-20 token contract. For tokens whose `transfer` function consumes more than 65,000 gas (e.g., fee-on-transfer tokens, rebasing tokens, tokens with hooks or complex accounting), every withdrawal transaction will revert on Ethereum with an out-of-gas error. The user's ckERC20 tokens are reimbursed after each failure, but the ckETH burned to pay the gas fee is **not** reimbursed, causing repeated financial loss and making it permanently impossible to convert those ckERC20 tokens back to their underlying ERC-20 on Ethereum.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two constants define the gas limits for all withdrawal transactions:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The `estimate_gas_limit` function unconditionally returns `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` for every ckERC20 withdrawal, with no per-token override:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This limit is then embedded directly into the EIP-1559 transaction submitted to Ethereum: [3](#0-2) 

The documentation explicitly acknowledges the assumption: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* [4](#0-3) 

When a withdrawal transaction fails on Ethereum (e.g., out-of-gas), the ckERC20 tokens are reimbursed to the user, but the documentation explicitly states: *"Overcharged transaction fees are not reimbursed."* [5](#0-4) 

The reimbursement logic confirms: on `TransactionStatus::Failure`, ckERC20 tokens are returned, but the ckETH gas fee is consumed and not returned: [6](#0-5) 

The `withdraw_erc20` endpoint is publicly callable by any non-anonymous principal: [7](#0-6) 

---

### Impact Explanation

**Impact: Medium-High**

For any ckERC20 token whose underlying ERC-20 `transfer` function requires more than 65,000 gas (e.g., fee-on-transfer tokens, rebasing tokens, tokens with transfer hooks, or tokens with complex internal accounting), every withdrawal transaction submitted to Ethereum will revert with out-of-gas. The consequences are:

1. **Permanent withdrawal unavailability**: The user can never convert their ckERC20 tokens back to the underlying ERC-20 on Ethereum. The tokens are not permanently locked on IC (they can still be transferred between IC accounts), but the bridge exit is permanently broken for that token type.
2. **Repeated ckETH financial loss**: Each failed withdrawal attempt burns ckETH for the gas fee. Since the ckETH fee is not reimbursed on failure, a user who retries will lose ckETH on every attempt with no recourse.
3. **No escape hatch**: There is no per-token gas limit override, no governance parameter to adjust the limit, and no mechanism for the user to specify a higher gas limit.

---

### Likelihood Explanation

**Likelihood: Low**

Most standard ERC-20 tokens (USDC, USDT, DAI, WBTC) use well under 65,000 gas for a `transfer` call. However, the ckERC20 system is designed to be extensible — any ERC-20 token can be added via a governance proposal. Tokens with non-trivial transfer logic (fee-on-transfer, rebasing, ERC-777 hooks, tokens that update multiple storage slots) routinely exceed 65,000 gas. As the set of supported ckERC20 tokens grows, the probability of including such a token increases. A user who deposits such a token into the bridge is then unable to withdraw it.

---

### Recommendation

1. **Make the gas limit configurable per token**: Store a `gas_limit` field in the `CkErc20Token` state struct, set at token registration time via the `add_ckerc20_token` endpoint.
2. **Allow governance upgrades**: Expose an upgrade parameter to adjust the gas limit for an existing token if the initial estimate proves insufficient.
3. **Alternatively, use on-chain gas estimation**: Before submitting the withdrawal transaction, use `eth_estimateGas` (via the EVM RPC canister) to determine the actual gas required for the specific token's `transfer` call, and use that value (with a safety margin) as the gas limit.

---

### Proof of Concept

1. A governance proposal adds a ckERC20 token whose underlying ERC-20 `transfer` function consumes 90,000 gas (e.g., a fee-on-transfer token that updates a fee recipient's balance and emits multiple events).
2. A user deposits 1,000 units of this token via the ERC-20 helper contract and receives 1,000 ckTOKEN on IC.
3. The user calls `withdraw_erc20` on the ckETH minter, approving the required ckETH for gas fees.
4. The minter burns the ckETH gas fee and queues the withdrawal.
5. `create_transactions_batch` calls `estimate_gas_limit`, which returns `GasAmount::new(65_000)` for all ckERC20 requests.
6. The minter submits an EIP-1559 transaction to Ethereum with `gas_limit: 65_000`.
7. The Ethereum transaction reverts with out-of-gas (the token's `transfer` needed 90,000 gas).
8. The minter detects `TransactionStatus::Failure` and reimburses the 1,000 ckTOKEN to the user.
9. The ckETH gas fee is **not** reimbursed — the user has lost it permanently.
10. Every subsequent withdrawal attempt repeats steps 3–9, draining the user's ckETH balance with no possibility of success.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L296-301)
```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1102-1110)
```rust
}

/// Creates an EIP-1559 transaction for the given withdrawal request.
/// The transaction fees are paid by the beneficiary,
/// meaning that the fees will be deducted from the withdrawal amount.
///
/// # Errors
/// * `CreateTransactionError::InsufficientTransactionFee` if the ETH withdrawal amount does not cover the transaction fee.
pub fn create_transaction(
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1169-1184)
```rust
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: gas_fee_estimate.max_priority_fee_per_gas,
                max_fee_per_gas: request_max_fee_per_gas,
                gas_limit,
                destination: request.erc20_contract_address,
                amount: Wei::ZERO,
                data: TransactionCallData::Erc20Transfer {
                    to: request.destination,
                    value: request.withdrawal_amount,
                }
                .encode(),
                access_list: Default::default(),
            })
        }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-432)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
    validate_ckerc20_active();
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawErc20Error::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
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
```
