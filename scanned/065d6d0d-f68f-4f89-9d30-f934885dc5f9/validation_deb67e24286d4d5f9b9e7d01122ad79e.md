### Title
Incomplete Blocklist Enforcement Allows Blocked Ethereum Addresses to Withdraw Existing ckETH Balances - (`rs/ethereum/cketh/minter/src/main.rs`, `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The ckETH minter enforces its Ethereum address blocklist only at two points: (1) rejecting deposits whose `from_address` is blocked, and (2) rejecting withdrawals whose *destination* address is blocked. It does **not** prevent a blocked Ethereum address's IC principal from withdrawing pre-existing ckETH balances to any non-blocked Ethereum address. This is the direct IC analog of M-1: the blocklist prevents some operations but does not prevent the sanctioned entity from continuing to transact.

---

### Finding Description

The ckETH minter maintains `ETH_ADDRESS_BLOCKLIST` and enforces it in two places:

**1. Deposit side** — `register_deposit_events` in `deposit.rs` checks only `event.from_address()` (the Ethereum sender): [1](#0-0) 

If the Ethereum sender is blocked, the deposit is recorded as `InvalidDeposit`. The IC-side beneficiary (`principal`) is never checked against the blocklist.

**2. Withdrawal side** — `validate_address_as_destination` in `address.rs` checks only the *destination* Ethereum address: [2](#0-1) 

`withdraw_eth` and `withdraw_erc20` in `main.rs` both call this function on the `recipient` argument, but perform **no check on the IC caller** (the principal initiating the withdrawal): [3](#0-2) [4](#0-3) 

The result is a gap: a blocked Ethereum address's IC principal can call `withdraw_eth` or `withdraw_erc20` with any non-blocked destination address and successfully drain its ckETH/ckERC-20 balance. The minter burns the ckETH from the principal and sends ETH to the non-blocked destination — the blocklist is never consulted for the *caller*.

---

### Impact Explanation

A sanctioned/blocked Ethereum address that holds ckETH (acquired before being blocked, or received via an ICRC-1 transfer from another user) can:

1. Call `withdraw_eth` from its IC principal with a non-blocked destination address.
2. The minter burns ckETH and sends ETH to the non-blocked address.
3. The blocked address has effectively moved funds out of the ckETH system, bypassing the blocklist entirely.

This defeats the purpose of the blocklist as a compliance/sanctions-enforcement mechanism. The impact is equivalent to M-1: the security feature is a partial no-op for principals that already hold ckETH.

---

### Likelihood Explanation

The entry path is straightforward and requires no privileged access:

- Any IC principal whose associated Ethereum address appears on `ETH_ADDRESS_BLOCKLIST` and that holds a non-zero ckETH balance can trigger this path.
- The caller only needs to be a non-anonymous IC principal (`validate_caller_not_anonymous`) and supply a non-blocked destination address.
- No admin key, governance majority, or threshold corruption is required. [5](#0-4) 

---

### Recommendation

Add a blocklist check on the *caller's* associated Ethereum address in the withdrawal flow. Because the IC principal is not directly an Ethereum address, the practical fix is to record the Ethereum address associated with each IC principal at deposit time (it is already available as `from_address` in `ReceivedEthEvent`) and reject withdrawal calls from principals whose recorded Ethereum address is blocked. Alternatively, mirror the Solidity pattern from M-1's fix: override the transfer hook to reject any operation involving a blocked principal.

```rust
// In withdraw_eth / withdraw_erc20, after validate_caller_not_anonymous():
if let Some(eth_addr) = read_state(|s| s.eth_address_of(caller)) {
    if crate::blocklist::is_blocked(&eth_addr) {
        return Err(WithdrawalError::SenderAddressBlocked { address: eth_addr.to_string() });
    }
}
```

---

### Proof of Concept

1. Ethereum address `A` (on `ETH_ADDRESS_BLOCKLIST`) previously deposited ETH and holds 1 ckETH under IC principal `P`.
2. `A` is added to the blocklist. Future deposits from `A` are rejected by `register_deposit_events`.
3. `P` calls `withdraw_eth { amount: 1_ckETH, recipient: "<non-blocked-address-B>", from_subaccount: None }`.
4. `validate_address_as_destination("<non-blocked-address-B>")` passes — `B` is not blocked.
5. The minter burns 1 ckETH from `P` and submits an Ethereum transaction sending ETH to `B`.
6. `A` has successfully moved funds through the ckETH minter despite being on the blocklist. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L323-341)
```rust
        if crate::blocklist::is_blocked(&event.from_address()) {
            log!(
                INFO,
                "Received event from a blocked address: {} for {} {scraping_id}",
                event.from_address(),
                event.value(),
            );
            mutate_state(|s| {
                process_event(
                    s,
                    EventType::InvalidDeposit {
                        event_source: event.source(),
                        reason: format!("blocked address {}", event.from_address()),
                    },
                )
            });
        } else {
            mutate_state(|s| process_event(s, event.into_deposit()));
        }
```

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

**File:** rs/ethereum/cketh/minter/src/main.rs (L61-67)
```rust
fn validate_caller_not_anonymous() -> candid::Principal {
    let principal = ic_cdk::api::msg_caller();
    if principal == candid::Principal::anonymous() {
        panic!("anonymous principal is not allowed");
    }
    principal
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L266-340)
```rust
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L400-414)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/blocklist.rs (L107-109)
```rust
pub fn is_blocked(address: &Address) -> bool {
    ETH_ADDRESS_BLOCKLIST.binary_search(address).is_ok()
}
```
