### Title
ckBTC Minter `Mode::ReadOnly` Blocks Withdrawals (ckBTC→BTC), Locking User Funds During Emergency - (File: rs/bitcoin/ckbtc/minter/src/state.rs)

### Summary
When the ckBTC minter is upgraded into `Mode::ReadOnly` (the emergency "kill-switch" mode), the `is_withdrawal_available_for` function returns an error for every caller, preventing any user from converting ckBTC back to BTC. This mirrors M-08 exactly: a disabled/paused state check is applied too broadly, blocking the one operation (fund retrieval) that users most need during an emergency.

### Finding Description
The ckBTC minter exposes a `Mode` enum that controls which operations are permitted:

```
ReadOnly              – "The minter does not allow any state modifications"
RestrictedTo(list)    – only listed principals can modify state
DepositsRestrictedTo  – only listed principals can deposit; withdrawals remain open
GeneralAvailability   – no restrictions
```

`is_withdrawal_available_for` is the gate for `retrieve_btc` / `retrieve_btc_with_approval`:

```rust
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
``` [1](#0-0) 

`retrieve_btc` calls this check as its very first action:

```rust
state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
    .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
``` [2](#0-1) 

The same guard is present in `retrieve_btc_with_approval`: [3](#0-2) 

The design already acknowledges the correct split: `DepositsRestrictedTo` allows withdrawals while blocking deposits. [4](#0-3)  `ReadOnly` should follow the same principle — block new deposits (`update_balance`) but keep the exit path (`retrieve_btc`) open. Instead, `ReadOnly` closes both directions symmetrically.

The existing test `test_upgrade_read_only` explicitly asserts that `retrieve_btc` is rejected in `ReadOnly` mode, confirming this is the current (flawed) behavior: [5](#0-4) 

### Impact Explanation
When the minter is upgraded to `Mode::ReadOnly` — the natural response to discovering a critical bug — every ckBTC holder loses the ability to exit the system. They cannot burn ckBTC and receive BTC back. Their ckBTC tokens remain transferable on the IC ledger, but the redemption path is completely severed. If the underlying reason for entering `ReadOnly` mode involves a risk to the minter's BTC treasury or the integrity of the peg, users are unable to protect themselves by exiting before the situation worsens.

### Likelihood Explanation
`ReadOnly` mode is activated by a canister upgrade with `UpgradeArgs { mode: Some(Mode::ReadOnly), .. }`. [6](#0-5)  This is a routine governance/NNS action taken in response to incidents. The probability that `ReadOnly` mode is ever activated is non-trivial (it exists precisely for emergencies), and every activation immediately and completely blocks all user withdrawals. No additional attacker action is required once the mode is set.

### Recommendation
In `is_withdrawal_available_for`, treat `Mode::ReadOnly` the same way `Mode::DepositsRestrictedTo` is treated — return `Ok(())` so that existing ckBTC holders can always redeem their tokens:

```rust
pub fn is_withdrawal_available_for(&self, p: &Principal) -> Result<(), String> {
    match self {
        // Allow withdrawals in all modes except RestrictedTo (allow-list).
        Self::GeneralAvailability | Self::DepositsRestrictedTo(_) | Self::ReadOnly => Ok(()),
        Self::RestrictedTo(allow_list) => {
            if !allow_list.contains(p) {
                return Err("BTC withdrawals are temporarily restricted".to_string());
            }
            Ok(())
        }
    }
}
```

Deposits (`update_balance`) should remain blocked in `ReadOnly` mode via `is_deposit_available_for`, which already returns an error for `ReadOnly`. [7](#0-6) 

### Proof of Concept
1. Deploy the ckBTC minter in `GeneralAvailability` mode.
2. User deposits BTC → receives ckBTC (normal flow).
3. Governance upgrades the minter with `Mode::ReadOnly` (e.g., to patch a bug).
4. User calls `retrieve_btc` or `retrieve_btc_with_approval` to redeem their ckBTC.
5. The call returns `Err(RetrieveBtcError::TemporarilyUnavailable("the minter is in read-only mode"))` — the user cannot exit.
6. The user's ckBTC is locked for the entire duration of the `ReadOnly` period, regardless of how long that lasts or what risk the minter's state poses to the peg.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L357-374)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L377-388)
```rust
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L727-728)
```rust
        if let Some(mode) = mode {
            self.mode = mode;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L152-153)
```rust
    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L250-251)
```rust
    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L450-467)
```rust
    // 2. retrieve_btc
    let retrieve_btc_args = RetrieveBtcArgs {
        amount: 10,
        address: "".into(),
    };
    let res = env
        .execute_ingress_as(
            authorized_principal.into(),
            minter_id,
            "retrieve_btc",
            Encode!(&retrieve_btc_args).unwrap(),
        )
        .expect("Failed to call retrieve_btc");
    let res = Decode!(&res.bytes(), Result<RetrieveBtcOk, RetrieveBtcError>).unwrap();
    assert!(
        matches!(res, Err(RetrieveBtcError::TemporarilyUnavailable(_))),
        "unexpected result: {res:?}"
    );
```
