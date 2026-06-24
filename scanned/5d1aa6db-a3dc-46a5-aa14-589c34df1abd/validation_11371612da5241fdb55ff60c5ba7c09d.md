### Title
TOCTOU Blocklist Bypass: Pending Withdrawal Requests Are Not Re-Checked Against Blocklist After Canister Upgrade — (`rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

The ckETH minter enforces the blocklist only at the moment a withdrawal is submitted (`withdraw_eth` / `withdraw_erc20`). Once an `AcceptedEthWithdrawalRequest` event is recorded, the destination address is never re-validated against the blocklist. Because the blocklist is a compile-time constant that can only change via a canister upgrade, and because the minter's event-sourced state (including pending withdrawal requests) fully survives upgrades via `replay_events()`, a withdrawal request accepted before an upgrade will be processed and sent to Ethereum after the upgrade — even if the destination address is now on the expanded blocklist.

---

### Finding Description

**Blocklist is compile-time only:**

`ETH_ADDRESS_BLOCKLIST` is a `const &[Address]` baked into the binary. [1](#0-0) 

`is_blocked` performs a binary search over this static array. [2](#0-1) 

**Blocklist check only at ingress:**

`validate_address_as_destination` is called once, synchronously, at the start of `withdraw_eth`: [3](#0-2) 

And at the start of `withdraw_erc20`: [4](#0-3) 

After the check passes, the burn is executed and the event is recorded: [5](#0-4) 

**No blocklist check in the async processing pipeline:**

`create_transactions_batch` reads pending requests and creates transactions with no blocklist re-check: [6](#0-5) 

`sign_transactions_batch` signs them with no blocklist re-check: [7](#0-6) 

`send_transactions_batch` broadcasts them to Ethereum with no blocklist re-check: [8](#0-7) 

These are all called from `process_retrieve_eth_requests`, which runs on a timer: [9](#0-8) 

**Pending withdrawal requests survive canister upgrades:**

`post_upgrade` calls `replay_events()`: [10](#0-9) 

`replay_events_internal` replays every `AcceptedEthWithdrawalRequest` event by calling `record_withdrawal_request` — with no blocklist check: [11](#0-10) 

`apply_state_transition` for `AcceptedEthWithdrawalRequest` does not call `is_blocked`: [12](#0-11) 

Timers are re-registered in `post_upgrade` via `setup_timers()`: [13](#0-12) 

---

### Impact Explanation

The stated invariant — *"ETH is not accepted from nor sent to addresses on this list"* — is violated. [14](#0-13) 

A sanctioned entity can receive ETH/ERC20 from the minter despite being on the blocklist, constituting a sanctions-compliance failure. The ckETH minter is a production chain-fusion canister holding real ETH value; sending funds to a newly-sanctioned address is a direct, irreversible on-chain impact.

---

### Likelihood Explanation

The attack is realistic and requires no privileged access:

1. An unprivileged user submits `withdraw_eth` to address X (not yet blocked). The burn succeeds and `AcceptedEthWithdrawalRequest` is recorded.
2. A legitimate NNS governance proposal to add address X to the blocklist passes and the minter is upgraded with the new binary.
3. `post_upgrade` replays all events — the pending withdrawal request for address X is restored to the queue with no blocklist re-check.
4. The timer fires `process_retrieve_eth_requests` → `create_transactions_batch` → `sign_transactions_batch` → `send_transactions_batch` — the transaction is sent to address X on Ethereum.

The attacker can observe a pending NNS proposal (all proposals are public) and front-run it by submitting a withdrawal in the voting window. The NNS voting period is typically days long, giving ample time to submit a withdrawal before the upgrade executes.

---

### Recommendation

Re-check the destination address against the blocklist inside `create_transactions_batch` before creating a transaction. If the destination is now blocked, the withdrawal request should be cancelled and the user's ckETH reimbursed (similar to the existing `InsufficientTransactionFee` reimbursement path). The same check should be applied during `replay_events_internal` or at the point where pending requests are dequeued for processing.

---

### Proof of Concept

A state-machine test can confirm this:

1. Install the minter with an initial blocklist that does **not** contain address `0xAAAA...`.
2. Call `withdraw_eth` with destination `0xAAAA...`; assert `AcceptedEthWithdrawalRequest` is recorded.
3. Upgrade the minter with a new WASM whose `ETH_ADDRESS_BLOCKLIST` includes `0xAAAA...`.
4. Advance the canister clock past `PROCESS_ETH_RETRIEVE_TRANSACTIONS_INTERVAL`.
5. Assert that `eth_sendRawTransaction` is called with `to = 0xAAAA...` — confirming the transaction was sent to the now-blocked address.

The `replay_events_internal` path at [12](#0-11) 
and the absence of any `is_blocked` call in [6](#0-5) 
make this locally reproducible without any privileged access.

### Citations

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L15-16)
```rust
/// ETH is not accepted from nor sent to addresses on this list.
/// NOTE: Keep it sorted!
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L17-109)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L138-149)
```rust
#[post_upgrade]
fn post_upgrade(minter_arg: Option<MinterArg>) {
    use ic_cketh_minter::lifecycle;
    match minter_arg {
        Some(MinterArg::InitArg(_)) => {
            ic_cdk::trap("cannot upgrade canister state with init args");
        }
        Some(MinterArg::UpgradeArg(upgrade_args)) => lifecycle::post_upgrade(Some(upgrade_args)),
        None => lifecycle::post_upgrade(None),
    }
    setup_timers();
}
```

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L330-335)
```rust
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L407-414)
```rust
    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawErc20Error::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L150-190)
