### Title
Inconsistent Address Validation in `withdraw_eth`/`withdraw_erc20` Traps Instead of Returning Structured Errors, Breaking 3rd-Party Canister Integrations - (File: rs/ethereum/cketh/minter/src/main.rs)

### Summary
The ckETH minter's `withdraw_eth` and `withdraw_erc20` endpoints call `ic_cdk::trap()` when the recipient Ethereum address is malformed or is the zero address (`Address::ZERO`), instead of returning a structured `Err(...)` variant. This is inconsistent with how blocked addresses are handled (which return `RecipientAddressBlocked`). Any canister that calls these endpoints via inter-canister call and receives a malformed or zero address from an untrusted user will receive a system-level `Reject` response rather than a typed error, causing unexpected failure in the calling canister.

### Finding Description
In `validate_address_as_destination` (`rs/ethereum/cketh/minter/src/address.rs`), three error variants are defined:

- `AddressValidationError::Invalid` — malformed address string
- `AddressValidationError::NotSupported` — the zero address (`0x000...000`)
- `AddressValidationError::Blocked` — address on the OFAC blocklist [1](#0-0) 

In both `withdraw_eth` and `withdraw_erc20`, the `Blocked` variant is mapped to a structured `WithdrawalError::RecipientAddressBlocked` / `WithdrawErc20Error::RecipientAddressBlocked` return value. However, `Invalid` and `NotSupported` are mapped to `ic_cdk::trap()`:

```rust
let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
    AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
        ic_cdk::trap(e.to_string())   // <-- system-level trap, not a structured Err
    }
    AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
        address: address.to_string(),
    },
})?;
``` [2](#0-1) [3](#0-2) 

`ic_cdk::trap()` aborts execution and causes the IC runtime to return a system-level `Reject` (reject code `CanisterError`) to the caller, rather than a Candid-encoded `Err(...)` value. This is fundamentally different from a structured error return: the caller cannot decode a typed error from a reject response.

The same pattern exists in the ckBTC minter, where `retrieve_btc` and `retrieve_btc_with_approval` trap when the destination address equals the minter's own address, instead of returning `Err(RetrieveBtcError::MalformedAddress(...))`: [4](#0-3) 

### Impact Explanation
Any canister (Alice's) that integrates with the ckETH minter and calls `withdraw_eth` or `withdraw_erc20` via inter-canister call will receive a system-level `Reject` response — not a typed `Err(WithdrawalError::...)` — when the recipient address is invalid or zero. Rust canisters that `await` inter-canister calls and pattern-match on `Ok`/`Err` will not see this as a typed error; instead, the `call` itself returns `Err((RejectionCode, String))`. If Alice's canister does not explicitly handle the rejection code path (which is a separate code path from the `Result<_, WithdrawalError>` path), the canister may panic, trap, or silently swallow the error. This can block subsequent operations in Alice's canister, corrupt its internal state if partial mutations occurred before the call, or be exploited by Eve to cause Alice's canister to fail in a controlled way.

The `WithdrawalError` type already has a `RecipientAddressBlocked` variant demonstrating that structured error returns are the intended API contract for address-related rejections. The trap path breaks this contract for two of the three address rejection cases.

### Likelihood Explanation
The ckETH minter is a production chain-fusion canister. Any third-party canister that wraps or proxies ckETH/ckERC20 withdrawals (e.g., DeFi protocols, aggregators, wallets) is affected. An unprivileged attacker (Eve) only needs to supply a malformed Ethereum address string or the zero address as the `recipient` field in a call to Alice's canister. This requires no special privileges, no key compromise, and no governance majority. The zero address (`0x0000000000000000000000000000000000000000`) is a well-known value that any user can supply.

### Recommendation
Replace the `ic_cdk::trap()` calls for `AddressValidationError::Invalid` and `AddressValidationError::NotSupported` with structured error variants. Add `InvalidRecipient` (or reuse `GenericError`) to `WithdrawalError` and `WithdrawErc20Error`, and return `Err(...)` instead of trapping. This makes the API contract consistent: all address-related rejections return typed errors that callers can match on. Apply the same fix to the ckBTC minter's `retrieve_btc` and `retrieve_btc_with_approval` functions where `ic_cdk::trap("illegal retrieve_btc target")` is used instead of `Err(RetrieveBtcError::MalformedAddress(...))`.

### Proof of Concept
1. Alice deploys a canister with the following logic:
   ```rust
   // Alice's canister calls withdraw_eth on the ckETH minter
   let result: Result<(Result<RetrieveEthRequest, WithdrawalError>,), (RejectionCode, String)> =
       ic_cdk::call(cketh_minter_id, "withdraw_eth", (WithdrawalArg {
           amount: 1_000_000_000_000_000_000u64.into(),
           recipient: user_supplied_address,  // attacker-controlled
           from_subaccount: None,
       },)).await;
   // Alice only handles Ok((Ok(...),)) and Ok((Err(WithdrawalError::...),))
   // She does NOT handle Err((RejectionCode::CanisterError, _))
   ```
2. Eve calls Alice's canister with `user_supplied_address = "0x0000000000000000000000000000000000000000"` (zero address) or `"0xinvalid"`.
3. The ckETH minter executes `ic_cdk::trap(e.to_string())` at `rs/ethereum/cketh/minter/src/main.rs:282`.
4. Alice's canister receives `Err((RejectionCode::CanisterError, "Address 0x0000000000000000000000000000000000000000 is not supported"))` from the `ic_cdk::call` future — not a `WithdrawalError` variant.
5. Alice's canister panics or behaves unexpectedly because it only handles the `Ok((Result<...>,))` branch.

The root cause is confirmed at: [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/address.rs (L46-56)
```rust
/// Validate whether the given address can be used as the destination of an Ethereum transaction.
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L158-160)
```rust
    if args.address == main_address_str {
        ic_cdk::trap("illegal retrieve_btc target");
    }
```
