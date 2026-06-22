### Title
Unintended Authorization Privilege Granted to ICP Ledger Archive 1 Canister in CMC `notify_create_canister` - (File: rs/nns/cmc/src/main.rs)

### Summary
A developer TODO/hack comment in production code in the Cycles Minting Canister (CMC) explicitly acknowledges that `ICP_LEDGER_ARCHIVE_1_CANISTER_ID` has been granted unintended authorization to call `notify_create_canister` on behalf of any user. This is a test artifact left in production that widens the trust boundary of the CMC's caller authorization check.

### Finding Description
In `rs/nns/cmc/src/main.rs`, the function `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator` is responsible for deciding which callers may invoke `notify_create_canister` on behalf of a third-party `creator`. The intended authorized callers are: (1) the creator themselves, and (2) the production NNS dapp backend canister.

However, a hack comment explicitly acknowledges that a third canister — `ICP_LEDGER_ARCHIVE_1_CANISTER_ID` — has also been added to the `ALLOWED_CALLERS` list under the alias `TEST_NNS_DAPP_BACKEND_CANISTER_ID`: [1](#0-0) 

The comment reads:

> "This is a hack to enable testing (related features) of nns-dapp. In tests, the nns-dapp backend canister happens to use ID of the production ICP ledger archive 1 canister. Ideally, the test nns-dapp backend canister would have the same ID as the production nns-dapp backend canister. This difference should probably be considered a bug. **This hack can be removed after that bug is fixed.**"

This means the production ICP ledger archive 1 canister — a system canister whose intended role is solely to store historical ICP ledger blocks — has been granted the privilege to call `notify_create_canister` on behalf of **any arbitrary user** (`creator`) on mainnet. [2](#0-1) 

### Impact Explanation
`notify_create_canister` in the CMC triggers canister creation charged against the `creator`'s ICP balance. An authorized caller can specify any `creator` principal. If the ICP ledger archive 1 canister were to make a cross-canister call to CMC's `notify_create_canister` with an attacker-chosen `creator`, it could drain that user's ICP by creating canisters on their behalf without their consent.

The unintended privilege exists on mainnet today. Any future bug in the ICP ledger archive 1 canister that allows an attacker to influence its outgoing cross-canister calls (e.g., a reentrancy, an upgrade exploit, or a logic bug in archive's canister code) would immediately translate into the ability to call `notify_create_canister` on behalf of arbitrary users.

### Likelihood Explanation
The ICP ledger archive 1 canister is a system canister controlled by the NNS. Direct exploitation requires either: (a) a bug in the archive canister that allows attacker-influenced cross-canister calls, or (b) a governance proposal to upgrade the archive canister with malicious code. The developer comment explicitly acknowledges this is a known bug that should be fixed, increasing the likelihood that it will remain unresolved and be discovered by a future attacker. Likelihood is **medium-low** given the system canister constraint, but the unintended privilege is real and present on mainnet.

### Recommendation
Remove `TEST_NNS_DAPP_BACKEND_CANISTER_ID` from `ALLOWED_CALLERS` in `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator`. Fix the underlying test infrastructure so that the test nns-dapp backend canister uses the correct canister ID, eliminating the need for this hack entirely. The TODO comment itself acknowledges this is the correct resolution. [1](#0-0) 

### Proof of Concept
1. On mainnet, the ICP ledger archive 1 canister (`qsgjb-riaaa-aaaaa-aaaga-cai`) is included in `ALLOWED_CALLERS` for `notify_create_canister`.
2. If the archive canister is upgraded (via NNS governance) or has a bug allowing attacker-influenced outgoing calls, it can call CMC's `notify_create_canister` with `creator = <victim_principal>`.
3. CMC's `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator` will return `Ok(())` because `ALLOWED_CALLERS.contains(&ICP_LEDGER_ARCHIVE_1_CANISTER_ID)` is `true`.
4. CMC proceeds to create a canister charged against the victim's ICP balance. [3](#0-2)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1438-1474)
```rust
fn authorize_caller_to_call_notify_create_canister_on_behalf_of_creator(
    caller: PrincipalId,
    creator: PrincipalId,
) -> Result<(), NotifyError> {
    if caller == creator {
        return Ok(());
    }

    // This is a hack to enable testing (related features) of nns-dapp. In
    // tests, the nns-dapp backend canister happens to use ID of the production
    // ICP ledger archive 1 canister. Ideally, the test nns-dapp backend
    // canister would have the same ID as the production nns-dapp backend
    // canister. This difference should probably be considered a bug. This hack
    // can be removed after that bug is fixed.
    const TEST_NNS_DAPP_BACKEND_CANISTER_ID: CanisterId = ICP_LEDGER_ARCHIVE_1_CANISTER_ID;
    lazy_static! {
        static ref ALLOWED_CALLERS: [PrincipalId; 2] = [
            PrincipalId::from(*NNS_DAPP_BACKEND_CANISTER_ID),
            PrincipalId::from(TEST_NNS_DAPP_BACKEND_CANISTER_ID),
        ];
    }

    if ALLOWED_CALLERS.contains(&caller) {
        return Ok(());
    }

    // Other is used, because adding a Unauthorized variant to NotifyError would
    // confuse old clients.
    let err = NotifyError::Other {
        error_code: NotifyErrorCode::Unauthorized as u64,
        error_message: format!(
            "{caller} is not authorized to call notify_create_canister on behalf \
             of {creator}. (Do not retry, because the same result will occur.)",
        ),
    };

    Err(err)
```
