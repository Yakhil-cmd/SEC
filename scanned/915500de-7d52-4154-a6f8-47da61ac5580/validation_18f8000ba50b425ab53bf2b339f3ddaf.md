### Title
Blocklist Check Performed Only at Withdrawal Request Creation, Not at Transaction Processing — (`File: rs/ethereum/cketh/minter/src/withdraw.rs`)

### Summary
The ckETH/ckERC20 minter validates the destination Ethereum address against the blocklist only when a withdrawal request is first submitted (`withdraw_eth` / `withdraw_erc20`). When the minter later processes the queued request and constructs the on-chain transaction (`create_transactions_batch`), no blocklist re-check is performed. If the minter is upgraded with an expanded blocklist after a withdrawal request has been accepted and tokens burned, the pending request will still be processed and ETH/ERC-20 tokens will be sent to the now-blocked address.

### Finding Description

**At request creation time**, `withdraw_eth` and `withdraw_erc20` both call `validate_address_as_destination`, which enforces the blocklist:

```rust
// rs/ethereum/cketh/minter/src/main.rs:280-287
let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
    AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
        address: address.to_string(),
    },
    ...
})?;
```

After passing this check, the ckETH tokens are burned and the `EthWithdrawalRequest` (or `Erc20WithdrawalRequest`) is pushed into `pending_withdrawal_requests`:

```rust
// rs/ethereum/cketh/minter/src/main.rs:330-335
mutate_state(|s| {
    process_event(s, EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()));
});
```

**At processing time**, `create_transactions_batch` dequeues pending requests and constructs Ethereum transactions without any blocklist re-check:

```rust
// rs/ethereum/cketh/minter/src/withdraw.rs:249-293
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions.withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
        // No blocklist check here
        match create_transaction(&request, nonce, gas_fee_estimate.clone(), gas_limit, ethereum_network) {
            Ok(transaction) => { /* sign and send */ }
            Err(CreateTransactionError::InsufficientTransactionFee { .. }) => { /* reschedule */ }
        };
    }
}
```

The `create_transaction` function in `rs/ethereum/cketh/minter/src/state/transactions/mod.rs` (lines 1110–1186) only checks for `InsufficientTransactionFee`; it performs no address validation.

The blocklist itself is a static compile-time array in `rs/ethereum/cketh/minter/src/blocklist.rs` (lines 17–105), updated only via canister upgrades (NNS governance proposals). After an upgrade that adds a new address to the blocklist, any already-queued withdrawal to that address will still be executed.

### Impact Explanation

The blocklist is the ckETH minter's primary compliance/sanctions enforcement mechanism. A pending withdrawal request that was accepted before an address was added to the blocklist will bypass the blocklist entirely and result in ETH or ERC-20 tokens being sent to a sanctioned/blocked Ethereum address. The tokens have already been burned from the user's ckETH/ckERC20 ledger account at request creation time, so there is no way to cancel the withdrawal without a separate intervention. This constitutes a **chain-fusion compliance bypass**: the minter sends funds to a blocked address in violation of its own policy.

### Likelihood Explanation

The blocklist is updated via NNS governance proposals that upgrade the minter canister. There is an inherent delay between when a withdrawal is submitted and when it is processed (the minter batches requests and processes them on a timer). During a governance-driven blocklist update, any withdrawal requests submitted to a newly-blocked address in the window between request acceptance and canister upgrade will be processed without re-validation. An unprivileged user can trigger this by calling `withdraw_eth` or `withdraw_erc20` directly — no special privileges are required. The likelihood is low in normal operation but non-negligible during active blocklist expansions.

### Recommendation

In `create_transactions_batch` (`rs/ethereum/cketh/minter/src/withdraw.rs`), re-check the destination address against the blocklist before calling `create_transaction`. If the destination is now blocked, move the request to a reimbursement flow (mint back the burned tokens to the user) rather than proceeding with the Ethereum transaction. This mirrors the existing `InsufficientTransactionFee` handling that already reschedules or cancels requests at processing time.

### Proof of Concept

