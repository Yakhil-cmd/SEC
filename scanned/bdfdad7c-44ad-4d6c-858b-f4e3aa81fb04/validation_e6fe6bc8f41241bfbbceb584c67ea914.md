### Title
Hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` Understates Real-World ERC20 Transfer Costs, Causing Irrecoverable ckETH Fee Loss on Failed Withdrawals - (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter applies a single hardcoded gas limit of `65_000` to every ckERC20 withdrawal transaction, regardless of the specific ERC20 token's actual on-chain gas consumption. Supported tokens such as USDC (an upgradeable proxy) and USDT (a non-standard implementation) can require more than 65,000 gas when transferring to a recipient whose balance storage slot is cold (zero/uninitialized). When the resulting Ethereum transaction fails due to out-of-gas, the minter reimburses the ckERC20 tokens but permanently retains the ckETH gas fee. This is the direct IC analog of the `TransferBenchmarkLib` bug: an optimistic, non-worst-case cost estimate causes real user fund loss.

---

### Finding Description

`CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` is defined as a compile-time constant: [1](#0-0) 

`estimate_gas_limit` returns this constant unconditionally for every ckERC20 request: [2](#0-1) 

This constant is then used directly in `create_transactions_batch` to build the Ethereum transaction: [3](#0-2) 

For ckERC20 withdrawals, `create_transaction` derives `max_fee_per_gas` from the user-supplied `max_transaction_fee` divided by this fixed gas limit: [4](#0-3) 

The gas fee estimate itself uses only the **20th-percentile** priority reward from the last 5 blocks — an optimistic lower bound: [5](#0-4) 

The documentation explicitly acknowledges the limitation: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* [6](#0-5) 

Supported tokens include USDC and USDT: [7](#0-6) 

Both are upgradeable proxy contracts. USDC's `transfer()` dispatches through a proxy and writes to a storage slot that may be cold (zero) for first-time recipients, routinely consuming 65,000–90,000 gas. USDT has a non-standard implementation with additional checks. Neither is a "standard ERC-20 contract" in the sense assumed by the constant.

The `withdraw_erc20` endpoint burns ckETH for the gas fee before the Ethereum transaction is sent: [8](#0-7) 

The `Erc20WithdrawalRequest` records `max_transaction_fee` (the burned ckETH amount) at request time: [9](#0-8) 

---

### Impact Explanation

When the Ethereum transaction fails due to out-of-gas (actual gas > 65,000), the minter reimburses the ckERC20 tokens but **does not** reimburse the ckETH gas fee. The documentation states: *"Overcharged transaction fees are not reimbursed."* [10](#0-9) 

The user permanently loses the ckETH gas fee — approximately `65,000 × max_fee_per_gas` wei. At 20 gwei this is ~0.0013 ETH (~$3–5 per failed withdrawal). The loss is not recoverable through any minter endpoint. This matches the external report's impact: under-accounting transfer costs pushes costs onto users.

---

### Likelihood Explanation

**High** for USDC/USDT withdrawals to first-time recipients. Cold storage slot writes on Ethereum cost an additional 20,000 gas (EIP-2929 / EIP-2200). A USDC transfer to a new address (zero balance) routinely exceeds 65,000 gas. This is a normal, everyday scenario — any user withdrawing ckUSDC or ckUSDT to a fresh Ethereum address triggers it. No adversarial action is required; the attacker is the protocol's own optimistic constant.

---

### Recommendation

1. **Per-token gas limits**: Benchmark each supported ERC20 token's worst-case `transfer()` gas (cold recipient, cold proxy implementation slot) and store per-token limits rather than a single constant.
2. **Safety margin**: Add a buffer (e.g., 20–30%) above the measured worst-case to account for future EVM opcode repricing.
3. **Dynamic estimation**: Consider using `eth_estimateGas` via HTTPS outcall before constructing the transaction, with the hardcoded constant as a floor.
4. **Reimburse on out-of-gas failure**: Distinguish out-of-gas failures (protocol limitation) from user-caused failures and reimburse the ckETH fee in the former case.

---

### Proof of Concept

1. User holds ckUSDC and calls `withdraw_erc20` targeting a fresh Ethereum address (zero USDC balance — cold storage slot).
2. `withdraw_erc20` calls `estimate_erc20_transaction_fee()` → `lazy_refresh_gas_fee_estimate()` → `estimate_transaction_fee()` using 20th-percentile reward, multiplied by `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT = 65_000`.
3. Minter burns the computed ckETH amount from the user's account via `cketh_ledger.burn_from(...)`.
4. `create_transactions_batch` calls `estimate_gas_limit(&request)` → returns `GasAmount::new(65_000)` unconditionally.
5. The signed Ethereum transaction is submitted with `gas_limit = 65_000`.
6. On Ethereum, the USDC proxy dispatches `transfer()` to the implementation contract; writing to the recipient's cold balance slot costs ~20,000 extra gas; total gas consumed ≈ 80,000 > 65,000 → transaction reverts with out-of-gas.
7. Minter detects the failed receipt, reimburses ckUSDC tokens, but does **not** reimburse the ckETH gas fee.
8. User's ckETH gas fee is permanently lost.

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L144-177)
```rust
/// ERC-20 withdrawal request issued by the user.
#[derive(Clone, Eq, PartialEq, Decode, Encode)]
pub struct Erc20WithdrawalRequest {
    /// Amount of burn ckETH that can be used to pay for the Ethereum transaction fees.
    #[n(0)]
    pub max_transaction_fee: Wei,
    /// The ERC-20 amount that the receiver will get.
    #[n(1)]
    pub withdrawal_amount: Erc20Value,
    /// The recipient's address of the sent ERC-20 tokens.
    #[n(2)]
    pub destination: Address,
    /// The transaction ID of the ckETH burn operation on the ckETH ledger.
    #[cbor(n(3), with = "crate::cbor::id")]
    pub cketh_ledger_burn_index: LedgerBurnIndex,
    /// Address of the ERC-20 smart contract that is the message call's recipient.
    #[n(4)]
    pub erc20_contract_address: Address,
    /// The ckERC20 ledger on which the minter burned the ckERC20 tokens.
    #[cbor(n(5), with = "icrc_cbor::principal")]
    pub ckerc20_ledger_id: Principal,
    /// The transaction ID of the ckERC20 burn operation on the ckERC20 ledger.
    #[cbor(n(6), with = "crate::cbor::id")]
    pub ckerc20_ledger_burn_index: LedgerBurnIndex,
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(7), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(8)]
    pub from_subaccount: Option<LedgerSubaccount>,
    /// The IC time at which the withdrawal request arrived.
    #[n(9)]
    pub created_at: u64,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1147-1184)
