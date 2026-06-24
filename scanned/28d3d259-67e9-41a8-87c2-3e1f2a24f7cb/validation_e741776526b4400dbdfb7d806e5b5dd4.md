### Title
ckBTC Minter Blocklist Check Races with Concurrent `retrieve_btc` Calls Allowing Withdrawal to a Tainted Address - (File: `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

### Summary
The ckBTC minter's `retrieve_btc` and `retrieve_btc_with_approval` functions perform a Bitcoin address taint check via an async inter-canister call to the Bitcoin checker canister, and then proceed to burn ckBTC and queue the withdrawal. Because the per-principal guard only prevents a single principal from having two concurrent `retrieve_btc` calls in flight simultaneously, two different principals (or the same principal using different subaccounts in `retrieve_btc_with_approval`) can race the check-then-burn sequence. More critically, the blocklist used by `validate_address_as_destination` in the ckETH minter is a **static compile-time list** embedded in the canister binary, meaning it can only be updated via an NNS upgrade proposal. During the window between when an address is identified as tainted and when the upgraded canister is deployed, any user can withdraw to that address. This is the direct IC analog of the "depositor front-runs a block transaction" vulnerability.

### Finding Description

In `retrieve_btc` (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`), the flow is:

1. Acquire a per-account guard (`retrieve_btc_guard`)
2. Check balance via async call to ledger (`balance_of`)
3. Call `check_address` — an **async inter-canister call** to the Bitcoin checker canister
4. If clean, call `burn_ckbtcs` — another async inter-canister call to the ledger

Between steps 3 and 4, the canister yields execution. During this yield, the minter's state can be mutated by other messages. The guard only prevents the **same account** from having two concurrent calls. A different account can simultaneously pass the check and proceed to burn.

More critically, the ckETH minter's `validate_address_as_destination` uses a **static, compile-time blocklist** (`ETH_ADDRESS_BLOCKLIST` in `rs/ethereum/cketh/minter/src/blocklist.rs`). Adding a new address to the blocklist requires an NNS governance proposal to upgrade the canister. During the proposal voting period (which can take days), any user who observes the pending upgrade proposal can immediately call `withdraw_eth` or `withdraw_erc20` to a soon-to-be-blocked address before the upgrade takes effect. This is a direct front-running analog: the "blocking transaction" is the NNS upgrade proposal, and the "withdrawal" is the `withdraw_eth` call.

For ckBTC, the Bitcoin checker canister's blocklist (`rs/bitcoin/checker/lib/blocklist.rs`) is similarly static and requires an upgrade to change. The `check_address` call is synchronous within the checker canister but the minter must make an async inter-canister call to reach it, creating a window.

### Impact Explanation

**ckETH/ckERC20 path:** A user who monitors the NNS proposal queue can call `withdraw_eth` or `withdraw_erc20` to a newly-to-be-blocked Ethereum address before the minter upgrade is executed. The blocklist check in `validate_address_as_destination` passes because the static list has not yet been updated. The ckETH tokens are burned and the minter queues an ETH transaction to the sanctioned address. This defeats the OFAC compliance intent of the blocklist.

**ckBTC path:** Similarly, a user can call `retrieve_btc_with_approval` to a Bitcoin address that is about to be added to the BTC blocklist. The `check_address` call to the Bitcoin checker canister passes (checker not yet upgraded), ckBTC is burned, and a BTC withdrawal to the tainted address is queued.

Impact: chain-fusion mint/burn/replay bug class — funds are sent to sanctioned/blocked addresses, violating the compliance guarantees the blocklist is intended to enforce.

### Likelihood Explanation

NNS upgrade proposals are public and visible on-chain before execution. The voting period is typically 4 days for non-critical proposals. Any user monitoring the NNS dashboard or the IC governance canister can observe a pending minter upgrade that adds addresses to the blocklist and race it. The attack requires no special privileges — only an unprivileged ingress call to `withdraw_eth` or `retrieve_btc_with_approval`. Likelihood is **medium**: it requires active monitoring of governance proposals and knowledge of what the upgrade contains, but the window is large (days) and the call is trivial.

### Recommendation

