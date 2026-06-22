### Title
Hardcoded `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` Applied Uniformly to All ckERC20 Tokens Causes Systematic Fee Overcharging and Irrecoverable ckETH Loss on Transaction Failure - (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter hardcodes a single gas limit of `65_000` for every ckERC20 withdrawal transaction, regardless of the actual gas requirements of the specific ERC-20 contract being called. Because the ckETH fee is burned upfront and overcharged fees are explicitly never reimbursed, users are systematically overcharged on every withdrawal. For tokens whose `transfer()` call exceeds 65,000 gas, the Ethereum transaction fails and the user permanently loses the ckETH burned for fees.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/withdraw.rs`, two constants define the gas limits for all withdrawal transactions:

```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
``` [1](#0-0) 

The `estimate_gas_limit` function returns this single constant for every ckERC20 request, with no per-token customization:

```rust
pub fn estimate_gas_limit(withdrawal_request: &WithdrawalRequest) -> GasAmount {
    match withdrawal_request {
        WithdrawalRequest::CkEth(_) => CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
        WithdrawalRequest::CkErc20(_) => CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT,
    }
}
``` [2](#0-1) 

This gas limit is used in two critical places:

1. **Fee estimation exposed to users** — `eip_1559_transaction_price` returns `CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT` for all ckERC20 tokens, so users approve and burn ckETH based on `65_000 * max_fee_per_gas`: [3](#0-2) 

2. **Transaction construction** — `create_transactions_batch` calls `estimate_gas_limit` and passes the result to `create_transaction`, which sets `gas_limit` in the on-chain EIP-1559 transaction: [4](#0-3) [5](#0-4) 

The ckERC20 withdrawal flow burns ckETH upfront for the fee. The documentation explicitly states that overcharged transaction fees are **never reimbursed**:

> "Overcharged transaction fees are not reimbursed." [6](#0-5) 

The documentation itself acknowledges the hardcoding: *"The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts."* This is the same class of design choice flagged in the reference report.

---

### Impact Explanation

**Scenario A — Token uses less than 65,000 gas (e.g., a simple ERC-20 using ~30,000–50,000 gas):**
Every user withdrawal is charged `65_000 * max_fee_per_gas` in ckETH, but the actual Ethereum cost is `actual_gas * effective_gas_price`. The difference is permanently lost. At 30 gwei gas price, a token using 30,000 gas results in ~0.00105 ETH (~$3–4) of irrecoverable overcharge per withdrawal.

**Scenario B — Token uses more than 65,000 gas (e.g., tokens with transfer hooks, fee-on-transfer, or rebasing logic):**
The Ethereum transaction runs out of gas and fails. The ckERC20 tokens are reimbursed, but the ckETH burned for the fee is **not reimbursed**. The user permanently loses the full fee amount (up to `65_000 * max_fee_per_gas` ckETH).

The `DEFAULT_MAX_TRANSACTION_FEE` used in tests is `30_000_000_000_000_000` wei (~0.03 ETH), confirming the magnitude of potential loss per withdrawal. [7](#0-6) 

---

### Likelihood Explanation

- The entry path is fully unprivileged: any user can call `withdraw_erc20` on the ckETH minter canister.
- The IC currently supports multiple ckERC20 tokens (ckUSDC, ckUSDT, ckUNI, etc.), each with different ERC-20 contract implementations and gas profiles.
- The overcharging in Scenario A affects **every single ckERC20 withdrawal** for tokens whose actual gas usage differs from 65,000.
- Scenario B is triggered whenever a supported ckERC20 token's `transfer()` call exceeds 65,000 gas, which is realistic for tokens with non-trivial transfer logic.
- No special conditions, timing, or attacker coordination is required.

---

### Recommendation

1. **Per-token gas limit registry**: Store a configurable `gas_limit` per supported ckERC20 token in the minter state (analogous to how Curve pool addresses are stored per asset in the reference report). This allows the correct gas limit to be set when a new token is added via governance proposal.

2. **Reimburse overcharged fees**: If `gas_used < gas_limit` in the finalized receipt, reimburse the difference `(gas_limit - gas_used) * effective_gas_price` in ckETH to the user.

3. **Fail-safe on out-of-gas**: If the Ethereum transaction fails due to out-of-gas, reimburse the ckETH fee rather than treating it as consumed.

---

### Proof of Concept

1. A user calls `eip_1559_transaction_price` for a ckERC20 token whose `transfer()` uses 30,000 gas. The minter returns a price based on `gas_limit = 65_000`.
2. The user approves the minter to burn `65_000 * max_fee_per_gas` ckETH and calls `withdraw_erc20`.
3. The minter burns the full ckETH fee and constructs an Ethereum transaction with `gas_limit = 65_000`.
4. The Ethereum transaction executes, using only 30,000 gas. The user receives their ERC-20 tokens.
5. The minter finalizes the transaction. The `(65_000 - 30_000) * effective_gas_price` ckETH difference is never reimbursed — permanently lost to the user.

The `create_transaction` function for ckERC20 confirms the gas limit is taken directly from the caller-supplied (hardcoded) value with no per-token adjustment: [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L43-44)
```rust
pub const CKETH_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(21_000);
pub const CKERC20_WITHDRAWAL_TRANSACTION_GAS_LIMIT: GasAmount = GasAmount::new(65_000);
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-264)
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L173-188)
```rust
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

**File:** rs/ethereum/cketh/docs/ckerc20.adoc (L270-275)
```text
. The ckETH minter attempts to estimate the current transaction fee and tries to burn the necessary amount of ckETH to pay for the transaction. The `gas_limit` for ckERC20 withdrawals is currently fixed to `65_000` and should be sufficient for standard ERC-20 contracts. This estimate must include some safety margin to ensure that the minter can resubmit the transaction if necessary, which requires an increase of at least 10% in the max priority fee per gas. If the burn fails (e.g., insufficient funds), the withdrawal request will be rejected. If the burn succeeds, the burn transaction index is used as the request identifier.
. The minter attempts to burn the specified token amount from the user account on the ckERC20 ledger. If the burn succeeds, the minter schedules a withdrawal task. If the burn fails (e.g., insufficient funds), the minter schedules the reimbursement of the burnt ckETH amount from the previous step minus some (small) penalty fee.
. The ckETH minter constructs a 0-ETH amount transaction containing the ERC-20 withdrawal (in `data` field) to the Ethereum network.
. The user can query the withdrawal status using the identifier from the erc20_withdraw response.
. Once the transaction gets enough confirmations, the minter considers the transaction finalized.
. The minter retrieves the receipt of the finalized transaction (as done currently by the ckETH minter) and will reimburse the ckERC20 tokens in case the transaction failed. Overcharged transaction fees are not reimbursed.
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L27-29)
```rust
const DEFAULT_MAX_TRANSACTION_FEE: u128 = 30_000_000_000_000_000;
const DEFAULT_CKERC20_MAX_FEE_PER_GAS: WeiPerGas =
    WeiPerGas::new(DEFAULT_MAX_TRANSACTION_FEE / 65_000_u128);
```
