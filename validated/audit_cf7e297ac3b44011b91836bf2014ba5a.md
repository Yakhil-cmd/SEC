### Title
Lack of Pause/Mode Mechanism for Withdrawals in ckETH Minter — (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary
The ckETH minter exposes `withdraw_eth` and `withdraw_erc20` endpoints that immediately burn ckETH on the IC ledger before queuing the corresponding Ethereum transaction. Unlike the ckBTC minter, which has a `Mode` enum (`ReadOnly`, `RestrictedTo`, `DepositsRestrictedTo`, `GeneralAvailability`) checked before every burn, the ckETH minter has no equivalent pause or mode mechanism. If the Ethereum-side infrastructure becomes unavailable (e.g., all EVM RPC providers are unreachable), users can still burn ckETH on IC while the corresponding ETH release is indefinitely deferred, with no reimbursement triggered until the Ethereum transaction is actually sent and fails.

---

### Finding Description

**ckBTC minter — has a mode/pause check:**

In `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`, both `retrieve_btc` and `retrieve_btc_with_approval` call `s.mode.is_withdrawal_available_for(&caller)` before any burn:

```rust
// retrieve_btc (line 152)
state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
    .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

The `Mode` enum in `rs/bitcoin/ckbtc/minter/src/state.rs` (lines 355–388) allows operators to set `Mode::ReadOnly` via an upgrade argument, which blocks all withdrawals while leaving deposits intact.

**ckETH minter — no mode/pause check:**

In `rs/ethereum/cketh/minter/src/main.rs`, `withdraw_eth` (lines 265–340) performs no mode check. It validates the caller is non-anonymous, validates the destination address, checks the minimum amount, then immediately burns ckETH:

```rust
match client
    .burn_from(Account { owner: caller, subaccount: from_subaccount }, amount, ...)
    .await
{
    Ok(ledger_burn_index) => { /* queue withdrawal */ }
    Err(e) => Err(WithdrawalError::from(e)),
}
```

The `State` struct in `rs/ethereum/cketh/minter/src/state.rs` (lines 54–100) contains no `mode` field. There is no `ReadOnly`, `RestrictedTo`, or equivalent variant anywhere in the ckETH minter codebase.

**`withdraw_erc20` compounds the issue:**

`withdraw_erc20` (lines 389–543) performs two sequential burns: first ckETH for the gas fee (line 448–459), then ckERC20 for the withdrawal amount (lines 468–477). If the ckERC20 burn fails, a reimbursement request is queued for the ckETH (lines 506–531), but this reimbursement is asynchronous and itself depends on the ckETH ledger being available. If the ckETH ledger is temporarily unavailable at reimbursement time, the user's ckETH fee is lost.

**Reimbursement does not cover the stuck-queue scenario:**

The `EthTransactions` state machine (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`, lines 361–377) only triggers reimbursement when a sent transaction fails on Ethereum. Withdrawal requests sitting in `pending_withdrawal_requests` — i.e., not yet sent because EVM RPC providers are down — never enter the reimbursement path. The user's ckETH is burned and the ETH is not released, with no recovery path until the Ethereum side recovers.

---

### Impact Explanation

Any user who calls `withdraw_eth` or `withdraw_erc20` while the EVM RPC layer is degraded will have their ckETH burned on the IC ledger with no corresponding ETH release and no reimbursement until the Ethereum transaction is eventually sent and finalized (or fails). During a prolonged EVM RPC outage, multiple users can accumulate burned ckETH with no recourse. Operators have no fine-grained mechanism to halt new withdrawals; the only option is a full canister stop via NNS governance, which also halts deposits and all other operations.

---

### Likelihood Explanation

EVM RPC provider outages are realistic: the ckETH minter depends on a small set of external JSON-RPC providers, and all of them being simultaneously unreachable (or returning inconsistent results that fail the multi-call consensus threshold) is a documented operational risk. The ckBTC minter was explicitly designed with `Mode::ReadOnly` to handle analogous Bitcoin-side outages. The absence of an equivalent in the ckETH minter is a gap that can be triggered by any user during any Ethereum-side degradation event, without any privileged access.

---

### Recommendation

Implement a `Mode` enum for the ckETH minter analogous to the one in the ckBTC minter (`rs/bitcoin/ckbtc/minter/src/state.rs`, lines 355–388). Add a `mode` field to the ckETH `State` struct and check `mode.is_withdrawal_available_for(&caller)` at the top of both `withdraw_eth` and `withdraw_erc20` before any ledger burn is attempted. Expose a `mode` field in `UpgradeArg` so that NNS governance can set `ReadOnly` during Ethereum-side incidents without fully stopping the canister.

---

### Proof of Concept