```rust
        WithdrawalRequest::CkErc20(request) => {
            // The transaction fee is already paid and must be at most
            // the `max_transaction_fee` in the withdrawal request, which, given a gas limit, gives us an upper bound on
            // the `max_fee_per_gas`. We allocate the maximum from the beginning to minimize
            // transaction resubmissions: even if the `base_fee_per_gas` increases considerably,
            // the transaction could still make it as long as `transaction.max_fee_per_gas >=  block.base_fee_per_gas`,
            // since the `priority_fee_per_gas` received by the miner is capped to (see https://eips.ethereum.org/EIPS/eip-1559)
            // min(transaction.max_priority_fee_per_gas, transaction.max_fee_per_gas - block.base_fee_per_gas).
            let request_max_fee_per_gas = request
                .max_transaction_fee
                .into_wei_per_gas(gas_limit)
                .expect("BUG: gas_limit should be non-zero");
            let actual_min_max_fee_per_gas = gas_fee_estimate.min_max_fee_per_gas();
            if actual_min_max_fee_per_gas > request_max_fee_per_gas {
                return Err(CreateTransactionError::InsufficientTransactionFee {
                    cketh_ledger_burn_index: request.cketh_ledger_burn_index,
                    allowed_max_transaction_fee: request.max_transaction_fee,
                    actual_max_transaction_fee: actual_min_max_fee_per_gas
                        .transaction_cost(gas_limit)
                        .unwrap_or(Wei::MAX),
                });
            }
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

**File:** rs/ethereum/cketh/minter/src/tx.rs (L660-670)
```rust
    async fn eth_fee_history() -> Result<FeeHistory, MultiCallError<FeeHistory>> {
        read_state(rpc_client)
            .fee_history((5_u8, BlockTag::Latest))
            .with_reward_percentiles(vec![20])
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(StrictMajorityByKey::new(|fee_history: &FeeHistory| {
                Nat::from(fee_history.oldest_block.clone())
            }))
    }
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L34-38)
```text
|USDC
|https://etherscan.io/token/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48[0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48]

|USDT
|https://etherscan.io/token/0xdAC17F958D2ee523a2206206994597C13D831ec7[0xdAC17F958D2ee523a2206206994597C13D831ec7]
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-270)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
```

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L275-275)
```text
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L430-460)
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
    {
        Ok(cketh_ledger_burn_index) => {
```
