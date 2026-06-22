### Title
`DepositsRestrictedTo` Mode Bypass via Caller/Owner Decoupling in ckBTC Minter `update_balance` - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

### Summary

The ckBTC minter's `DepositsRestrictedTo` mode is intended to restrict BTC-to-ckBTC deposits to a specific allowlist of principals. However, the deposit restriction check is applied only to the **caller** of `update_balance`, while ckBTC is minted to the **`args.owner`** field, which can be any arbitrary principal. Any whitelisted caller can therefore mint ckBTC to a non-whitelisted principal's account, bypassing the deposit restriction for the recipient.

### Finding Description

The `update_balance` endpoint in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` accepts an `UpdateBalanceArgs` struct with an optional `owner` field:

```rust
pub struct UpdateBalanceArgs {
    pub owner: Option<Principal>,   // can be any principal
    pub subaccount: Option<Subaccount>,
}
```

The deposit restriction check is performed against the **caller**:

```rust
state::read_state(|s| s.mode.is_deposit_available_for(&caller))
    .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
```

But the BTC address is derived from, and ckBTC is minted to, the **`args.owner`** account:

```rust
let caller_account = Account {
    owner: args.owner.unwrap_or(caller),
    subaccount: args.subaccount,
};
// ...
let address = state::read_state(|s| runtime.derive_user_address(s, &caller_account));
// ... ckBTC is minted to caller_account
```

The code comment explicitly acknowledges this design:

> "When the minter is in the mode using a whitelist we only want a certain set of principal to be able to mint. But we also want those principals to mint at any desired address. Therefore, the check below is on 'caller'."

The `Mode::DepositsRestrictedTo` is defined as "Only specified principals can deposit BTC":

```rust
/// Only the specified principals can deposit BTC.
DepositsRestrictedTo(Vec<Principal>),
```

And `is_deposit_available_for` enforces this only on the caller:

```rust
Self::DepositsRestrictedTo(allow_list) => {
    if !allow_list.contains(p) {
        return Err("BTC deposits are temporarily restricted".to_string());
    }
    Ok(())
}
```

### Impact Explanation

When the minter is in `DepositsRestrictedTo` mode (e.g., during a restricted launch or compliance enforcement), a non-whitelisted principal (Bob) can still receive ckBTC:

1. Bob pre-sends BTC to his derived Bitcoin address (computable from his IC principal via `get_btc_address({owner: bob})`).
2. Any whitelisted caller (Alice) calls `update_balance({owner: bob, subaccount: None})`.
3. The minter passes the deposit check (Alice is whitelisted), derives Bob's BTC address, finds the UTXOs, and mints ckBTC to Bob's ledger account.

The `DepositsRestrictedTo` restriction is therefore a caller-side gate only — it does not prevent non-whitelisted principals from receiving ckBTC. If the restriction is intended for compliance or KYC purposes (preventing specific principals from acquiring ckBTC), it is ineffective.

### Likelihood Explanation

The likelihood depends on the operational context:

- **Medium**: If any whitelisted principal operates an automated service that processes UTXOs for arbitrary addresses (e.g., a relayer or notification bot), it would naturally call `update_balance({owner: <any>})` for any address with new UTXOs, including non-whitelisted ones.
- **Lower**: If whitelisted principals only call `update_balance` for their own accounts, exploitation requires social engineering or cooperation.

The attack requires no privileged access — any IC user can pre-send BTC to their derived address and wait for a whitelisted caller to trigger the mint.

### Recommendation

If `DepositsRestrictedTo` is intended to restrict which principals can **receive** ckBTC (not just who can trigger minting), the check should be applied to `args.owner` (the recipient) rather than (or in addition to) the caller:

```rust
let effective_owner = args.owner.unwrap_or(caller);
state::read_state(|s| s.mode.is_deposit_available_for(&effective_owner))
    .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
```

If the current behavior (whitelisted callers can mint to any address) is intentional, the mode name and documentation should be updated to clarify that `DepositsRestrictedTo` restricts callers, not recipients, to avoid operator misconfiguration.

### Proof of Concept

**Setup**: Minter upgraded to `Mode::DepositsRestrictedTo(vec![alice])`.

**Attacker (Bob, not in allowlist)**:
1. Calls `get_btc_address({owner: bob})` → receives Bob's derived BTC address (no restriction on this query).
2. Sends BTC to that address on the Bitcoin network.
3. Waits for confirmations.

**Whitelisted caller (Alice)**:
4. Calls `update_balance({owner: Some(bob), subaccount: None})` as Alice.
5. Minter checks `is_deposit_available_for(&alice)` → passes (Alice is whitelisted).
6. Minter derives Bob's BTC address, finds the UTXOs Bob deposited, and mints ckBTC to Bob's ledger account.

**Result**: Bob receives ckBTC despite not being in the `DepositsRestrictedTo` allowlist. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L148-167)
```rust
    let caller = runtime.caller();
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }

    // Record start time of method execution for metrics
    let start_time = runtime.time();

    // When the minter is in the mode using a whitelist we only want a certain
    // set of principal to be able to mint. But we also want those principals
    // to mint at any desired address. Therefore, the check below is on "caller".
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;

    init_ecdsa_public_key().await;

    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L343-374)
```rust
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
