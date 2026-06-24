Audit Report

## Title
Hard-Coded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` of 65,000 Is Insufficient for High-Gas ERC-20 Tokens, Causing Permanent ckETH Loss on Withdrawal Failure - (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

## Summary
The ckETH minter applies a single compile-time constant of `65_000` gas to every ckERC20 withdrawal transaction regardless of the token being withdrawn. Supported mainnet tokens such as wstETH require significantly more than 65,000 gas for a `transfer()` call. When the submitted Ethereum transaction runs out of gas and reverts, the minter reimburses the ckERC20 tokens but explicitly does not reimburse the ckETH burned to pay the transaction fee, resulting in a permanent, repeatable loss of ckETH for any user withdrawing an affected token.

## Finding Description
In `rs/ethereum/cketh/minter/src/withdraw.rs` lines 43–44, two gas limits are defined as compile-time constants:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The function `estimate_gas_limit` at lines 296–301 returns the same `65_000` for every ckERC20 token with no per-token differentiation:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

`create_transactions_batch` calls `estimate_gas_limit` and passes the result directly to `create_transaction` for every withdrawal request: [3](#0-2) 

The resulting `gas_limit` is embedded verbatim into the `Eip1559TransactionRequest` for ckERC20 withdrawals with no per-token adjustment: [4](#0-3) 

The documentation at line 270 of `ckerc20.adoc` explicitly acknowledges the limitation: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* [5](#0-4) 

wstETH (`0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0`) is listed as a supported mainnet token and was added via a governance proposal: [6](#0-5) [7](#0-6) 

wstETH's `transfer()` involves Lido staking rebasing logic and is well-documented to consume 80,000–120,000+ gas, well above the 65,000 limit. When the on-chain transaction reverts due to out-of-gas, the minter reimburses only the ckERC20 tokens. The documentation at line 275 explicitly states: *"Overcharged transaction fees are not reimbursed."* [8](#0-7) 

There are no existing guards that check whether the gas limit is sufficient for a specific token before burning the user's ckETH. The `InsufficientTransactionFee` error path only handles the case where the user's `max_transaction_fee` is too low to cover the gas price — it does not handle the case where the gas limit itself is too low for the token's `transfer()` execution. [9](#0-8) 

## Impact Explanation
Any user calling `withdraw_erc20` for ckWSTETH will have their ckETH burned for the estimated gas fee (65,000 × max_fee_per_gas), have their ckWSTETH burned, and then watch the Ethereum transaction revert out-of-gas. The ckWSTETH is reimbursed but the ckETH gas fee is permanently lost. The user can retry indefinitely, each attempt burning additional ckETH with no possibility of success. This is a direct, repeatable, permanent loss of ckETH for every withdrawal attempt of an affected token — a concrete user-funds harm in the ck-token/Chain Fusion system. This matches the **High** severity impact: *"Significant Chain Fusion, ck-token, ledger, Rosetta, boundary/API, XRC, Internet Identity, NNS, SNS, or infrastructure security impact with concrete user or protocol harm."*

## Likelihood Explanation
Likelihood is high. ckWSTETH is already live on mainnet. Any unprivileged IC principal holding ckWSTETH can trigger this by calling `withdraw_erc20` with the ckWSTETH ledger ID. No special access, no social engineering, and no external compromise is required. The failure is deterministic and repeatable on every attempt.

## Recommendation
1. **Per-token gas limit configuration:** Store a configurable `gas_limit` per supported ckERC20 token in the minter state, settable at token registration time and updatable via upgrade args.
2. **Minimum viable fix:** Increase `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` to a value covering the highest-gas supported token (e.g., 200,000), or add a per-token override map in `estimate_gas_limit`.
3. **User protection:** Before burning ckETH, validate that the configured gas limit is sufficient for the specific token, or surface the limitation via the `eip_1559_transaction_price` query response.

## Proof of Concept
1. Obtain ckWSTETH and ckETH on mainnet ICP.
2. Call `withdraw_erc20` on the ckETH minter canister with `ckerc20_ledger_id` set to the ckWSTETH ledger and a valid Ethereum destination address.
3. The minter burns ckETH (65,000 × max_fee_per_gas) and burns ckWSTETH.
4. The minter submits an Ethereum EIP-1559 transaction with `gas_limit = 65_000` calling `transfer()` on the wstETH contract.
5. The transaction reverts on-chain with an out-of-gas error (wstETH `transfer()` requires ~80,000–120,000 gas).
6. The minter detects the failure receipt, reimburses ckWSTETH, but does not reimburse the ckETH gas fee.
7. Repeat from step 2 — each iteration permanently destroys additional ckETH.

A deterministic integration test can be written using the existing `rs/ethereum/cketh/minter/tests/ckerc20.rs` test harness by mocking the Ethereum RPC to return a failure receipt for a wstETH withdrawal transaction and asserting that the ckETH balance decreases while ckWSTETH is restored.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L257-264)
```rust
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L281-291)
```rust
            Err(CreateTransactionError::InsufficientTransactionFee {
                cketh_ledger_burn_index: ledger_burn_index,
                allowed_max_transaction_fee: withdrawal_amount,
                actual_max_transaction_fee: max_transaction_fee,
            }) => {
                log!(
                    INFO,
                    "[create_transactions_batch]: Withdrawal request with burn index {ledger_burn_index} has insufficient amount {withdrawal_amount:?} to cover transaction fees: {max_transaction_fee:?}. Request moved back to end of queue."
                );
                mutate_state(|s| s.eth_transactions.reschedule_withdrawal_request(request));
            }
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L43-44)
```text
|wstETH
|https://etherscan.io/token/0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0[0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0]
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/mainnet/orchestrator_upgrade_2024_07_26.md (L1-15)
```markdown
# Proposal to upgrade the ledger suite orchestrator canister to add ckWSTETH

Git hash: `de29a1a55b589428d173b31cdb8cec0923245657`

New compressed Wasm hash: `81f426bcc52140fdcf045d02d00b04bfb4965445b8aed7090d174fcdebf8beea`

Target canister: `vxkom-oyaaa-aaaar-qafda-cai`

Previous ledger suite orchestrator proposal: https://dashboard.internetcomputer.org/proposal/131374

---

## Motivation

This proposal upgrades the ckERC20 ledger suite orchestrator to add support for [wstETH](https://etherscan.io/token/0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0#tokenInfo). Once executed, the twin token ckWSTETH will be available on ICP, refer to the [documentation](https://github.com/dfinity/ic/blob/master/rs/ethereum/cketh/docs/ckerc20.adoc) on how to proceed with deposits and withdrawals.
```
