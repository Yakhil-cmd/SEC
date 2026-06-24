### Title
ckBTC Minter `ReadOnly`/`RestrictedTo` Mode Blocks Withdrawals for Users Who Pre-Deposited to the Withdrawal Subaccount - (File: rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs)

### Summary

The ckBTC minter's `retrieve_btc` function checks the minter's operational `Mode` before processing any withdrawal. When the minter is placed in `ReadOnly` or `RestrictedTo` mode, all calls to `retrieve_btc` are rejected — including calls from users who have already irreversibly transferred ckBTC into the minter's withdrawal subaccount in the first step of the legacy two-step withdrawal flow. Those users' ckBTC becomes locked with no recovery path while the mode restriction is active.

### Finding Description

The ckBTC minter supports two withdrawal flows:

1. **Legacy flow** (`retrieve_btc`): User first calls `get_withdrawal_account()` to obtain a dedicated subaccount of the minter, then transfers ckBTC to that subaccount via the ledger, then calls `retrieve_btc()` to trigger the BTC send.
2. **Approval flow** (`retrieve_btc_with_approval`): User pre-approves the minter via `icrc2_approve`, then calls `retrieve_btc_with_approval()` which atomically burns and queues the request.

In both functions, the very first substantive check is a mode gate:

```rust
// retrieve_btc (legacy)
state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
    .map_err(RetrieveBtcError::TemporarilyUnavailable)?;

// retrieve_btc_with_approval
state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
    .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;
``` [1](#0-0) [2](#0-1) 

`is_withdrawal_available_for` returns an error for both `Mode::ReadOnly` and `Mode::RestrictedTo` (when the caller is not in the allow list):

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
``` [3](#0-2) 

In the **legacy flow**, the ledger transfer of ckBTC to the minter's withdrawal subaccount happens **before** `retrieve_btc` is called. If the minter's mode is changed to `ReadOnly` or `RestrictedTo` between the user's ledger transfer and their `retrieve_btc` call, the function returns `TemporarilyUnavailable` immediately and the user's ckBTC remains stranded in the minter's withdrawal subaccount. There is no mechanism in the minter to refund or recover these pre-deposited funds while the mode restriction is active.

The `Mode` is set via governance upgrade proposals, as confirmed by the upgrade args interface:

```
mode : opt Mode;
``` [4](#0-3) 

The test `test_upgrade_read_only` explicitly confirms that `retrieve_btc` is rejected in `ReadOnly` mode: [5](#0-4) 

### Impact Explanation

A user who has completed step 1 of the legacy withdrawal (transferred ckBTC to the minter's withdrawal subaccount) but not yet called `retrieve_btc` will have their ckBTC locked in the minter's subaccount for the entire duration of the mode restriction. The minter provides no refund endpoint for pre-deposited withdrawal funds. The ckBTC is not in the user's own account and cannot be spent or recovered by the user unilaterally. If the restriction lasts for an extended period (e.g., during a security incident or prolonged maintenance), user funds are effectively frozen.

### Likelihood Explanation

The `ReadOnly` and `RestrictedTo` modes are legitimate operational controls used during upgrades and security incidents (as evidenced by the real upgrade proposals in the repository). The window between a user's ledger transfer and their `retrieve_btc` call is non-zero — the two-step flow is explicitly documented and users are instructed to perform it sequentially. Any governance-triggered mode change during this window affects all users mid-flow. The likelihood is low-to-medium but the impact is high when it occurs.

### Recommendation

The mode check in `retrieve_btc` (legacy flow) should distinguish between users who have already pre-deposited funds into the withdrawal subaccount and those who have not. Specifically, if a user has a non-zero balance in their minter withdrawal subaccount, `retrieve_btc` should be permitted even in `ReadOnly` or `RestrictedTo` mode, since the user has already committed their funds. Alternatively, the minter should expose a refund endpoint that allows users to reclaim ckBTC from their withdrawal subaccount when the minter is in a restricted mode.

### Proof of Concept

1. User calls `get_withdrawal_account()` → receives minter subaccount `S`.
2. User calls `icrc1_transfer` on the ckBTC ledger, sending `X` ckBTC to subaccount `S`. This transfer is recorded on the ledger and is irreversible from the user's side.
3. NNS governance passes an upgrade proposal setting the minter to `Mode::ReadOnly` (e.g., for a security incident).
4. User calls `retrieve_btc({ address: "bc1q...", amount: X })`.
5. The minter executes:
   ```rust
   state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
       .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
   // Returns Err("the minter is in read-only mode") immediately
   ```
6. The call returns `Err(TemporarilyUnavailable("the minter is in read-only mode"))`.
7. The user's `X` ckBTC remains in the minter's withdrawal subaccount. The user cannot spend it (it is not in their own account), cannot retrieve it (mode blocks `retrieve_btc`), and has no refund path. Funds are locked for the duration of the restriction. [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L250-251)
```rust
    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;
```

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

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L264-266)
```text
    /// If set, overrides the current minter's operation mode.
    mode : opt Mode;

```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L717-735)
```text
    // Returns the account to which the caller should deposit ckBTC
    // before withdrawing BTC using the [retrieve_btc] endpoint.
    get_withdrawal_account : () -> (Account);


    // Submits a request to convert ckBTC to BTC.
    //
    // # Note
    //
    // The BTC retrieval process is slow.  Instead of
    // synchronously waiting for a BTC transaction to settle, this
    // method returns a request ([block_index]) that the caller can use
    // to query the request status.
    //
    // # Preconditions
    //
    // * The caller deposited the requested amount in ckBTC to the account
    //   that the [get_withdrawal_account] endpoint returns.
    retrieve_btc : (RetrieveBtcArgs) -> (variant { Ok : RetrieveBtcOk; Err : RetrieveBtcError });
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
