### Title
Hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` Causes ckERC20 Withdrawals to Be Permanently Stuck — (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter uses a compile-time constant `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000` for every ckERC20 → ERC-20 withdrawal transaction. There is no admin or governance mechanism to adjust this value without a full NNS canister upgrade. If any supported ERC-20 token's `transfer` function requires more than 65,000 gas (due to complex contract logic, Ethereum opcode repricing hard forks, or token-specific features), every withdrawal transaction for that token will revert on Ethereum. The minter will resubmit with higher fees (RBF) but the same gas limit indefinitely, leaving user funds stuck.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two gas limits are declared as immutable `pub const` values:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The function `estimate_gas_limit` dispatches on the withdrawal type and always returns the hardcoded constant for ckERC20:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This `gas_limit` is passed directly into `create_transaction` for every ckERC20 withdrawal in `create_transactions_batch`: [3](#0-2) 

The official documentation explicitly acknowledges the assumption: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* [4](#0-3) 

The ckETH minter's `UpgradeArgs` (the only runtime-configurable path) contains no field for `gas_limit`, meaning the only way to change this value is a full NNS governance proposal to upgrade the canister Wasm — a process that takes days.

When a withdrawal transaction is submitted to Ethereum with `gas_limit = 65_000` and the ERC-20 `transfer` call consumes more gas, the transaction is mined but reverts (out-of-gas). The resubmission loop in `resubmit_transactions_batch` will keep bumping the fee (RBF) but reuses the same gas limit from the stored transaction, so every resubmission also reverts. The user's ckERC20 tokens were already burned at the start of the withdrawal; they are locked until the minter is upgraded.

---

### Impact Explanation

- **Stuck withdrawals / locked user funds**: Any ckERC20 token whose `transfer` function exceeds 65,000 gas will have all withdrawal requests permanently stuck. The user's ckERC20 tokens are burned on the IC ledger but the corresponding ERC-20 tokens are never delivered.
- **No runtime escape hatch**: Unlike the ckBTC minter (which exposes `retrieve_btc_min_amount`, `check_fee`, etc. as upgradeable fields), the ckETH minter exposes no `gas_limit` field in `UpgradeArgs`. Recovery requires an NNS proposal, canister upgrade, and re-processing of stuck requests — a multi-day process.
- **Cascading nonce blockage**: Because the minter processes withdrawals sequentially by nonce, a stuck transaction for one token can block all subsequent ckERC20 withdrawals for all tokens.

---

### Likelihood Explanation

- The ckETH minter already supports USDC, USDT, and other ERC-20 tokens. USDT's `transfer` includes a blacklist check; complex tokens (ERC-777, fee-on-transfer, rebasing) routinely exceed 65,000 gas.
- Ethereum hard forks have historically repriced opcodes (EIP-2929 raised `SLOAD` from 200 to 2,100 gas), which can push previously-safe contracts over the limit without any code change.
- The ckETH minter has already experienced a real stuck-withdrawal incident (June 2025) caused by a different hardcoded fee parameter (`minimum_fee_per_vbyte` in the ckBTC minter), demonstrating that hardcoded operational parameters in chain-fusion minters are a proven failure mode. [5](#0-4) 

---

### Recommendation

1. **Make `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` a per-token configurable field** stored in the minter's state alongside each supported ERC-20 token's metadata.
2. **Expose a setter in `UpgradeArgs`** (or a dedicated admin endpoint) so the NNS or a privileged role can update the gas limit for a specific token without a full canister upgrade — mirroring how `retrieve_btc_min_amount` and `check_fee` are handled in the ckBTC minter. [6](#0-5) 

3. **Add a circuit-breaker**: if a transaction receipt shows `status = 0` (revert) and `gasUsed ≈ gasLimit`, detect the out-of-gas condition and immediately reimburse the user rather than resubmitting indefinitely.

---

### Proof of Concept

1. A new ERC-20 token is added to the ckETH minter (e.g., a rebasing token whose `transfer` costs ~80,000 gas).
2. A user calls `withdraw_erc20` on the ckETH minter. The minter burns the user's ckERC20 tokens on the IC ledger.
3. `create_transactions_batch` calls `estimate_gas_limit`, which returns the hardcoded `65_000`.
4. The signed Ethereum transaction is submitted with `gas_limit = 65_000`.
5. The ERC-20 `transfer` reverts on Ethereum (out of gas). The receipt shows `status = 0`, `gasUsed = 65_000`.
6. `resubmit_transactions_batch` creates a replacement transaction with a higher fee but the **same** `gas_limit = 65_000` (copied from the stored transaction).
7. Every resubmission also reverts. The withdrawal is permanently stuck. The user's ckERC20 tokens are burned with no ERC-20 delivered.
8. Recovery requires an NNS governance proposal to upgrade the minter canister — a multi-day process — while user funds remain locked. [1](#0-0) [2](#0-1) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L208-247)
```rust
async fn resubmit_transactions_batch(
    latest_transaction_count: Option<TransactionCount>,
    gas_fee_estimate: &GasFeeEstimate,
) {
    if read_state(|s| s.eth_transactions.is_sent_tx_empty()) {
        return;
    }
    let latest_transaction_count = match latest_transaction_count {
        Some(latest_transaction_count) => latest_transaction_count,
        None => {
            return;
        }
    };
    let transactions_to_resubmit = read_state(|s| {
        s.eth_transactions
            .create_resubmit_transactions(latest_transaction_count, gas_fee_estimate.clone())
    });
    for result in transactions_to_resubmit {
        match result {
            Ok((withdrawal_id, transaction)) => {
                log!(
                    INFO,
                    "[resubmit_transactions_batch]: transactions to resubmit {transaction:?}"
                );
                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::ReplacedTransaction {
                            withdrawal_id,
                            transaction,
                        },
                    )
                });
            }
            Err(e) => {
                log!(INFO, "Failed to resubmit transaction: {e:?}");
            }
        }
    }
}
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-293)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        log!(DEBUG, "[create_transactions_batch]: processing {request:?}",);
        let ethereum_network = read_state(State::ethereum_network);
        let nonce = read_state(|s| s.eth_transactions.next_transaction_nonce());
        let gas_limit = estimate_gas_limit(&request);
        match create_transaction(
            &request,
            nonce,
            gas_fee_estimate.clone(),
            gas_limit,
            ethereum_network,
        ) {
            Ok(transaction) => {
                log!(
                    DEBUG,
                    "[create_transactions_batch]: created transaction {transaction:?}",
                );

                mutate_state(|s| {
                    process_event(
                        s,
                        EventType::CreatedTransaction {
                            withdrawal_id: request.cketh_ledger_burn_index(),
                            transaction,
                        },
                    );
                });
            }
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
        };
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L269-270)
```text
. The minter checks the desired destination address against the blocklist, and rejects the request if the destination is blocked.
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L19-33)
```markdown
Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L248-270)
```text
type UpgradeArgs = record {
    // The minimal amount of BTC that can be converted to ckBTC.
    // UTXOs with lower values will be ignored.
    deposit_btc_min_amount : opt nat64;

    // The minimal amount of ckBTC that the minter converts to BTC.
    retrieve_btc_min_amount : opt nat64;

    /// Maximum time in nanoseconds that a transaction should spend in the queue
    /// before being sent.
    max_time_in_queue_nanos : opt nat64;

    /// The minimum number of confirmations required for the minter to
    /// accept a Bitcoin transaction.
    min_confirmations : opt nat32;

    /// If set, overrides the current minter's operation mode.
    mode : opt Mode;

    /// The fee per Bitcoin check.
    check_fee : opt nat64;

    /// The fee paid per check by the KYT canister (deprecated, use check_fee instead).
```