1. All configured EVM RPC providers become unreachable (or return inconsistent results failing the multi-call strategy).
2. User calls `withdraw_eth` with a valid amount and recipient.
3. `withdraw_eth` passes all checks (non-anonymous caller, valid address, amount ≥ minimum).
4. `client.burn_from(...)` succeeds — ckETH is permanently burned on the IC ledger.
5. The `EthWithdrawalRequest` is added to `eth_transactions.pending_withdrawal_requests`.
6. The timer task `process_withdrawal` attempts to create an Ethereum transaction but fails because EVM RPC is unreachable; the request remains in `pending_withdrawal_requests`.
7. No reimbursement is triggered (reimbursement only fires after a transaction is sent and fails on-chain).
8. The user's ckETH is burned with no ETH released and no recovery path until EVM RPC recovers.
9. Operators cannot stop new users from repeating steps 2–8 because there is no `Mode::ReadOnly` equivalent in the ckETH minter. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
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
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-543)
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
            log!(
                INFO,
                "[withdraw_erc20]: burning {} {} from account {}",
                ckerc20_withdrawal_amount,
                ckerc20_token.ckerc20_token_symbol,
                ckerc20_account
            );
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
            {
                Ok(ckerc20_ledger_burn_index) => {
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
                        withdrawal_amount: ckerc20_withdrawal_amount,
                        destination,
                        cketh_ledger_burn_index,
                        ckerc20_ledger_id: ckerc20_token.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index,
                        erc20_contract_address: ckerc20_token.erc20_contract_address,
                        from: caller,
                        from_subaccount: from_ckerc20_subaccount
                            .and_then(LedgerSubaccount::from_bytes),
                        created_at: now,
                    };
                    log!(
                        INFO,
                        "[withdraw_erc20]: queuing withdrawal request {:?}",
                        withdrawal_request
                    );
                    mutate_state(|s| {
                        process_event(
                            s,
                            EventType::AcceptedErc20WithdrawalRequest(withdrawal_request.clone()),
                        );
                    });
                    Ok(RetrieveErc20Request::from(withdrawal_request))
                }
                Err(ckerc20_burn_error) => {
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
                    if reimbursed_amount > Wei::ZERO {
                        let reimbursement_request = ReimbursementRequest {
                            ledger_burn_index: cketh_ledger_burn_index,
                            reimbursed_amount: reimbursed_amount.change_units(),
                            to: cketh_account.owner,
                            to_subaccount: cketh_account
                                .subaccount
                                .and_then(LedgerSubaccount::from_bytes),
                            transaction_hash: None,
                        };
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::FailedErc20WithdrawalRequest(reimbursement_request),
                            );
                        });
                    }
                    Err(WithdrawErc20Error::CkErc20LedgerError {
                        cketh_block_index: Nat::from(cketh_ledger_burn_index.get()),
                        error: ckerc20_burn_error.into(),
                    })
                }
            }
        }
        Err(cketh_burn_error) => Err(WithdrawErc20Error::CkEthLedgerError {
            error: cketh_burn_error.into(),
        }),
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-153)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L355-388)
```rust
impl Mode {
    /// Returns Ok if the specified principal can convert BTC to ckBTC.
    pub fn is_deposit_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("access to the minter is temporarily restricted".to_string());
                }
                Ok(())
            }
            Self::DepositsRestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC deposits are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }

    /// Returns Ok if the specified principal can convert ckBTC to BTC.
    pub fn is_withdrawal_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability | Self::DepositsRestrictedTo(_) => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC withdrawals are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L54-100)
```rust
pub struct State {
    pub ethereum_network: EthereumNetwork,
    pub ecdsa_key_name: String,
    pub cketh_ledger_id: Principal,
    pub log_scrapings: LogScrapings,
    pub ecdsa_public_key: Option<EcdsaPublicKeyResult>,
    pub cketh_minimum_withdrawal_amount: Wei,
    pub ethereum_block_height: CandidBlockTag,
    pub first_scraped_block_number: BlockNumber,
    pub last_observed_block_number: Option<BlockNumber>,
    pub events_to_mint: BTreeMap<EventSource, ReceivedEvent>,
    pub minted_events: BTreeMap<EventSource, MintedEvent>,
    pub invalid_events: BTreeMap<EventSource, InvalidEventReason>,
    pub eth_transactions: EthTransactions,
    pub skipped_blocks: BTreeMap<Address, BTreeSet<BlockNumber>>,

    /// Current balance of ETH held by the minter.
    /// Computed based on audit events.
    pub eth_balance: EthBalance,

    /// Current balance of ERC-20 tokens held by the minter.
    /// Computed based on audit events.
    pub erc20_balances: Erc20Balances,

    /// Per-principal lock for pending withdrawals
    pub pending_withdrawal_principals: BTreeSet<Principal>,

    /// Locks preventing concurrent execution timer tasks
    pub active_tasks: HashSet<TaskType>,

    /// Number of HTTP outcalls since the last upgrade.
    /// Used to correlate request and response in logs.
    pub http_request_counter: u64,

    pub last_transaction_price_estimate: Option<(u64, GasFeeEstimate)>,

    /// Canister ID of the ledger suite orchestrator that
    /// can add new ERC-20 token to the minter
    pub ledger_suite_orchestrator_id: Option<Principal>,

    /// Canister ID of the EVM RPC canister that
    /// handles communication with Ethereum
    pub evm_rpc_id: Principal,

    /// ERC-20 tokens that the minter can mint:
    /// - primary key: ledger ID for the ckERC20 token
    /// - secondary key: ERC-20 contract address on Ethereum
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