```rust
pub async fn process_retrieve_eth_requests() {
    let _guard = match TimerGuard::new(TaskType::RetrieveEth) {
        Ok(guard) => guard,
        Err(e) => {
            log!(
                DEBUG,
                "Failed retrieving timer guard to process ETH requests: {e:?}",
            );
            return;
        }
    };

    if read_state(|s| !s.eth_transactions.has_pending_requests()) {
        return;
    }

    let gas_fee_estimate = match lazy_refresh_gas_fee_estimate().await {
        Some(gas_fee_estimate) => gas_fee_estimate,
        None => {
            log!(
                INFO,
                "Failed retrieving gas fee estimate to process ETH requests",
            );
            return;
        }
    };

    let latest_transaction_count = latest_transaction_count().await;
    resubmit_transactions_batch(latest_transaction_count, &gas_fee_estimate).await;
    create_transactions_batch(gas_fee_estimate);
    sign_transactions_batch().await;
    send_transactions_batch(latest_transaction_count).await;
    finalize_transactions_batch().await;

    if read_state(|s| s.eth_transactions.has_pending_requests()) {
        ic_cdk_timers::set_timer(
            crate::PROCESS_ETH_RETRIEVE_TRANSACTIONS_RETRY_INTERVAL,
            async { process_retrieve_eth_requests().await },
        );
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

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L303-339)
```rust
async fn sign_transactions_batch() {
    let transactions_batch: Vec<_> = read_state(|s| {
        s.eth_transactions
            .transactions_to_sign_batch(TRANSACTIONS_TO_SIGN_BATCH_SIZE)
    });
    log!(DEBUG, "Signing transactions {transactions_batch:?}");
    let results = join_all(
        transactions_batch
            .into_iter()
            .map(|(withdrawal_id, tx)| async move { (withdrawal_id, tx.sign().await) }),
    )
    .await;
    let mut errors = Vec::new();
    for (withdrawal_id, result) in results {
        match result {
            Ok(transaction) => mutate_state(|s| {
                process_event(
                    s,
                    EventType::SignedTransaction {
                        withdrawal_id,
                        transaction,
                    },
                )
            }),
            Err(e) => errors.push(e),
        }
    }
    if !errors.is_empty() {
        // At this point there might be a gap in transaction nonces between signed transactions, e.g.,
        // transactions 1,2,4,5 were signed, but 3 was not due to some unexpected error.
        // This means that transactions 4 and 5 are currently stuck until transaction 3 is signed.
        // However, we still proceed with transactions 4 and 5 since that way they might be mined faster
        // once transaction 3 is sent on the next iteration. Otherwise, we would need to re-sign transactions 4 and 5
        // and send them (together with transaction 3) on the next iteration.
        log!(INFO, "Errors encountered during signing: {errors:?}");
    }
}
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L340-384)
```rust
async fn send_transactions_batch(latest_transaction_count: Option<TransactionCount>) {
    let latest_transaction_count = match latest_transaction_count {
        Some(latest_transaction_count) => latest_transaction_count,
        None => {
            return;
        }
    };
    let transactions_to_send: Vec<_> = read_state(|s| {
        s.eth_transactions
            .transactions_to_send_batch(latest_transaction_count, TRANSACTIONS_TO_SEND_BATCH_SIZE)
    });

    let rpc_client = read_state(rpc_client);
    let results = join_all(transactions_to_send.iter().map(async |tx| {
        rpc_client
            .send_raw_transaction(tx.raw_transaction_hex())
            .with_cycles(MIN_ATTACHED_CYCLES)
            .try_send()
            .await
            .reduce_with_strategy(AnyOf)
    }))
    .await;

    for (signed_tx, result) in zip(transactions_to_send, results) {
        log!(DEBUG, "Sent transaction {signed_tx:?}: {result:?}");
        match result {
            Ok(SendRawTransactionStatus::Ok(_)) | Ok(SendRawTransactionStatus::NonceTooLow) => {
                // In case of resubmission we may hit the case of SendRawTransactionStatus::NonceTooLow
                // if the stuck transaction was mined in the meantime.
                // It will be cleaned-up once the transaction is finalized.
            }
            Ok(SendRawTransactionStatus::InsufficientFunds)
            | Ok(SendRawTransactionStatus::NonceTooHigh) => log!(
                INFO,
                "Failed to send transaction {signed_tx:?}: {result:?}. Will retry later.",
            ),
            Err(e) => {
                log!(
                    INFO,
                    "Failed to send transaction {signed_tx:?}: {e:?}. Will retry later."
                )
            }
        };
    }
}
```

**File:** rs/ethereum/cketh/minter/src/lifecycle/upgrade.rs (L35-43)
```rust
pub fn post_upgrade(upgrade_args: Option<UpgradeArg>) {
    let start = ic_cdk::api::instruction_counter();

    STATE.with(|cell| {
        *cell.borrow_mut() = Some(replay_events());
    });
    if let Some(args) = upgrade_args {
        mutate_state(|s| process_event(s, EventType::Upgrade(args)))
    }
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L72-76)
```rust
        EventType::AcceptedEthWithdrawalRequest(request) => {
            state
                .eth_transactions
                .record_withdrawal_request(request.clone());
        }
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L189-204)
```rust
fn replay_events_internal<T: IntoIterator<Item = Event>>(events: T) -> State {
    let mut events_iter = events.into_iter();
    let mut state = match events_iter
        .next()
        .expect("the event log should not be empty")
    {
        Event {
            payload: EventType::Init(init_arg),
            ..
        } => State::try_from(init_arg).expect("state initialization should succeed"),
        other => panic!("the first event must be an Init event, got: {other:?}"),
    };
    for event in events_iter {
        apply_state_transition(&mut state, &event.payload);
    }
    state
```
