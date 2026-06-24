### Title
Unauthorized Cross-Account Financial Data Disclosure via `get_known_utxos` and `retrieve_btc_status_v2_by_account` - (File: rs/bitcoin/ckbtc/minter/src/main.rs)

### Summary
The ckBTC minter exposes `get_known_utxos` and `retrieve_btc_status_v2_by_account` as publicly callable endpoints that accept an arbitrary `owner`/`target` account parameter with no check that the caller is authorized to access that account's data. Any unprivileged ingress sender can enumerate the pending Bitcoin UTXOs (amounts, outpoints) and the complete withdrawal history (amounts, Bitcoin txids, reimbursement details) for any IC principal. This is the direct IC analog of the Filsnap address-disclosure bug: sensitive per-account financial state is silently readable by any third party without the account owner's knowledge or consent.

### Finding Description
**Root cause — `get_known_utxos`:**

`get_known_utxos` is registered as a `#[query]` endpoint in `rs/bitcoin/ckbtc/minter/src/main.rs`:

```rust
#[query]
fn get_known_utxos(args: UpdateBalanceArgs) -> Vec<Utxo> {
    ic_ckbtc_minter::queries::get_known_utxos(args)
}
```

The implementation in `rs/bitcoin/ckbtc/minter/src/queries.rs` resolves the target account as:

```rust
pub fn get_known_utxos(args: UpdateBalanceArgs) -> Vec<Utxo> {
    read_state(|s| {
        s.known_utxos_for_account(&Account {
            owner: args.owner.unwrap_or(ic_cdk::api::msg_caller()),
            subaccount: args.subaccount,
        })
    })
}
```

When `args.owner` is `Some(victim_principal)`, the caller identity is completely ignored. No check is performed that `msg_caller() == args.owner`. The endpoint returns the full UTXO set (outpoints, values, heights) for the specified account.

**Root cause — `retrieve_btc_status_v2_by_account`:**

```rust
#[query]
fn retrieve_btc_status_v2_by_account(target: Option<Account>) -> Vec<BtcRetrievalStatusV2> {
    read_state(|s| s.retrieve_btc_status_v2_by_account(target))
}
```

The state method in `rs/bitcoin/ckbtc/minter/src/state.rs`:

```rust
pub fn retrieve_btc_status_v2_by_account(
    &self,
    target: Option<Account>,
) -> Vec<BtcRetrievalStatusV2> {
    let target_account = target.unwrap_or(Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: None,
    });
    // ... returns all withdrawal block indices and statuses for target_account
}
```

Again, when `target` is `Some(victim_account)`, the caller is not verified. The response includes block indices, Bitcoin txids, reimbursement amounts, and account details for the victim.

**Contrast with protected endpoints:** `get_withdrawal_account`, `retrieve_btc`, `retrieve_btc_with_approval`, and `update_balance` all call `check_anonymous_caller()` and/or verify the caller against the owner. The two query endpoints above have no equivalent guard.

**`get_btc_address` note:** This `#[update]` endpoint also accepts an arbitrary `owner` with no caller check, and the test `get_btc_address_from_anonymous_caller_should_succeed` explicitly validates that an anonymous caller can retrieve any principal's deposit address. However, since the deposit address is deterministically derivable from the public ECDSA key and the principal (both public), this is lower severity than the UTXO/withdrawal-history endpoints.

### Impact Explanation
An unprivileged attacker can:

1. **Enumerate pending BTC deposits for any IC principal** — `get_known_utxos` returns the full UTXO set (outpoint txid, vout, value in satoshis, confirmation height) for any account. This reveals how much BTC a user has deposited but not yet converted, and the on-chain transaction identifiers of those deposits.

2. **Retrieve complete withdrawal history for any IC principal** — `retrieve_btc_status_v2_by_account` returns every `retrieve_btc` request ever made by an account, including amounts, Bitcoin txids, reimbursement details (account, amount, reason), and current status. This creates a full financial dossier on any ckBTC user.

3. **Link IC principals to Bitcoin transactions** — combining both endpoints, an attacker can map any IC principal to their Bitcoin on-chain activity, breaking the pseudonymity that Bitcoin users typically rely on.

The impact is financial surveillance / privacy violation affecting all ckBTC users. Funds cannot be stolen via these endpoints, but the disclosed data (UTXO amounts, withdrawal amounts, Bitcoin txids, reimbursement accounts) is sensitive financial information that users have a reasonable expectation of privacy over.

### Likelihood Explanation
Exploitation requires no privileges, no keys, and no special role. Any entity that can submit an ingress message or canister call to the ckBTC minter — i.e., any Internet user — can call these query endpoints. The ckBTC minter is a production mainnet canister with a large user base, making mass enumeration of all user financial activity trivially feasible. The attacker only needs to know (or enumerate) victim principal IDs, which are often public (e.g., from NNS neuron records, dapp frontends, or the ledger).