1. User calls `withdraw_eth` with `recipient = "0x0330070FD38Ec3bB94F58FA55D40368271E9e54A"` (an address not yet on the blocklist). Tokens are burned; request enters `pending_withdrawal_requests`.
2. NNS governance adopts a proposal upgrading the ckETH minter with a new binary that adds `0x0330070FD38Ec3bB94F58FA55D40368271E9e54A` to `ETH_ADDRESS_BLOCKLIST`.
3. After the upgrade, the minter timer fires and calls `create_transactions_batch`. The function dequeues the pending request and calls `create_transaction` — no blocklist check occurs.
4. The transaction is signed via threshold ECDSA and submitted to Ethereum, sending ETH to the now-blocked address.
5. The `is_address_blocked` query endpoint confirms the address is blocked, yet the transfer has already been executed.

**Relevant code locations:**
- Creation-time check: [1](#0-0) 
- Processing (no check): [2](#0-1) 
- `create_transaction` (no address validation): [3](#0-2) 
- Blocklist definition: [4](#0-3) 
- `pending_withdrawal_requests` queue: [5](#0-4)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L280-287)
```rust
    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L361-377)
```rust
pub struct EthTransactions {
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
    // Processed withdrawal requests (transaction created, sent, or finalized).
    pub(in crate::state) processed_withdrawal_requests:
        BTreeMap<LedgerBurnIndex, WithdrawalRequest>,
    pub(in crate::state) created_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, TransactionRequest>,
    pub(in crate::state) sent_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, Vec<SignedTransactionRequest>>,
    pub(in crate::state) finalized_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, FinalizedEip1559Transaction>,
    pub(in crate::state) next_nonce: TransactionNonce,

    pub(in crate::state) maybe_reimburse: BTreeSet<LedgerBurnIndex>,
    pub(in crate::state) reimbursement_requests: BTreeMap<ReimbursementIndex, ReimbursementRequest>,
    pub(in crate::state) reimbursed: BTreeMap<ReimbursementIndex, ReimbursedResult>,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L1110-1145)
```rust
pub fn create_transaction(
    withdrawal_request: &WithdrawalRequest,
    nonce: TransactionNonce,
    gas_fee_estimate: GasFeeEstimate,
    gas_limit: GasAmount,
    ethereum_network: EthereumNetwork,
) -> Result<Eip1559TransactionRequest, CreateTransactionError> {
    assert!(
        gas_limit > GasAmount::ZERO,
        "BUG: gas limit should be non-zero"
    );
    match withdrawal_request {
        WithdrawalRequest::CkEth(request) => {
            let transaction_price = gas_fee_estimate.to_price(gas_limit);
            let max_transaction_fee = transaction_price.max_transaction_fee();
            let tx_amount = match request.withdrawal_amount.checked_sub(max_transaction_fee) {
                Some(tx_amount) => tx_amount,
                None => {
                    return Err(CreateTransactionError::InsufficientTransactionFee {
                        cketh_ledger_burn_index: request.ledger_burn_index,
                        allowed_max_transaction_fee: request.withdrawal_amount,
                        actual_max_transaction_fee: max_transaction_fee,
                    });
                }
            };
            Ok(Eip1559TransactionRequest {
                chain_id: ethereum_network.chain_id(),
                nonce,
                max_priority_fee_per_gas: transaction_price.max_priority_fee_per_gas,
                max_fee_per_gas: transaction_price.max_fee_per_gas,
                gas_limit: transaction_price.gas_limit,
                destination: request.destination,
                amount: tx_amount,
                data: Vec::new(),
                access_list: Default::default(),
            })
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L15-109)
```rust
/// ETH is not accepted from nor sent to addresses on this list.
/// NOTE: Keep it sorted!
const ETH_ADDRESS_BLOCKLIST: &[Address] = &[
    ethereum_address!("0330070FD38Ec3bB94F58FA55D40368271E9e54A"),
    ethereum_address!("04DBA1194ee10112fE6C3207C0687DEf0e78baCf"),
    ethereum_address!("08723392Ed15743cc38513C4925f5e6be5c17243"),
    ethereum_address!("08b2eFdcdB8822EfE5ad0Eae55517cf5DC544251"),
    ethereum_address!("0931cA4D13BB4ba75D9B7132AB690265D749a5E7"),
    ethereum_address!("098B716B8Aaf21512996dC57EB0615e2383E2f96"),
    ethereum_address!("0Ee5067b06776A89CcC7dC8Ee369984AD7Db5e06"),
    ethereum_address!("12de548F79a50D2bd05481C8515C1eF5183666a9"),
    ethereum_address!("1967d8af5bd86a497fb3dd7899a020e47560daaf"),
    ethereum_address!("1999ef52700c34de7ec2b68a28aafb37db0c5ade"),
    ethereum_address!("19aa5fe80d33a56d56c78e82ea5e50e5d80b4dff"),
    ethereum_address!("19F8f2B0915Daa12a3f5C9CF01dF9E24D53794F7"),
    ethereum_address!("1da5821544e25c636c1417ba96ade4cf6d2f9b5a"),
    ethereum_address!("21B8d56BDA776bbE68655A16895afd96F5534feD"),
    ethereum_address!("2f389ce8bd8ff92de3402ffce4691d17fc4f6535"),
    ethereum_address!("308ed4b7b49797e1a98d3818bff6fe5385410370"),
    ethereum_address!("35fB6f6DB4fb05e6A4cE86f2C93691425626d4b1"),
    ethereum_address!("39D908dac893CBCB53Cc86e0ECc369aA4DeF1A29"),
    ethereum_address!("3AD9dB589d201A710Ed237c829c7860Ba86510Fc"),
    ethereum_address!("3cbded43efdaf0fc77b9c55f6fc9988fcc9b757d"),
    ethereum_address!("3Cffd56B47B7b41c56258D9C7731ABaDc360E073"),
    ethereum_address!("3e37627dEAA754090fBFbb8bd226c1CE66D255e9"),
    ethereum_address!("43fa21d92141BA9db43052492E0DeEE5aa5f0A93"),
    ethereum_address!("48549a34ae37b12f6a30566245176994e17c6b4a"),
    ethereum_address!("4f47bc496083c727c5fbe3ce9cdf2b0f6496270c"),
    ethereum_address!("502371699497d08D5339c870851898D6D72521Dd"),
    ethereum_address!("530a64c0ce595026a4a556b703644228179e2d57"),
    ethereum_address!("532b77b33a040587e9fd1800088225f99b8b0e8a"),
    ethereum_address!("53b6936513e738f44FB50d2b9476730C0Ab3Bfc1"),
    ethereum_address!("5512d943ed1f7c8a43f3435c85f7ab68b30121b0"),
    ethereum_address!("57EC89A0C056163A0314e413320f9B3ABe761259"),
    ethereum_address!("5A14E72060c11313E38738009254a90968F58f51"),
    ethereum_address!("5a7a51bfb49f190e5a6060a5bc6052ac14a3b59f"),
    ethereum_address!("5d5b5dafecbf31bdb08bfd3edad4f2694372d0ef"),
    ethereum_address!("5f48c2a71b2cc96e3f0ccae4e39318ff0dc375b2"),
    ethereum_address!("67d40EE1A85bf4a4Bb7Ffae16De985e8427B6b45"),
    ethereum_address!("6be0ae71e6c41f2f9d0d1a3b8d0f75e6f6a0b46e"),
    ethereum_address!("6f1ca141a28907f78ebaa64fb83a9088b02a8352"),
    ethereum_address!("72a5843cc08275C8171E582972Aa4fDa8C397B2A"),
    ethereum_address!("747AFB5c7A7fc34B547cD0FDEbf9b91759C5a52b"),
    ethereum_address!("76EA76CA4Eb727f18956aB93445a94c5280412B9"),
    ethereum_address!("797d7ae72ebddcdea2a346c1834e04d1f8df102b"),
    ethereum_address!("7CEd75026204aC29C34bEA98905D4C949F27361e"),
    ethereum_address!("7Db418b5D567A4e0E8c59Ad71BE1FcE48f3E6107"),
    ethereum_address!("7F19720A857F834887FC9A7bC0a0fBe7Fc7f8102"),
    ethereum_address!("7F367cC41522cE07553e823bf3be79A889DEbe1B"),
    ethereum_address!("7FF9cFad3877F21d41Da833E2F775dB0569eE3D9"),
    ethereum_address!("83E5bC4Ffa856BB84Bb88581f5Dd62A433A25e0D"),
    ethereum_address!("8576acc5c05d6ce88f4e49bf65bdf0c62f91353c"),
    ethereum_address!("8Dce2aAC0dE82bdCAf6b4373B79f94331b8e4995"),
    ethereum_address!("901bb9583b24d97e995513c6778dc6888ab6870e"),
    ethereum_address!("931546D9e66836AbF687d2bc64B30407bAc8C568"),
    ethereum_address!("95584C303FCd48AF5c6B9873015f2AD0ca84EaE3"),
    ethereum_address!("961c5be54a2ffc17cf4cb021d863c42dacd47fc1"),
    ethereum_address!("97b1043abd9e6fc31681635166d430a458d14f9c"),
    ethereum_address!("983a81ca6FB1e441266D2FbcB7D8E530AC2E05A2"),
    ethereum_address!("9Be599d7867f5E1a2D7Ec6dB9710dF2b98A15573"),
    ethereum_address!("9c2bc757b66f24d60f016b6237f8cdd414a879fa"),
    ethereum_address!("9f4cda013e354b8fc285bf4b9a60460cee7f7ea9"),
    ethereum_address!("a0e1c89Ef1a489c9C7dE96311eD5Ce5D32c20E4B"),
    ethereum_address!("a7e5d5a720f06526557c513402f2e6b5fa20b008"),
    ethereum_address!("b338962B92CD818D6aef0A32a9ECD01212a71f33"),
    ethereum_address!("b637f84b66876ebf609c2a4208905f9ddac9d075"),
    ethereum_address!("b6f5ec1a0a9cd1526536d3f0426c429529471f40"),
    ethereum_address!("c103b7dc095c904b92081eef0c1640081ec01c10"),
    ethereum_address!("c2a3829F459B3Edd87791c74cD45402BA0a20Be3"),
    ethereum_address!("c455f7fd3e0e12afd51fba5c106909934d8a0e4a"),
    ethereum_address!("cB74874f1e06Fcf80A306e06e5379A44B488bA2D"),
    ethereum_address!("d04E33461FEA8302c5E1e13895b60cEe8AEfda7F"),
    ethereum_address!("d0975b32cea532eadddfc9c60481976e39db3472"),
    ethereum_address!("d5ED34b52AC4ab84d8FA8A231a3218bbF01Ed510"),
    ethereum_address!("D8500C631dC32FA18645B7436344a99E4825e10e"),
    ethereum_address!("d882cfc20f52f2599d84b8e8d58c7fb62cfe344b"),
    ethereum_address!("db2720ebad55399117ddb4c4a4afd9a4ccada8fe"),
    ethereum_address!("dcbEfFBECcE100cCE9E4b153C4e15cB885643193"),
    ethereum_address!("e1d865c3d669dcc8c57c8d023140cb204e672ee4"),
    ethereum_address!("e1e4c5e5ed8f03ae61b581e2def126025f2b9401"),
    ethereum_address!("e3d35f68383732649669aa990832e017340dbca5"),
    ethereum_address!("e7aa314c77f4233c18c6cc84384a9247c0cf367b"),
    ethereum_address!("E950DC316b836e4EeFb8308bf32Bf7C72a1358FF"),
    ethereum_address!("ed6e0a7e4ac94d976eebfb82ccf777a3c6bad921"),
    ethereum_address!("EFE301d259F525cA1ba74A7977b80D5b060B3ccA"),
    ethereum_address!("f3701f445b6bdafedbca97d1e477357839e4120d"),
    ethereum_address!("f4377edA661e04B6DDA78969796Ed31658D602D4"),
    ethereum_address!("F7B31119c2682c88d88D455dBb9d5932c65Cf1bE"),
    ethereum_address!("Fb3eFf152ea55D1BfA04Dbdd509A80fD7b72cdEB"),
    ethereum_address!("Fda1Ec4A6178d4916b001a065422D31EBE5F62FF"),
];

pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
}
```
