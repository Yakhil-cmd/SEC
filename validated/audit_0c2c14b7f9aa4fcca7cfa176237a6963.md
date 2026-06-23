### Title
ckBTC Minter Mode Check Locks User Funds in Withdrawal Account When Mode Changes After Transfer - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

### Summary
The ckBTC minter's legacy `retrieve_btc()` endpoint applies a `Mode`-based allowlist check at the start of the function. In the legacy withdrawal flow, users first transfer ckBTC to a minter-controlled withdrawal subaccount, and only then call `retrieve_btc()`. If the minter's mode is changed to `RestrictedTo` (excluding the user) or `ReadOnly` between these two steps, the user's ckBTC is permanently locked in the minter's withdrawal subaccount with no automatic recovery mechanism.

### Finding Description

The ckBTC minter defines a `Mode` enum that controls which principals may interact with the minter: [1](#0-0) 

The `is_withdrawal_available_for()` method enforces this allowlist for withdrawals: [2](#0-1) 

This check is applied at the very beginning of `retrieve_btc()`, before any burn occurs: [3](#0-2) 

The legacy withdrawal flow (still exposed in the public DID interface) requires two separate user actions:

1. The user calls `get_withdrawal_account()` to obtain a minter-owned subaccount (owner = minter canister, subaccount = `hash(caller_principal)`).
2. The user transfers ckBTC to that subaccount via the ckBTC ledger — funds are now held by the minter canister.
3. The user calls `retrieve_btc()`, which checks the mode, then burns from the withdrawal account. [4](#0-3) 

The minter's mode can be changed at any time via a canister upgrade with `UpgradeArgs { mode: Some(...) }`: [5](#0-4) 

If the mode is changed to `RestrictedTo(allow_list)` where the user is not in `allow_list`, or to `ReadOnly`, between steps 2 and 3 above, `retrieve_btc()` returns `TemporarilyUnavailable` and the user's ckBTC remains in the minter's withdrawal subaccount. Because the withdrawal account is owned by the minter canister (not the user), the user cannot authorize a transfer back via the ledger. There is no automatic reimbursement path for this case — the reimbursement machinery only covers requests that have already been accepted into `pending_retrieve_btc_requests`: [6](#0-5) 

The new ICRC-2 flow (`retrieve_btc_with_approval`) does not have this issue because the ICRC-2 approval does not commit funds; the burn happens inside the function after the mode check passes.

### Impact Explanation

A user who has transferred ckBTC to the minter's withdrawal subaccount (step 2) and then finds themselves excluded from the minter's allowlist (due to a mode change) has their ckBTC permanently locked. The minter has no endpoint to return funds from a withdrawal subaccount when `retrieve_btc()` is blocked by the mode check. The only recovery path is a subsequent governance proposal to change the mode back or add the user to the allow list — there is no on-chain self-service recovery. This is a direct ledger conservation violation: ckBTC is burned from the user's perspective (transferred to a minter-controlled account) but BTC is never sent.

### Likelihood Explanation

The trigger requires a governance proposal (NNS vote) to change the minter's mode to `RestrictedTo` or `ReadOnly`. This is a legitimate operational action (e.g., during a security incident or migration), not a malicious one. The ckBTC minter was historically deployed with `Mode::ReadOnly` and later switched to `GeneralAvailability`. Any future mode restriction that excludes users who have already transferred to withdrawal accounts would silently lock their funds. The legacy `retrieve_btc` flow remains active in the public interface: [7](#0-6) 

The likelihood is low but non-zero, as it requires a specific ordering of events (user transfer followed by mode change before `retrieve_btc()` call).

### Recommendation

1. Remove the mode check from `retrieve_btc()` for the purpose of processing funds already committed to the withdrawal account, or add a dedicated endpoint (e.g., `reclaim_withdrawal_account_funds()`) that allows users to retrieve ckBTC from their withdrawal subaccount when `retrieve_btc()` is blocked.
2. Alternatively, fully deprecate the legacy `retrieve_btc` flow and require all users to migrate to `retrieve_btc_with_approval`, which does not have this issue because no funds are committed prior to the mode check.
3. At minimum, document that changing the mode to `RestrictedTo` or `ReadOnly` may lock funds for users who have already transferred to their withdrawal accounts.

### Proof of Concept

1. Minter is in `GeneralAvailability` mode.
2. User calls `get_withdrawal_account()` → receives account `{ owner: minter_id, subaccount: hash(user_principal) }`.
3. User calls `icrc1_transfer` on the ckBTC ledger to send `N` ckBTC to the withdrawal account. Funds are now held by the minter canister.
4. A governance proposal is submitted and executed: `UpgradeArgs { mode: Some(Mode::RestrictedTo(vec![other_principal])) }`. The user's principal is not in the allow list.
5. User calls `retrieve_btc({ amount: N, address: "..." })`.
6. `retrieve_btc()` executes `s.mode.is_withdrawal_available_for(&caller)` → returns `Err("BTC withdrawals are temporarily restricted")` → function returns `RetrieveBtcError::TemporarilyUnavailable`.
7. User's `N` ckBTC remains in the minter's withdrawal subaccount. The user cannot transfer it back (they do not own the account). No reimbursement is scheduled. Funds are locked indefinitely until a further governance action changes the mode.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L343-353)
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

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L727-729)
```rust
        if let Some(mode) = mode {
            self.mode = mode;
        }
```

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

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L17-37)
```rust
pub fn accept_retrieve_btc_request<R: CanisterRuntime>(
    state: &mut CkBtcMinterState,
    request: RetrieveBtcRequest,
    runtime: &R,
) {
    record_event(
        EventType::AcceptedRetrieveBtcRequest(request.clone()),
        runtime,
    );
    state.pending_retrieve_btc_requests.push(request.clone());
    if let Some(account) = request.reimbursement_account {
        state
            .retrieve_btc_account_to_block_indices
            .entry(account)
            .and_modify(|entry| entry.push(request.block_index))
            .or_insert(vec![request.block_index]);
    }
    if let Some(kyt_provider) = request.kyt_provider {
        *state.owed_kyt_amount.entry(kyt_provider).or_insert(0) += state.check_fee;
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L735-735)
```text
    retrieve_btc : (RetrieveBtcArgs) -> (variant { Ok : RetrieveBtcOk; Err : RetrieveBtcError });
```
