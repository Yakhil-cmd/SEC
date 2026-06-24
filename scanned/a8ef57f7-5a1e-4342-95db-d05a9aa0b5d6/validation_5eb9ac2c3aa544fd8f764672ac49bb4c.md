### Title
Insufficient Pausability of Critical Fund-Transfer Functions in the ckETH Minter - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The ckETH minter canister exposes `withdraw_eth` and `withdraw_erc20` as publicly callable update methods that burn ckETH/ckERC20 tokens and queue ETH/ERC-20 withdrawal transactions. Unlike the ckBTC minter — which implements a `Mode` enum (`ReadOnly`, `RestrictedTo`, `DepositsRestrictedTo`, `GeneralAvailability`) checked at the entry of every critical function — the ckETH minter has no equivalent operational mode or per-function pause guard. In the event of a discovered exploit, the only recourse is a slow NNS governance proposal to stop the entire canister, which takes days to pass.

---

### Finding Description

The ckBTC minter explicitly implements a `Mode` enum in `rs/bitcoin/ckbtc/minter/src/state.rs` and checks it at the top of every critical fund-transfer function:

```rust
// rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs:152-153
state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
    .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
``` [1](#0-0) 

The `Mode` enum supports `ReadOnly` (blocks all state changes), `RestrictedTo` (allowlist), and `DepositsRestrictedTo` (partial restriction): [2](#0-1) 

This mode can be changed via a canister upgrade argument (`UpgradeArgs { mode: Some(Mode::ReadOnly), .. }`), which itself requires an NNS proposal but is a targeted, fast-path action that does not stop the canister entirely. [3](#0-2) 

By contrast, the ckETH minter's `withdraw_eth` function performs only two checks before burning tokens and queuing an ETH withdrawal:

1. `validate_caller_not_anonymous()` — rejects anonymous callers.
2. `retrieve_withdraw_guard(caller)` — a **concurrency guard** that prevents the same principal from having two simultaneous in-flight requests.

```rust
// rs/ethereum/cketh/minter/src/main.rs:265-278
#[update]
async fn withdraw_eth(...) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(...)
    });
    // No mode/pause check — proceeds directly to burn and queue
``` [4](#0-3) 

The same absence applies to `withdraw_erc20`: [5](#0-4) 

The `retrieve_withdraw_guard` is purely a concurrency limiter (max concurrent requests per principal, max total pending), not a pause mechanism: [6](#0-5) 

The ckETH minter's Candid interface exposes no `mode` field in its `MinterArg`, confirming there is no upgrade-time mode switch: [7](#0-6) 

---

### Impact Explanation

If a vulnerability is discovered in `withdraw_eth` or `withdraw_erc20` (e.g., a burn-without-sufficient-balance bug, a re-entrancy-like issue across async await points, or a ledger interaction flaw), the DFINITY team has no fine-grained emergency stop for just those functions. The only options are:

1. **Stop the entire ckETH minter canister** via an NNS `StopOrStartCanister` governance proposal — this requires a full NNS vote, which takes multiple days to reach quorum and execute.
2. **Upgrade the canister** with a patched Wasm — also requires an NNS proposal and days of voting.

During this window, an attacker can continue calling `withdraw_eth` or `withdraw_erc20` to drain ckETH/ckERC20 holdings. The ckETH minter controls real ETH and ERC-20 balances on Ethereum via threshold ECDSA, so exploitation results in irreversible loss of bridged funds.

---

### Likelihood Explanation

The ckETH minter is a high-value target: it custodies all ETH and ERC-20 tokens bridged to the IC. The `withdraw_eth` and `withdraw_erc20` functions are publicly callable by any non-anonymous principal. The async nature of the minter (it awaits ledger `burn_from` calls before recording state) creates a window where subtle ordering bugs could be exploited. The absence of a mode guard means any such bug is immediately exploitable at full scale with no operator-controlled circuit breaker. Likelihood is **medium** (requires a real vulnerability in the withdrawal path) but the impact amplification from the lack of pausability is **high**.

---

### Recommendation

Add an operational `Mode` enum to the ckETH minter state, mirroring the ckBTC minter's design. At minimum, implement a `ReadOnly` mode that causes `withdraw_eth` and `withdraw_erc20` to return `WithdrawalError::TemporarilyUnavailable` immediately. Expose this mode as an `UpgradeArgs` field so it can be activated via a targeted NNS canister upgrade proposal (faster than a full stop/start cycle). The check should be the first operation in each critical function, before any async calls.

Reference implementation in ckBTC: [8](#0-7) 

---

### Proof of Concept

**Step 1**: Confirm ckBTC minter has mode guard on `retrieve_btc`: [9](#0-8) 

**Step 2**: Confirm ckETH minter `withdraw_eth` has no equivalent mode guard — only a concurrency guard: [10](#0-9) 

**Step 3**: Confirm the ckETH minter's `MinterArg` / upgrade path has no `mode` field, so there is no upgrade-time circuit breaker: [11](#0-10) 

**Step 4**: Confirm that stopping the ckETH minter requires a full NNS governance proposal (`StopOrStartCanister`), which takes days: [12](#0-11) 

An attacker who discovers a vulnerability in `withdraw_eth` has a multi-day exploitation window before any operator-controlled pause can take effect, during which they can repeatedly call the function to drain bridged ETH/ERC-20 assets.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L146-165)
```rust
pub async fn retrieve_btc<R: CanisterRuntime>(
    args: RetrieveBtcArgs,
    runtime: &R,
) -> Result<RetrieveBtcOk, RetrieveBtcError> {
    let caller = ic_cdk::api::msg_caller();

    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

    let _ecdsa_public_key = init_ecdsa_public_key().await;
    let main_address_str = state::read_state(|s| runtime.derive_minter_address_str(s));

    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }

    let _guard = retrieve_btc_guard(Account {
        owner: caller,
        subaccount: None,
    })?;
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L339-388)
```rust
/// Controls which operations the minter can perform.
#[derive(
    Default, Clone, Eq, PartialEq, Debug, Serialize, candid::CandidType, serde::Deserialize,
)]
pub enum Mode {
    /// Minter's state is read-only.
    ReadOnly,
    /// Only the specified principals can modify the minter's state.
    RestrictedTo(Vec<Principal>),
    /// Only the specified principals can deposit BTC.
    DepositsRestrictedTo(Vec<Principal>),
    #[default]
    /// No restrictions on the minter interactions.
    GeneralAvailability,
}

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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L182-191)
```text
type Mode = variant {
    // The minter does not allow any state modifications.
    ReadOnly;
    // Only specified principals can modify minter's state.
    RestrictedTo : vec principal;
    // Only specified principals can convert BTC to ckBTC.
    DepositsRestrictedTo : vec principal;
    // Anyone can interact with the minter.
    GeneralAvailability;
};
```

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-405)
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
```

**File:** rs/ethereum/cketh/minter/src/guard/tests.rs (L1-53)
```rust
use crate::test_fixtures::initial_state;

mod retrieve_eth_guard {
    use crate::guard::tests::init_state;
    use crate::guard::{GuardError, MAX_CONCURRENT, MAX_PENDING, retrieve_withdraw_guard};
    use crate::numeric::{LedgerBurnIndex, Wei};
    use crate::state::mutate_state;
    use crate::state::transactions::EthWithdrawalRequest;
    use candid::Principal;
    use ic_ethereum_types::Address;

    #[test]
    fn should_error_on_reentrant_principal() {
        init_state();
        let principal = principal_with_id(1);
        let _guard = retrieve_withdraw_guard(principal).unwrap();

        assert_eq!(
            retrieve_withdraw_guard(principal),
            Err(GuardError::AlreadyProcessing)
        )
    }

    #[test]
    fn should_allow_reentrant_principal_after_drop() {
        init_state();
        let principal = principal_with_id(1);
        {
            let _guard = retrieve_withdraw_guard(principal).unwrap();
        }

        assert!(retrieve_withdraw_guard(principal).is_ok());
    }

    #[test]
    fn should_allow_limited_number_of_principals() {
        init_state();
        let mut guards: Vec<_> = (0..MAX_CONCURRENT)
            .map(|i| retrieve_withdraw_guard(principal_with_id(i as u64)).unwrap())
            .collect();

        for additional_principal in MAX_CONCURRENT..2 * MAX_CONCURRENT {
            assert_eq!(
                retrieve_withdraw_guard(principal_with_id(additional_principal as u64)),
                Err(GuardError::TooManyConcurrentRequests)
            );
        }

        {
            let _guard = guards.pop().expect("should have at least one guard");
        }
        assert!(retrieve_withdraw_guard(principal_with_id(MAX_CONCURRENT as u64)).is_ok());
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L1937-1948)
```text
message StopOrStartCanister {
  // The target canister ID to call stop_canister or start_canister on. The canister must be
  // controlled by NNS Root, and it cannot be NNS Governance or Lifeline. Required.
  optional ic_base_types.pb.v1.PrincipalId canister_id = 1;

  // The action to take on the canister. Required.
  enum CanisterAction {
    CANISTER_ACTION_UNSPECIFIED = 0;
    CANISTER_ACTION_STOP = 1;
    CANISTER_ACTION_START = 2;
  }
  optional CanisterAction action = 2;
```