### Recommendation
Add a caller-authorization guard to both endpoints: if the caller-supplied `owner`/`target` differs from `msg_caller()`, reject the request unless the caller is a controller of the minter or the account owner. A minimal fix:

```rust
pub fn get_known_utxos(args: UpdateBalanceArgs) -> Vec<Utxo> {
    let caller = ic_cdk::api::msg_caller();
    let owner = args.owner.unwrap_or(caller);
    if owner != caller {
        ic_cdk::trap("caller is not authorized to query this account");
    }
    read_state(|s| {
        s.known_utxos_for_account(&Account { owner, subaccount: args.subaccount })
    })
}
```

Apply the same pattern to `retrieve_btc_status_v2_by_account`. If cross-account queries are needed for legitimate use cases (e.g., dapp backends querying on behalf of users), introduce an explicit allowlist or require the account owner to pre-authorize the caller, analogous to the Filsnap fix of requiring `fil_configure` before any data is disclosed.

### Proof of Concept

**Step 1:** Alice (`alice_principal`) deposits 0.5 BTC to the ckBTC minter. The minter records the UTXO in `utxos_state_addresses[alice_account]`.

**Step 2:** Attacker (any principal, including anonymous) submits a query call:
```
dfx canister --network ic call minter get_known_utxos \
  '(record { owner = opt principal "alice_principal"; subaccount = null })'
```
Response: Alice's pending UTXO set including outpoint txid, vout, and value (50,000,000 satoshis).

**Step 3:** Attacker submits:
```
dfx canister --network ic call minter retrieve_btc_status_v2_by_account \
  '(opt record { owner = principal "alice_principal"; subaccount = null })'
```
Response: All of Alice's withdrawal requests with block indices, Bitcoin txids, amounts, and reimbursement details.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L186-194)
```rust
#[query]
fn retrieve_btc_status_v2_by_account(target: Option<Account>) -> Vec<BtcRetrievalStatusV2> {
    read_state(|s| s.retrieve_btc_status_v2_by_account(target))
}

#[query]
fn get_known_utxos(args: UpdateBalanceArgs) -> Vec<Utxo> {
    ic_ckbtc_minter::queries::get_known_utxos(args)
}
```

**File:** rs/bitcoin/ckbtc/minter/src/queries.rs (L37-44)
```rust
pub fn get_known_utxos(args: UpdateBalanceArgs) -> Vec<Utxo> {
    read_state(|s| {
        s.known_utxos_for_account(&Account {
            owner: args.owner.unwrap_or(ic_cdk::api::msg_caller()),
            subaccount: args.subaccount,
        })
    })
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L798-822)
```rust
    pub fn retrieve_btc_status_v2_by_account(
        &self,
        target: Option<Account>,
    ) -> Vec<BtcRetrievalStatusV2> {
        let target_account = target.unwrap_or(Account {
            owner: ic_cdk::api::msg_caller(),
            subaccount: None,
        });

        let block_indices: Vec<u64> = self
            .retrieve_btc_account_to_block_indices
            .get(&target_account)
            .unwrap_or(&vec![])
            .to_vec();

        let result: Vec<BtcRetrievalStatusV2> = block_indices
            .iter()
            .map(|&block_index| BtcRetrievalStatusV2 {
                block_index,
                status_v2: Some(self.retrieve_btc_status_v2(block_index)),
            })
            .collect();

        result
    }
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L688-694)
```text
    get_btc_address : (record { owner: opt principal; subaccount : opt blob }) -> (text);

    // Returns UTXOs of the given account known by the minter (with no
    // guarantee in the ordering of the returned values).
    //
    // If the owner is not set, it defaults to the caller's principal.
    get_known_utxos: (record { owner: opt principal; subaccount : opt blob }) -> (vec Utxo) query;
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L759-765)
```text
    // Returns the withdrawal statues by account.
    //
    // # Note
    // The _v2_ part indicates that you get a response in line with the retrieve_btc_status_v2 endpoint,
    // i.e., you get a vector of RetrieveBtcStatusV2 and not RetrieveBtcStatus.
    //
    retrieve_btc_status_v2_by_account : (opt Account) -> (vec record { block_index: nat64; status_v2: opt RetrieveBtcStatusV2; }) query;
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L769-785)
```rust
fn get_btc_address_from_anonymous_caller_should_succeed() {
    let env = new_state_machine();
    let args = MinterArg::Init(default_init_args());
    let args = Encode!(&args).unwrap();
    let minter_id = env.install_canister(minter_wasm(), args, None).unwrap();

    let btc_address = get_btc_address(
        &env,
        PrincipalId::new_anonymous(),
        minter_id,
        &GetBtcAddressArgs {
            owner: Some(Principal::from(SENDER_ID)),
            subaccount: None,
        },
    );
    assert!(!btc_address.is_empty());
}
```