1. **Short term:** Implement a two-step withdrawal process for ckETH/ckBTC: a user first signals intent to withdraw (locking funds), and the actual transfer is only executed after a mandatory delay (e.g., 24 hours). This ensures that any blocklist upgrade can take effect before the withdrawal is finalized.
2. **Short term:** For ckBTC, re-check the destination address against the Bitcoin checker canister immediately before signing and broadcasting the Bitcoin transaction (in the `process_logic` task), not only at request submission time.
3. **Long term:** Consider a dynamic, governance-updatable blocklist that can be updated without a full canister upgrade, reducing the window between identification and enforcement.

### Proof of Concept

**ckETH front-run scenario:**

1. DFINITY submits NNS proposal to upgrade the ckETH minter, adding address `0xABCD...` to `ETH_ADDRESS_BLOCKLIST` in `rs/ethereum/cketh/minter/src/blocklist.rs`.
2. Eve observes the proposal on the NNS dashboard. The proposal is in voting state (4-day window).
3. Eve calls `withdraw_eth` with `recipient = "0xABCD..."` and `amount = X`. The current minter binary does not have `0xABCD...` in its blocklist.
4. `validate_address_as_destination` at line 53 calls `crate::blocklist::is_blocked(&address)` — returns `false` because the static list is not yet updated.
5. The minter burns Eve's ckETH and queues an ETH transaction to `0xABCD...`.
6. The NNS proposal passes and the minter is upgraded — but the withdrawal is already queued and will be executed.

**Relevant code path:**

`withdraw_eth` → `validate_address_as_destination` → `is_blocked` (static list check, no async, no re-check at execution time) [1](#0-0) [2](#0-1) [3](#0-2) 

**ckBTC path — check happens before burn, no re-check at broadcast:** [4](#0-3) [5](#0-4) 

**Static blocklists (require upgrade to change):** [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/address.rs (L47-56)
```rust
pub fn validate_address_as_destination(address: &str) -> Result<Address, AddressValidationError> {
    let address =
        Address::from_str(address).map_err(|e| AddressValidationError::Invalid { error: e })?;
    if address == Address::ZERO {
        return Err(AddressValidationError::NotSupported(address));
    }
    if crate::blocklist::is_blocked(&address) {
        return Err(AddressValidationError::Blocked(address));
    }
    Ok(address)
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L17-30)
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
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L107-109)
```rust
pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L181-210)
```rust
    let balance = balance_of(caller).await?;
    if args.amount > balance {
        return Err(RetrieveBtcError::InsufficientFunds { balance });
    }

    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
    match status {
        BtcAddressCheckStatus::Tainted => {
            log!(
                Priority::Debug,
                "rejected an attempt to withdraw {} BTC to address {} due to failed Bitcoin check",
                crate::tx::DisplayAmount(args.amount),
                args.address,
            );
            return Err(RetrieveBtcError::GenericError {
                error_message: "Destination address is tainted".to_string(),
                error_code: ErrorCode::TaintedAddress as u64,
            });
        }
        BtcAddressCheckStatus::Clean => {}
    }

    let burn_memo = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: Some(Status::Accepted),
    };
    let block_index =
        burn_ckbtcs(caller, args.amount, crate::memo::encode(&burn_memo).into()).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L283-319)
```rust
    match check_address(
        btc_checker_principal,
        parsed_address.display(btc_network),
        runtime,
    )
    .await
    {
        Err(error) => {
            return Err(RetrieveBtcWithApprovalError::GenericError {
                error_message: format!(
                    "Failed to call Bitcoin checker canister with error: {error:?}"
                ),
                error_code: ErrorCode::CheckCallFailed as u64,
            });
        }
        Ok(status) => match status {
            BtcAddressCheckStatus::Tainted => {
                return Err(RetrieveBtcWithApprovalError::GenericError {
                    error_message: "Destination address is tainted".to_string(),
                    error_code: ErrorCode::TaintedAddress as u64,
                });
            }
            BtcAddressCheckStatus::Clean => {}
        },
    }

    let burn_memo_icrc2 = BurnMemo::Convert {
        address: Some(&args.address),
        kyt_fee: None,
        status: None,
    };
    let block_index = burn_ckbtcs_icrc2(
        caller_account,
        args.amount,
        crate::memo::encode(&burn_memo_icrc2).into(),
    )
    .await?;
```

**File:** rs/bitcoin/checker/lib/blocklist.rs (L532-536)
```rust
pub fn is_blocked(address: &Address) -> bool {
    BTC_ADDRESS_BLOCKLIST
        .binary_search(&address.to_string().as_ref())
        .is_ok()
}
```
